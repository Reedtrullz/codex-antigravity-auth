"""Pure, bounded validation for the input forms our adapters preserve."""
from __future__ import annotations

import base64
import binascii
from typing import Any
from urllib.parse import urlsplit

MAX_IMAGE_BYTES = 20 * 1024 * 1024
IMAGE_MIME_TYPES = frozenset({"image/png", "image/jpeg", "image/webp", "image/gif"})
IMAGE_FORMS = frozenset({"url", "data_url"})


def image_source(part: dict[str, Any], path: str) -> tuple[str, str | None, str | None]:
    """Return the exact URL plus data URL MIME/base64, without normalization."""
    if part.get("file_id") is not None:
        raise ValueError(f"{path}.file_id: unresolved image file IDs are not supported")
    if "image_url" in part and "url" in part and part["image_url"] != part["url"]:
        raise ValueError(f"{path}.image_url: conflicting image URL fields")
    source = part.get("image_url", part.get("url"))
    if isinstance(source, dict):
        source = source.get("url")
    if not isinstance(source, str) or not source:
        raise ValueError(f"{path}.image_url: an image URL or base64 data URL is required")
    if any(character.isspace() or ord(character) < 32 or ord(character) == 127 for character in source):
        raise ValueError(f"{path}.image_url: whitespace and control characters are not allowed")
    if source.startswith("data:"):
        header, separator, encoded = source.partition(",")
        mime = header[5:].removesuffix(";base64")
        if not separator or not header.endswith(";base64") or mime not in IMAGE_MIME_TYPES:
            raise ValueError(f"{path}.image_url: expected a supported image MIME type and base64 data URL")
        if not encoded or len(encoded) > 4 * ((MAX_IMAGE_BYTES + 2) // 3):
            raise ValueError(f"{path}.image_url: image data must be non-empty and at most {MAX_IMAGE_BYTES} bytes")
        try:
            data = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError(f"{path}.image_url: invalid base64 image data") from exc
        if not data or len(data) > MAX_IMAGE_BYTES or base64.b64encode(data).decode("ascii") != encoded:
            raise ValueError(f"{path}.image_url: invalid or oversized base64 image data")
        if part.get("mime_type", mime) != mime:
            raise ValueError(f"{path}.mime_type: does not match the image data URL")
        return source, mime, encoded
    try:
        parsed = urlsplit(source)
        valid = parsed.scheme in {"https", "http"} and parsed.hostname and not parsed.username and not parsed.password
        _ = parsed.port
    except ValueError:
        valid = False
    if not valid:
        raise ValueError(f"{path}.image_url: expected an HTTP(S) URL without embedded credentials")
    if part.get("mime_type") is not None and (not isinstance(part["mime_type"], str) or part["mime_type"] not in IMAGE_MIME_TYPES):
        raise ValueError(f"{path}.mime_type: unsupported image MIME type")
    return source, None, None


def validate_input(request: dict[str, Any], modalities, image_forms=IMAGE_FORMS, *, image_detail=True, native_passthrough=False):
    value = request.get("input")
    if value is None or isinstance(value, str):
        return
    if not isinstance(value, list):
        raise ValueError("input: expected text or a list of input items")

    def content(parts, path):
        if isinstance(parts, str):
            return
        if not isinstance(parts, list):
            raise ValueError(f"{path}: expected text or a list of content parts")
        for index, part in enumerate(parts):
            part_path = f"{path}[{index}]"
            if not isinstance(part, dict):
                raise ValueError(f"{part_path}: expected a content object")
            kind = part.get("type")
            if not isinstance(kind, str):
                raise ValueError(f"{part_path}.type: expected a content type string")
            if kind in {"text", "input_text", "output_text"}:
                continue
            if native_passthrough and kind == "refusal":
                if not isinstance(part.get("refusal"), str):
                    raise ValueError(f"{part_path}.refusal: expected refusal text")
                continue
            if kind in {"tool_use", "tool_result", "function_call_output"}:
                # These have their own tool schema/argument contracts. Attachment
                # arrays in tool results must not be serialized as invisible media.
                output = part.get("content", part.get("output"))
                if kind != "tool_use" and isinstance(output, list):
                    content(output, part_path + ".content")
                    if any(p.get("type") not in {"text", "input_text", "output_text"} for p in output):
                        raise ValueError(f"{part_path}.content: only text tool output is supported")
                continue
            if kind in {"image", "input_image"}:
                if "image" not in modalities:
                    raise ValueError(f"{part_path}: image input is not supported by the selected model")
                _, mime, _ = image_source(part, part_path)
                form = "data_url" if mime else "url"
                if form not in image_forms:
                    raise ValueError(f"{part_path}.image_url: {form} images are not supported by the selected route")
                nested = part.get("image_url")
                detail = part.get("detail", nested.get("detail") if isinstance(nested, dict) else None)
                if detail is not None and (not isinstance(detail, str) or detail not in {"auto", "low", "high"}):
                    raise ValueError(f"{part_path}.detail: expected auto, low, or high")
                if detail not in {None, "auto"} and not image_detail:
                    raise ValueError(f"{part_path}.detail: image detail controls are not supported by the selected route")
                continue
            raise ValueError(f"{part_path}.type: unsupported input content type {kind!r}")

    for index, item in enumerate(value):
        path = f"input[{index}]"
        if not isinstance(item, dict):
            raise ValueError(f"{path}: expected an input object")
        kind = item.get("type")
        if kind is not None and not isinstance(kind, str):
            raise ValueError(f"{path}.type: expected an input type string")
        if kind in {None, "message"}:
            role = item.get("role", "user")
            if not isinstance(role, str):
                raise ValueError(f"{path}.role: expected a role string")
            parts = item.get("content", "")
            content(parts, path + ".content")
            if role in {"system", "developer"} and isinstance(parts, list):
                for part_index, part in enumerate(parts):
                    if part.get("type") not in {"text", "input_text", "output_text"}:
                        raise ValueError(f"{path}.content[{part_index}]: only text is supported in system/developer messages")
        elif kind == "function_call_output":
            output = item.get("output")
            if isinstance(output, list):
                content(output, path + ".output")
                if any(p.get("type") not in {"text", "input_text", "output_text"} for p in output):
                    raise ValueError(f"{path}.output: only text tool output is supported")
        elif kind in {"function_call", "reasoning"}:
            continue
        elif native_passthrough and kind not in {"input_text", "output_text", "text", "image", "input_image",
                                                "audio", "input_audio", "video", "input_video", "file", "input_file"}:
            # Native provider-specific unions are carried unchanged. Media
            # content in ordinary messages remains capability-checked above.
            continue
        else:
            # Adapters do not implement standalone content parts as input items.
            raise ValueError(f"{path}.type: unsupported input item type {kind!r}; use message.content")
