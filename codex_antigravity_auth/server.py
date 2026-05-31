import json
import httpx
from typing import AsyncGenerator
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import StreamingResponse
from .accounts import AccountManager
from .byok import all_provider_configs, resolve_api_key, split_provider_model
from .transform import transform_chat_response, transform_request, transform_request_to_chat, transform_response
from .constants import ANTIGRAVITY_ENDPOINT_PROD, get_platform

app = FastAPI(title="Codex Antigravity Gateway")
account_manager = AccountManager()

# ── Model catalog for native Codex Desktop picker ──
AVAILABLE_MODELS = [
    {"id": "gemini-3.5-flash-high", "display_name": "Gemini 3.5 Flash (Agent High)", "context_window": 1000000},
    {"id": "gemini-3.1-pro-high",    "display_name": "Gemini 3.1 Pro (Reasoning)", "context_window": 1000000},
    {"id": "claude-3.5-sonnet",      "display_name": "Claude Sonnet 4.6 (Google)",  "context_window": 200000},
    {"id": "claude-opus-4-6",        "display_name": "Claude Opus 4.6 (Google)",   "context_window": 200000},
]

@app.get("/v1/models")
async def list_models():
    """Return model catalog so Codex Desktop can populate its picker dropdown."""
    import time
    byok_models = []
    for provider_id, provider in all_provider_configs().items():
        for model_entry in provider.get("models", []):
            if isinstance(model_entry, dict):
                provider_model = model_entry.get("id")
                display_name = model_entry.get("display_name") or model_entry.get("displayName") or provider_model
                context_window = model_entry.get("context_window") or model_entry.get("contextWindow") or 128000
            else:
                provider_model = str(model_entry)
                display_name = provider_model
                context_window = 128000
            if not provider_model:
                continue
            byok_models.append({
                "id": f"{provider_id}:{provider_model}",
                "object": "model",
                "created": int(time.time()),
                "owned_by": provider_id,
                "display_name": f"{provider.get('displayName', provider_id)}: {display_name}",
                "context_window": context_window,
            })
    return {
        "object": "list",
        "data": [
            {
                "id": m["id"],
                "object": "model",
                "created": int(time.time()),
                "owned_by": "google-antigravity",
                "display_name": m["display_name"],
                "context_window": m["context_window"],
            }
            for m in AVAILABLE_MODELS
        ] + byok_models,
    }

def build_headers(account: dict) -> dict:
    platform = get_platform()
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/537.36 (KHTML, like Gecko) Antigravity/2.0.0 Chrome/138.0.7204.235 Electron/37.3.1 Safari/537.36",
        "X-Goog-Api-Client": "google-cloud-sdk vscode_cloudshelleditor/0.1",
        "Client-Metadata": f'{{"ideType":"ANTIGRAVITY","platform":"{platform}","pluginType":"GEMINI"}}',
        "Content-Type": "application/json",
        "Authorization": f"Bearer {account['accessToken']}",
    }
    
    # Inject fingerprint if available
    fp = account.get("fingerprint")
    if fp:
        headers["User-Agent"] = fp.get("userAgent", headers["User-Agent"])
        headers["X-Goog-Api-Client"] = fp.get("apiClient", headers["X-Goog-Api-Client"])
        if fp.get("clientMetadata"):
            metadata = dict(fp["clientMetadata"])
            if fp.get("deviceId"):
                metadata["deviceId"] = fp["deviceId"]
            if fp.get("sessionToken"):
                metadata["sessionToken"] = fp["sessionToken"]
            headers["Client-Metadata"] = json.dumps(metadata)
            
    return headers


def build_openai_compatible_headers(provider: dict) -> dict:
    api_key = resolve_api_key(provider)
    if not api_key:
        raise HTTPException(
            status_code=401,
            detail=f"No API key configured for provider '{provider['id']}'. Set {provider.get('apiKeyEnv', 'provider API key')} or run provider set.",
        )
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    headers.update(provider.get("headers", {}) or {})
    return headers


def chat_completions_url(provider: dict) -> str:
    base_url = provider.get("baseUrl", "").rstrip("/")
    if not base_url:
        raise HTTPException(status_code=500, detail=f"Provider '{provider['id']}' has no baseUrl configured")
    if base_url.endswith("/chat/completions"):
        return base_url
    return f"{base_url}/chat/completions"

@app.post("/v1/responses")
async def create_response(request: Request):
    try:
        codex_req = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")
        
    model = codex_req.get("model", "gemini-3.5-flash-high")
    stream = codex_req.get("stream", False)
    provider_id, provider_model = split_provider_model(model)
    if provider_id:
        providers = all_provider_configs()
        provider = providers.get(provider_id)
        if not provider:
            raise HTTPException(status_code=404, detail=f"BYOK provider '{provider_id}' is not configured")
        if provider.get("kind") != "openai_chat":
            raise HTTPException(status_code=500, detail=f"Unsupported BYOK provider kind: {provider.get('kind')}")
        if stream:
            return StreamingResponse(
                openai_compatible_sse_generator(codex_req, provider, provider_model, model),
                media_type="text/event-stream",
            )
        return await create_openai_compatible_response(codex_req, provider, provider_model, model)
    
    # 1. Select account automatically from pool
    account = account_manager.select_active_account(model)
    if not account:
        raise HTTPException(
            status_code=500,
            detail="No Google accounts available. Run `codex-antigravity login` to connect an account."
        )
        
    # 2. Translate standard Responses API request to Antigravity request envelope
    antigravity_req = transform_request(codex_req)
    backend_model = antigravity_req.get("model")
    
    # Route target action based on streaming mode
    action = "streamGenerateContent" if stream else "generateContent"
    backend_url = f"{ANTIGRAVITY_ENDPOINT_PROD}/v1internal:{action}"
    if stream:
        backend_url += "?alt=sse"
        
    headers = build_headers(account)
    
    # Perform HTTP POST request to Antigravity endpoint with error recovery & rotation
    async def request_backend() -> httpx.Response | None:
        async with httpx.AsyncClient(timeout=60.0) as client:
            try:
                # Do NOT stream the response inside this function, we just do normal or stream connection
                if stream:
                    # Let the StreamingResponse generator handle actual streaming
                    return None
                else:
                    res = await client.post(backend_url, json=antigravity_req, headers=headers)
                    return res
            except Exception as e:
                account_manager.mark_failure(account["email"], f"Connection error: {e}")
                return None

    # Handle standard non-streaming response path
    if not stream:
        res = await request_backend()
        if not res:
            # Retry with rotated account on connection failures
            new_account = account_manager.select_active_account(model)
            if new_account:
                account = new_account
                headers = build_headers(account)
                res = await request_backend()
                
        if not res:
            raise HTTPException(status_code=502, detail="Failed to communicate with Antigravity backend after rotation")

        if res.status_code in (401, 403, 429):
            reason = "Rate limited / Quota exceeded" if res.status_code == 429 else f"Auth failure {res.status_code}: {res.text}"
            account_manager.mark_failure(account["email"], reason)
            new_account = account_manager.select_active_account(model)
            if new_account:
                account = new_account
                headers = build_headers(account)
                res = await request_backend()
            if not res:
                raise HTTPException(status_code=502, detail="Failed to communicate with Antigravity backend after rotation")
            
        if res.status_code in (401, 403):
            # Token might be invalidated or verification required
            account_manager.mark_failure(account["email"], f"Auth failure {res.status_code}: {res.text}")
            raise HTTPException(status_code=res.status_code, detail=f"Google Authentication failure: {res.text}")
            
        if res.status_code == 429:
            account_manager.mark_failure(account["email"], "Rate limited / Quota exceeded")
            raise HTTPException(status_code=429, detail="Antigravity account rate limit reached. Auto-switching to next account.")
            
        if res.status_code != 200:
            raise HTTPException(status_code=res.status_code, detail=f"Google Antigravity API error: {res.text}")
            
        try:
            gemini_resp = res.json()
            # If the response is wrapped as a list (stream chunk structure)
            if isinstance(gemini_resp, list) and gemini_resp:
                gemini_resp = gemini_resp[0]
            codex_resp = transform_response(gemini_resp, model)
            return codex_resp
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Response translation failed: {e}")

    # Handle standard SSE streaming response path
    async def sse_generator() -> AsyncGenerator[str, None]:
        import uuid
        response_id = f"resp_{uuid.uuid4().hex[:12]}"
        output_text = ""
        usage = None
        next_output_index = 1
        
        # 1. response.created
        yield f"data: {json.dumps({'type': 'response.created', 'response': {'id': response_id, 'object': 'response', 'status': 'in_progress'}})}\n\n"
        
        # 2. response.output_item.added (message)
        msg_id = f"msg_{uuid.uuid4().hex[:8]}"
        yield f"data: {json.dumps({'type': 'response.output_item.added', 'response_id': response_id, 'output_index': 0, 'item': {'type': 'message', 'id': msg_id, 'role': 'assistant', 'status': 'in_progress', 'content': []}})}\n\n"
        
        # 3. response.content_part.added
        yield f"data: {json.dumps({'type': 'response.content_part.added', 'response_id': response_id, 'output_index': 0, 'content_index': 0, 'part': {'type': 'output_text', 'text': ''}})}\n\n"

        async def fail_stream(code: str, message: str) -> AsyncGenerator[str, None]:
            yield f"data: {json.dumps({'type': 'error', 'error': {'code': code, 'message': message}})}\n\n"
            yield f"data: {json.dumps({'type': 'response.failed', 'response': {'id': response_id, 'object': 'response', 'status': 'failed', 'error': {'code': code, 'message': message}}})}\n\n"
            yield "data: [DONE]\n\n"

        async def parse_stream_line(line: str) -> AsyncGenerator[str, None]:
            nonlocal output_text, usage, next_output_index
            line = line.strip()
            if not line or not line.startswith("data:"):
                return
            data_payload = line[5:].strip()
            if data_payload == "[DONE]":
                return
            try:
                parsed = json.loads(data_payload)
            except json.JSONDecodeError as e:
                print(f"[*] Skipping invalid Antigravity SSE JSON chunk: {e}")
                return
            # If list-wrapped chunk format
            if isinstance(parsed, list) and parsed:
                parsed = parsed[0]
            if not isinstance(parsed, dict):
                return
            if "response" in parsed and isinstance(parsed["response"], dict):
                parsed = parsed["response"]
            if parsed.get("usageMetadata"):
                usage_meta = parsed["usageMetadata"]
                usage = {
                    "input_tokens": usage_meta.get("promptTokenCount", 0),
                    "output_tokens": usage_meta.get("candidatesTokenCount", 0),
                    "total_tokens": usage_meta.get("totalTokenCount", 0),
                }
            candidates = parsed.get("candidates", [])
            for cand in candidates:
                content = cand.get("content", {})
                parts = content.get("parts", [])
                for part in parts:
                    # Yield reasoning/thinking blocks in separate reasoning events
                    if part.get("thought") is True or part.get("type") == "thinking":
                        thought_text = part.get("text", "") or part.get("thinking", "")
                        yield f"data: {json.dumps({'type': 'response.reasoning.delta', 'response_id': response_id, 'delta': thought_text})}\n\n"
                    elif "text" in part:
                        output_text += part["text"]
                        yield f"data: {json.dumps({'type': 'response.content_part.delta', 'response_id': response_id, 'output_index': 0, 'content_index': 0, 'delta': part['text']})}\n\n"
                    elif "functionCall" in part:
                        fc = part["functionCall"]
                        call_id = fc.get("id") or f"call_{uuid.uuid4().hex[:8]}"
                        item_id = f"fc_{uuid.uuid4().hex[:8]}"
                        output_index = next_output_index
                        next_output_index += 1
                        item = {
                            "type": "function_call",
                            "id": item_id,
                            "call_id": call_id,
                            "name": fc.get("name"),
                            "arguments": json.dumps(fc.get("args", {})),
                        }
                        yield f"data: {json.dumps({'type': 'response.output_item.added', 'response_id': response_id, 'output_index': output_index, 'item': item})}\n\n"
                        yield f"data: {json.dumps({'type': 'response.output_item.done', 'response_id': response_id, 'output_index': output_index, 'item': item})}\n\n"

        completed = False
        last_error = None
        attempts = [account]
        attempt_num = 0
        while attempt_num < len(attempts):
            stream_account = attempts[attempt_num]
            stream_headers = build_headers(stream_account)
            try:
                async with httpx.AsyncClient(timeout=60.0) as client:
                    async with client.stream("POST", backend_url, json=antigravity_req, headers=stream_headers) as res:
                        if res.status_code != 200:
                            if res.status_code in (401, 403, 429):
                                body_bytes = await res.aread()
                                body_text = body_bytes.decode("utf-8", errors="ignore")
                                account_manager.mark_failure(stream_account["email"], f"Streaming HTTP {res.status_code}: {body_text}")
                            last_error = f"Google Antigravity returned HTTP {res.status_code}"
                        else:
                            buffer = ""
                            async for chunk in res.aiter_text():
                                buffer += chunk
                                while "\n" in buffer:
                                    line, buffer = buffer.split("\n", 1)
                                    async for event in parse_stream_line(line):
                                        yield event
                            if buffer.strip():
                                async for event in parse_stream_line(buffer):
                                    yield event
                            completed = True
            except Exception as e:
                account_manager.mark_failure(stream_account["email"], f"Streaming connection error: {e}")
                last_error = str(e)

            if completed:
                break
            if attempt_num == 0:
                rotated = account_manager.select_active_account(model)
                if rotated and rotated.get("email") != stream_account.get("email"):
                    attempts.append(rotated)
                    attempt_num += 1
                    continue
            break

        if not completed:
            async for event in fail_stream("backend_error", last_error or "Google Antigravity stream failed"):
                yield event
            return
                
        # 4. Final completion events
        yield f"data: {json.dumps({'type': 'response.content_part.done', 'response_id': response_id, 'output_index': 0, 'content_index': 0, 'part': {'type': 'output_text', 'text': output_text}})}\n\n"
        yield f"data: {json.dumps({'type': 'response.output_item.done', 'response_id': response_id, 'output_index': 0, 'item': {'type': 'message', 'id': msg_id, 'role': 'assistant', 'status': 'completed', 'content': [{'type': 'output_text', 'text': output_text, 'annotations': []}]}})}\n\n"
        done_response = {'id': response_id, 'object': 'response', 'status': 'completed'}
        if usage:
            done_response["usage"] = usage
        yield f"data: {json.dumps({'type': 'response.done', 'response': done_response})}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(sse_generator(), media_type="text/event-stream")


async def create_openai_compatible_response(codex_req: dict, provider: dict, provider_model: str, display_model: str) -> dict:
    payload = transform_request_to_chat(codex_req, provider_model)
    payload["stream"] = False
    url = chat_completions_url(provider)
    headers = build_openai_compatible_headers(provider)
    async with httpx.AsyncClient(timeout=provider.get("timeout", 120.0)) as client:
        try:
            res = await client.post(url, json=payload, headers=headers)
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"{provider['id']} connection error: {e}") from e
    if res.status_code != 200:
        raise HTTPException(status_code=res.status_code, detail=f"{provider['id']} API error: {res.text}")
    try:
        return transform_chat_response(res.json(), display_model)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"{provider['id']} response translation failed: {e}") from e


async def openai_compatible_sse_generator(
    codex_req: dict,
    provider: dict,
    provider_model: str,
    display_model: str,
) -> AsyncGenerator[str, None]:
    import uuid

    response_id = f"resp_{uuid.uuid4().hex[:12]}"
    msg_id = f"msg_{uuid.uuid4().hex[:8]}"
    output_text = ""
    usage = None
    tool_calls: dict[int, dict] = {}
    tool_output_indices: dict[int, int] = {}
    next_output_index = 1

    yield f"data: {json.dumps({'type': 'response.created', 'response': {'id': response_id, 'object': 'response', 'status': 'in_progress', 'model': display_model}})}\n\n"
    yield f"data: {json.dumps({'type': 'response.output_item.added', 'response_id': response_id, 'output_index': 0, 'item': {'type': 'message', 'id': msg_id, 'role': 'assistant', 'status': 'in_progress', 'content': []}})}\n\n"
    yield f"data: {json.dumps({'type': 'response.content_part.added', 'response_id': response_id, 'output_index': 0, 'content_index': 0, 'part': {'type': 'output_text', 'text': ''}})}\n\n"

    payload = transform_request_to_chat({**codex_req, "stream": True}, provider_model)
    url = chat_completions_url(provider)
    headers = build_openai_compatible_headers(provider)

    async def fail_stream(code: str, message: str) -> AsyncGenerator[str, None]:
        yield f"data: {json.dumps({'type': 'error', 'error': {'code': code, 'message': message}})}\n\n"
        yield f"data: {json.dumps({'type': 'response.failed', 'response': {'id': response_id, 'object': 'response', 'status': 'failed', 'model': display_model, 'error': {'code': code, 'message': message}}})}\n\n"
        yield "data: [DONE]\n\n"

    async def parse_chat_stream_line(line: str) -> AsyncGenerator[str, None]:
        nonlocal output_text, usage, next_output_index
        line = line.strip()
        if not line or not line.startswith("data:"):
            return
        data_payload = line[5:].strip()
        if data_payload == "[DONE]":
            return
        try:
            parsed = json.loads(data_payload)
        except json.JSONDecodeError as e:
            print(f"[*] Skipping invalid {provider['id']} SSE JSON chunk: {e}")
            return
        if parsed.get("usage"):
            provider_usage = parsed["usage"]
            usage = {
                "input_tokens": provider_usage.get("prompt_tokens", provider_usage.get("input_tokens", 0)),
                "output_tokens": provider_usage.get("completion_tokens", provider_usage.get("output_tokens", 0)),
                "total_tokens": provider_usage.get("total_tokens", 0),
            }
        for choice in parsed.get("choices", []) or []:
            delta = choice.get("delta", {}) or {}
            if delta.get("content"):
                output_text += delta["content"]
                yield f"data: {json.dumps({'type': 'response.content_part.delta', 'response_id': response_id, 'output_index': 0, 'content_index': 0, 'delta': delta['content']})}\n\n"
            for tool_delta in delta.get("tool_calls", []) or []:
                idx = int(tool_delta.get("index", 0))
                generated_call_id = tool_delta.get("id") or f"call_{uuid.uuid4().hex[:8]}"
                state = tool_calls.setdefault(idx, {
                    "id": generated_call_id,
                    "type": "function_call",
                    "call_id": generated_call_id,
                    "name": "",
                    "arguments": "",
                })
                if tool_delta.get("id"):
                    state["id"] = tool_delta["id"]
                    state["call_id"] = tool_delta["id"]
                fn = tool_delta.get("function", {}) or {}
                if fn.get("name"):
                    state["name"] += fn["name"]
                if fn.get("arguments"):
                    state["arguments"] += fn["arguments"]
                if idx not in tool_output_indices:
                    tool_output_indices[idx] = next_output_index
                    next_output_index += 1

    try:
        async with httpx.AsyncClient(timeout=provider.get("timeout", 120.0)) as client:
            async with client.stream("POST", url, json=payload, headers=headers) as res:
                if res.status_code != 200:
                    body = (await res.aread()).decode("utf-8", errors="ignore")
                    async for event in fail_stream("backend_error", f"{provider['id']} returned HTTP {res.status_code}: {body}"):
                        yield event
                    return
                buffer = ""
                async for chunk in res.aiter_text():
                    buffer += chunk
                    while "\n" in buffer:
                        line, buffer = buffer.split("\n", 1)
                        async for event in parse_chat_stream_line(line):
                            yield event
                if buffer.strip():
                    async for event in parse_chat_stream_line(buffer):
                        yield event
    except Exception as e:
        async for event in fail_stream("connection_error", str(e)):
            yield event
        return

    for idx in sorted(tool_calls):
        item = tool_calls[idx]
        output_index = tool_output_indices[idx]
        yield f"data: {json.dumps({'type': 'response.output_item.added', 'response_id': response_id, 'output_index': output_index, 'item': item})}\n\n"
        yield f"data: {json.dumps({'type': 'response.output_item.done', 'response_id': response_id, 'output_index': output_index, 'item': item})}\n\n"

    yield f"data: {json.dumps({'type': 'response.content_part.done', 'response_id': response_id, 'output_index': 0, 'content_index': 0, 'part': {'type': 'output_text', 'text': output_text}})}\n\n"
    yield f"data: {json.dumps({'type': 'response.output_item.done', 'response_id': response_id, 'output_index': 0, 'item': {'type': 'message', 'id': msg_id, 'role': 'assistant', 'status': 'completed', 'content': [{'type': 'output_text', 'text': output_text, 'annotations': []}]}})}\n\n"
    done_response = {"id": response_id, "object": "response", "status": "completed", "model": display_model}
    if usage:
        done_response["usage"] = usage
    yield f"data: {json.dumps({'type': 'response.done', 'response': done_response})}\n\n"
    yield "data: [DONE]\n\n"
