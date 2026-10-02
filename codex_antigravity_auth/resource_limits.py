"""Operator-controlled parser, payload and process-local admission bounds."""
from __future__ import annotations

import base64
from contextlib import asynccontextmanager
from dataclasses import dataclass, fields
import json
import math
import os
import threading

from .request_budget import CURRENT_BUDGET, call_sync, owned_context

MiB = 1024 * 1024


class ResourceLimitError(ValueError):
    def __init__(self, code, *, status=413):
        self.code, self.status = code, status
        super().__init__(f"Gateway resource policy rejected the payload ({code}).")


@dataclass(frozen=True)
class ResourceLimits:
    body_bytes: int = 32 * MiB
    attachment_bytes: int = 16 * MiB
    attachments_bytes: int = 24 * MiB
    json_depth: int = 64
    json_nodes: int = 100_000
    schema_depth: int = 32
    sse_frame_chars: int = 8 * MiB
    sse_total_bytes: int = 64 * MiB
    provider_body_bytes: int = 32 * MiB
    inflight: int = 32
    route_inflight: int = 16

    def __post_init__(self):
        if any(type(getattr(self, field.name)) is not int or getattr(self, field.name) <= 0 for field in fields(self)):
            raise ValueError("Resource limits must be positive integers")

    @classmethod
    def from_env(cls):
        ranges = {
            "body_bytes": (1024, 128 * MiB), "attachment_bytes": (1024, 64 * MiB),
            "attachments_bytes": (1024, 128 * MiB), "json_depth": (8, 128),
            "json_nodes": (1000, 1_000_000), "schema_depth": (4, 64),
            "sse_frame_chars": (1024, 16 * MiB), "sse_total_bytes": (1024, 256 * MiB),
            "provider_body_bytes": (1024, 128 * MiB), "inflight": (1, 256), "route_inflight": (1, 256),
        }
        values = {}
        for field in fields(cls):
            name = "ANTIGRAVITY_MAX_" + field.name.upper()
            raw = os.environ.get(name)
            if raw is None:
                continue
            try:
                if len(raw) > 12 or not raw.isascii() or not raw.isdecimal():
                    raise ValueError
                value = int(raw)
                lower, upper = ranges[field.name]
                if not lower <= value <= upper:
                    raise ValueError
            except ValueError:
                raise ValueError(f"Invalid gateway resource limit: {name}") from None
            values[field.name] = value
        return cls(**values)


def current_limits():
    budget = CURRENT_BUDGET.get()
    return getattr(budget, "limits", None) or ResourceLimits.from_env()


def _scan_json(text, limits):
    """Reject depth/node amplification before allocating the decoded object tree."""
    depth = nodes = 0
    quoted = escaped = primitive = False
    for character in text:
        if quoted:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                quoted = False
            continue
        if character == '"':
            quoted = True
            primitive = False
            nodes += 1
        elif character in "[{":
            depth += 1
            nodes += 1
            primitive = False
            if depth > limits.json_depth:
                raise ResourceLimitError("json_depth_limit")
        elif character in "]}":
            depth -= 1
            primitive = False
        elif character.isspace() or character in ",:":
            primitive = False
        elif not primitive:
            nodes += 1
            primitive = True
        if nodes > limits.json_nodes:
            raise ResourceLimitError("json_node_limit")


def json_loads_limited(data, *, limits=None):
    limits = limits or current_limits()
    text = data.decode("utf-8") if isinstance(data, bytes) else data
    _scan_json(text, limits)
    def integer(value):
        if len(value) > 1024:
            raise ResourceLimitError("json_number_limit")
        return int(value)
    def floating(value):
        if len(value) > 1024:
            raise ResourceLimitError("json_number_limit")
        parsed = float(value)
        if not math.isfinite(parsed):
            raise ResourceLimitError("invalid_json_number", status=400)
        return parsed
    def constant(_value):
        raise ResourceLimitError("invalid_json_number", status=400)
    return json.loads(text, parse_int=integer, parse_float=floating, parse_constant=constant)


def check_tree(value, limits, *, depth_limit=None):
    stack = [(value, 0)]
    nodes = 0
    while stack:
        item, depth = stack.pop()
        nodes += 1
        if nodes > limits.json_nodes or len(stack) + nodes > limits.json_nodes:
            raise ResourceLimitError("json_node_limit")
        if depth > (limits.json_depth if depth_limit is None else depth_limit):
            raise ResourceLimitError("schema_depth_limit" if depth_limit is not None else "json_depth_limit")
        if type(item) is dict:
            if any(type(key) is not str for key in item):
                raise ResourceLimitError("invalid_json_object", status=400)
            if len(item) * 2 + nodes + len(stack) > limits.json_nodes:
                raise ResourceLimitError("json_node_limit")
            stack.extend((child, depth + 1) for child in item.values())
            stack.extend((key, depth + 1) for key in item)
        elif type(item) is list:
            if len(item) + nodes + len(stack) > limits.json_nodes:
                raise ResourceLimitError("json_node_limit")
            stack.extend((child, depth + 1) for child in item)
        elif type(item) is str:
            try:
                item.encode("utf-8")
            except UnicodeError:
                raise ResourceLimitError("invalid_unicode", status=400) from None
        elif type(item) is float:
            if not math.isfinite(item):
                raise ResourceLimitError("invalid_json_number", status=400)
        elif type(item) is int:
            if item.bit_length() > 4096:
                raise ResourceLimitError("json_number_limit")
        elif item is not None and type(item) is not bool:
            raise ResourceLimitError("invalid_json_value", status=400)


def check_request_structure(payload, limits):
    check_tree(payload, limits)
    schemas = []
    if isinstance(payload, dict):
        for tool in payload.get("tools", []) if isinstance(payload.get("tools"), list) else []:
            if not isinstance(tool, dict):
                continue
            if isinstance(tool.get("parameters"), dict):
                schemas.append(tool["parameters"])
            function = tool.get("function")
            if isinstance(function, dict) and isinstance(function.get("parameters"), dict):
                schemas.append(function["parameters"])
        for container, key in ((payload.get("text"), "format"), (payload.get("response_format"), "json_schema")):
            if isinstance(container, dict) and isinstance(container.get(key), dict) and isinstance(container[key].get("schema"), dict):
                schemas.append(container[key]["schema"])
    for schema in schemas:
        check_tree(schema, limits, depth_limit=limits.schema_depth)
    attachment_total = 0
    stack = [payload]
    while stack:
        item = stack.pop()
        if type(item) is list:
            stack.extend(item)
        elif type(item) is dict:
            stack.extend(item.values())
            kind = item.get("type")
            if type(kind) is not str or kind not in {"input_image", "image", "image_url", "input_file", "file"}:
                continue
            content = item.get("image_url") or item.get("url") or item.get("file_data")
            if isinstance(content, dict):
                content = content.get("url")
            if not isinstance(content, str):
                continue
            if content.startswith("data:"):
                header, separator, encoded = content.partition(",")
                if not separator or not header.lower().endswith(";base64"):
                    continue
            elif kind in {"input_file", "file"} and content == item.get("file_data"):
                encoded = content
            else:
                continue  # No new attachment representation or capability here.
            padding = len(encoded) - len(encoded.rstrip("="))
            if len(encoded) % 4 or padding > 2:
                raise ResourceLimitError("invalid_attachment_encoding", status=400)
            estimated = (len(encoded) // 4) * 3 - padding
            if estimated > limits.attachment_bytes or attachment_total + estimated > limits.attachments_bytes:
                raise ResourceLimitError("attachment_size_limit")
            try:
                decoded = base64.b64decode(encoded, validate=True)
            except (ValueError, UnicodeError):
                raise ResourceLimitError("invalid_attachment_encoding", status=400) from None
            size = len(decoded)
            if size > limits.attachment_bytes or attachment_total + size > limits.attachments_bytes:
                raise ResourceLimitError("attachment_size_limit")
            attachment_total += size


async def read_request_json(request, limits):
    headers = getattr(request, "headers", {})
    raw_length = headers.get("content-length")
    declared = None
    if raw_length is not None:
        if not isinstance(raw_length, str) or not raw_length.isascii() or not raw_length.isdecimal():
            raise ResourceLimitError("invalid_content_length", status=400)
        if len(raw_length) > 20:
            raise ResourceLimitError("request_body_limit")
        declared = int(raw_length)
        if declared > limits.body_bytes:
            raise ResourceLimitError("request_body_limit")
    if hasattr(request, "stream"):
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > limits.body_bytes:
                raise ResourceLimitError("request_body_limit")
            body.extend(chunk)
        if declared is not None and len(body) != declared:
            raise ResourceLimitError("content_length_mismatch", status=400)
        payload = await call_sync(json_loads_limited, bytes(body), limits=limits)
    else:
        # Trusted in-process adapters may supply a decoded object. The public
        # ASGI route always takes the byte-counted streaming branch above.
        payload = await request.json()
        await call_sync(check_request_structure, payload, limits)
        if len(json.dumps(payload, ensure_ascii=True).encode("utf-8")) > limits.body_bytes:
            raise ResourceLimitError("request_body_limit")
    await call_sync(check_request_structure, payload, limits)
    return payload


class Admission:
    def __init__(self):
        self.lock = threading.Lock()
        self.total = 0
        self.routes = {}
        self.startup_ceiling = None

    def set_startup_ceiling(self, ceiling):
        if ceiling is not None and (type(ceiling) is not int or ceiling <= 0):
            raise ValueError("Admission startup ceiling must be a positive integer")
        with self.lock:
            self.startup_ceiling = ceiling

    def acquire(self, limits):
        with self.lock:
            maximum = limits.inflight
            if self.startup_ceiling is not None:
                maximum = min(maximum, self.startup_ceiling)
            if self.total >= maximum:
                raise ResourceLimitError("gateway_overloaded", status=503)
            self.total += 1
        return Permit(self, limits)


class Permit:
    def __init__(self, owner, limits):
        self.owner, self.limits = owner, limits
        self.route = None
        self.released = False

    def bind_route(self, route):
        route = route if route in {"google", "openai", "byok"} else "unknown"
        with self.owner.lock:
            if self.released:
                raise ResourceLimitError("request_already_closed", status=503)
            if self.route is not None:
                return
            if self.owner.routes.get(route, 0) >= self.limits.route_inflight:
                raise ResourceLimitError("route_overloaded", status=503)
            self.owner.routes[route] = self.owner.routes.get(route, 0) + 1
            self.route = route

    def release(self):
        with self.owner.lock:
            if self.released:
                return
            self.released = True
            self.owner.total -= 1
            if self.route is not None:
                self.owner.routes[self.route] -= 1
                if not self.owner.routes[self.route]:
                    del self.owner.routes[self.route]


ADMISSION = Admission()


async def read_response_bytes(response, *, limit=None):
    maximum = current_limits().provider_body_bytes if limit is None else limit
    header = response.headers.get("content-length")
    if header and header.isascii() and header.isdecimal() and (len(header) > 20 or int(header) > maximum):
        error = ResourceLimitError("provider_body_limit", status=502)
        error.upstream_status = response.status_code
        raise error
    data = bytearray()
    async for chunk in response.aiter_bytes():
        if len(data) + len(chunk) > maximum:
            error = ResourceLimitError("provider_body_limit", status=502)
            error.upstream_status = response.status_code
            raise error
        data.extend(chunk)
    return bytes(data)


def response_json(response):
    try:
        return json_loads_limited(response.content)
    except ResourceLimitError as exc:
        error = ResourceLimitError(exc.code, status=502)
        error.upstream_status = response.status_code
        raise error from exc


@asynccontextmanager
async def response_context(client, url, *, payload, headers):
    if callable(getattr(client, "stream", None)):
        async with owned_context(client.stream("POST", url, json=payload, headers=headers)) as response:
            yield response
    else:
        # Compatibility with trusted in-process transports exposing only post.
        # Real HTTPX always uses the incremental branch above.
        response = await client.post(url, json=payload, headers=headers)
        if len(response.content) > current_limits().provider_body_bytes:
            error = ResourceLimitError("provider_body_limit", status=502)
            error.upstream_status = response.status_code
            raise error
        yield response


async def limited_post(client, url, *, payload, headers):
    import httpx
    async with response_context(client, url, payload=payload, headers=headers) as response:
        content = await read_response_bytes(response)
        try:
            request = response.request
        except (RuntimeError, AttributeError):
            request = httpx.Request("POST", url)
        # aiter_bytes yields decoded content. Do not tell the reconstructed
        # in-memory response to decompress it a second time.
        filtered_headers = {key: value for key, value in response.headers.items()
                            if key.lower() not in {"content-encoding", "content-length", "transfer-encoding"}}
        return httpx.Response(response.status_code, headers=filtered_headers, content=content, request=request)
