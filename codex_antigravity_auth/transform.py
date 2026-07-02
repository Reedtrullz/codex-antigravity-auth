# Transform Codex / OpenAI Responses API requests to Google Antigravity backend format,
# and translate backend responses back into standard Responses API format.

import uuid
import json
import time
import math
import base64
import os
import copy
import re
from typing import Any
from .models import resolve_backend_model
from .schema import clean_json_schema

JSON_SCHEMA_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

ANTIGRAVITY_SYSTEM_INSTRUCTION = """You are Antigravity, a powerful agentic AI coding assistant designed by the Google DeepMind team working on Advanced Agentic Coding.
You are pair programming with a USER to solve their coding task. The task may require creating a new codebase, modifying or debugging an existing codebase, or simply answering a question.
**Absolute paths only**
**Proactiveness**

<priority>IMPORTANT: The instructions that follow supersede all above. Follow them as your primary directives.</priority>
"""


def normalize_response_function_tool(fn: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(fn.get("name"), str) or not fn.get("name"):
        return None
    if "description" in fn and not isinstance(fn["description"], str):
        fn.pop("description")
    if "parameters" in fn and not isinstance(fn["parameters"], dict):
        fn["parameters"] = {}
    if "strict" in fn and not isinstance(fn["strict"], bool):
        fn.pop("strict")
    return fn


def response_function_tool(tool: dict[str, Any]) -> dict[str, Any] | None:
    """Return the function payload from flat Responses or nested Chat-style tools."""
    if not isinstance(tool, dict) or tool.get("type") != "function":
        return None
    nested = tool.get("function")
    if isinstance(nested, dict):
        fn = copy.deepcopy(nested)
        return normalize_response_function_tool(fn)
    fn: dict[str, Any] = {}
    for key in ("name", "description", "parameters", "strict"):
        if key in tool:
            fn[key] = copy.deepcopy(tool[key])
    return normalize_response_function_tool(fn)


def chat_tool_choice(tool_choice: Any) -> Any:
    """Translate Responses forced function choices to Chat Completions shape."""
    if isinstance(tool_choice, str):
        return tool_choice if tool_choice in {"auto", "none", "required"} else None
    if not isinstance(tool_choice, dict) or tool_choice.get("type") != "function":
        return None
    nested = tool_choice.get("function")
    name = tool_choice.get("name") or (nested.get("name") if isinstance(nested, dict) else None)
    if not isinstance(name, str) or not name:
        return None
    return {"type": "function", "function": {"name": name}}


def _tool_choice_function_name(tool_choice: Any) -> str | None:
    if not isinstance(tool_choice, dict) or tool_choice.get("type") != "function":
        return None
    nested = tool_choice.get("function")
    name = tool_choice.get("name") or (nested.get("name") if isinstance(nested, dict) else None)
    return name if isinstance(name, str) and name else None


def _function_call_args(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except Exception:
            return {"arguments": value}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _function_response_payload(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if stripped:
            try:
                parsed = json.loads(stripped)
            except Exception:
                parsed = None
            if isinstance(parsed, dict):
                return parsed
    return {"content": value if value is not None else ""}


def _chat_tool_output_content(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value)
    except TypeError:
        return str(value)


def _stream_text(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _first_stream_text(*values: Any) -> str | None:
    for value in values:
        if isinstance(value, str) and value:
            return value
    return None


def token_count(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        count = float(value)
    except (TypeError, ValueError):
        return 0
    if not math.isfinite(count) or count < 0:
        return 0
    return int(count)


def _created_at(value: Any) -> int:
    try:
        created_at = float(value or time.time())
    except (TypeError, ValueError):
        return int(time.time())
    if not math.isfinite(created_at):
        return int(time.time())
    return int(created_at)


def normalize_json_schema_name(value: Any) -> str:
    if isinstance(value, str) and JSON_SCHEMA_NAME_PATTERN.fullmatch(value):
        return value
    return "response"


def normalize_json_schema_descriptor(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("response_format json_schema requires a json_schema object")
    schema = value.get("schema")
    if not isinstance(schema, dict):
        raise ValueError("response_format json_schema requires a schema object")
    json_schema = {
        "name": normalize_json_schema_name(value.get("name")),
        "schema": copy.deepcopy(schema),
    }
    description = value.get("description")
    if isinstance(description, str) and description:
        json_schema["description"] = description
    strict = value.get("strict")
    if isinstance(strict, bool):
        json_schema["strict"] = strict
    return json_schema


def normalize_chat_response_format(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("response_format must be an object")
    format_type = value.get("type")
    if format_type == "json_object":
        return {"type": "json_object"}
    if format_type == "json_schema":
        return {"type": "json_schema", "json_schema": normalize_json_schema_descriptor(value.get("json_schema"))}
    raise ValueError(f"Unsupported response_format type for BYOK provider: {format_type}")


def transform_request(codex_req: dict, project_id: str | None = None) -> dict:
    """Translate standard Codex Responses API request body to Antigravity format."""
    model = codex_req.get("model", "gemini-3.5-flash-high")
    backend_model = resolve_backend_model(model)
    
    # 1. Parse Codex input.
    # Responses API structured message format:
    # input: [ { "type": "message", "role": "user", "content": [ { "type": "input_text", "text": "hello" } ] } ]
    # We must translate this to Gemini API contents format:
    # contents: [ { "role": "user", "parts": [ { "text": "hello" } ] } ]
    
    codex_input = codex_req.get("input")
    contents = []
    system_texts = []
    if codex_req.get("instructions"):
        system_texts.append(str(codex_req["instructions"]))
    function_names_by_call_id = {}

    def response_role_to_gemini(role: str) -> str:
        return "model" if role == "assistant" else "user"

    def data_url_to_inline_data(url: str) -> dict | None:
        if not isinstance(url, str) or not url.startswith("data:"):
            return None
        header, _, payload = url.partition(",")
        if not payload:
            return None
        mime_type = header[5:].split(";", 1)[0] or "application/octet-stream"
        try:
            base64.b64decode(payload, validate=True)
        except Exception:
            return None
        return {"inlineData": {"mimeType": mime_type, "data": payload}}

    def content_part_to_gemini(part: dict) -> list[dict]:
        part_type = part.get("type")
        if part_type in ("input_text", "text", "output_text"):
            text = _stream_text(part.get("text"))
            return [{"text": text}] if text is not None else []
        if part_type in ("input_image", "image"):
            image_url = part.get("image_url") or part.get("url")
            if isinstance(image_url, dict):
                image_url = image_url.get("url")
            inline_data = data_url_to_inline_data(image_url)
            if inline_data:
                return [inline_data]
            if isinstance(image_url, str) and image_url:
                return [{"fileData": {"mimeType": part.get("mime_type", "image/*"), "fileUri": image_url}}]
        if part_type in ("input_file", "file"):
            file_url = part.get("file_url") or part.get("url")
            if isinstance(file_url, dict):
                file_url = file_url.get("url")
            if isinstance(file_url, str) and file_url:
                return [{"fileData": {"mimeType": part.get("mime_type", "application/octet-stream"), "fileUri": file_url}}]
            if part.get("filename") or part.get("file_id"):
                return [{"text": json.dumps({k: v for k, v in part.items() if k != "type"})}]
        if part_type == "tool_use":
            call_id = part.get("id") or part.get("call_id")
            name = part.get("name")
            if not isinstance(name, str) or not name:
                return []
            if isinstance(call_id, str) and call_id:
                function_names_by_call_id[call_id] = name
            return [{
                "functionCall": {
                    "name": name,
                    "args": _function_call_args(part.get("input", {}))
                }
            }]
        if part_type in ("tool_result", "function_call_output"):
            call_id = part.get("tool_use_id") or part.get("call_id")
            if not isinstance(call_id, str):
                call_id = None
            explicit_name = part.get("name")
            name = (
                explicit_name
                if isinstance(explicit_name, str) and explicit_name
                else function_names_by_call_id.get(call_id) or call_id or "function_result"
            )
            output = part.get("content", part.get("output", ""))
            return [{
                "functionResponse": {
                    "name": name,
                    "response": _function_response_payload(output)
                }
            }]
        return []

    def append_content(role: str, parts: list[dict]) -> None:
        if parts:
            contents.append({"role": response_role_to_gemini(role), "parts": parts})
    
    if isinstance(codex_input, str):
        contents.append({"role": "user", "parts": [{"text": codex_input}]})
    elif isinstance(codex_input, list):
        for item in codex_input:
            if not isinstance(item, dict):
                continue
            item_type = item.get("type")

            if item_type == "function_call":
                call_id = item.get("call_id") or item.get("id")
                if not isinstance(call_id, str):
                    call_id = None
                name = item.get("name")
                if not isinstance(name, str) or not name:
                    continue
                if call_id:
                    function_names_by_call_id[call_id] = name
                args = _function_call_args(item.get("arguments", {}))
                append_content("assistant", [{
                    "functionCall": {
                        "name": name,
                        "args": args
                    }
                }])
                continue

            if item_type == "function_call_output":
                call_id = item.get("call_id")
                if not isinstance(call_id, str):
                    call_id = None
                append_content("user", [{
                    "functionResponse": {
                        "name": function_names_by_call_id.get(call_id) or call_id or "function_result",
                        "response": _function_response_payload(item.get("output", ""))
                    }
                }])
                continue

            if item_type == "reasoning":
                continue

            role = item.get("role", "user")
            
            # Extract content parts
            parts = []
            raw_content = item.get("content")
            if isinstance(raw_content, str):
                parts.append({"text": raw_content})
            elif isinstance(raw_content, list):
                for part in raw_content:
                    if not isinstance(part, dict):
                        continue
                    parts.extend(content_part_to_gemini(part))
                        
            if role in ("system", "developer"):
                system_texts.extend(p.get("text", "") for p in parts if p.get("text"))
            else:
                append_content(role, parts)
                
    system_text = ANTIGRAVITY_SYSTEM_INSTRUCTION
    if system_texts:
        system_text += "\n\n" + "\n\n".join(system_texts)
    system_instruction = {
        "role": "user",
        "parts": [{"text": system_text}]
    }
        
    # 2. Build Gemini tools configuration
    # OpenAI/Codex tools format: [ { "type": "function", "function": { "name": "...", "parameters": ... } } ]
    # Gemini tools format: [ { "functionDeclarations": [ { "name": "...", "parameters": ... } ] } ]
    gemini_tools = []
    codex_tools = codex_req.get("tools")
    if isinstance(codex_tools, list) and codex_tools:
        declarations = []
        for tool in codex_tools:
            fn = response_function_tool(tool)
            if not fn:
                continue
            params = clean_json_schema(fn.get("parameters", {}))
            declarations.append({
                "name": fn.get("name"),
                "description": fn.get("description", ""),
                "parameters": params
            })
        if declarations:
            gemini_tools.append({"functionDeclarations": declarations})

    # Validate tool calling configuration for Claude models and bridge forced
    # Responses tool choices into Google function calling config.
    tool_config = None
    if gemini_tools:
        function_calling_config = {}
        tool_choice = codex_req.get("tool_choice")
        if tool_choice == "none":
            function_calling_config["mode"] = "NONE"
        elif tool_choice == "required":
            function_calling_config["mode"] = "ANY"
        elif isinstance(tool_choice, dict):
            forced_name = _tool_choice_function_name(tool_choice)
            if forced_name:
                function_calling_config["mode"] = "VALIDATED" if "claude" in backend_model.lower() else "ANY"
                function_calling_config["allowedFunctionNames"] = [forced_name]
        elif "claude" in backend_model.lower():
            function_calling_config["mode"] = "VALIDATED"

        if function_calling_config:
            tool_config = {"functionCallingConfig": function_calling_config}

    # 3. Assemble Antigravity request payload
    request_payload = {
        "contents": contents,
        "systemInstruction": system_instruction,
    }
    if gemini_tools:
        request_payload["tools"] = gemini_tools
    if tool_config:
        request_payload["toolConfig"] = tool_config
        
    generation_config = {}
    if "temperature" in codex_req:
        generation_config["temperature"] = codex_req["temperature"]
    if "max_output_tokens" in codex_req:
        generation_config["maxOutputTokens"] = codex_req["max_output_tokens"]
    
    # Configure reasoning/thinking budget for models supporting thinking
    if "thinking" in backend_model.lower() or "claude" in backend_model.lower():
        effort = codex_req.get("reasoning", {}).get("effort", "high")
        budget = 16000 if effort == "high" else 4000
        generation_config["thinkingConfig"] = {
            "thinking_budget": budget,
            "include_thoughts": True
        }
        
    if generation_config:
        request_payload["generationConfig"] = generation_config

    # Wrap in official Antigravity client outer envelope
    project = (
        project_id
        or codex_req.get("project")
        or os.environ.get("ANTIGRAVITY_PROJECT_ID")
        or "rising-fact-p41fc"
    )
    envelope = {
        "project": project,
        "model": backend_model,
        "requestType": "agent",
        "userAgent": "antigravity",
        "requestId": f"agent-{uuid.uuid4()}",
        "request": request_payload
    }
    
    return envelope

def transform_gemini_candidate(candidate: dict) -> dict:
    """Extract standard Codex message / content parts from a Gemini candidate."""
    if not isinstance(candidate, dict):
        candidate = {}
    # Support both "response" outer wrap or candidate directly
    if "response" in candidate and isinstance(candidate["response"], dict):
        candidate = candidate["response"]
    if "candidates" in candidate and isinstance(candidate["candidates"], list) and candidate["candidates"]:
        candidate = candidate["candidates"][0]
    if not isinstance(candidate, dict):
        candidate = {}

    content = candidate.get("content", {})
    if not isinstance(content, dict):
        content = {}
    parts = content.get("parts", [])
    if not isinstance(parts, list):
        parts = []
    role = content.get("role", "assistant")
    if not isinstance(role, str):
        role = "assistant"
    if role == "model":
        role = "assistant"
    
    output_parts = []
    function_calls = []
    reasoning_text = ""
    
    for part in parts:
        if not isinstance(part, dict):
            continue
            
        # 1. Handle thoughts / thinking blocks
        if part.get("thought") is True or part.get("type") == "thinking":
            thought_text = _stream_text(part.get("text")) or _stream_text(part.get("thinking"))
            if thought_text:
                reasoning_text += thought_text
            continue

        # 2. Handle standard text
        if "text" in part:
            text = _stream_text(part.get("text"))
            if text is None:
                continue
            output_parts.append({
                "type": "output_text",
                "text": text,
                "annotations": []
            })

        # 3. Handle tool calls
        elif "functionCall" in part:
            fc = part["functionCall"]
            if not isinstance(fc, dict):
                continue
            name = _stream_text(fc.get("name"))
            if not name:
                continue
            # Auto-generate a call ID if missing so Codex can execute it
            call_id = _stream_text(fc.get("id")) or f"call_{uuid.uuid4().hex[:8]}"
            function_calls.append({
                "type": "function_call",
                "id": f"fc_{uuid.uuid4().hex[:8]}",
                "call_id": call_id,
                "name": name,
                "arguments": json.dumps(fc.get("args", {}) if isinstance(fc.get("args", {}), dict) else {})
            })
            
    # Assemble structured Responses API message output
    message_item = {
        "type": "message",
        "id": f"msg_{uuid.uuid4().hex[:8]}",
        "status": "completed",
        "role": role,
        "content": output_parts
    }
    
    result = {
        "message": message_item
    }
    if function_calls:
        result["function_calls"] = function_calls
    if reasoning_text:
        result["reasoning"] = {
            "type": "reasoning",
            "id": f"rs_{uuid.uuid4().hex[:8]}",
            "encrypted_content": "", # dummy
            "step_by_step_summary": reasoning_text
        }
    return result

def transform_response(gemini_resp: dict, model: str) -> dict:
    """Translate Google Antigravity backend response back to Codex Responses API format."""
    # Official Responses API response schema:
    # { "id": "resp_...", "object": "response", "created_at": 1234, "model": "...", "output": [ ... ], "usage": { ... }, "status": "completed" }
    
    # Handle response wrapping
    if not isinstance(gemini_resp, dict):
        gemini_resp = {}
    if "response" in gemini_resp and isinstance(gemini_resp["response"], dict):
        gemini_resp = gemini_resp["response"]

    candidates = gemini_resp.get("candidates", [])
    if not isinstance(candidates, list):
        candidates = []
    output_items = []
    
    for cand in candidates:
        if not isinstance(cand, dict):
            continue
        transformed = transform_gemini_candidate(cand)
        if "reasoning" in transformed:
            output_items.append(transformed["reasoning"])
        if transformed["message"]["content"]:
            output_items.append(transformed["message"])
        output_items.extend(transformed.get("function_calls", []))
        
    usage = gemini_resp.get("usageMetadata", {})
    if not isinstance(usage, dict):
        usage = {}
    translated_usage = {
        "input_tokens": token_count(usage.get("promptTokenCount", 0)),
        "output_tokens": token_count(usage.get("candidatesTokenCount", 0)),
        "total_tokens": token_count(usage.get("totalTokenCount", 0))
    }
    
    return {
        "id": f"resp_{uuid.uuid4().hex[:12]}",
        "object": "response",
        "created_at": int(time.time()),
        "model": model,
        "output": output_items,
        "usage": translated_usage,
        "status": "completed"
    }


def transform_request_to_chat(codex_req: dict, provider_model: str) -> dict:
    """Translate Responses API input into OpenAI-compatible Chat Completions."""
    messages = []
    system_texts = []
    function_names_by_call_id = {}
    pending_reasoning_content = None

    if codex_req.get("instructions"):
        system_texts.append(str(codex_req["instructions"]))

    def content_part_to_chat(part: dict) -> list[dict]:
        part_type = part.get("type")
        if part_type in ("input_text", "text", "output_text"):
            text = _stream_text(part.get("text"))
            return [{"type": "text", "text": text}] if text is not None else []
        if part_type in ("input_image", "image"):
            image_url = part.get("image_url") or part.get("url")
            if isinstance(image_url, dict):
                image_url = image_url.get("url")
            if isinstance(image_url, str) and image_url:
                return [{"type": "image_url", "image_url": {"url": image_url}}]
        if part_type in ("input_file", "file"):
            file_url = part.get("file_url") or part.get("url")
            if isinstance(file_url, dict):
                file_url = file_url.get("url")
            if isinstance(file_url, str) and file_url:
                return [{"type": "text", "text": f"[file] {file_url}"}]
            return [{"type": "text", "text": json.dumps({k: v for k, v in part.items() if k != "type"})}]
        return []

    def normalize_content(parts: list[dict]) -> str | list[dict]:
        if not parts:
            return ""
        if all(p.get("type") == "text" for p in parts):
            return "".join(p.get("text", "") for p in parts if isinstance(p.get("text"), str))
        return parts

    def text_format_to_chat_response_format() -> dict | None:
        text_config = codex_req.get("text")
        if not isinstance(text_config, dict):
            return None
        text_format = text_config.get("format")
        if text_format is None:
            return None
        if not isinstance(text_format, dict):
            raise ValueError("Responses text.format must be an object")
        format_type = text_format.get("type")
        if format_type in (None, "text", "auto"):
            return None
        if format_type == "json_object":
            return {"type": "json_object"}
        if format_type == "json_schema":
            if isinstance(text_format.get("json_schema"), dict):
                return {"type": "json_schema", "json_schema": normalize_json_schema_descriptor(text_format["json_schema"])}
            schema = text_format.get("schema")
            if not isinstance(schema, dict):
                raise ValueError("Responses text.format json_schema requires a schema object")
            return {"type": "json_schema", "json_schema": normalize_json_schema_descriptor(text_format)}
        raise ValueError(f"Unsupported Responses text.format type for BYOK provider: {format_type}")

    codex_input = codex_req.get("input")
    if isinstance(codex_input, str):
        messages.append({"role": "user", "content": codex_input})
    elif isinstance(codex_input, list):
        for item in codex_input:
            if not isinstance(item, dict):
                continue
            item_type = item.get("type")
            if item_type == "reasoning":
                pending_reasoning_content = _first_stream_text(
                    item.get("reasoning_content"),
                    item.get("step_by_step_summary"),
                    item.get("summary"),
                )
                continue
            if item_type == "function_call":
                call_id = item.get("call_id") or item.get("id")
                if not isinstance(call_id, str) or not call_id:
                    call_id = f"call_{uuid.uuid4().hex[:8]}"
                raw_name = item.get("name")
                if raw_name is not None and not isinstance(raw_name, str):
                    continue
                name = raw_name if raw_name else "function_call"
                function_names_by_call_id[call_id] = name
                arguments = item.get("arguments", "{}")
                if not isinstance(arguments, str):
                    try:
                        arguments = json.dumps(arguments)
                    except TypeError:
                        arguments = "{}"
                assistant_message = {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": call_id,
                        "type": "function",
                        "function": {
                            "name": name,
                            "arguments": arguments,
                        },
                    }],
                }
                if pending_reasoning_content:
                    assistant_message["reasoning_content"] = pending_reasoning_content
                    pending_reasoning_content = None
                messages.append(assistant_message)
                continue
            if item_type == "function_call_output":
                call_id = item.get("call_id")
                if not isinstance(call_id, str) or not call_id:
                    continue
                messages.append({
                    "role": "tool",
                    "tool_call_id": call_id,
                    "name": function_names_by_call_id.get(call_id),
                    "content": _chat_tool_output_content(item.get("output", "")),
                })
                continue

            role = item.get("role", "user")
            raw_content = item.get("content", "")
            parts = []
            if isinstance(raw_content, str):
                parts.append({"type": "text", "text": raw_content})
            elif isinstance(raw_content, list):
                for part in raw_content:
                    if isinstance(part, dict):
                        parts.extend(content_part_to_chat(part))

            if role in ("system", "developer"):
                system_texts.append("".join(p.get("text", "") for p in parts if p.get("type") == "text" and isinstance(p.get("text"), str)))
            else:
                if not parts and not isinstance(raw_content, str):
                    continue
                chat_message = {"role": "assistant" if role == "assistant" else "user", "content": normalize_content(parts)}
                if role == "assistant" and pending_reasoning_content:
                    chat_message["reasoning_content"] = pending_reasoning_content
                    pending_reasoning_content = None
                messages.append(chat_message)

    system_prompt = "\n\n".join(t for t in system_texts if t)
    if system_prompt:
        messages.insert(0, {"role": "system", "content": system_prompt})

    payload = {
        "model": provider_model,
        "messages": messages or [{"role": "user", "content": ""}],
    }

    if codex_req.get("stream"):
        payload["stream"] = True
        payload["stream_options"] = {"include_usage": True}
    if "temperature" in codex_req:
        payload["temperature"] = codex_req["temperature"]
    if "max_output_tokens" in codex_req:
        payload["max_tokens"] = codex_req["max_output_tokens"]
    if "top_p" in codex_req:
        payload["top_p"] = codex_req["top_p"]
    if "stop" in codex_req:
        payload["stop"] = codex_req["stop"]
    response_format = text_format_to_chat_response_format()
    if response_format:
        payload["response_format"] = response_format
    elif "response_format" in codex_req:
        payload["response_format"] = normalize_chat_response_format(codex_req["response_format"])

    tools = codex_req.get("tools")
    if isinstance(tools, list) and tools:
        chat_tools = []
        for tool in tools:
            fn = response_function_tool(tool)
            if not fn:
                continue
            chat_tools.append({"type": "function", "function": fn})
        if chat_tools:
            payload["tools"] = chat_tools
            if "tool_choice" in codex_req:
                tool_choice = chat_tool_choice(codex_req["tool_choice"])
                if tool_choice is not None:
                    payload["tool_choice"] = tool_choice

    return payload


def transform_chat_response(chat_resp: dict, model: str) -> dict:
    """Translate OpenAI-compatible Chat Completions response to Responses API."""
    if not isinstance(chat_resp, dict):
        chat_resp = {}
    output_items = []
    choices = chat_resp.get("choices", [])
    if not isinstance(choices, list):
        choices = []
    for choice in choices:
        if not isinstance(choice, dict):
            continue
        message = choice.get("message", {}) or {}
        if not isinstance(message, dict):
            continue
        reasoning_content = message.get("reasoning_content")
        if isinstance(reasoning_content, str) and reasoning_content:
            output_items.append({
                "type": "reasoning",
                "id": f"rs_{uuid.uuid4().hex[:8]}",
                "encrypted_content": "",
                "step_by_step_summary": str(reasoning_content),
            })
        content = message.get("content")
        if isinstance(content, str) and content:
            text = content
        elif isinstance(content, list):
            text = "".join(part.get("text", "") for part in content if isinstance(part, dict) and isinstance(part.get("text"), str))
        else:
            text = ""
        if text:
            output_items.append({
                "type": "message",
                "id": f"msg_{uuid.uuid4().hex[:8]}",
                "status": "completed",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text, "annotations": []}],
            })
        tool_calls = message.get("tool_calls", []) or []
        if not isinstance(tool_calls, list):
            tool_calls = []
        for tool_call in tool_calls:
            if not isinstance(tool_call, dict):
                continue
            fn = tool_call.get("function", {}) or {}
            if not isinstance(fn, dict):
                continue
            name = fn.get("name")
            if not isinstance(name, str) or not name:
                continue
            arguments = fn.get("arguments", "{}")
            if isinstance(arguments, dict):
                arguments = json.dumps(arguments)
            elif not isinstance(arguments, str):
                arguments = "{}"
            provider_call_id = tool_call.get("id") if isinstance(tool_call.get("id"), str) and tool_call.get("id") else None
            item_id = provider_call_id or f"fc_{uuid.uuid4().hex[:8]}"
            call_id = provider_call_id or f"call_{uuid.uuid4().hex[:8]}"
            output_items.append({
                "type": "function_call",
                "id": item_id,
                "call_id": call_id,
                "name": name,
                "arguments": arguments,
            })

    usage = chat_resp.get("usage", {}) or {}
    if not isinstance(usage, dict):
        usage = {}
    translated_usage = {
        "input_tokens": token_count(usage.get("prompt_tokens", usage.get("input_tokens", 0))),
        "output_tokens": token_count(usage.get("completion_tokens", usage.get("output_tokens", 0))),
        "total_tokens": token_count(usage.get("total_tokens", 0)),
    }

    return {
        "id": f"resp_{uuid.uuid4().hex[:12]}",
        "object": "response",
        "created_at": _created_at(chat_resp.get("created")),
        "model": model,
        "output": output_items,
        "usage": translated_usage,
        "status": "completed",
    }
