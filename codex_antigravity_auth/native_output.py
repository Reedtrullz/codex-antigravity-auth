"""Versioned native Responses output shapes; never apply translation heuristics."""
from __future__ import annotations

from copy import deepcopy
import math
import re
from urllib.parse import urlsplit

CONTRACT_VERSION = 1
MAX_ITEMS = 10000
MAX_NODES = 100000
MAX_BYTES = 8 * 1024 * 1024
MAX_DEPTH = 32
ITEM_TYPES = {"message", "function_call", "custom_tool_call", "reasoning", "web_search_call"}
ITEM_STATUS = {"in_progress", "completed", "incomplete"}
EVENT_TYPES = {"response.created", "response.in_progress", "response.completed", "response.incomplete", "response.failed",
               "response.output_item.added", "response.output_item.done", "response.output_text.annotation.added"}
for prefix in ("response.content_part", "response.reasoning_summary_part"):
    EVENT_TYPES.update({prefix + ".added", prefix + ".done"})
for prefix in ("response.output_text", "response.refusal", "response.reasoning_summary_text", "response.reasoning_text",
               "response.function_call_arguments", "response.custom_tool_call_input"):
    EVENT_TYPES.update({prefix + ".delta", prefix + ".done"})
EVENT_TYPES.update("response.web_search_call." + value for value in ("in_progress", "searching", "completed"))


class NativeOutputError(ValueError):
    def __init__(self, code="invalid_native_output"):
        self.code = code
        super().__init__(f"The provider returned invalid or unsupported native output (contract v{CONTRACT_VERSION}).")


def _require(condition, code="invalid_native_output"):
    if not condition:
        raise NativeOutputError(code)


def check_json(value, *, budget=None):
    """Bound decoded JSON without printing any provider values on failure."""
    stack = [(value, 0)]
    nodes = total = 0
    while stack:
        node, depth = stack.pop()
        nodes += 1
        _require(nodes <= MAX_NODES and depth <= MAX_DEPTH, "native_output_limit")
        if type(node) is dict:
            _require(len(node) * 2 + nodes + len(stack) <= MAX_NODES, "native_output_limit")
            _require(all(type(key) is str for key in node))
            stack.extend((child, depth + 1) for child in node.values())
            stack.extend((key, depth + 1) for key in node)
        elif type(node) is list:
            _require(len(node) <= MAX_ITEMS and len(node) + nodes + len(stack) <= MAX_NODES, "native_output_limit")
            stack.extend((child, depth + 1) for child in node)
        elif type(node) is str:
            try:
                total += len(node.encode("utf-8"))
            except UnicodeError:
                raise NativeOutputError() from None
            _require(total <= MAX_BYTES, "native_output_limit")
        elif type(node) is int:
            _require(abs(node) <= 2**63 - 1)
        elif type(node) is float:
            _require(math.isfinite(node))
        else:
            _require(node is None or type(node) is bool)
    if budget is not None:
        _require(budget[0] + total <= MAX_BYTES and budget[1] + nodes <= MAX_NODES, "native_output_limit")
        budget[0] += total
        budget[1] += nodes
    return total


def _object(value, required=(), optional=()):
    _require(type(value) is dict)
    _require(set(required) <= value.keys())
    _require(value.keys() <= set(required) | set(optional), "unsupported_native_field")


def _text(value, *, empty=True, nullable=False):
    _require((nullable and value is None) or (type(value) is str and (empty or bool(value))))


def _integer(value):
    _require(type(value) is int and 0 <= value <= 2**63 - 1)


def _url(value):
    _text(value, empty=False)
    try:
        parsed = urlsplit(value)
        valid = parsed.scheme in {"http", "https"} and bool(parsed.hostname) and parsed.username is None and parsed.password is None
        if parsed.port is not None:
            valid = valid and 1 <= parsed.port <= 65535
    except ValueError:
        valid = False
    _require(valid)


def validate_annotation(value):
    _require(type(value) is dict)
    kind = value.get("type")
    fields = {
        "url_citation": {"type", "start_index", "end_index", "url", "title"},
        "file_citation": {"type", "file_id", "filename", "index"},
        "container_file_citation": {"type", "container_id", "file_id", "filename", "start_index", "end_index"},
        "file_path": {"type", "file_id", "index"},
    }
    _require(type(kind) is str and kind in fields, "unsupported_native_annotation")
    _object(value, fields[kind])
    for name, item in value.items():
        if name in {"index", "start_index", "end_index"}:
            _integer(item)
        elif name == "url":
            _url(item)
        else:
            _text(item)
    if "start_index" in value:
        _require(value["start_index"] <= value["end_index"])


def _logprob(value, *, top=False):
    _object(value, {"token", "bytes", "logprob"}, {"top_logprobs"} if not top else ())
    _text(value["token"])
    _require(type(value["logprob"]) in {int, float} and math.isfinite(value["logprob"]))
    _require(type(value["bytes"]) is list and all(type(byte) is int and 0 <= byte <= 255 for byte in value["bytes"]))
    if "top_logprobs" in value:
        _require(type(value["top_logprobs"]) is list)
        for candidate in value["top_logprobs"]:
            _logprob(candidate, top=True)


def validate_part(part, *, reasoning=False, summary=False):
    if reasoning or summary:
        _object(part, {"type", "text"})
        _require(part["type"] == ("summary_text" if summary else "reasoning_text"), "unsupported_native_content")
        _text(part["text"])
        return
    _require(type(part) is dict)
    if part.get("type") == "output_text":
        _object(part, {"type", "text"}, {"annotations", "logprobs"})
        _text(part["text"])
        if "annotations" in part:
            _require(type(part["annotations"]) is list)
            for annotation in part["annotations"]:
                validate_annotation(annotation)
        if part.get("logprobs") is not None:
            _require(type(part["logprobs"]) is list)
            for logprob in part["logprobs"]:
                _logprob(logprob)
    elif part.get("type") == "refusal":
        _object(part, {"type", "refusal"})
        _text(part["refusal"])
    else:
        raise NativeOutputError("unsupported_native_content")


def validate_item(item, *, partial=False):
    _require(type(item) is dict)
    kind = item.get("type")
    _require(type(kind) is str and kind in ITEM_TYPES, "unsupported_native_output_item")
    fields = {
        "message": {"id", "type", "role", "status", "content", "phase"},
        "reasoning": {"id", "type", "summary", "content", "encrypted_content", "status"},
        "web_search_call": {"id", "type", "status", "action"},
        "function_call": {"id", "type", "status", "call_id", "name", "arguments", "namespace", "async", "caller"},
        "custom_tool_call": {"id", "type", "status", "call_id", "name", "input", "namespace", "async", "caller", "created_by"},
    }
    _object(item, {"type"}, fields[kind])
    if "id" in item:
        nullable = kind in {"function_call", "custom_tool_call"}
        _text(item["id"], empty=False, nullable=nullable)
        if item["id"] is not None:
            _require(len(item["id"]) <= 65536, "native_output_limit")
    if item.get("status") is not None:
        statuses = ITEM_STATUS | ({"searching", "failed"} if kind == "web_search_call" else set())
        _require(type(item["status"]) is str and item["status"] in statuses)
    if kind == "message":
        # Retain legacy omission of message id/role/status, but supplied facts
        # cannot contradict an assistant output message. Never invent its IDs.
        if "role" in item:
            _require(item["role"] == "assistant")
        if "status" in item:
            _require(type(item["status"]) is str)
        if "phase" in item:
            _require(item["phase"] is None or item["phase"] in ("commentary", "final_answer"))
        if not partial or "content" in item:
            _require(type(item.get("content")) is list)
            for part in item["content"]:
                validate_part(part)
            if not partial and not item["content"]:
                _text(item.get("id"), empty=False)
    elif kind in {"function_call", "custom_tool_call"}:
        content_key = "arguments" if kind == "function_call" else "input"
        for key in ("call_id", "name", content_key):
            if not partial or key in item:
                _text(item.get(key), empty=partial or key == content_key)
        for key in ("namespace", "created_by"):
            if key in item:
                _text(item[key], nullable=True)
        if item.get("async") is not None:
            _require(type(item["async"]) is bool)
        if item.get("caller") is not None:
            caller = item["caller"]
            _require(type(caller) is dict)
            if caller.get("type") == "direct":
                _object(caller, {"type"})
            elif caller.get("type") == "program":
                _object(caller, {"type", "caller_id"})
                _text(caller["caller_id"], empty=False)
            else:
                raise NativeOutputError("unsupported_native_caller")
    elif kind == "reasoning":
        if not partial:
            _text(item.get("id"), empty=False)
        if not partial or "summary" in item:
            _require(type(item.get("summary")) is list)
            for part in item["summary"]:
                validate_part(part, summary=True)
        if item.get("content") is not None:
            _require(type(item["content"]) is list)
            for part in item["content"]:
                validate_part(part, reasoning=True)
        if "encrypted_content" in item:
            _text(item["encrypted_content"], nullable=True)
    else:
        if not partial:
            _text(item.get("id"), empty=False)
            _require(type(item.get("status")) is str)
        if not partial or "action" in item:
            action = item.get("action")
            _require(type(action) is dict)
            action_type = action.get("type")
            if action_type == "search":
                _object(action, {"type"}, {"query", "queries", "sources"})
                if action.get("query") is not None:
                    _text(action["query"])
                if action.get("queries") is not None:
                    _require(type(action["queries"]) is list)
                    for query in action["queries"]:
                        _text(query)
                if action.get("sources") is not None:
                    _require(type(action["sources"]) is list)
                    for source in action["sources"]:
                        _object(source, {"type", "url"})
                        _require(source["type"] == "url")
                        _url(source["url"])
            elif action_type == "open_page":
                _object(action, {"type"}, {"url"})
                if action.get("url") is not None:
                    _url(action["url"])
            elif action_type == "find_in_page":
                _object(action, {"type", "url", "pattern"})
                _url(action["url"])
                _text(action["pattern"])
            else:
                raise NativeOutputError("unsupported_native_search_action")


def validate_output(output, *, partial=False):
    _require(type(output) is list)
    check_json(output)
    ids, calls = set(), set()
    for item in output:
        validate_item(item, partial=partial)
        for key, seen in (("id", ids), ("call_id", calls)):
            value = item.get(key)
            if value:
                _require(value not in seen, "duplicate_native_item")
                seen.add(value)
    return deepcopy(output)


def failed_response(payload, display_model, code):
    response = {"object": "response", "status": "failed", "model": display_model, "output": [],
                "error": {"code": code, "message": f"The provider response did not satisfy native output contract v{CONTRACT_VERSION}."}}
    if type(payload) is dict:
        if type(payload.get("id")) is str and 0 < len(payload["id"]) <= 65536:
            try:
                payload["id"].encode("utf-8")
                response["id"] = payload["id"]
            except UnicodeError:
                pass
        if type(payload.get("usage")) is dict:
            usage = payload["usage"]
            # Do not reparse rejected numbers through float(): oversized JSON
            # integers can overflow while constructing the error response.
            def count(name):
                value = usage.get(name)
                return value if type(value) is int and 0 <= value <= 2**63 - 1 else 0
            incoming, outgoing = count("input_tokens"), count("output_tokens")
            response["usage"] = {
                "input_tokens": incoming, "output_tokens": outgoing,
                "total_tokens": min(2**63 - 1, max(count("total_tokens"), incoming + outgoing)),
            }
    return response


def validate_response(payload, *, display_model):
    if type(payload) is not dict:
        raise ValueError("native Responses payload must be an object")
    status = payload.get("status")
    if status is not None and (type(status) is not str or status not in {"completed", "incomplete", "failed"}):
        raise ValueError("native Responses payload has an invalid status")
    output = payload.get("output")
    if type(output) is not list:
        raise ValueError("native Responses payload output must be a list")
    try:
        check_json(payload)
        output = validate_output(output)
    except NativeOutputError as exc:
        return failed_response(payload, display_model, exc.code)
    response = deepcopy(payload)
    response["model"] = display_model
    response["status"] = status or ("completed" if output else "failed")
    response["output"] = output
    if response["status"] == "completed" and not output:
        return failed_response(payload, display_model, "empty_response")
    if response["status"] == "failed":
        error = payload.get("error")
        code = error.get("code") if type(error) is dict else None
        safe_code = code if type(code) is str and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code) else "provider_error"
        response["error"] = {"code": safe_code, "message": "The provider request failed."}
    return response


def validate_event(event):
    """Validate data-bearing native SSE variants without interpreting tool input."""
    kind = event.get("type")
    _require(kind in EVENT_TYPES, "unsupported_native_event")
    check_json({key: value for key, value in event.items() if key != "response"})
    response = event.get("response")
    if response is not None:
        _require(type(response) is dict)
        check_json(response)
        if "output" in response and kind not in {"response.completed", "response.incomplete", "response.failed"}:
            validate_output(response["output"], partial=True)
    for key in ("output_index", "content_index", "summary_index", "annotation_index"):
        if key in event:
            _integer(event[key])
            _require(event[key] < MAX_ITEMS, "native_output_limit")
    expected_item = None
    if kind in {"response.output_item.added", "response.output_item.done"}:
        _require(type(event.get("output_index")) is int)
        validate_item(event.get("item"), partial=kind.endswith(".added"))
        expected_item = event["item"]["type"]
    elif kind.startswith("response.web_search_call."):
        expected_item = "web_search_call"
    elif kind.startswith("response.reasoning"):
        expected_item = "reasoning"
        if "summary_part" in kind:
            validate_part(event.get("part"), summary=True)
        else:
            _text(event.get("delta" if kind.endswith(".delta") else "text"))
    elif kind.startswith("response.function_call_arguments."):
        expected_item = "function_call"
        _text(event.get("delta" if kind.endswith(".delta") else "arguments"))
    elif kind.startswith("response.custom_tool_call_input."):
        expected_item = "custom_tool_call"
        _text(event.get("delta" if kind.endswith(".delta") else "input"))
    elif kind.startswith("response.content_part."):
        part = event.get("part")
        is_reasoning = type(part) is dict and part.get("type") == "reasoning_text"
        validate_part(part, reasoning=is_reasoning)
        expected_item = "reasoning" if is_reasoning else "message"
    elif kind.startswith("response.output_text."):
        expected_item = "message"
        if kind.endswith("annotation.added"):
            _require("annotation" in event)
            if event["annotation"] is not None:
                validate_annotation(event["annotation"])
        else:
            _text(event.get("delta" if kind.endswith(".delta") else "text"))
    elif kind.startswith("response.refusal."):
        expected_item = "message"
        _text(event.get("delta" if kind.endswith(".delta") else "refusal"))
    return expected_item


def _merge_snapshot(complete, supplied):
    """Fill omitted nested fields; supplied list positions and facts must agree."""
    if supplied is None:
        return deepcopy(complete)
    if complete is None:
        return deepcopy(supplied)
    _require(type(complete) is type(supplied), "conflicting_native_item")
    if type(complete) is dict:
        merged = deepcopy(complete)
        for key, value in supplied.items():
            merged[key] = _merge_snapshot(complete[key], value) if key in complete else deepcopy(value)
        return merged
    if type(complete) is list:
        _require(len(complete) == len(supplied), "conflicting_native_item")
        return [_merge_snapshot(left, right) for left, right in zip(complete, supplied)]
    _require(complete == supplied, "conflicting_native_item")
    return deepcopy(supplied)


def reconcile_output(output, completed):
    """Use complete item snapshots, never partial encrypted data from added events."""
    if output is None:
        output = []
    _require(type(output) is list)
    # Validate the merged terminal shape below: even required nested fields
    # may be omitted here when a complete done snapshot already supplies them.
    check_json(output)
    result = deepcopy(output)
    for index, item in sorted(completed.items()):
        _require(index <= len(result), "incomplete_native_output")
        if index == len(result):
            result.append(deepcopy(item))
            continue
        result[index] = _merge_snapshot(item, result[index])
    return validate_output(result)
