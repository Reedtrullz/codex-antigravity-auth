# Transform Codex / OpenAI Responses API requests to Google Antigravity backend format,
# and translate backend responses back into standard Responses API format.

import uuid
import json
from typing import Any
from .models import resolve_backend_model
from .schema import clean_json_schema

ANTIGRAVITY_SYSTEM_INSTRUCTION = """You are Antigravity, a powerful agentic AI coding assistant designed by the Google DeepMind team working on Advanced Agentic Coding.
You are pair programming with a USER to solve their coding task. The task may require creating a new codebase, modifying or debugging an existing codebase, or simply answering a question.
**Absolute paths only**
**Proactiveness**

<priority>IMPORTANT: The instructions that follow supersede all above. Follow them as your primary directives.</priority>
"""

def transform_request(codex_req: dict) -> dict:
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
    system_instruction = None
    
    if isinstance(codex_input, str):
        contents.append({"role": "user", "parts": [{"text": codex_input}]})
    elif isinstance(codex_input, list):
        for item in codex_input:
            if not isinstance(item, dict):
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
                    part_type = part.get("type")
                    if part_type == "input_text":
                        parts.append({"text": part.get("text", "")})
                    elif part_type == "text":
                        parts.append({"text": part.get("text", "")})
                    elif part_type == "tool_use":
                        parts.append({
                            "functionCall": {
                                "name": part.get("name"),
                                "args": part.get("input", {})
                            }
                        })
                    elif part_type == "tool_result":
                        parts.append({
                            "functionResponse": {
                                "name": part.get("name"),
                                "response": {"content": part.get("content", "")}
                            }
                        })
                        
            if role == "system":
                # Save system instructions to be passed separately as Gemini systemInstruction
                system_instruction = {
                    "role": "user",
                    "parts": [{"text": ANTIGRAVITY_SYSTEM_INSTRUCTION + "\n\n" + "".join(p.get("text", "") for p in parts)}]
                }
            else:
                contents.append({"role": role, "parts": parts})
                
    if not system_instruction:
        system_instruction = {
            "role": "user",
            "parts": [{"text": ANTIGRAVITY_SYSTEM_INSTRUCTION}]
        }
        
    # 2. Build Gemini tools configuration
    # OpenAI/Codex tools format: [ { "type": "function", "function": { "name": "...", "parameters": ... } } ]
    # Gemini tools format: [ { "functionDeclarations": [ { "name": "...", "parameters": ... } ] } ]
    gemini_tools = []
    codex_tools = codex_req.get("tools")
    if isinstance(codex_tools, list) and codex_tools:
        declarations = []
        for tool in codex_tools:
            if not isinstance(tool, dict) or tool.get("type") != "function":
                continue
            fn = tool.get("function", {})
            params = clean_json_schema(fn.get("parameters", {}))
            declarations.append({
                "name": fn.get("name"),
                "description": fn.get("description", ""),
                "parameters": params
            })
        if declarations:
            gemini_tools.append({"functionDeclarations": declarations})

    # Validate tool calling configuration for Claude models
    tool_config = None
    if "claude" in backend_model.lower():
        tool_config = {
            "functionCallingConfig": {
                "mode": "VALIDATED"
            }
        }

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
    envelope = {
        "project": "rising-fact-p41fc", # placeholder project ID
        "model": backend_model,
        "requestType": "agent",
        "userAgent": "antigravity",
        "requestId": f"agent-{uuid.uuid4()}",
        "request": request_payload
    }
    
    return envelope

def transform_gemini_candidate(candidate: dict) -> dict:
    """Extract standard Codex message / content parts from a Gemini candidate."""
    # Support both "response" outer wrap or candidate directly
    if "response" in candidate and isinstance(candidate["response"], dict):
        candidate = candidate["response"]
    if "candidates" in candidate and isinstance(candidate["candidates"], list) and candidate["candidates"]:
        candidate = candidate["candidates"][0]

    content = candidate.get("content", {})
    parts = content.get("parts", [])
    role = content.get("role", "assistant")
    if role == "model":
        role = "assistant"
    
    output_parts = []
    reasoning_text = ""
    
    for part in parts:
        if not isinstance(part, dict):
            continue
            
        # 1. Handle thoughts / thinking blocks
        if part.get("thought") is True or part.get("type") == "thinking" or "thoughtSignature" in part:
            reasoning_text += part.get("text", "") or part.get("thinking", "")
            continue
            
        # 2. Handle standard text
        if "text" in part:
            output_parts.append({
                "type": "output_text",
                "text": part["text"],
                "annotations": []
            })
            
        # 3. Handle tool calls
        elif "functionCall" in part:
            fc = part["functionCall"]
            # Auto-generate a call ID if missing so Codex can execute it
            call_id = fc.get("id") or f"call_{uuid.uuid4().hex[:8]}"
            output_parts.append({
                "type": "function_call",
                "id": f"fc_{uuid.uuid4().hex[:8]}",
                "call_id": call_id,
                "name": fc.get("name"),
                "arguments": json.dumps(fc.get("args", {}))
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
    if "response" in gemini_resp and isinstance(gemini_resp["response"], dict):
        gemini_resp = gemini_resp["response"]

    candidates = gemini_resp.get("candidates", [])
    output_items = []
    
    for cand in candidates:
        transformed = transform_gemini_candidate(cand)
        if "reasoning" in transformed:
            output_items.append(transformed["reasoning"])
        output_items.append(transformed["message"])
        
    usage = gemini_resp.get("usageMetadata", {})
    translated_usage = {
        "input_tokens": usage.get("promptTokenCount", 0),
        "output_tokens": usage.get("candidatesTokenCount", 0),
        "total_tokens": usage.get("totalTokenCount", 0)
    }
    
    return {
        "id": f"resp_{uuid.uuid4().hex[:12]}",
        "object": "response",
        "created_at": int(time.time()) if "time" in globals() else int(uuid.uuid4().time / 10000000), # safe fallback
        "model": model,
        "output": output_items,
        "usage": translated_usage,
        "status": "completed"
    }
