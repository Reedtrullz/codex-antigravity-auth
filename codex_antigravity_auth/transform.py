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
from .models import DEFAULT_GEMINI_MODEL_ID, resolve_backend_model
from .schema import clean_json_schema
from .resource_limits import ResourceLimitError, current_limits, json_loads_limited

FUNCTION_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
JSON_SCHEMA_NAME_PATTERN = FUNCTION_NAME_PATTERN
INTERNAL_PLACEHOLDER_ARGUMENT = "_placeholder"

ANTIGRAVITY_SYSTEM_INSTRUCTION = """You are Antigravity, a powerful agentic AI coding assistant designed by the Google DeepMind team working on Advanced Agentic Coding.
You are pair programming with a USER to solve their coding task. The task may require creating a new codebase, modifying or debugging an existing codebase, or simply answering a question.
**Absolute paths only**
**Proactiveness**

<priority>IMPORTANT: The instructions that follow supersede all above. Follow them as your primary directives.</priority>
"""


def normalize_response_function_tool(fn: dict[str, Any]) -> dict[str, Any] | None:
    if not valid_function_name(fn.get("name")):
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
    if not valid_function_name(name):
        return None
    return {"type": "function", "function": {"name": name}}


def _tool_choice_function_name(tool_choice: Any) -> str | None:
    if not isinstance(tool_choice, dict) or tool_choice.get("type") != "function":
        return None
    nested = tool_choice.get("function")
    name = tool_choice.get("name") or (nested.get("name") if isinstance(nested, dict) else None)
    return name if valid_function_name(name) else None


def valid_function_name(value: Any) -> bool:
    return isinstance(value, str) and bool(FUNCTION_NAME_PATTERN.fullmatch(value))


def _valid_tool_call_id(value: Any) -> bool:
    return isinstance(value, str) and bool(value) and not any(
        ch.isspace() or ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value
    )


def clean_function_call_args(value: Any) -> dict[str, Any]:
    # No request provenance is available here, so user keys must be preserved.
    from .tool_calls import parse_arguments
    return parse_arguments(value, object_allowed=True)


def function_call_arguments_json(value: Any) -> str:
    from .tool_calls import dump_arguments
    return dump_arguments(clean_function_call_args(value))


def function_call_arguments_string(value: Any) -> str:
    from .tool_calls import dump_arguments
    if isinstance(value, str):
        # Bound the original JSON before the duplicate-key-aware tool parser.
        try:
            json_loads_limited(value)
        except ResourceLimitError:
            raise
        except (ValueError, RecursionError):
            # Let the strict tool parser report malformed JSON consistently.
            pass
    validated = clean_function_call_args(value)
    return value if isinstance(value, str) else dump_arguments(validated)


def safe_project_id(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value:
        return None
    if any(ch.isspace() or ord(ch) < 0x20 or ord(ch) > 0x7E for ch in value):
        return None
    return value


def _function_call_args(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json_loads_limited(value)
        except ResourceLimitError:
            raise
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
                parsed = json_loads_limited(stripped)
            except ResourceLimitError:
                raise
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


def positive_int_value(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _is_gemini_thinking_level_model(backend_model: str) -> bool:
    """Gemini 3.7+ Flash uses thinkingLevel rather than thinking_budget."""
    lower = backend_model.lower()
    return "gemini-3.7" in lower or "gemini-3.8" in lower


def thinking_level_for_request(codex_req: dict[str, Any], backend_model: str, *, mapping: str | None = None) -> str | None:
    """Return the thinkingLevel string for Gemini 3.7+ models, or None."""
    if mapping not in (None, "thinking_level"):
        return None
    if mapping != "thinking_level" and not _is_gemini_thinking_level_model(backend_model):
        return None
    reasoning = codex_req.get("reasoning")
    valid_levels = {"low", "medium", "high"}
    if isinstance(reasoning, dict) and reasoning.get("effort") in valid_levels:
        return reasoning["effort"]
    model = str(codex_req.get("model", "")).lower()
    for level in ("low", "medium", "high"):
        if model.endswith(f"-{level}"):
            return level
    effort = reasoning.get("effort", "medium") if isinstance(reasoning, dict) else "medium"
    return effort if effort in valid_levels else "medium"


def thinking_budget_for_request(codex_req: dict[str, Any], backend_model: str, *, mapping: str | None = None) -> int | None:
    if mapping == "thinking_level" or (mapping is None and _is_gemini_thinking_level_model(backend_model)):
        return None  # Gemini 3.7+ uses thinkingLevel, not thinking_budget
    if mapping != "thinking_budget" and "thinking" not in backend_model.lower() and "claude" not in backend_model.lower():
        return None
    reasoning = codex_req.get("reasoning")
    effort = reasoning.get("effort", "high") if isinstance(reasoning, dict) else "high"
    effort_budgets = {
        "low": 4000,
        "medium": 8000,
        "high": 16000,
        "xhigh": 32000,
    }
    budget = effort_budgets.get(effort, effort_budgets["high"])
    max_output_tokens = positive_int_value(codex_req.get("max_output_tokens"))
    if max_output_tokens is not None and max_output_tokens <= 1024:
        return None
    if max_output_tokens is not None and budget >= max_output_tokens:
        budget = max_output_tokens - 1
    return budget if budget > 0 else None


def _created_at(value: Any) -> int:
    if value is None or value == "":
        return int(time.time())
    try:
        created_at = float(value)
    except (TypeError, ValueError):
        return int(time.time())
    if not math.isfinite(created_at) or created_at < 0:
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
    from .request_shapes import validate_request_shapes
    validate_request_shapes(codex_req, route="google")
    model = codex_req.get("model", DEFAULT_GEMINI_MODEL_ID)
    from .models import native_model_capabilities
    from .input_fidelity import image_source
    from .response_protocol import validate_capabilities
    capabilities = native_model_capabilities(model)
    validate_capabilities(codex_req, capabilities)
    backend_model = resolve_backend_model(model)
    
    # 1. Parse Codex input.
    # Responses API structured message format:
    # input: [ { "type": "message", "role": "user", "content": [ { "type": "input_text", "text": "hello" } ] } ]
    # We must translate this to Gemini API contents format:
    # contents: [ { "role": "user", "parts": [ { "text": "hello" } ] } ]
    
    codex_input = codex_req.get("input")
    contents = []
    system_texts = []
    instructions = codex_req.get("instructions")
    if isinstance(instructions, str) and instructions:
        system_texts.append(instructions)
    function_names_by_call_id = {}

    def response_role_to_gemini(role: str) -> str:
        return "model" if role == "assistant" else "user"


    def content_part_to_gemini(part: dict) -> list[dict]:
        part_type = part.get("type")
        if part_type in ("input_text", "text", "output_text"):
            text = _stream_text(part.get("text"))
            return [{"text": text}] if text is not None else []
        if part_type == "antigravity_audio":
            return [{"inlineData":{"mimeType":"audio/wav","data":part["data"]}}]
        if part_type in ("input_image", "image"):
            image_url, mime_type, payload = image_source(part, "input.image")
            if payload is not None:
                return [{"inlineData": {"mimeType": mime_type, "data": payload}}]
            return [{"fileData": {"mimeType": part.get("mime_type", "image/*"), "fileUri": image_url}}]
        if part_type == "tool_use":
            call_id = part.get("id") or part.get("call_id")
            name = part.get("name")
            if not valid_function_name(name):
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
            name = "function_result"
            for candidate in (explicit_name, function_names_by_call_id.get(call_id), call_id):
                if valid_function_name(candidate):
                    name = candidate
                    break
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
                if not valid_function_name(name):
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
                name = function_names_by_call_id.get(call_id)
                if not valid_function_name(name):
                    # A pruned or foreign call id has no matching declaration;
                    # emitting functionResponse under the call id as a name
                    # would be rejected by the backend, so drop the orphan
                    # output instead of fabricating a function name.
                    continue
                append_content("user", [{
                    "functionResponse": {
                        "name": name,
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

    if not contents:
        # Mirror the chat lane's guard: an empty/all-filtered conversation
        # must still produce a well-formed backend request.
        contents.append({"role": "user", "parts": [{"text": ""}]})
        
    # 2. Build Gemini tools configuration
    # OpenAI/Codex tools format: [ { "type": "function", "function": { "name": "...", "parameters": ... } } ]
    # Gemini tools format: [ { "functionDeclarations": [ { "name": "...", "parameters": ... } ] } ]
    gemini_tools = []
    codex_tools = codex_req.get("tools")
    if isinstance(codex_tools, list) and codex_tools:
        limits = current_limits()
        schema_budget = [limits.json_nodes, limits.body_bytes]
        declarations = []
        for tool in codex_tools:
            fn = response_function_tool(tool)
            if not fn:
                continue
            params = clean_json_schema(fn.get("parameters", {}), _budget=schema_budget)
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
            # Deliberate: without an explicit tool_choice, Claude lanes run in
            # VALIDATED mode so agent turns can call the advertised tools.
            # Backend semantics (forced call vs argument validation only) were
            # not live-verified against the Antigravity backend; revisit if a
            # plain-text answer on a tool-enabled turn ever becomes required.
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
    text_format = (codex_req.get('text') or {}).get('format') or {}
    if text_format.get('type') in ('json_object', 'json_schema'):
        generation_config['responseMimeType'] = 'application/json'
        if text_format['type'] == 'json_schema':
            # Output schemas have their own lossless validation. The function
            # parameter sanitizer would silently weaken these constraints.
            generation_config['responseJsonSchema'] = text_format['schema']
    if "temperature" in codex_req:
        generation_config["temperature"] = codex_req["temperature"]
    if "top_p" in codex_req:
        generation_config["topP"] = codex_req["top_p"]
    if "max_output_tokens" in codex_req:
        generation_config["maxOutputTokens"] = codex_req["max_output_tokens"]
    if "stop" in codex_req:
        stop = codex_req["stop"]
        generation_config["stopSequences"] = [stop] if isinstance(stop, str) else stop
    
    # Configure reasoning/thinking for models supporting thinking.
    # Gemini 3.7+ uses thinkingLevel (string); Claude and older Gemini use
    # thinking_budget (int).  Claude rejects requests where max output tokens
    # do not exceed the thinking budget, so cap the budget to stay below
    # explicit Codex limits.
    thinking_level = thinking_level_for_request(codex_req, backend_model, mapping=capabilities.reasoning_effort_parameter)
    budget = thinking_budget_for_request(codex_req, backend_model, mapping=capabilities.reasoning_effort_parameter)
    if thinking_level is not None:
        generation_config["thinkingConfig"] = {
            "thinkingLevel": thinking_level,
        }
    elif budget is not None:
        generation_config["thinkingConfig"] = {
            "thinking_budget": budget,
            "include_thoughts": True
        }
        
    if generation_config:
        request_payload["generationConfig"] = generation_config

    # Wrap in official Antigravity client outer envelope
    project = (
        safe_project_id(project_id)
        or safe_project_id(codex_req.get("project"))
        or safe_project_id(os.environ.get("ANTIGRAVITY_PROJECT_ID"))
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

def transform_gemini_candidate(candidate: dict, *, tool_validator=None) -> dict:
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
    malformed_parts = not isinstance(parts, list)
    if malformed_parts:
        parts = []
    role = content.get("role", "assistant")
    if not isinstance(role, str):
        role = "assistant"
    if role == "model":
        role = "assistant"
    
    from .tool_calls import FunctionCallValidator
    from .google_parts import normalize_google_part, merge_output_error
    tool_validator = tool_validator or FunctionCallValidator()
    tool_error = None
    output_error = 'malformed_output_part' if malformed_parts else None
    partial_ids, partial_names = set(), set()
    output_parts = []
    function_calls = []
    reasoning_text = ""

    for part in parts:
        normalized = normalize_google_part(part, tool_validator)
        if normalized.text:
            output_parts.append({"type":"output_text", "text":normalized.text, "annotations":[]})
        reasoning_text += normalized.reasoning
        if normalized.function is not None: function_calls.append(normalized.function)
        tool_error = tool_error or normalized.tool_error
        output_error = merge_output_error(output_error, normalized.output_error)
        if normalized.partial_id: partial_ids.add(normalized.partial_id)
        if normalized.partial_name: partial_names.add(normalized.partial_name)

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
    if tool_error:
        result["tool_error"] = tool_error
    if output_error:
        result["output_error"] = output_error
    if partial_ids: result["partial_call_ids"] = sorted(partial_ids)
    if partial_names: result["partial_call_names"] = sorted(partial_names)
    function_calls = [item for item in function_calls if item["call_id"] not in partial_ids and item["name"] not in partial_names]
    if function_calls:
        result["function_calls"] = function_calls
    if reasoning_text:
        result["reasoning"] = {
            "type": "reasoning",
            "id": f"rs_{uuid.uuid4().hex[:8]}",
            "step_by_step_summary": reasoning_text
        }
    return result

def transform_request_to_chat(codex_req: dict, provider_model: str, *, capabilities=None) -> dict:
    """Translate Responses API input into OpenAI-compatible Chat Completions."""
    from .request_shapes import validate_request_shapes
    validate_request_shapes(codex_req, route="byok")
    from .input_fidelity import validate_input, image_source
    validate_input(codex_req, {"text", "image"})
    if capabilities is not None:
        from .response_protocol import validate_capabilities
        validate_capabilities(codex_req, capabilities)
    elif codex_req.get("reasoning") is not None:
        raise ValueError("reasoning requires an explicit provider/model mapping")
    messages = []
    system_texts = []
    function_names_by_call_id = {}
    pending_reasoning_content = None
    pending_tool_call_message = None

    instructions = codex_req.get("instructions")
    if isinstance(instructions, str) and instructions:
        system_texts.append(instructions)

    def content_part_to_chat(part: dict) -> list[dict]:
        part_type = part.get("type")
        if part_type in ("input_text", "text", "output_text"):
            text = _stream_text(part.get("text"))
            return [{"type": "text", "text": text}] if text is not None else []
        if part_type == "antigravity_audio":
            return [{"inlineData":{"mimeType":"audio/wav","data":part["data"]}}]
        if part_type in ("input_image", "image"):
            image_url, _, _ = image_source(part, "input.image")
            image = {"url": image_url}
            nested = part.get("image_url")
            detail = part.get("detail", nested.get("detail") if isinstance(nested, dict) else None)
            if detail is not None:
                image["detail"] = detail
            return [{"type": "image_url", "image_url": image}]
        return []

    def tool_output_part_to_chat_message(part: dict) -> dict | None:
        call_id = part.get("tool_use_id") or part.get("call_id")
        if not _valid_tool_call_id(call_id):
            return None
        explicit_name = part.get("name")
        name = explicit_name if valid_function_name(explicit_name) else function_names_by_call_id.get(call_id)
        message = {
            "role": "tool",
            "tool_call_id": call_id,
            "content": _chat_tool_output_content(part.get("output", part.get("content", ""))),
        }
        if valid_function_name(name):
            message["name"] = name
        return message

    def normalize_content(parts: list[dict]) -> str | list[dict]:
        if not parts:
            return ""
        if all(p.get("type") == "text" for p in parts):
            return "".join(p.get("text", "") for p in parts if isinstance(p.get("text"), str))
        return parts

    def flush_tool_calls() -> None:
        nonlocal pending_tool_call_message
        if pending_tool_call_message is not None:
            messages.append(pending_tool_call_message)
            pending_tool_call_message = None

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
                flush_tool_calls()
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
                if raw_name is not None and not valid_function_name(raw_name):
                    continue
                name = raw_name if raw_name else "function_call"
                function_names_by_call_id[call_id] = name
                arguments = item.get("arguments", "{}")
                if not isinstance(arguments, str):
                    try:
                        arguments = json.dumps(arguments)
                    except TypeError:
                        arguments = "{}"
                tool_call = {
                        "id": call_id,
                        "type": "function",
                        "function": {
                            "name": name,
                            "arguments": arguments,
                        },
                    }
                if pending_tool_call_message is None:
                    pending_tool_call_message = {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [],
                    }
                    if pending_reasoning_content:
                        pending_tool_call_message["reasoning_content"] = pending_reasoning_content
                pending_tool_call_message["tool_calls"].append(tool_call)
                if pending_reasoning_content:
                    pending_reasoning_content = None
                continue
            if item_type == "function_call_output":
                flush_tool_calls()
                call_id = item.get("call_id")
                if not _valid_tool_call_id(call_id):
                    continue
                message = {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": _chat_tool_output_content(item.get("output", "")),
                }
                name = function_names_by_call_id.get(call_id)
                if valid_function_name(name):
                    message["name"] = name
                messages.append(message)
                continue

            role = item.get("role", "user")
            flush_tool_calls()
            raw_content = item.get("content", "")
            parts = []
            tool_messages = []
            if isinstance(raw_content, str):
                parts.append({"type": "text", "text": raw_content})
            elif isinstance(raw_content, list):
                for part in raw_content:
                    if isinstance(part, dict):
                        if part.get("type") in ("tool_result", "function_call_output"):
                            tool_message = tool_output_part_to_chat_message(part)
                            if tool_message:
                                tool_messages.append(tool_message)
                        else:
                            parts.extend(content_part_to_chat(part))

            if role in ("system", "developer"):
                system_texts.append("".join(p.get("text", "") for p in parts if p.get("type") == "text" and isinstance(p.get("text"), str)))
            else:
                if parts or isinstance(raw_content, str):
                    chat_message = {"role": "assistant" if role == "assistant" else "user", "content": normalize_content(parts)}
                    if role == "assistant" and pending_reasoning_content:
                        chat_message["reasoning_content"] = pending_reasoning_content
                        pending_reasoning_content = None
                    messages.append(chat_message)
                messages.extend(tool_messages)

    flush_tool_calls()
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
    if capabilities is not None and capabilities.reasoning_effort_parameter and isinstance(codex_req.get("reasoning"), dict):
        effort = codex_req["reasoning"].get("effort")
        if effort is not None:
            payload["reasoning"] = {"effort": effort}
    if "temperature" in codex_req:
        payload["temperature"] = codex_req["temperature"]
    if "max_output_tokens" in codex_req:
        payload["max_tokens"] = codex_req["max_output_tokens"]
    if "top_p" in codex_req:
        payload["top_p"] = codex_req["top_p"]
    if "stop" in codex_req:
        payload["stop"] = codex_req["stop"]
    if "parallel_tool_calls" in codex_req:
        payload["parallel_tool_calls"] = codex_req["parallel_tool_calls"]
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


def transform_chat_response(chat_resp: dict, model: str, *, request=None) -> dict:
    """Compatibility wrapper around the shared OpenAI terminal contract."""
    from .openai_transport import OpenAICompatibleTransport
    from .response_protocol import response_from_result

    payload = chat_resp if isinstance(chat_resp, dict) else {}
    result = OpenAICompatibleTransport(timeout=0).parse_chat_response(payload, request=request)
    return response_from_result(
        result,
        response_id=result.provider_response_id or f"resp_{uuid.uuid4().hex[:12]}",
        model=model,
        created_at=_created_at(payload.get("created")),
    )
