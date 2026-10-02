"""Synthetic fault injection: never connect to a real provider or user store."""
import asyncio
from copy import deepcopy
import json
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import HTTPException
from starlette.requests import ClientDisconnect

from codex_antigravity_auth import request_budget as budgets, server

ROUTES = ["google", "byok", "openai", "openai_oauth"]
NATIVE = {"id": "resp_fixture", "status": "completed", "output": [{"type": "message", "role": "assistant", "id": "msg_fixture",
          "status": "completed", "content": [{"type": "output_text", "text": "fixture"}]}]}


class Request:
    def __init__(self, route, stream=False):
        self.payload = {"model": "fixture:model" if route == "byok" else "gemini-3.8-flash", "input": "fixture", "stream": stream}
        self.disconnected = False
    async def json(self):
        return deepcopy(self.payload)
    async def is_disconnected(self):
        return self.disconnected


@pytest.fixture
def setup_route(monkeypatch):
    def setup(route, timeout=0.08):
        monkeypatch.setattr(server, "GOOGLE_BACKEND_TIMEOUT_SECONDS", timeout)
        monkeypatch.setattr(server, "STREAM_IDLE_TIMEOUT_SECONDS", timeout)
        monkeypatch.setattr(server, "STREAM_TOTAL_TIMEOUT_SECONDS", 1.0)
        monkeypatch.setattr(budgets, "DRAIN_SECONDS", 0.015)
        monkeypatch.setattr(budgets, "CLEANUP_SECONDS", 0.08)
        monkeypatch.setattr(budgets, "DISCONNECT_POLL_SECONDS", 0.005)
        monkeypatch.setattr(server, "is_unified_mode_enabled", lambda: True)
        monkeypatch.setattr(server, "classify_route", lambda *a, **kw: "openai" if route.startswith("openai") else route)
        monkeypatch.setattr(server, "schedule_refresh_accounts_ahead", lambda **kw: False)
        monkeypatch.setattr(server, "resolve_openai_auth", lambda: SimpleNamespace(kind="codex_oauth" if route == "openai_oauth" else "api_key"))
        monkeypatch.setattr(server, "openai_responses_url", lambda auth: "https://example.invalid/responses")
        monkeypatch.setattr(server, "openai_request_headers", lambda auth: {"Authorization": "Bearer synthetic-only"})
        monkeypatch.setattr(server, "all_provider_configs", lambda: {"fixture": {"id": "fixture", "kind": "openai_chat",
            "baseUrl": "https://example.invalid/v1", "apiKey": "synthetic-only", "models": ["model"]}})
        acquire = AsyncMock(return_value={"email": "fixture@example.invalid", "accessToken": "synthetic-only", "projectId": "fixture"})
        release = AsyncMock()
        monkeypatch.setattr(server, "acquire_active_account_for_request", acquire)
        monkeypatch.setattr(server, "release_account_for_request", release)
        monkeypatch.setattr(server, "record_attempt_outcome", AsyncMock())
        records = []
        monkeypatch.setattr(server, "write_request_record", lambda row: records.append(deepcopy(row)))
        return SimpleNamespace(acquire=acquire, release=release, records=records)
    return setup


def success_response(route):
    if route == "google":
        return httpx.Response(200, json={"response": {"candidates": [{"content": {"parts": [{"text": "fixture"}]}, "finishReason": "STOP"}]}})
    if route == "byok":
        return httpx.Response(200, json={"choices": [{"index": 0, "message": {"content": "fixture"}, "finish_reason": "stop"}]})
    if route == "openai_oauth":
        return httpx.Response(200, content=("data: " + json.dumps({"type": "response.completed", "response": NATIVE}) + "\n\n").encode())
    return httpx.Response(200, json=NATIVE)


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("reason", ["deadline", "disconnect"])
def test_nonstream_http_body_is_bounded_and_client_and_lease_close_once(monkeypatch, setup_route, route, reason):
    state = setup_route(route)
    clients = []
    async def scenario():
        entered = asyncio.Event()
        class Client:
            def __init__(self, **kw):
                self.closed = 0; clients.append(self)
            async def __aenter__(self): return self
            async def __aexit__(self, *args): self.closed += 1
            async def post(self, *args, **kwargs):
                entered.set()
                await asyncio.Future()
        monkeypatch.setattr(server.httpx, "AsyncClient", Client)
        request = Request(route)
        task = asyncio.create_task(server.create_response(request))
        await entered.wait()
        if reason == "disconnect": request.disconnected = True
        with pytest.raises(HTTPException if reason == "deadline" else ClientDisconnect) as caught:
            await asyncio.wait_for(task, 0.8)
        if reason == "deadline": assert caught.value.status_code == 504
    asyncio.run(scenario())
    assert clients and all(client.closed == 1 for client in clients)
    assert state.release.await_count == (1 if route == "google" else 0)
    terminals = [row for row in state.records if row["lifecycle_phase"] == "terminal"]
    assert len(terminals) == 1
    assert terminals[0]["error_class"] == ("request_deadline_exceeded" if reason == "deadline" else "cancelled")


@pytest.mark.parametrize("route,preparation", [("openai", "resolve_openai_auth"), ("byok", "all_provider_configs"),
                                               ("google", "response_model_id"), ("openai_oauth", "resolve_openai_auth")])
def test_slow_sync_preparation_returns_without_late_provider_dispatch(monkeypatch, setup_route, route, preparation):
    state = setup_route(route)
    entered, unblock = threading.Event(), threading.Event()
    original = getattr(server, preparation)
    def slow(*args, **kwargs):
        entered.set(); unblock.wait(1)
        return original(*args, **kwargs)
    monkeypatch.setattr(server, preparation, slow)
    clients = []
    monkeypatch.setattr(server.httpx, "AsyncClient", lambda **kw: clients.append(1))
    started = time.monotonic()
    try:
        with pytest.raises(HTTPException) as caught:
            asyncio.run(server.create_response(Request(route)))
        assert caught.value.status_code == 504 and time.monotonic() - started < 0.8
    finally:
        unblock.set()
    assert entered.is_set() and not clients
    assert not state.acquire.called


@pytest.mark.parametrize("route", ROUTES)
def test_slow_request_json_is_bounded_before_preparation(monkeypatch, setup_route, route):
    setup_route(route)
    request = Request(route)
    async def slow_json(): await asyncio.Future()
    request.json = slow_json
    with pytest.raises(HTTPException) as caught:
        asyncio.run(server.create_response(request))
    assert caught.value.status_code == 504


@pytest.mark.parametrize("route", ROUTES)
def test_blocked_diagnostic_write_does_not_hold_request_open(monkeypatch, setup_route, route):
    state = setup_route(route)
    unblock, entered = threading.Event(), threading.Event()
    class Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def post(self, *args, **kw): return success_response(route)
    monkeypatch.setattr(server.httpx, "AsyncClient", lambda **kw: Client())
    def slow(row): entered.set(); unblock.wait(1)
    monkeypatch.setattr(server, "write_request_record", slow)
    started = time.monotonic()
    try:
        with pytest.raises(HTTPException) as caught:
            asyncio.run(server.create_response(Request(route)))
        assert caught.value.status_code == 504
        assert time.monotonic() - started < 2.5
    finally: unblock.set()
    assert entered.is_set()
    assert state.release.await_count == (1 if route == "google" else 0)


def test_late_account_acquisition_is_released_without_dispatch(monkeypatch, setup_route):
    state = setup_route("google")
    async def scenario():
        finish = asyncio.Event()
        async def acquire(*args):
            await finish.wait()
            return {"email": "late@example.invalid", "accessToken": "synthetic-only"}
        monkeypatch.setattr(server, "acquire_active_account_for_request", acquire)
        with pytest.raises(HTTPException) as caught:
            await server.create_response(Request("google"))
        assert caught.value.status_code == 504
        finish.set()
        for _ in range(20):
            if state.release.await_count: break
            await asyncio.sleep(0.005)
        state.release.assert_awaited_once_with("late@example.invalid")
    asyncio.run(scenario())


def test_refresh_in_progress_is_retryable_and_logged_without_a_lease(monkeypatch, setup_route):
    state = setup_route("google")

    async def acquire(*_args):
        raise server.AccountRefreshInProgress()

    monkeypatch.setattr(server, "acquire_active_account_for_request", acquire)
    with pytest.raises(HTTPException) as caught:
        asyncio.run(server.create_response(Request("google")))

    assert caught.value.status_code == 503
    assert caught.value.headers["Retry-After"] == "1"
    assert state.release.await_count == 0
    assert state.records[-1]["http_status"] == 503
    assert state.records[-1]["error_class"] == "account_refresh_in_progress"


@pytest.mark.parametrize("phase", ["idle", "total"])
def test_stream_event_idle_and_total_policies_fail_once_without_replay(monkeypatch, phase):
    monkeypatch.setattr(budgets, "DRAIN_SECONDS", 0.01)
    async def scenario():
        budget = budgets.RequestBudget(Request("openai"), timeout=1, release_account=AsyncMock())
        budget.stream_idle = 0.03 if phase == "idle" else 0.2
        budget.deadline = time.monotonic() + (0.5 if phase == "idle" else 0.04)
        closed = []
        async def source():
            try:
                yield 'data: {"type":"response.output_text.delta","delta":"fixture"}\n\n'
                if phase == "idle": await asyncio.Future()
                else:
                    while True:
                        await asyncio.sleep(0.008)
                        yield 'data: {"type":"response.output_text.delta","delta":"x"}\n\n'
            finally: closed.append(True)
        chunks = [chunk async for chunk in budgets.stream_with_budget(source(), budget)]
        assert closed == [True]
        failures = [json.loads(chunk[6:]) for chunk in chunks if '"response.failed"' in chunk]
        assert len(failures) == 1
        assert failures[0]["response"]["error"]["code"] == ("stream_idle_timeout" if phase == "idle" else "request_deadline_exceeded")
        assert chunks.count("data: [DONE]\n\n") == 1
    asyncio.run(scenario())


def test_observed_terminal_is_not_replaced_when_only_trailing_diagnostics_stall(monkeypatch):
    monkeypatch.setattr(budgets, "DRAIN_SECONDS", 0.01)
    async def scenario():
        budget = budgets.RequestBudget(Request("openai"), timeout=1, release_account=AsyncMock())
        budget.stream_idle = 0.02
        async def source():
            yield "data: " + json.dumps({"type": "response.completed", "response": NATIVE}) + "\n\n"
            await asyncio.Future()
        chunks = [chunk async for chunk in budgets.stream_with_budget(source(), budget)]
        assert sum("response.completed" in chunk for chunk in chunks) == 1
        assert not any("response.failed" in chunk for chunk in chunks)
        assert chunks[-1] == "data: [DONE]\n\n"
    asyncio.run(scenario())


def stream_clients(monkeypatch, route, *, repeated=False, slow_connect=False):
    clients, contexts = [], []
    class Response:
        status_code = 200
        headers = {}
        async def aiter_bytes(self):
            index = 0
            while True:
                if route == "google":
                    payload = {"candidates": [{"content": {"parts": [{"text": "fixture"}]}}]}
                elif route == "byok":
                    payload = {"choices": [{"index": 0, "delta": {"content": "fixture"}}]}
                elif index == 0:
                    payload = {"type": "response.created", "response": {"id": "resp_fixture", "status": "in_progress", "output": []}}
                else:
                    payload = {"type": "response.output_text.delta", "delta": "fixture"}
                yield ("data: " + json.dumps(payload) + "\n\n").encode()
                index += 1
                if not repeated: await asyncio.Future()
                await asyncio.sleep(0.008)
    class Context:
        def __init__(self): self.closed = 0; contexts.append(self)
        async def __aenter__(self):
            if slow_connect: await asyncio.Future()
            return Response()
        async def __aexit__(self, *args): self.closed += 1
    class Client:
        def __init__(self, **kw): self.closed = 0; clients.append(self)
        async def __aenter__(self): return self
        async def __aexit__(self, *args): self.closed += 1
        async def aclose(self): self.closed += 1
        def stream(self, *args, **kw): return Context()
    monkeypatch.setattr(server.httpx, "AsyncClient", Client)
    return clients, contexts


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("policy", ["idle", "total"])
def test_route_stream_timeouts_preserve_partial_output_and_close_owned_resources(monkeypatch, setup_route, route, policy):
    state = setup_route(route, timeout=0.2)
    monkeypatch.setattr(server, "STREAM_IDLE_TIMEOUT_SECONDS", 0.04 if policy == "idle" else 0.3)
    monkeypatch.setattr(server, "STREAM_TOTAL_TIMEOUT_SECONDS", 1.0 if policy == "idle" else 0.1)
    clients, contexts = stream_clients(monkeypatch, route, repeated=policy == "total")
    async def scenario():
        response = await server.create_response(Request(route, stream=True))
        return [chunk async for chunk in response.body_iterator]
    chunks = asyncio.run(scenario())
    assert clients and all(client.closed == 1 for client in clients)
    assert contexts and all(context.closed == 1 for context in contexts)
    assert state.release.await_count == (1 if route == "google" else 0)
    events = [json.loads(line[6:]) for chunk in chunks for line in chunk.splitlines()
              if line.startswith("data: ") and line != "data: [DONE]"]
    terminal = [event for event in events if event.get("type") in {"response.completed", "response.incomplete", "response.failed"}]
    assert len(terminal) == 1 and terminal[0]["type"] == "response.failed"
    assert terminal[0]["response"]["error"]["code"] == ("stream_idle_timeout" if policy == "idle" else "request_deadline_exceeded")
    assert any(event["type"] == "response.created" for event in events)
    assert state.records[-1]["status"] == "failed"
    assert state.records[-1]["terminal_reason"] == terminal[0]["response"]["error"]["code"]
    assert state.acquire.await_count == (1 if route == "google" else 0)  # No timeout-driven rotation.


@pytest.mark.parametrize("route", ROUTES)
def test_disconnect_before_stream_iteration_releases_preacquired_resources(monkeypatch, setup_route, route):
    state = setup_route(route, timeout=0.2)
    clients, contexts = stream_clients(monkeypatch, route)
    async def scenario():
        response = await server.create_response(Request(route, stream=True))
        async def receive(): return {"type": "http.disconnect"}
        async def send(message): pass
        await response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)
    asyncio.run(scenario())
    assert all(client.closed == 1 for client in clients)
    assert all(context.closed == 1 for context in contexts)
    assert state.release.await_count == (1 if route == "google" else 0)
    assert state.records[-1]["cancelled"] is True


@pytest.mark.parametrize("route", ["openai", "openai_oauth"])
def test_native_stream_connect_deadline_closes_client_before_response_exists(monkeypatch, setup_route, route):
    setup_route(route)
    clients, contexts = stream_clients(monkeypatch, route, slow_connect=True)
    with pytest.raises(HTTPException) as caught:
        asyncio.run(server.create_response(Request(route, stream=True)))
    assert caught.value.status_code == 504
    assert len(clients) == 1 and clients[0].closed == 1
    assert contexts[0].closed == 0  # Context entry never produced an owned response.


def test_stream_timeout_metadata_is_validated_and_not_forwarded():
    request = {"model": "gemini-3.8-flash", "input": "fixture", "metadata": {
        "antigravity_request_timeout_seconds": 90,
        "antigravity_stream_idle_timeout_seconds": 180,
        "antigravity_stream_total_timeout_seconds": 3600,
    }}
    assert server.validate_response_request_body(deepcopy(request))["metadata"] == request["metadata"]
    for key in request["metadata"]:
        for value in (True, float("inf"), 0, "slow", 8000):
            bad = deepcopy(request); bad["metadata"][key] = value
            with pytest.raises(HTTPException): server.validate_response_request_body(bad)


def test_slow_close_is_bounded_and_other_owned_resources_are_attempted(monkeypatch):
    monkeypatch.setattr(budgets, "CLEANUP_SECONDS", 0.02)
    async def scenario():
        calls = []
        async def blocked():
            calls.append("blocked")
            await asyncio.Future()
        async def other(): calls.append("other")
        started = time.monotonic()
        await budgets.shielded_cleanup(blocked, other)
        assert time.monotonic() - started < 0.3
        assert sorted(calls) == ["blocked", "other"]
    asyncio.run(scenario())


def test_timeout_failure_uses_observed_native_response_id_without_created(monkeypatch):
    monkeypatch.setattr(budgets, "DRAIN_SECONDS", 0.01)
    async def scenario():
        budget = budgets.RequestBudget(Request("openai"), timeout=1, release_account=AsyncMock())
        budget.stream_idle = 0.02
        async def source():
            yield 'data: {"type":"response.output_text.delta","response_id":"resp_observed","delta":"fixture"}\n\n'
            await asyncio.Future()
        chunks = [chunk async for chunk in budgets.stream_with_budget(source(), budget)]
        assert json.loads(chunks[-2][6:])["response"]["id"] == "resp_observed"
    asyncio.run(scenario())


@pytest.mark.parametrize("route", ROUTES)
def test_downstream_backpressure_is_bounded_and_releases_ownership(monkeypatch, setup_route, route):
    state = setup_route(route, timeout=0.2)
    monkeypatch.setattr(server, "STREAM_TOTAL_TIMEOUT_SECONDS", 0.06)
    clients, contexts = stream_clients(monkeypatch, route, repeated=True)
    async def scenario():
        response = await server.create_response(Request(route, stream=True))
        async def receive(): await asyncio.Future()
        async def send(message):
            if message["type"] == "http.response.body": await asyncio.Future()
        started = time.monotonic()
        await asyncio.wait_for(response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send), 0.8)
        assert time.monotonic() - started < 0.8
    asyncio.run(scenario())
    assert all(client.closed == 1 for client in clients)
    assert all(context.closed == 1 for context in contexts)
    assert state.release.await_count == (1 if route == "google" else 0)
    assert state.records[-1]["status"] == "failed"
    assert state.records[-1]["terminal_reason"] == "request_deadline_exceeded"


@pytest.mark.parametrize("route", ROUTES)
def test_deadline_metadata_is_stripped_from_every_provider_payload(monkeypatch, setup_route, route):
    setup_route(route, timeout=0.2)
    payloads = []
    class Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def post(self, *args, **kwargs):
            payloads.append(kwargs["json"])
            return success_response(route)
    monkeypatch.setattr(server.httpx, "AsyncClient", lambda **kw: Client())
    request = Request(route)
    request.payload["metadata"] = {"antigravity_request_timeout_seconds": 10,
                                   "antigravity_stream_idle_timeout_seconds": 20,
                                   "antigravity_stream_total_timeout_seconds": 60}
    asyncio.run(server.create_response(request))
    assert payloads and "antigravity_stream" not in json.dumps(payloads)
    assert "antigravity_request_timeout_seconds" not in json.dumps(payloads)


@pytest.mark.parametrize("route", ROUTES)
def test_late_log_write_cannot_restore_success_after_deadline(monkeypatch, setup_route, route):
    state = setup_route(route)
    unblock = threading.Event()
    rows = []
    class Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def post(self, *args, **kw): return success_response(route)
    monkeypatch.setattr(server.httpx, "AsyncClient", lambda **kw: Client())
    def blocked_writer(row):
        unblock.wait(1)
        rows.append(deepcopy(row))
    monkeypatch.setattr(server, "write_request_record", blocked_writer)
    async def scenario():
        with pytest.raises(HTTPException) as caught:
            await server.create_response(Request(route))
        assert caught.value.status_code == 504
        unblock.set()
        for _ in range(100):
            if any(row.get("terminal_reason") == "request_deadline_exceeded" for row in rows):
                await asyncio.sleep(0.02)
                break
            await asyncio.sleep(0.005)
    try: asyncio.run(scenario())
    finally: unblock.set()
    assert rows[-1]["status"] == "failed" and rows[-1]["terminal_reason"] == "request_deadline_exceeded"
    assert state.release.await_count == (1 if route == "google" else 0)


@pytest.mark.parametrize("route", ROUTES)
def test_disconnect_after_partial_stream_closes_resources_and_logs_once(monkeypatch, setup_route, route):
    state = setup_route(route, timeout=0.2)
    clients, contexts = stream_clients(monkeypatch, route)
    async def scenario():
        response = await server.create_response(Request(route, stream=True))
        visible = asyncio.Event()
        async def receive():
            await visible.wait()
            return {"type": "http.disconnect"}
        async def send(message):
            if message["type"] == "http.response.body" and message.get("body"):
                visible.set()
        await asyncio.wait_for(response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send), 0.8)
    asyncio.run(scenario())
    assert all(client.closed == 1 for client in clients)
    assert all(context.closed == 1 for context in contexts)
    assert state.release.await_count == (1 if route == "google" else 0)
    terminal = [row for row in state.records if row["lifecycle_phase"] == "terminal"]
    assert len(terminal) == 1 and terminal[0]["cancelled"] is True


@pytest.mark.parametrize("route", ["openai", "openai_oauth"])
def test_native_open_stream_is_closed_if_start_diagnostic_expires(monkeypatch, setup_route, route):
    setup_route(route)
    clients, contexts = stream_clients(monkeypatch, route)
    unblock = threading.Event()
    monkeypatch.setattr(server, "write_request_record", lambda row: unblock.wait(1))
    try:
        with pytest.raises(HTTPException) as caught:
            asyncio.run(server.create_response(Request(route, stream=True)))
        assert caught.value.status_code == 504
    finally:
        unblock.set()
    assert clients[0].closed == contexts[0].closed == 1


def test_preparation_failure_after_account_acquisition_releases_lease(monkeypatch, setup_route):
    state = setup_route("google")
    unblock = threading.Event()
    monkeypatch.setattr(server, "get_platform", lambda: (unblock.wait(1), "fixture")[1])
    try:
        with pytest.raises(HTTPException) as caught:
            asyncio.run(server.create_response(Request("google")))
        assert caught.value.status_code == 504
    finally: unblock.set()
    assert state.acquire.await_count == state.release.await_count == 1


@pytest.mark.parametrize("route", ROUTES)
def test_already_disconnected_request_never_acquires_or_resolves_auth(monkeypatch, setup_route, route):
    state = setup_route(route)
    auth = []
    monkeypatch.setattr(server, "resolve_openai_auth", lambda: auth.append(True))
    request = Request(route); request.disconnected = True
    with pytest.raises(ClientDisconnect):
        asyncio.run(server.create_response(request))
    assert not state.acquire.called and not auth


def test_late_native_connect_result_is_closed_without_being_transferred(monkeypatch, setup_route):
    setup_route("openai")
    closed = []
    async def scenario():
        finish = asyncio.Event()
        class Context:
            async def __aenter__(self):
                try: await finish.wait()
                except asyncio.CancelledError: await finish.wait()
                return SimpleNamespace(status_code=200)
            async def __aexit__(self, *args): closed.append("response")
        class Client:
            def stream(self, *args, **kw): return Context()
            async def aclose(self): closed.append("client")
        monkeypatch.setattr(server.httpx, "AsyncClient", lambda **kw: Client())
        with pytest.raises(HTTPException) as caught:
            await server.create_response(Request("openai", stream=True))
        assert caught.value.status_code == 504 and closed == ["client"]
        finish.set()
        for _ in range(50):
            if len(closed) == 2: break
            await asyncio.sleep(0.005)
        assert closed == ["client", "response"]
    asyncio.run(scenario())


@pytest.mark.parametrize("route,preparation", [("openai", "resolve_openai_auth"), ("openai_oauth", "resolve_openai_auth"),
                                               ("byok", "all_provider_configs"), ("google", "native_model_capabilities")])
def test_public_stream_total_expires_during_preparation_before_http_200(monkeypatch, setup_route, route, preparation):
    state = setup_route(route, timeout=3)
    unblock = threading.Event()
    original = getattr(server, preparation)
    def slow(*args, **kwargs):
        unblock.wait(2)
        return original(*args, **kwargs)
    monkeypatch.setattr(server, preparation, slow)
    clients, contexts = stream_clients(monkeypatch, route)
    request = Request(route, stream=True)
    request.payload["metadata"] = {"antigravity_request_timeout_seconds": 3,
                                   "antigravity_stream_total_timeout_seconds": 1}
    started = time.monotonic()
    try:
        with pytest.raises(HTTPException) as caught:
            asyncio.run(server.create_response(request))
        assert caught.value.status_code == 504
        assert time.monotonic() - started < 1.8
    finally:
        unblock.set()
    assert not clients and not contexts and not state.acquire.called


@pytest.mark.parametrize("route", ROUTES)
def test_total_expiry_after_preparation_handoff_sends_504_without_sse_headers(monkeypatch, setup_route, route):
    state = setup_route(route, timeout=0.3)
    clients, contexts = stream_clients(monkeypatch, route)
    async def scenario():
        response = await server.create_response(Request(route, stream=True))
        response.budget.deadline = time.monotonic() - 0.001  # scheduler delay before ASGI response execution
        sent = []
        async def receive(): await asyncio.Future()
        async def send(message): sent.append(message)
        await response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)
        assert [message["status"] for message in sent if message["type"] == "http.response.start"] == [504]
        assert not any(b"data:" in message.get("body", b"") for message in sent)
    asyncio.run(scenario())
    assert all(client.closed == 1 for client in clients)
    assert all(context.closed == 1 for context in contexts)
    assert state.release.await_count == (1 if route == "google" else 0)
    assert state.records[-1]["terminal_reason"] == "request_deadline_exceeded"


@pytest.mark.parametrize("route", ["google", "openai", "openai_oauth"])
def test_short_total_during_owned_preparation_releases_resources(monkeypatch, setup_route, route):
    state = setup_route(route, timeout=0.3)
    monkeypatch.setattr(server, "STREAM_TOTAL_TIMEOUT_SECONDS", 0.04)
    clients, contexts = stream_clients(monkeypatch, route)
    unblock = threading.Event()
    if route == "google":
        monkeypatch.setattr(server, "get_platform", lambda: (unblock.wait(1), "fixture")[1])
    else:
        monkeypatch.setattr(server, "write_request_record", lambda row: unblock.wait(1))
    started = time.monotonic()
    try:
        with pytest.raises(HTTPException) as caught:
            asyncio.run(server.create_response(Request(route, stream=True)))
        assert caught.value.status_code == 504 and time.monotonic() - started < 0.25
    finally: unblock.set()
    assert all(client.closed == 1 for client in clients)
    assert all(context.closed == 1 for context in contexts)
    assert state.release.await_count == (1 if route == "google" else 0)


def test_stream_handoff_never_extends_an_expired_preparation_budget():
    budget = budgets.RequestBudget(Request("google"), timeout=1, release_account=AsyncMock())
    budget.deadline = time.monotonic() - 0.001
    budget.stream_total = 1800
    with pytest.raises(budgets.RequestDeadlineExceeded): budget.start_stream()
    assert budget.failure_code == "request_deadline_exceeded"


def test_stream_total_override_does_not_limit_nonstream_preparation(monkeypatch, setup_route):
    setup_route("openai", timeout=3)
    original = server.resolve_openai_auth
    monkeypatch.setattr(server, "resolve_openai_auth", lambda: (time.sleep(1.05), original())[1])
    class Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def post(self, *args, **kw): return success_response("openai")
    monkeypatch.setattr(server.httpx, "AsyncClient", lambda **kw: Client())
    request = Request("openai")
    request.payload["metadata"] = {"antigravity_request_timeout_seconds": 3,
                                   "antigravity_stream_total_timeout_seconds": 1}
    result = asyncio.run(server.create_response(request))
    assert result["status"] == "completed"


def test_expiry_between_header_check_and_dispatch_cannot_use_failure_grace(monkeypatch, setup_route):
    setup_route("openai", timeout=0.3)
    clients, contexts = stream_clients(monkeypatch, "openai")
    async def scenario():
        response = await server.create_response(Request("openai", stream=True))
        original = response.budget.run
        first = True
        async def delayed_dispatch(factory, **kwargs):
            nonlocal first
            if first:
                first = False
                response.budget.deadline = time.monotonic() - 0.001
            return await original(factory, **kwargs)
        response.budget.run = delayed_dispatch
        sent = []
        async def receive(): await asyncio.Future()
        async def send(message): sent.append(message)
        await response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)
        assert [message["status"] for message in sent if message["type"] == "http.response.start"] == [504]
    asyncio.run(scenario())
    assert clients[0].closed == contexts[0].closed == 1
