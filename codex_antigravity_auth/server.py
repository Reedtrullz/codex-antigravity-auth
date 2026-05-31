import json
import httpx
from typing import AsyncGenerator
from fastapi import FastAPI, Request, Response, HTTPException
from fastapi.responses import StreamingResponse
from .accounts import AccountManager
from .transform import transform_request, transform_response, transform_gemini_candidate
from .constants import ANTIGRAVITY_ENDPOINT_PROD, get_platform

app = FastAPI(title="Codex Antigravity Gateway")
account_manager = AccountManager()

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
        headers["deviceId"] = fp.get("deviceId", "")
        headers["sessionToken"] = fp.get("sessionToken", "")
        headers["User-Agent"] = fp.get("userAgent", headers["User-Agent"])
        headers["X-Goog-Api-Client"] = fp.get("apiClient", headers["X-Goog-Api-Client"])
        if fp.get("clientMetadata"):
            headers["Client-Metadata"] = json.dumps(fp["clientMetadata"])
            
    return headers

@app.post("/v1/responses")
async def create_response(request: Request):
    try:
        codex_req = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")
        
    model = codex_req.get("model", "gemini-3.5-flash-high")
    stream = codex_req.get("stream", False)
    
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
        
        # 1. response.created
        yield f"data: {json.dumps({'type': 'response.created', 'response': {'id': response_id, 'object': 'response', 'status': 'in_progress'}})}\n\n"
        
        # 2. response.output_item.added (message)
        msg_id = f"msg_{uuid.uuid4().hex[:8]}"
        yield f"data: {json.dumps({'type': 'response.output_item.added', 'response_id': response_id, 'output_index': 0, 'item': {'type': 'message', 'id': msg_id, 'role': 'assistant', 'status': 'in_progress', 'content': []}})}\n\n"
        
        # 3. response.content_part.added
        yield f"data: {json.dumps({'type': 'response.content_part.added', 'response_id': response_id, 'output_index': 0, 'content_index': 0, 'part': {'type': 'output_text', 'text': ''}})}\n\n"

        async with httpx.AsyncClient(timeout=60.0) as client:
            try:
                async with client.stream("POST", backend_url, json=antigravity_req, headers=headers) as res:
                    if res.status_code != 200:
                        yield f"data: {json.dumps({'type': 'error', 'error': {'code': 'backend_error', 'message': f'Google Antigravity returned HTTP {res.status_code}'}})}\n\n"
                        return

                    # Parse stream chunks and output them in standard Responses API delta events
                    buffer = ""
                    async for chunk in res.aiter_text():
                        buffer += chunk
                        while "\n" in buffer:
                            line, buffer = buffer.split("\n", 1)
                            line = line.strip()
                            if not line:
                                continue
                            
                            # Parse Google stream lines. Format:
                            # data: {"candidates": [{"content": {"parts": [{"text": "..."}]}}]}
                            # Also supports raw wrapped response chunks
                            if line.startswith("data:"):
                                data_payload = line[5:].strip()
                                if data_payload == "[DONE]":
                                    continue
                                try:
                                    parsed = json.loads(data_payload)
                                    # If list-wrapped chunk format
                                    if isinstance(parsed, list) and parsed:
                                        parsed = parsed[0]
                                    if "response" in parsed and isinstance(parsed["response"], dict):
                                        parsed = parsed["response"]
                                    candidates = parsed.get("candidates", [])
                                    for cand in candidates:
                                        content = cand.get("content", {})
                                        parts = content.get("parts", [])
                                        for part in parts:
                                            # Yield reasoning/thinking blocks in separate reasoning events
                                            if part.get("thought") is True or part.get("type") == "thinking" or "thoughtSignature" in part:
                                                thought_text = part.get("text", "") or part.get("thinking", "")
                                                # Send reasoning delta event
                                                yield f"data: {json.dumps({'type': 'response.reasoning.delta', 'response_id': response_id, 'delta': thought_text})}\n\n"
                                            elif "text" in part:
                                                yield f"data: {json.dumps({'type': 'response.content_part.delta', 'response_id': response_id, 'output_index': 0, 'content_index': 0, 'delta': part['text']})}\n\n"
                                            elif "functionCall" in part:
                                                fc = part["functionCall"]
                                                call_id = fc.get("id") or f"call_{uuid.uuid4().hex[:8]}"
                                                # Yield tool call delta/done structures
                                                yield f"data: {json.dumps({'type': 'response.output_item.added', 'response_id': response_id, 'output_index': 1, 'item': {'type': 'function_call', 'id': f'fc_{uuid.uuid4().hex[:8]}', 'call_id': call_id, 'name': fc.get('name'), 'arguments': json.dumps(fc.get('args', {}))}})}\n\n"
                                                yield f"data: {json.dumps({'type': 'response.output_item.done', 'response_id': response_id, 'output_index': 1, 'item': {'type': 'function_call', 'id': f'fc_{uuid.uuid4().hex[:8]}', 'call_id': call_id, 'name': fc.get('name'), 'arguments': json.dumps(fc.get('args', {}))}})}\n\n"
                                except Exception:
                                    pass
            except Exception as e:
                yield f"data: {json.dumps({'type': 'error', 'error': {'code': 'connection_error', 'message': str(e)}})}\n\n"
                
        # 4. Final completion events
        yield f"data: {json.dumps({'type': 'response.output_item.done', 'response_id': response_id, 'output_index': 0, 'item': {'type': 'message', 'id': msg_id, 'role': 'assistant', 'status': 'completed', 'content': []}})}\n\n"
        yield f"data: {json.dumps({'type': 'response.done', 'response': {'id': response_id, 'object': 'response', 'status': 'completed'}})}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(sse_generator(), media_type="text/event-stream")
