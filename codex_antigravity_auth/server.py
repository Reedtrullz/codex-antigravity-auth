import json
import math
import os
import secrets
import httpx
import email.utils
import re
from datetime import datetime, timezone
from typing import AsyncGenerator
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from .accounts import AccountManager
from .byok import (
    all_provider_configs,
    resolve_api_key,
    split_provider_model,
    validate_http_base_url,
    validate_provider_api_key,
    validate_provider_headers,
)
from .transform import transform_chat_response, transform_request, transform_request_to_chat, transform_response
from .constants import ANTIGRAVITY_ENDPOINT_PROD, get_platform, is_loopback_host
from .redaction import redact_secret_text

app = FastAPI(title="Codex Antigravity Gateway")
account_manager = AccountManager()


@app.middleware("http")
async def require_remote_gateway_token(request: Request, call_next):
    client_host = request.client.host if request.client else None
    if is_loopback_host(client_host):
        return await call_next(request)

    token = os.environ.get("ANTIGRAVITY_GATEWAY_TOKEN")
    allow_remote = os.environ.get("ANTIGRAVITY_ALLOW_REMOTE") == "1"
    expected_auth = f"Bearer {token}" if token else ""
    supplied_auth = request.headers.get("authorization", "")
    if allow_remote and token and secrets.compare_digest(supplied_auth, expected_auth):
        return await call_next(request)

    return JSONResponse(
        status_code=403,
        content={"detail": "Remote access requires ANTIGRAVITY_ALLOW_REMOTE=1 and a valid bearer token."},
    )

# ── Model catalog for native Codex Desktop picker ──
AVAILABLE_MODELS = [
    {"id": "gemini-3.5-flash-high", "display_name": "Gemini 3.5 Flash (Agent High)", "context_window": 1000000},
    {"id": "gemini-3.5-flash-medium", "display_name": "Gemini 3.5 Flash (General)", "context_window": 1000000},
    {"id": "gemini-3.1-pro-high",    "display_name": "Gemini 3.1 Pro (Reasoning)", "context_window": 1000000},
    {"id": "claude-3.5-sonnet",      "display_name": "Claude Sonnet 4.6 (Google)",  "context_window": 200000},
    {"id": "claude-opus-4-6",        "display_name": "Claude Opus 4.6 (Google)",   "context_window": 200000},
]


def safe_error_detail(value: object) -> str:
    return redact_secret_text(str(value))


def retry_after_seconds_from_response(res: httpx.Response) -> float | None:
    retry_after = res.headers.get("retry-after")
    if retry_after:
        try:
            return max(0.0, float(retry_after))
        except ValueError:
            try:
                retry_at = email.utils.parsedate_to_datetime(retry_after)
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=timezone.utc)
                return max(0.0, (retry_at - datetime.now(timezone.utc)).total_seconds())
            except Exception:
                pass

    try:
        payload = res.json()
    except Exception:
        return None

    details = []
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict) and isinstance(error.get("details"), list):
            details.extend(error["details"])
        if isinstance(payload.get("details"), list):
            details.extend(payload["details"])

    for detail in details:
        if not isinstance(detail, dict):
            continue
        retry_delay = detail.get("retryDelay")
        if isinstance(retry_delay, str):
            match = re.fullmatch(r"(\d+(?:\.\d+)?)s", retry_delay)
            if match:
                return float(match.group(1))
        if isinstance(retry_delay, dict):
            seconds = retry_delay.get("seconds", 0)
            nanos = retry_delay.get("nanos", 0)
            try:
                return float(seconds) + (float(nanos) / 1_000_000_000)
            except (TypeError, ValueError):
                continue
    return None

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
    try:
        api_key = validate_provider_api_key(api_key)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"Provider '{provider['id']}' {str(e)}") from e
    if not api_key:
        raise HTTPException(
            status_code=401,
            detail=f"No API key configured for provider '{provider['id']}'. Set {provider.get('apiKeyEnv', 'provider API key')} or run provider set.",
        )
    provider_headers = provider.get("headers", {}) or {}
    try:
        provider_headers = validate_provider_headers(provider_headers)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    headers.update(provider_headers or {})
    return headers


def chat_completions_url(provider: dict) -> str:
    base_url = provider.get("baseUrl", "")
    if not isinstance(base_url, str):
        raise HTTPException(status_code=400, detail=f"Provider '{provider['id']}' baseUrl must be a string")
    if not base_url.strip():
        raise HTTPException(status_code=500, detail=f"Provider '{provider['id']}' has no baseUrl configured")
    try:
        base_url = validate_http_base_url(base_url, label=f"Provider '{provider['id']}' baseUrl")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    if base_url.endswith("/chat/completions"):
        url = base_url
    else:
        url = f"{base_url}/chat/completions"
    return url


def openai_compatible_timeout(provider: dict) -> float:
    timeout = provider.get("timeout", 120.0)
    if (
        not isinstance(timeout, (int, float))
        or isinstance(timeout, bool)
        or not math.isfinite(float(timeout))
        or float(timeout) <= 0
    ):
        raise HTTPException(status_code=400, detail=f"Provider '{provider['id']}' timeout must be a positive number")
    return float(timeout)


def reject_unsupported_previous_response(codex_req: dict) -> None:
    if codex_req.get("previous_response_id"):
        raise HTTPException(
            status_code=400,
            detail="previous_response_id is not supported by this stateless gateway; resend the full conversation in input.",
        )


def prepare_openai_compatible_request(
    codex_req: dict,
    provider: dict,
    provider_model: str,
    *,
    stream: bool,
) -> tuple[dict, str, dict, float]:
    try:
        payload = transform_request_to_chat({**codex_req, "stream": stream}, provider_model)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    payload["stream"] = stream
    headers = build_openai_compatible_headers(provider)
    url = chat_completions_url(provider)
    timeout = openai_compatible_timeout(provider)
    return payload, url, headers, timeout

@app.post("/v1/responses")
async def create_response(request: Request):
    try:
        codex_req = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    reject_unsupported_previous_response(codex_req)
        
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
            payload, url, headers, timeout = prepare_openai_compatible_request(codex_req, provider, provider_model, stream=True)
            return StreamingResponse(
                openai_compatible_sse_generator(payload, url, headers, timeout, provider, model),
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
        
    def build_google_request(selected_account: dict) -> tuple[dict, dict]:
        project_id = selected_account.get("projectId") or selected_account.get("managedProjectId")
        return transform_request(codex_req, project_id=project_id), build_headers(selected_account)
    
    # Route target action based on streaming mode
    action = "streamGenerateContent" if stream else "generateContent"
    backend_url = f"{ANTIGRAVITY_ENDPOINT_PROD}/v1internal:{action}"
    if stream:
        backend_url += "?alt=sse"
    
    # Perform HTTP POST request to Antigravity endpoint with error recovery & rotation
    async def request_backend(selected_account: dict) -> httpx.Response | None:
        antigravity_req, headers = build_google_request(selected_account)
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
                account_manager.mark_failure(selected_account["email"], f"Connection error: {safe_error_detail(e)}")
                return None

    # Handle standard non-streaming response path
    if not stream:
        response_account = account
        res = await request_backend(response_account)
        if not res:
            # Retry with rotated account on connection failures
            new_account = account_manager.select_active_account(model)
            if new_account:
                response_account = new_account
                res = await request_backend(response_account)
                
        if not res:
            raise HTTPException(status_code=502, detail="Failed to communicate with Antigravity backend after rotation")

        if res.status_code in (401, 403, 429):
            reason = "Rate limited / Quota exceeded" if res.status_code == 429 else f"Auth failure {res.status_code}: {safe_error_detail(res.text)}"
            account_manager.mark_failure(response_account["email"], reason, retry_after_seconds_from_response(res))
            new_account = account_manager.select_active_account(model)
            if new_account:
                response_account = new_account
                res = await request_backend(response_account)
            if not res:
                raise HTTPException(status_code=502, detail="Failed to communicate with Antigravity backend after rotation")
            
        if res.status_code in (401, 403):
            # Token might be invalidated or verification required
            account_manager.mark_failure(response_account["email"], f"Auth failure {res.status_code}: {safe_error_detail(res.text)}")
            raise HTTPException(status_code=res.status_code, detail=f"Google Authentication failure: {safe_error_detail(res.text)}")
            
        if res.status_code == 429:
            account_manager.mark_failure(response_account["email"], "Rate limited / Quota exceeded", retry_after_seconds_from_response(res))
            raise HTTPException(status_code=429, detail="Antigravity account rate limit reached. Auto-switching to next account.")
            
        if res.status_code != 200:
            raise HTTPException(status_code=res.status_code, detail=f"Google Antigravity API error: {safe_error_detail(res.text)}")
            
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
            message = safe_error_detail(message)
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
                        yield f"data: {json.dumps({'type': 'response.reasoning_text.delta', 'response_id': response_id, 'delta': thought_text})}\n\n"
                    elif "text" in part:
                        output_text += part["text"]
                        yield f"data: {json.dumps({'type': 'response.output_text.delta', 'response_id': response_id, 'output_index': 0, 'content_index': 0, 'delta': part['text']})}\n\n"
                    elif "functionCall" in part:
                        fc = part["functionCall"]
                        call_id = fc.get("id") or f"call_{uuid.uuid4().hex[:8]}"
                        item_id = f"fc_{uuid.uuid4().hex[:8]}"
                        output_index = next_output_index
                        next_output_index += 1
                        arguments = json.dumps(fc.get("args", {}))
                        item = {
                            "type": "function_call",
                            "id": item_id,
                            "call_id": call_id,
                            "name": fc.get("name"),
                            "arguments": "",
                        }
                        yield f"data: {json.dumps({'type': 'response.output_item.added', 'response_id': response_id, 'output_index': output_index, 'item': item})}\n\n"
                        if arguments:
                            yield f"data: {json.dumps({'type': 'response.function_call_arguments.delta', 'response_id': response_id, 'item_id': item_id, 'output_index': output_index, 'delta': arguments})}\n\n"
                        item["arguments"] = arguments
                        yield f"data: {json.dumps({'type': 'response.function_call_arguments.done', 'response_id': response_id, 'item_id': item_id, 'output_index': output_index, 'arguments': arguments})}\n\n"
                        yield f"data: {json.dumps({'type': 'response.output_item.done', 'response_id': response_id, 'output_index': output_index, 'item': item})}\n\n"

        completed = False
        last_error = None
        attempts = [account]
        attempt_num = 0
        while attempt_num < len(attempts):
            stream_account = attempts[attempt_num]
            stream_req, stream_headers = build_google_request(stream_account)
            try:
                async with httpx.AsyncClient(timeout=60.0) as client:
                    async with client.stream("POST", backend_url, json=stream_req, headers=stream_headers) as res:
                        if res.status_code != 200:
                            if res.status_code in (401, 403, 429):
                                body_bytes = await res.aread()
                                body_text = body_bytes.decode("utf-8", errors="ignore")
                                account_manager.mark_failure(
                                    stream_account["email"],
                                    f"Streaming HTTP {res.status_code}: {safe_error_detail(body_text)}",
                                    retry_after_seconds_from_response(res),
                                )
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
                account_manager.mark_failure(stream_account["email"], f"Streaming connection error: {safe_error_detail(e)}")
                last_error = safe_error_detail(e)

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
        yield f"data: {json.dumps({'type': 'response.completed', 'response': done_response})}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(sse_generator(), media_type="text/event-stream")


async def create_openai_compatible_response(codex_req: dict, provider: dict, provider_model: str, display_model: str) -> dict:
    payload, url, headers, timeout = prepare_openai_compatible_request(codex_req, provider, provider_model, stream=False)
    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            res = await client.post(url, json=payload, headers=headers)
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"{provider['id']} connection error: {safe_error_detail(e)}") from e
    if res.status_code != 200:
        raise HTTPException(status_code=res.status_code, detail=f"{provider['id']} API error: {safe_error_detail(res.text)}")
    try:
        return transform_chat_response(res.json(), display_model)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"{provider['id']} response translation failed: {e}") from e


async def openai_compatible_sse_generator(
    payload: dict,
    url: str,
    headers: dict,
    timeout: float,
    provider: dict,
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

    async def fail_stream(code: str, message: str) -> AsyncGenerator[str, None]:
        message = safe_error_detail(message)
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
            if delta.get("reasoning_content"):
                yield f"data: {json.dumps({'type': 'response.reasoning_text.delta', 'response_id': response_id, 'delta': delta['reasoning_content']})}\n\n"
            if delta.get("content"):
                output_text += delta["content"]
                yield f"data: {json.dumps({'type': 'response.output_text.delta', 'response_id': response_id, 'output_index': 0, 'content_index': 0, 'delta': delta['content']})}\n\n"
            for tool_delta in delta.get("tool_calls", []) or []:
                idx = int(tool_delta.get("index", 0))
                generated_call_id = tool_delta.get("id") or f"call_{uuid.uuid4().hex[:8]}"
                state = tool_calls.setdefault(idx, {
                    "id": f"fc_{uuid.uuid4().hex[:8]}",
                    "type": "function_call",
                    "call_id": generated_call_id,
                    "name": "",
                    "arguments": "",
                })
                if tool_delta.get("id"):
                    state["call_id"] = tool_delta["id"]
                fn = tool_delta.get("function", {}) or {}
                new_tool_item = idx not in tool_output_indices
                if fn.get("name"):
                    state["name"] += fn["name"]
                if new_tool_item:
                    tool_output_indices[idx] = next_output_index
                    next_output_index += 1
                    yield f"data: {json.dumps({'type': 'response.output_item.added', 'response_id': response_id, 'output_index': tool_output_indices[idx], 'item': dict(state)})}\n\n"
                if fn.get("arguments"):
                    state["arguments"] += fn["arguments"]
                    yield f"data: {json.dumps({'type': 'response.function_call_arguments.delta', 'response_id': response_id, 'item_id': state['id'], 'output_index': tool_output_indices[idx], 'delta': fn['arguments']})}\n\n"

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
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
        async for event in fail_stream("connection_error", safe_error_detail(e)):
            yield event
        return

    for idx in sorted(tool_calls):
        item = tool_calls[idx]
        output_index = tool_output_indices[idx]
        arguments = item.get("arguments", "")
        yield f"data: {json.dumps({'type': 'response.function_call_arguments.done', 'response_id': response_id, 'item_id': item['id'], 'output_index': output_index, 'arguments': arguments})}\n\n"
        yield f"data: {json.dumps({'type': 'response.output_item.done', 'response_id': response_id, 'output_index': output_index, 'item': item})}\n\n"

    yield f"data: {json.dumps({'type': 'response.content_part.done', 'response_id': response_id, 'output_index': 0, 'content_index': 0, 'part': {'type': 'output_text', 'text': output_text}})}\n\n"
    yield f"data: {json.dumps({'type': 'response.output_item.done', 'response_id': response_id, 'output_index': 0, 'item': {'type': 'message', 'id': msg_id, 'role': 'assistant', 'status': 'completed', 'content': [{'type': 'output_text', 'text': output_text, 'annotations': []}]}})}\n\n"
    done_response = {"id": response_id, "object": "response", "status": "completed", "model": display_model}
    if usage:
        done_response["usage"] = usage
    yield f"data: {json.dumps({'type': 'response.completed', 'response': done_response})}\n\n"
    yield "data: [DONE]\n\n"
