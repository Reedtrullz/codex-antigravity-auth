"""Real ASGI -> HTTP -> adapter -> terminal/telemetry, using synthetic identity."""
import asyncio
import json
import os
import socket
import subprocess
import sys

import httpx
import pytest
from fastapi.testclient import TestClient

from codex_antigravity_auth import server
from codex_antigravity_auth.observability import request_log_path
from _test_isolation import expected_denial
from fake_upstream import frames, split_bytes, upstream


def configure_chat(monkeypatch, base):
    monkeypatch.setattr(server, "all_provider_configs", lambda: {"fixture": {
        "id": "fixture", "kind": "openai_chat", "baseUrl": base + "/v1",
        "apiKey": "synthetic-test-key", "models": ["text-model"],
    }})


@pytest.mark.parametrize("seed", [0, 1, 17])
@pytest.mark.parametrize("finish,delta,terminal", [
    ("stop", {"content": "hello"}, "completed"),
    ("length", {"content": "partial"}, "incomplete"),
    ("content_filter", {"refusal": "declined"}, "completed"),
])
def test_chat_http_replay(monkeypatch, seed, finish, delta, terminal):
    wire = frames({"choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
                  {"choices": [{"index": 0, "delta": {}, "finish_reason": finish}], "usage": {"prompt_tokens": 2, "completion_tokens": 1}}, "[DONE]")
    with upstream((200, {"Content-Type": "text/event-stream"}, split_bytes(wire, seed))) as (base, requests):
        configure_chat(monkeypatch, base)
        response = TestClient(server.app).post("/v1/responses", json={"model": "fixture:text-model", "input": "hello", "stream": True})
    assert response.status_code == 200
    events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ") and line != "data: [DONE]"]
    terminals = [event for event in events if event.get("type") in {"response.completed", "response.failed", "response.incomplete"}]
    assert [e["type"] for e in terminals] == ["response." + terminal]
    terminal_response = terminals[0]["response"]
    if finish == "content_filter":
        assert terminal_response["output"][0]["content"] == [{"type": "refusal", "refusal": "The provider declined this response (CONTENT_FILTER)."}]
    elif finish == "length":
        assert terminal_response["incomplete_details"]["reason"] == "max_output_tokens"
    assert requests[0]["path"] == "/v1/chat/completions"
    assert requests[0]["body"]["messages"] == [{"role": "user", "content": "hello"}]
    assert requests[0]["headers"]["Authorization"] == "Bearer synthetic-test-key"
    records = [json.loads(line) for line in request_log_path().read_text().splitlines()]
    assert records[-1]["status"] == ("success" if terminal == "completed" else "incomplete")
    assert "synthetic-test-key" not in request_log_path().read_text()


@pytest.mark.parametrize("wire", [b"data: {broken}\n\n", frames({"choices": [{"delta": {"content": "partial"}}]})])
def test_partial_or_malformed_stream_never_succeeds(monkeypatch, wire):
    with upstream((200, {"Content-Type": "text/event-stream"}, split_bytes(wire))) as (base, _):
        configure_chat(monkeypatch, base)
        response = TestClient(server.app).post("/v1/responses", json={"model": "fixture:text-model", "input": "hello", "stream": True})
    assert '"type": "response.completed"' not in response.text
    assert '"type": "response.failed"' in response.text


def test_unowned_loopback_and_external_network_are_denied():
    for endpoint in [("127.0.0.1", 51122), ("203.0.113.1", 443)]:
        with expected_denial(), socket.socket() as sock, pytest.raises(AssertionError, match="not an owned fixture"):
            sock.connect(endpoint)
    with expected_denial(), pytest.raises(AssertionError, match="DNS denied"):
        socket.getaddrinfo("example.invalid", 443)


@pytest.mark.parametrize("environment", [{}, {"OPENAI_API_KEY": "synthetic-canary", "CODEX_HOME": "/nonexistent-test-state"}])
def test_python_child_inherits_isolation_even_with_empty_env(environment):
    code = """
import os, socket, keyring
from _test_isolation import expected_denial
assert os.environ.get('OPENAI_API_KEY') is None
assert os.environ.get('CODEX_HOME') is None
assert os.environ['ANTIGRAVITY_TEST_ROOT']
import sys
assert os.environ['HOME'] == sys.argv[1]
try:
    with expected_denial():
        socket.create_connection(('127.0.0.1', 51122))
except AssertionError:
    pass
else:
    raise AssertionError('network escaped')
try:
    keyring.get_password('synthetic-service', 'synthetic-user')
except keyring.errors.NoKeyringError:
    pass
else:
    raise AssertionError('keyring escaped')
"""
    result = subprocess.run([sys.executable, "-c", code, os.environ["HOME"]], env=environment, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_tool_round_trip_uses_real_http(monkeypatch):
    first = {"choices": [{"finish_reason": "tool_calls", "message": {"tool_calls": [{"id": "call_fixture", "type": "function", "function": {"name": "lookup", "arguments": "{}"}}]}}]}
    second = {"choices": [{"finish_reason": "stop", "message": {"content": "answer"}}]}
    replies = [(200, {"Content-Type": "application/json"}, json.dumps(value).encode()) for value in [first, second]]
    with upstream(*replies) as (base, requests):
        configure_chat(monkeypatch, base)
        client = TestClient(server.app)
        response = client.post("/v1/responses", json={"model": "fixture:text-model", "input": "lookup", "tools": [{"type": "function", "name": "lookup", "parameters": {"type": "object"}}]}).json()
        call = response["output"][0]
        result = client.post("/v1/responses", json={"model": "fixture:text-model", "input": [call, {"type": "function_call_output", "call_id": call["call_id"], "output": "fixture-result"}]}).json()
    assert result["status"] == "completed"
    assert requests[1]["body"]["messages"][-1] == {"role": "tool", "tool_call_id": "call_fixture", "content": "fixture-result", "name": "lookup"}


def test_google_refresh_and_generation_use_synthetic_store_and_http(monkeypatch):
    from codex_antigravity_auth import oauth, google_transport, accounts, storage
    from urllib.parse import parse_qs
    refresh = {"access_token": "refreshed-synthetic-token", "expires_in": 3600}
    generation = {"response": {"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": "fixture answer", "thoughtSignature": "opaque"}]}}]}}
    replies = [(200, {"Content-Type": "application/json"}, json.dumps(value).encode()) for value in [refresh, generation]]
    with upstream(*replies) as (base, requests):
        # Replace only endpoint discovery; run real form HTTP, refresh, encrypted
        # storage, account selection, transport, normalization and logging.
        original_form = oauth.post_form
        monkeypatch.setattr(oauth, "post_form", lambda _url, fields: original_form(base + "/token", fields))
        monkeypatch.setenv("ANTIGRAVITY_CLIENT_ID", "synthetic-client")
        monkeypatch.setenv("ANTIGRAVITY_CLIENT_SECRET", "synthetic-secret")
        original_transport = google_transport.GoogleTransport
        monkeypatch.setattr(server, "GoogleTransport", lambda **kw: original_transport(endpoint=base, **kw))
        manager = accounts.AccountManager()
        monkeypatch.setattr(server, "account_manager", manager)
        storage.save_accounts({"accounts": [{"email": "fixture@example.invalid", "refreshToken": "synthetic-refresh", "accessToken": "expired-synthetic", "expiresAt": 1, "projectId": "fixture-project"}]})
        response = TestClient(server.app).post("/v1/responses", json={"model": "gemini-3.8-flash", "input": "hello"})
    assert response.status_code == 200
    assert response.json()["output"][0]["content"][0]["text"] == "fixture answer"
    assert parse_qs(requests[0]["body"])["refresh_token"] == ["synthetic-refresh"]
    assert requests[1]["headers"]["Authorization"] == "Bearer refreshed-synthetic-token"
    assert not manager._in_flight
    assert storage.load_accounts()["accounts"][0]["accessToken"] == "refreshed-synthetic-token"


def test_all_byte_splits_preserve_chat_sse_unicode():
    from codex_antigravity_auth.openai_transport import iter_sse_data
    wire = 'data: {"choices":[{"delta":{"content":"hé🙂"}}]}\n\ndata: [DONE]\n\n'.encode()

    async def replay(split):
        class Bytes(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield wire[:split]
                yield wire[split:]
        response = httpx.Response(200, stream=Bytes())
        try:
            return [event async for event in iter_sse_data(response, label="fixture")]
        finally:
            await response.aclose()

    for split in range(1, len(wire)):
        events = asyncio.run(replay(split))
        assert json.loads(events[0])["choices"][0]["delta"]["content"] == "hé🙂"
        assert events[-1] == "[DONE]"


def test_google_retry_after_rotates_and_persists_cooldown(monkeypatch):
    from types import SimpleNamespace
    from codex_antigravity_auth import accounts, storage, google_transport
    now = 2_000_000_000.0
    monkeypatch.setattr(accounts, "time", SimpleNamespace(time=lambda: now))
    generation = {"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": "second account"}]}}]}
    with upstream((429, {"Retry-After": "600"}, b'{"error":{"message":"rate limit"}}'),
                  (200, {"Content-Type": "application/json"}, json.dumps(generation).encode())) as (base, requests):
        monkeypatch.setattr(server, "GoogleTransport", lambda **kw: google_transport.GoogleTransport(endpoint=base, **kw))
        manager = accounts.AccountManager()
        monkeypatch.setattr(server, "account_manager", manager)
        storage.save_accounts({"accounts": [{"email": f"fixture-{i}@example.invalid", "accessToken": f"synthetic-{i}", "expiresAt": now + 3600, "projectId": "fixture-project"} for i in range(2)]})
        response = TestClient(server.app).post("/v1/responses", json={"model": "gemini-3.8-flash", "input": "hello"})
    assert response.status_code == 200
    assert len(requests) == 2
    assert requests[0]["headers"]["Authorization"] != requests[1]["headers"]["Authorization"]
    assert not manager._in_flight
    cooldowns = storage.load_accounts()["accountState"]["cooldowns"]
    assert any(value.get("gemini", 0) == now + 600 for value in cooldowns.values())
    records = [json.loads(line) for line in request_log_path().read_text().splitlines()]
    assert records[-1]["rotation_attempted"] is True
    # Successful-rotation telemetry currently drops retry_after_source (#37);
    # the exact persisted expiry above proves the header affected real policy.


def test_google_http_stream_disconnect_releases_lease(monkeypatch):
    from codex_antigravity_auth import accounts, storage, google_transport
    from starlette.requests import Request
    import time
    wire = frames({"candidates": [{"content": {"parts": [{"text": "partial"}]}}]})
    with upstream((200, {"Content-Type": "text/event-stream"}, wire)) as (base, requests):
        monkeypatch.setattr(server, "GoogleTransport", lambda **kw: google_transport.GoogleTransport(endpoint=base, **kw))
        manager = accounts.AccountManager()
        monkeypatch.setattr(server, "account_manager", manager)
        storage.save_accounts({"accounts": [{"email": "cancel@example.invalid", "accessToken": "synthetic-token", "expiresAt": time.time() + 3600, "projectId": "fixture-project"}]})

        async def scenario():
            body = json.dumps({"model": "gemini-3.8-flash", "input": "hello", "stream": True}).encode()
            async def receive_request():
                return {"type": "http.request", "body": body, "more_body": False}
            scope = {"type": "http", "method": "POST", "path": "/v1/responses", "headers": [], "query_string": b"", "client": ("127.0.0.1", 1), "server": ("127.0.0.1", 80), "scheme": "http"}
            request = Request(scope, receive_request)
            response = await server.create_response(request)
            started = asyncio.Event()
            async def receive_disconnect():
                await started.wait()
                return {"type": "http.disconnect"}
            async def send(message):
                if message["type"] == "http.response.body" and b"response.created" in message.get("body", b""):
                    started.set()
                    await asyncio.sleep(0.05)
            await asyncio.wait_for(response(scope, receive_disconnect, send), 3)
        asyncio.run(scenario())
    assert len(requests) == 1
    assert not manager._in_flight
    records = [json.loads(line) for line in request_log_path().read_text().splitlines()]
    assert records[-1]["cancelled"] is True


def test_protected_file_reads_and_mutations_are_denied_with_synthetic_canary(tmp_path):
    from _test_isolation import _protected_paths
    private = tmp_path / "fixture-credentials"
    private.mkdir()
    canary = private / "credential.json"
    canary.write_text("synthetic-canary")
    replacement = tmp_path / "replacement"
    replacement.write_text("synthetic-replacement")
    _protected_paths.append(private)
    try:
        for operation in (lambda: canary.read_text(), lambda: canary.unlink(),
                          lambda: os.replace(replacement, canary), lambda: canary.chmod(0o777)):
            with expected_denial(), pytest.raises(AssertionError, match="credential/configuration path"):
                operation()
    finally:
        _protected_paths.remove(private)
    assert canary.read_text() == "synthetic-canary"
    assert replacement.read_text() == "synthetic-replacement"


def test_google_parser_all_byte_splits_preserve_terminal_and_unicode():
    from codex_antigravity_auth.google_transport import GoogleTransport, AccountLease
    wire = ('data: {"candidates":[{"finishReason":"STOP","content":{"parts":[{"text":"hé🙂"}]}}]}\n\n').encode()

    async def replay(split):
        class Bytes(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield wire[:split]
                yield wire[split:]
        def client_factory(**kwargs):
            return httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=Bytes())), **kwargs)
        transport = GoogleTransport(timeout=1, client_factory=client_factory)
        return [event async for event in transport.stream_events({"model": "gemini-3.8-flash", "input": "hi"}, AccountLease("synthetic", "project", "synthetic"), response_id="resp_fixture", display_model="fixture")]
    for split in range(1, len(wire)):
        events = asyncio.run(replay(split))
        terminal = next(event for event in events if isinstance(event, dict) and event.get("type") == "response.completed")
        assert terminal["response"]["output"][0]["content"][0]["text"] == "hé🙂"


def test_native_parser_unicode_byte_split_contract():
    from codex_antigravity_auth.openai_transport import NativeResponsesStreamAdapter
    wire = 'data: {"type":"response.output_text.delta","delta":"hé🙂"}\n\n'.encode()
    for split in range(1, len(wire)):
        adapter = NativeResponsesStreamAdapter(display_model="fixture")
        events = adapter.consume_bytes(wire[:split]) + adapter.consume_bytes(wire[split:])
        assert events[0]["delta"] == "hé🙂"


def test_windows_style_socketpair_uses_an_owned_listener():
    from _test_isolation import guarded_socketpair, _allowed_endpoints
    before = set(_allowed_endpoints)
    left, right = guarded_socketpair()
    try:
        left.sendall(b"synthetic")
        assert right.recv(9) == b"synthetic"
        assert _allowed_endpoints == before
    finally:
        left.close()
        right.close()
