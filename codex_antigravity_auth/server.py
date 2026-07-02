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
from .transform import token_count, transform_chat_response, transform_request, transform_request_to_chat, transform_response
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


def finite_retry_after_seconds(value: object) -> float | None:
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(seconds):
        return None
    return max(0.0, seconds)


def retry_after_seconds_from_response(res: httpx.Response) -> float | None:
    retry_after = res.headers.get("retry-after")
    if retry_after:
        parsed_seconds = finite_retry_after_seconds(retry_after)
        if parsed_seconds is not None:
            return parsed_seconds
        else:
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
                parsed_seconds = finite_retry_after_seconds(float(seconds) + (float(nanos) / 1_000_000_000))
            except (TypeError, ValueError):
                continue
            if parsed_seconds is not None:
                return parsed_seconds
    return None


def provider_has_usable_key(provider: dict) -> bool:
    try:
        return bool(validate_provider_api_key(resolve_api_key(provider)))
    except ValueError:
        return False


@app.get("/v1/models")
async def list_models():
    """Return model catalog so Codex Desktop can populate its picker dropdown."""
    import time
    byok_models = []
    for provider_id, provider in all_provider_configs().items():
        if not provider_has_usable_key(provider):
            continue
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


def validate_response_request_body(value: object) -> dict:
    if not isinstance(value, dict):
        raise HTTPException(status_code=400, detail="Request JSON body must be an object")
    reasoning = value.get("reasoning")
    if reasoning is not None and not isinstance(reasoning, dict):
        raise HTTPException(status_code=400, detail="reasoning must be an object")
    validate_response_generation_options(value)
    validate_response_tool_choice(value)
    return value


def validate_finite_number_option(value: object, field_name: str, *, minimum: float, maximum: float | None = None) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise HTTPException(status_code=400, detail=f"{field_name} must be a finite number")
    number = float(value)
    if number < minimum or (maximum is not None and number > maximum):
        if maximum is None:
            raise HTTPException(status_code=400, detail=f"{field_name} must be greater than or equal to {minimum:g}")
        raise HTTPException(status_code=400, detail=f"{field_name} must be between {minimum:g} and {maximum:g}")


def validate_response_generation_options(codex_req: dict) -> None:
    if "temperature" in codex_req:
        validate_finite_number_option(codex_req["temperature"], "temperature", minimum=0.0, maximum=2.0)
    if "top_p" in codex_req:
        validate_finite_number_option(codex_req["top_p"], "top_p", minimum=0.0, maximum=1.0)
    if "max_output_tokens" in codex_req:
        value = codex_req["max_output_tokens"]
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise HTTPException(status_code=400, detail="max_output_tokens must be a positive integer")
    if "stop" in codex_req:
        stop = codex_req["stop"]
        values = [stop] if isinstance(stop, str) else stop
        if not isinstance(values, list) or not values:
            raise HTTPException(status_code=400, detail="stop must be a string or a non-empty list of strings")
        for item in values:
            if not isinstance(item, str) or not item or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in item):
                raise HTTPException(status_code=400, detail="stop values must be non-empty strings without control characters")


def validate_response_tool_choice(codex_req: dict) -> None:
    if "tool_choice" not in codex_req:
        return
    tool_choice = codex_req.get("tool_choice")
    if isinstance(tool_choice, str):
        if tool_choice not in {"auto", "none", "required"}:
            raise HTTPException(status_code=400, detail="tool_choice must be auto, none, required, or a function choice object")
        return
    if not isinstance(tool_choice, dict) or tool_choice.get("type") != "function":
        raise HTTPException(status_code=400, detail="tool_choice must be auto, none, required, or a function choice object")
    nested = tool_choice.get("function")
    name = tool_choice.get("name") or (nested.get("name") if isinstance(nested, dict) else None)
    if not isinstance(name, str) or not name or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in name):
        raise HTTPException(status_code=400, detail="tool_choice function name must be a non-empty string without control characters")


def response_stream_flag(codex_req: dict) -> bool:
    if "stream" not in codex_req:
        return False
    stream = codex_req.get("stream")
    if not isinstance(stream, bool):
        raise HTTPException(status_code=400, detail="stream must be a boolean")
    return stream


def response_model_id(codex_req: dict) -> str:
    raw_model = codex_req.get("model", "gemini-3.5-flash-high")
    if not isinstance(raw_model, str):
        raise HTTPException(status_code=400, detail="model must be a string")
    model = raw_model.strip()
    if not model:
        raise HTTPException(status_code=400, detail="model must be non-empty")
    if any(ch.isspace() or ord(ch) < 0x20 or ord(ch) == 0x7F for ch in model):
        raise HTTPException(status_code=400, detail="model must not contain whitespace or control characters")
    return model


def validate_provider_model_id(provider_id: str | None, provider_model: str) -> None:
    if provider_id and not provider_model:
        raise HTTPException(status_code=400, detail=f"Provider '{provider_id}' model id must be non-empty")


def chat_tool_call_delta_index(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        index = int(value)
    except (TypeError, ValueError):
        return None
    if index < 0:
        return None
    return index


def stream_string(value: object) -> str | None:
    return value if isinstance(value, str) else None


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
    codex_req = validate_response_request_body(codex_req)

    reject_unsupported_previous_response(codex_req)
        
    model = response_model_id(codex_req)
    codex_req["model"] = model
    stream = response_stream_flag(codex_req)
    provider_id, provider_model = split_provider_model(model)
    validate_provider_model_id(provider_id, provider_model)
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
            raise HTTPException(status_code=500, detail=f"Response translation failed: {safe_error_detail(e)}")

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
            if isinstance(parsed.get("usageMetadata"), dict):
                usage_meta = parsed["usageMetadata"]
                usage = {
                    "input_tokens": token_count(usage_meta.get("promptTokenCount", 0)),
                    "output_tokens": token_count(usage_meta.get("candidatesTokenCount", 0)),
                    "total_tokens": token_count(usage_meta.get("totalTokenCount", 0)),
                }
            candidates = parsed.get("candidates", [])
            if not isinstance(candidates, list):
                return
            for cand in candidates:
                if not isinstance(cand, dict):
                    continue
                content = cand.get("content", {})
                if not isinstance(content, dict):
                    continue
                parts = content.get("parts", [])
                if not isinstance(parts, list):
                    continue
                for part in parts:
                    if not isinstance(part, dict):
                        continue
                    # Yield reasoning/thinking blocks in separate reasoning events
                    if part.get("thought") is True or part.get("type") == "thinking":
                        thought_text = stream_string(part.get("text")) or stream_string(part.get("thinking"))
                        if thought_text:
                            yield f"data: {json.dumps({'type': 'response.reasoning_text.delta', 'response_id': response_id, 'delta': thought_text})}\n\n"
                    elif "text" in part:
                        text = stream_string(part.get("text"))
                        if text is None:
                            continue
                        output_text += text
                        yield f"data: {json.dumps({'type': 'response.output_text.delta', 'response_id': response_id, 'output_index': 0, 'content_index': 0, 'delta': text})}\n\n"
                    elif "functionCall" in part:
                        fc = part["functionCall"]
                        if not isinstance(fc, dict):
                            continue
                        name = stream_string(fc.get("name"))
                        if not name:
                            continue
                        call_id = stream_string(fc.get("id")) or f"call_{uuid.uuid4().hex[:8]}"
                        item_id = f"fc_{uuid.uuid4().hex[:8]}"
                        output_index = next_output_index
                        next_output_index += 1
                        args = fc.get("args", {})
                        arguments = json.dumps(args if isinstance(args, dict) else {})
                        item = {
                            "type": "function_call",
                            "id": item_id,
                            "call_id": call_id,
                            "name": name,
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
        raise HTTPException(status_code=500, detail=f"{provider['id']} response translation failed: {safe_error_detail(e)}") from e


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
        if not isinstance(parsed, dict):
            return
        if isinstance(parsed.get("usage"), dict):
            provider_usage = parsed["usage"]
            usage = {
                "input_tokens": token_count(provider_usage.get("prompt_tokens", provider_usage.get("input_tokens", 0))),
                "output_tokens": token_count(provider_usage.get("completion_tokens", provider_usage.get("output_tokens", 0))),
                "total_tokens": token_count(provider_usage.get("total_tokens", 0)),
            }
        choices = parsed.get("choices", []) or []
        if not isinstance(choices, list):
            return
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            delta = choice.get("delta", {}) or {}
            if not isinstance(delta, dict):
                continue
            reasoning_content = stream_string(delta.get("reasoning_content"))
            if reasoning_content:
                yield f"data: {json.dumps({'type': 'response.reasoning_text.delta', 'response_id': response_id, 'delta': reasoning_content})}\n\n"
            content_delta = stream_string(delta.get("content"))
            if content_delta:
                output_text += content_delta
                yield f"data: {json.dumps({'type': 'response.output_text.delta', 'response_id': response_id, 'output_index': 0, 'content_index': 0, 'delta': content_delta})}\n\n"
            tool_deltas = delta.get("tool_calls", []) or []
            if not isinstance(tool_deltas, list):
                continue
            for tool_delta in tool_deltas:
                if not isinstance(tool_delta, dict):
                    continue
                idx = chat_tool_call_delta_index(tool_delta.get("index", 0))
                if idx is None:
                    continue
                fn = tool_delta.get("function", {}) or {}
                if not isinstance(fn, dict):
                    continue
                generated_call_id = stream_string(tool_delta.get("id")) or f"call_{uuid.uuid4().hex[:8]}"
                state = tool_calls.setdefault(idx, {
                    "id": f"fc_{uuid.uuid4().hex[:8]}",
                    "type": "function_call",
                    "call_id": generated_call_id,
                    "name": "",
                    "arguments": "",
                })
                tool_call_id = stream_string(tool_delta.get("id"))
                if tool_call_id:
                    state["call_id"] = tool_call_id
                buffered_arguments = state.get("arguments", "")
                name_delta = stream_string(fn.get("name"))
                arguments_delta = stream_string(fn.get("arguments"))
                if name_delta:
                    state["name"] += name_delta
                new_tool_item = idx not in tool_output_indices and bool(state["name"]) and arguments_delta is not None
                if new_tool_item:
                    tool_output_indices[idx] = next_output_index
                    next_output_index += 1
                    item = dict(state)
                    item["arguments"] = ""
                    yield f"data: {json.dumps({'type': 'response.output_item.added', 'response_id': response_id, 'output_index': tool_output_indices[idx], 'item': item})}\n\n"
                    if buffered_arguments:
                        yield f"data: {json.dumps({'type': 'response.function_call_arguments.delta', 'response_id': response_id, 'item_id': state['id'], 'output_index': tool_output_indices[idx], 'delta': buffered_arguments})}\n\n"
                if arguments_delta:
                    state["arguments"] += arguments_delta
                    if idx in tool_output_indices:
                        yield f"data: {json.dumps({'type': 'response.function_call_arguments.delta', 'response_id': response_id, 'item_id': state['id'], 'output_index': tool_output_indices[idx], 'delta': arguments_delta})}\n\n"

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
        if not item.get("name"):
            continue
        if idx not in tool_output_indices:
            tool_output_indices[idx] = next_output_index
            next_output_index += 1
            added_item = dict(item)
            added_item["arguments"] = ""
            yield f"data: {json.dumps({'type': 'response.output_item.added', 'response_id': response_id, 'output_index': tool_output_indices[idx], 'item': added_item})}\n\n"
            if item.get("arguments"):
                yield f"data: {json.dumps({'type': 'response.function_call_arguments.delta', 'response_id': response_id, 'item_id': item['id'], 'output_index': tool_output_indices[idx], 'delta': item['arguments']})}\n\n"
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
