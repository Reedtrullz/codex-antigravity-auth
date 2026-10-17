"""Synthetic fault injection: never connect to a real provider or user store."""
import asyncio
from copy import deepcopy
import json
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import HTTPException
from starlette.requests import ClientDisconnect

from codex_antigravity_auth import request_budget as budgets, server
from codex_antigravity_auth.account_binding import GATEWAY_INSTANCE
from deadline_injection import HANG_GUARD_SECONDS, DiagnosticWriteDeadline, budget_deadline_on_entry

# Timing policy: a short deadline under test is armed when the fixture reaches
# the phase under test (see test_support/deadline_injection.py), never at
# request start, and fixtures whose subject is not the deadline get a generous
# GENEROUS_TIMEOUT. Wall-clock bounds only separate "bounded" from "waited for
# the blocked operation" and keep a wide margin; hang guards use
# HANG_GUARD_SECONDS. Scheduler stalls on loaded runners must not change which
# phase a test exercises.
GENEROUS_TIMEOUT = 5
# Blocked fixtures hold out at least this long, so a wall-clock bound below it
# still proves the request did not wait for them.
BLOCK_SECONDS = 10

ROUTES = ["google", "byok", "openai", "openai_oauth"]
NATIVE = {"id": "resp_fixture", "status": "completed", "output": [{"type": "message", "role": "assistant", "id": "msg_fixture",
          "status": "completed", "content": [{"type": "output_text", "text": "fixture"}]}]}


class Request:
    def __init__(self, route, stream=False):
        self.payload = {"model": "fixture:model" if route == "byok" else "gemini-3.8-flash", "input": "fixture", "stream": stream}
        self.disconnected = False
        self.headers = {}
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
        # Cleanup, drain and abort-diagnostic bounds only cut off stalled
        # fixtures. With the production-like 15-80ms values, a scheduler stall
        # could cut off a fast terminal write or close and the test then saw
        # zero or two terminal rows. Tests whose subject is a stalled close or
        # drain set a short bound themselves.
        monkeypatch.setattr(budgets, "DRAIN_SECONDS", HANG_GUARD_SECONDS)
        monkeypatch.setattr(budgets, "CLEANUP_SECONDS", HANG_GUARD_SECONDS)
        monkeypatch.setattr(server, "ABORT_DIAGNOSTIC_SECONDS", HANG_GUARD_SECONDS)
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


def deadline_on_phase_entry(monkeypatch, timeout=0.08):
    return budget_deadline_on_entry(monkeypatch, timeout)


async def eventually(predicate, timeout=HANG_GUARD_SECONDS):
    """Poll a late-cleanup observation without a tight wall-clock window."""
    stop = time.monotonic() + timeout
    while not predicate() and time.monotonic() < stop:
        await asyncio.sleep(0.005)
    return predicate()


# Hang guard for the nonstream fixture; its self-test below shortens it.
PROVIDER_ENTRY_TIMEOUT = HANG_GUARD_SECONDS


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("reason", ["deadline", "disconnect"])
def test_nonstream_http_body_is_bounded_and_client_and_lease_close_once(monkeypatch, setup_route, route, reason):
    state = setup_route(route, timeout=GENEROUS_TIMEOUT)
    clients = []
    async def scenario():
        entered = asyncio.Event()
        arm_deadline = deadline_on_phase_entry(monkeypatch) if reason == "deadline" else None
        class Client:
            def __init__(self, **kw):
                self.closed = 0; clients.append(self)
            async def __aenter__(self): return self
            async def __aexit__(self, *args): self.closed += 1
            async def post(self, *args, **kwargs):
                if arm_deadline is not None:
                    arm_deadline()
                entered.set()
                await asyncio.Future()
        monkeypatch.setattr(server.httpx, "AsyncClient", Client)
        request = Request(route)
        task = asyncio.create_task(server.create_response(request))
        entry = asyncio.create_task(entered.wait())
        try:
            await asyncio.wait({entry, task}, timeout=PROVIDER_ENTRY_TIMEOUT, return_when=asyncio.FIRST_COMPLETED)
            if not entered.is_set():
                if task.done():
                    await task
                    raise AssertionError("request completed before provider entry")
                raise TimeoutError("timed out waiting for provider entry")
            if reason == "disconnect": request.disconnected = True
            with pytest.raises(HTTPException if reason == "deadline" else ClientDisconnect) as caught:
                await asyncio.wait_for(task, HANG_GUARD_SECONDS)
            if reason == "deadline": assert caught.value.status_code == 504
        finally:
            entry.cancel()
            task.cancel()
            await asyncio.gather(entry, task, return_exceptions=True)
    asyncio.run(scenario())
    assert clients and all(client.closed == 1 for client in clients)
    assert state.release.await_count == (1 if route == "google" else 0)
    terminals = [row for row in state.records if row["lifecycle_phase"] == "terminal"]
    assert len(terminals) == 1
    assert terminals[0]["error_class"] == ("request_deadline_exceeded" if reason == "deadline" else "cancelled")


@pytest.mark.parametrize("outcome", ["deadline", "completed", "never_entered"])
def test_nonstream_deadline_scenario_cannot_hang_before_provider_entry(monkeypatch, setup_route, outcome):
    original_run = asyncio.run
    failure = HTTPException(504, "synthetic pre-provider deadline")
    cleaned = []
    async def before_provider(request):
        try:
            if outcome == "deadline":
                raise failure
            if outcome == "completed":
                return object()
            await asyncio.Future()
        finally:
            cleaned.append(True)
    def bounded_run(coro):
        async def bounded():
            return await asyncio.wait_for(coro, HANG_GUARD_SECONDS)
        return original_run(bounded())
    # This tests the fixture's own provider-entry guard, so keep it short here.
    monkeypatch.setattr(sys.modules[__name__], "PROVIDER_ENTRY_TIMEOUT", 0.3)
    monkeypatch.setattr(server, "create_response", before_provider)
    monkeypatch.setattr(asyncio, "run", bounded_run)
    if outcome == "deadline":
        with pytest.raises(HTTPException) as caught:
            test_nonstream_http_body_is_bounded_and_client_and_lease_close_once(monkeypatch, setup_route, "google", "deadline")
        assert caught.value is failure
    else:
        expected = AssertionError if outcome == "completed" else TimeoutError
        with pytest.raises(expected, match="provider entry"):
            test_nonstream_http_body_is_bounded_and_client_and_lease_close_once(monkeypatch, setup_route, "google", "deadline")
    assert cleaned == [True]


@pytest.mark.parametrize("phase", ["body", "bound_acquisition"])
@pytest.mark.parametrize("reason", ["deadline", "disconnect"])
def test_phase_fault_survives_preparation_longer_than_fault_budget(monkeypatch, setup_route, phase, reason):
    read_json = Request.json
    async def delayed_json(request):
        await asyncio.sleep(0.12)
        return await read_json(request)
    monkeypatch.setattr(Request, "json", delayed_json)
    if phase == "body":
        test_nonstream_http_body_is_bounded_and_client_and_lease_close_once(monkeypatch, setup_route, "google", reason)
    else:
        test_late_bound_account_acquisition_is_released_without_dispatch(monkeypatch, setup_route, reason)


@pytest.mark.parametrize("route,preparation", [("openai", "resolve_openai_auth"), ("byok", "all_provider_configs"),
                                               ("google", "response_model_id"), ("openai_oauth", "resolve_openai_auth")])
def test_slow_sync_preparation_returns_without_late_provider_dispatch(monkeypatch, setup_route, route, preparation):
    state = setup_route(route, timeout=GENEROUS_TIMEOUT)
    entered, unblock = [], threading.Event()
    original = getattr(server, preparation)
    clients = []
    monkeypatch.setattr(server.httpx, "AsyncClient", lambda **kw: clients.append(1))
    async def scenario():
        arm_deadline = deadline_on_phase_entry(monkeypatch)
        def slow(*args, **kwargs):
            if not entered:
                entered.append(time.monotonic())
                arm_deadline()  # Expire while this synchronous step is blocked.
            unblock.wait(BLOCK_SECONDS)
            return original(*args, **kwargs)
        monkeypatch.setattr(server, preparation, slow)
        with pytest.raises(HTTPException) as caught:
            await server.create_response(Request(route))
        assert caught.value.status_code == 504
        # Returned while the preparation step was still blocked.
        assert entered and not unblock.is_set()
        assert time.monotonic() - entered[0] < BLOCK_SECONDS / 2
    try:
        asyncio.run(scenario())
    finally:
        unblock.set()
    assert not clients
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
    state = setup_route(route, timeout=GENEROUS_TIMEOUT)
    write_deadline = DiagnosticWriteDeadline().install(monkeypatch)
    unblock, entered = threading.Event(), []
    class Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def post(self, *args, **kw):
            write_deadline.arm()  # The next log write is the one that stalls.
            return success_response(route)
    monkeypatch.setattr(server.httpx, "AsyncClient", lambda **kw: Client())
    def slow(row):
        if not write_deadline.armed.is_set():
            return  # Rows before provider dispatch are not the stall under test.
        if not entered: entered.append(time.monotonic())
        unblock.wait(BLOCK_SECONDS)
    monkeypatch.setattr(server, "write_request_record", slow)
    try:
        with pytest.raises(HTTPException) as caught:
            asyncio.run(server.create_response(Request(route)))
        assert caught.value.status_code == 504
        # Bounded by the armed deadline, not the blocked writer or the 2s
        # diagnostic ceiling.
        assert entered and not unblock.is_set()
        assert time.monotonic() - entered[0] < 1.9
    finally: unblock.set()
    assert state.release.await_count == (1 if route == "google" else 0)


def test_late_account_acquisition_is_released_without_dispatch(monkeypatch, setup_route):
    state = setup_route("google", timeout=GENEROUS_TIMEOUT)
    async def scenario():
        finish = asyncio.Event()
        arm_deadline = deadline_on_phase_entry(monkeypatch)
        async def acquire(*args):
            arm_deadline()  # Expire while acquisition is in flight.
            await finish.wait()
            return {"email": "late@example.invalid", "accessToken": "synthetic-only"}
        monkeypatch.setattr(server, "acquire_active_account_for_request", acquire)
        with pytest.raises(HTTPException) as caught:
            await server.create_response(Request("google"))
        assert caught.value.status_code == 504
        assert not state.release.await_count
        finish.set()
        await eventually(lambda: state.release.await_count)
        state.release.assert_awaited_once_with("late@example.invalid")
    asyncio.run(scenario())


@pytest.mark.parametrize("reason", ["deadline", "disconnect"])
def test_late_bound_account_acquisition_is_released_without_dispatch(monkeypatch, setup_route, reason):
    state = setup_route("google", timeout=GENEROUS_TIMEOUT)
    monkeypatch.setattr(
        server,
        "classify_route",
        lambda model, **kwargs: "antigravity" if model == "gemini-3.8-flash" else "byok",
    )

    async def scenario():
        finish = asyncio.Event()
        started = asyncio.Event()
        arm_deadline = deadline_on_phase_entry(monkeypatch) if reason == "deadline" else None

        async def acquire(*args):
            if arm_deadline is not None:
                arm_deadline()
            started.set()
            await finish.wait()
            return {"email": "late-bound@example.invalid", "accessToken": "synthetic-only"}

        monkeypatch.setattr(server, "acquire_bound_account_for_request", acquire)
        request = Request("google")
        request.headers["X-Anti-Account-Binding"] = json.dumps({
            "schemaVersion": 1,
            "gatewayInstance": GATEWAY_INSTANCE,
            "accountRef": "acct_" + "b" * 12,
            "inventorySha256": "c" * 64,
        })
        task = asyncio.create_task(server.create_response(request))
        try:
            await asyncio.wait_for(started.wait(), HANG_GUARD_SECONDS)
            if reason == "disconnect":
                request.disconnected = True
            with pytest.raises(HTTPException if reason == "deadline" else ClientDisconnect) as caught:
                await asyncio.wait_for(task, HANG_GUARD_SECONDS)
            if reason == "deadline":
                assert isinstance(caught.value, HTTPException)
                assert caught.value.status_code == 504
        finally:
            finish.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await eventually(lambda: state.release.await_count)
        state.release.assert_awaited_once_with("late-bound@example.invalid")

    asyncio.run(scenario())


def test_refresh_in_progress_is_retryable_and_logged_without_a_lease(monkeypatch, setup_route):
    state = setup_route("google", timeout=GENEROUS_TIMEOUT)

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


@pytest.mark.parametrize("seam,route", [
    ("google_json", "google"),
    ("google_parse", "google"),
    ("openai_oauth_sse", "openai_oauth"),
    ("openai_json", "openai"),
])
@pytest.mark.parametrize(
    "failure_type,reason",
    [(budgets.RequestDeadlineExceeded, "deadline"), (ClientDisconnect, "disconnect")],
    ids=["deadline", "disconnect"],
)
def test_sync_decode_and_translation_failures_keep_outer_deadline_or_disconnect(
    monkeypatch, setup_route, seam, route, failure_type, reason,
):
    state = setup_route(route, timeout=GENEROUS_TIMEOUT)
    response = success_response(route)
    clients = []

    class Client:
        def __init__(self, **_kwargs):
            self.closed = 0
            clients.append(self)
        async def __aenter__(self):
            return self
        async def __aexit__(self, *_args):
            self.closed += 1
        async def post(self, *_args, **_kwargs):
            return response

    def fail(*_args, **_kwargs):
        reached.append(True)
        raise failure_type()

    reached = []
    monkeypatch.setattr(server.httpx, "AsyncClient", Client)
    if seam in {"google_json", "openai_json"}:
        # Both routes use the bounded shared decoder, not HTTPX Response.json().
        monkeypatch.setattr(server, "response_json", fail)
    elif seam == "google_parse":
        monkeypatch.setattr(server.GoogleTransport, "parse_response", fail)
    else:
        # OAuth responses are decoded incrementally by the terminal-authority adapter.
        monkeypatch.setattr(server.NativeResponsesStreamAdapter, "consume_bytes", fail)

    if reason == "deadline":
        with pytest.raises(HTTPException) as caught:
            asyncio.run(server.create_response(Request(route)))
        assert caught.value.status_code == 504
        expected_error = "request_deadline_exceeded"
    else:
        with pytest.raises(ClientDisconnect):
            asyncio.run(server.create_response(Request(route)))
        expected_error = "cancelled"

    assert reached == [True]
    assert clients and all(client.closed == 1 for client in clients)
    assert state.release.await_count == (1 if route == "google" else 0)
    terminal = [row for row in state.records if row["lifecycle_phase"] == "terminal"]
    assert len(terminal) == 1
    assert terminal[0]["error_class"] == expected_error
    assert terminal[0]["status"] == ("failed" if reason == "deadline" else "cancelled")
    if reason == "deadline":
        assert terminal[0]["http_status"] == 504
    else:
        assert terminal[0]["cancelled"] is True


@pytest.mark.parametrize("failure_type,reason", [
    (budgets.RequestDeadlineExceeded, "deadline"), (ClientDisconnect, "disconnect"),
])
def test_injected_decoder_failure_survives_slow_preparation(monkeypatch, setup_route, failure_type, reason):
    read_json = Request.json
    async def delayed_json(request):
        await asyncio.sleep(0.25)
        return await read_json(request)
    monkeypatch.setattr(Request, "json", delayed_json)
    test_sync_decode_and_translation_failures_keep_outer_deadline_or_disconnect(
        monkeypatch, setup_route, "openai_oauth_sse", "openai_oauth", failure_type, reason,
    )


@pytest.mark.parametrize("phase", ["idle", "total"])
def test_stream_event_idle_and_total_policies_fail_once_without_replay(monkeypatch, phase):
    monkeypatch.setattr(budgets, "DRAIN_SECONDS", 0.01)
    async def scenario():
        budget = budgets.RequestBudget(Request("openai"), timeout=GENEROUS_TIMEOUT, release_account=AsyncMock())
        budget.stream_idle = GENEROUS_TIMEOUT
        closed = []
        async def source():
            try:
                # Arm the policy under test once the stream is producing, so a
                # slow first step cannot expire it before any output exists.
                if phase == "idle":
                    budget.stream_idle = 0.03
                else:
                    budget.deadline = time.monotonic() + 0.04
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
        budget = budgets.RequestBudget(Request("openai"), timeout=GENEROUS_TIMEOUT, release_account=AsyncMock())
        budget.stream_idle = GENEROUS_TIMEOUT
        async def source():
            budget.stream_idle = 0.02  # Only the trailing silence is short.
            yield "data: " + json.dumps({"type": "response.completed", "response": NATIVE}) + "\n\n"
            await asyncio.Future()
        chunks = [chunk async for chunk in budgets.stream_with_budget(source(), budget)]
        assert sum("response.completed" in chunk for chunk in chunks) == 1
        assert not any("response.failed" in chunk for chunk in chunks)
        assert chunks[-1] == "data: [DONE]\n\n"
    asyncio.run(scenario())


def stream_clients(monkeypatch, route, *, repeated=False, slow_connect=False, on_first_read=None, on_connect=None):
    clients, contexts = [], []
    class Response:
        status_code = 200
        headers = {}
        async def aiter_bytes(self):
            if on_first_read is not None: on_first_read()
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
            if on_connect is not None: on_connect()
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
@pytest.mark.parametrize("preparation_delay", [0, 0.15])
def test_route_stream_timeouts_preserve_partial_output_and_close_owned_resources(monkeypatch, setup_route, route, policy, preparation_delay):
    state = setup_route(route, timeout=GENEROUS_TIMEOUT)
    # This fixture tests an established stream. Preparation expiry has separate
    # tests, and the Google stream dispatches upstream inside its first body
    # read, so both policies start generous and the short one is armed only at
    # the fake first upstream read. Before, the 40ms idle cap also covered
    # Google's dispatch and could expire before any client existed.
    monkeypatch.setattr(server, "STREAM_IDLE_TIMEOUT_SECONDS", GENEROUS_TIMEOUT)
    monkeypatch.setattr(server, "STREAM_TOTAL_TIMEOUT_SECONDS", GENEROUS_TIMEOUT)
    factory = server._new_request_budget
    active = {}
    def capture_budget(request):
        budget = factory(request)
        active['budget'] = budget
        return budget
    monkeypatch.setattr(server, '_new_request_budget', capture_budget)
    def arm_stream_deadline():
        # Idle: a short silence cap well inside the total. Total: a short
        # total cap well inside the idle cap, while chunks keep flowing.
        if policy == 'idle':
            active['budget'].stream_idle = 0.04
        else:
            active['budget'].deadline = time.monotonic() + 0.1
    clients, contexts = stream_clients(monkeypatch, route, repeated=policy == "total", on_first_read=arm_stream_deadline)
    async def scenario():
        request = Request(route, stream=True)
        read_json = request.json
        async def delayed_json():
            await asyncio.sleep(preparation_delay)
            return await read_json()
        request.json = delayed_json
        response = await server.create_response(request)
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
    state = setup_route(route, timeout=GENEROUS_TIMEOUT)
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
    setup_route(route, timeout=GENEROUS_TIMEOUT)
    async def scenario():
        arm_deadline = deadline_on_phase_entry(monkeypatch)
        # Expire while the connect is in flight, not before it started.
        clients, contexts = stream_clients(monkeypatch, route, slow_connect=True, on_connect=arm_deadline)
        with pytest.raises(HTTPException) as caught:
            await server.create_response(Request(route, stream=True))
        return caught, clients, contexts
    caught, clients, contexts = asyncio.run(scenario())
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
        await asyncio.wait_for(budgets.shielded_cleanup(blocked, other), HANG_GUARD_SECONDS)
        # The blocked close never finishes, so any return proves the bound.
        assert time.monotonic() - started < BLOCK_SECONDS / 2
        assert sorted(calls) == ["blocked", "other"]
    asyncio.run(scenario())


def test_timeout_failure_uses_observed_native_response_id_without_created(monkeypatch):
    monkeypatch.setattr(budgets, "DRAIN_SECONDS", 0.01)
    async def scenario():
        budget = budgets.RequestBudget(Request("openai"), timeout=5, release_account=AsyncMock())
        budget.stream_idle = 5
        async def source():
            yield 'data: {"type":"response.output_text.delta","response_id":"resp_observed","delta":"fixture"}\n\n'
            await asyncio.Future()
        stream = budgets.stream_with_budget(source(), budget)
        first = await anext(stream)
        assert json.loads(first[6:])["response_id"] == "resp_observed"
        # Test identity preservation after an observed event. A 20ms wall-clock
        # deadline could instead expire before observation under suite load.
        budget.deadline = time.monotonic() - 1
        chunks = [chunk async for chunk in stream]
        assert json.loads(chunks[-2][6:])["response"]["id"] == "resp_observed"
        assert chunks[-1] == "data: [DONE]\n\n"
    asyncio.run(scenario())


STREAM_TOTAL_GENEROUS = 5.0


@pytest.mark.parametrize("route", ROUTES)
def test_downstream_backpressure_is_bounded_and_releases_ownership(monkeypatch, setup_route, route):
    # Preparation gets generous budgets so a slow runner cannot expire the
    # request before the stream is handed off. Previously the 60ms total-stream
    # cap also covered preparation and flaked on macOS CI with a 504 from
    # create_response. The short cap under test is armed at stream handoff.
    state = setup_route(route, timeout=GENEROUS_TIMEOUT)
    monkeypatch.setattr(server, "STREAM_TOTAL_TIMEOUT_SECONDS", STREAM_TOTAL_GENEROUS)
    new_budget = server._new_request_budget
    def short_stream_budget(request):
        budget = new_budget(request)
        start_stream = budget.start_stream
        def start_short_stream():
            start_stream()
            now = time.monotonic()
            budget.stream_total = now - budget.started + 0.06
            budget.deadline = now + 0.06
        budget.start_stream = start_short_stream
        return budget
    monkeypatch.setattr(server, "_new_request_budget", short_stream_budget)
    clients, contexts = stream_clients(monkeypatch, route, repeated=True)
    async def scenario():
        response = await server.create_response(Request(route, stream=True))
        async def receive(): await asyncio.Future()
        async def send(message):
            if message["type"] == "http.response.body": await asyncio.Future()
        started = time.monotonic()
        await asyncio.wait_for(response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send), HANG_GUARD_SECONDS)
        # Ended by the 60ms handoff cap, well before the 5s stream total.
        assert time.monotonic() - started < STREAM_TOTAL_GENEROUS - 1
    asyncio.run(scenario())
    assert all(client.closed == 1 for client in clients)
    assert all(context.closed == 1 for context in contexts)
    assert state.release.await_count == (1 if route == "google" else 0)
    assert state.records[-1]["status"] == "failed"
    assert state.records[-1]["terminal_reason"] == "request_deadline_exceeded"


@pytest.mark.parametrize("route", ROUTES)
def test_deadline_metadata_is_stripped_from_every_provider_payload(monkeypatch, setup_route, route):
    setup_route(route, timeout=GENEROUS_TIMEOUT)
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
    # The deadline must expire while the provider's success row is being
    # written. An 80ms budget from request start could instead expire during
    # preparation on a loaded runner (no provider call, no lease), so the
    # short timeout is armed at provider dispatch and bounds the stalled write.
    state = setup_route(route, timeout=GENEROUS_TIMEOUT)
    write_deadline = DiagnosticWriteDeadline().install(monkeypatch)
    unblock = threading.Event()
    rows, stalled = [], []
    class Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def post(self, *args, **kw):
            write_deadline.arm()
            return success_response(route)
    monkeypatch.setattr(server.httpx, "AsyncClient", lambda **kw: Client())
    def blocked_writer(row):
        if write_deadline.armed.is_set():
            stalled.append(row["status"])
            unblock.wait(BLOCK_SECONDS)
        rows.append(deepcopy(row))
    monkeypatch.setattr(server, "write_request_record", blocked_writer)
    async def scenario():
        with pytest.raises(HTTPException) as caught:
            await server.create_response(Request(route))
        assert caught.value.status_code == 504
        # The stalled write was the provider's success row.
        assert stalled and stalled[0] == "success"
        unblock.set()
        def settled():
            return (any(row["status"] == "success" for row in rows)
                    and rows[-1]["terminal_reason"] == "request_deadline_exceeded")
        await eventually(settled)
        await asyncio.sleep(0.02)
    try: asyncio.run(scenario())
    finally: unblock.set()
    assert rows[-1]["status"] == "failed" and rows[-1]["terminal_reason"] == "request_deadline_exceeded"
    assert state.release.await_count == (1 if route == "google" else 0)


@pytest.mark.parametrize("route", ROUTES)
def test_disconnect_after_partial_stream_closes_resources_and_logs_once(monkeypatch, setup_route, route):
    state = setup_route(route, timeout=GENEROUS_TIMEOUT)
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
        await asyncio.wait_for(response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send), HANG_GUARD_SECONDS)
    asyncio.run(scenario())
    assert all(client.closed == 1 for client in clients)
    assert all(context.closed == 1 for context in contexts)
    assert state.release.await_count == (1 if route == "google" else 0)
    terminal = [row for row in state.records if row["lifecycle_phase"] == "terminal"]
    assert len(terminal) == 1 and terminal[0]["cancelled"] is True


@pytest.mark.parametrize("route", ["openai", "openai_oauth"])
def test_native_open_stream_is_closed_if_start_diagnostic_expires(monkeypatch, setup_route, route):
    setup_route(route, timeout=GENEROUS_TIMEOUT)
    # Arm once the native stream is open, so the stalled write is its
    # stream_started row rather than an earlier phase.
    write_deadline = DiagnosticWriteDeadline().install(monkeypatch)
    clients, contexts = stream_clients(monkeypatch, route, on_connect=write_deadline.arm)
    unblock = threading.Event()
    def blocked_writer(row):
        if write_deadline.armed.is_set():
            unblock.wait(BLOCK_SECONDS)
    monkeypatch.setattr(server, "write_request_record", blocked_writer)
    try:
        with pytest.raises(HTTPException) as caught:
            asyncio.run(server.create_response(Request(route, stream=True)))
        assert caught.value.status_code == 504
    finally:
        unblock.set()
    assert clients[0].closed == contexts[0].closed == 1


def test_preparation_failure_after_account_acquisition_releases_lease(monkeypatch, setup_route):
    state = setup_route("google", timeout=GENEROUS_TIMEOUT)
    unblock = threading.Event()
    async def scenario():
        arm_deadline = deadline_on_phase_entry(monkeypatch)
        def platform():
            arm_deadline()  # Runs in a worker after the lease is acquired.
            unblock.wait(BLOCK_SECONDS)
            return "fixture"
        monkeypatch.setattr(server, "get_platform", platform)
        with pytest.raises(HTTPException) as caught:
            await server.create_response(Request("google"))
        assert caught.value.status_code == 504
    try:
        asyncio.run(scenario())
    finally: unblock.set()
    assert state.acquire.await_count == state.release.await_count == 1


@pytest.mark.parametrize("route", ROUTES)
def test_already_disconnected_request_never_acquires_or_resolves_auth(monkeypatch, setup_route, route):
    state = setup_route(route, timeout=GENEROUS_TIMEOUT)
    auth = []
    monkeypatch.setattr(server, "resolve_openai_auth", lambda: auth.append(True))
    request = Request(route); request.disconnected = True
    with pytest.raises(ClientDisconnect):
        asyncio.run(server.create_response(request))
    assert not state.acquire.called and not auth


@pytest.mark.parametrize("route", ROUTES)
def test_already_disconnected_request_survives_slow_body_read(monkeypatch, setup_route, route):
    read_json = Request.json
    async def delayed_json(request):
        await asyncio.sleep(0.12)
        return await read_json(request)
    monkeypatch.setattr(Request, "json", delayed_json)
    test_already_disconnected_request_never_acquires_or_resolves_auth(monkeypatch, setup_route, route)


def test_late_native_connect_result_is_closed_without_being_transferred(monkeypatch, setup_route):
    setup_route("openai", timeout=GENEROUS_TIMEOUT)
    # The connect ignores the first cancellation, so the drain wait always
    # times out; keep it short. The fixture is still blocked when it expires.
    monkeypatch.setattr(budgets, "DRAIN_SECONDS", 0.015)
    closed = []
    async def scenario():
        finish = asyncio.Event()
        arm_deadline = deadline_on_phase_entry(monkeypatch)
        class Context:
            async def __aenter__(self):
                arm_deadline()  # Expire while the connect is in flight.
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
        await eventually(lambda: len(closed) == 2)
        assert closed == ["client", "response"]
    asyncio.run(scenario())


@pytest.mark.parametrize("route,preparation", [("openai", "resolve_openai_auth"), ("openai_oauth", "resolve_openai_auth"),
                                               ("byok", "all_provider_configs"), ("google", "native_model_capabilities")])
def test_public_stream_total_expires_during_preparation_before_http_200(monkeypatch, setup_route, route, preparation):
    # The 1s stream total from metadata is the subject, armed from request
    # start as in production. The 3s request timeout and the 10s blocked step
    # bound what a broken stream-total cap would take instead.
    state = setup_route(route, timeout=3)
    unblock = threading.Event()
    original = getattr(server, preparation)
    def slow(*args, **kwargs):
        unblock.wait(BLOCK_SECONDS)
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
        # Below the 3s request timeout: the 1s stream total ended preparation.
        assert time.monotonic() - started < 2.9
    finally:
        unblock.set()
    assert not clients and not contexts and not state.acquire.called


@pytest.mark.parametrize("route", ROUTES)
def test_total_expiry_after_preparation_handoff_sends_504_without_sse_headers(monkeypatch, setup_route, route):
    # Expiry is injected after preparation; scheduler load must not move this
    # fixture into the separately tested preparation-expiry phase.
    state = setup_route(route, timeout=GENEROUS_TIMEOUT)
    monkeypatch.setattr(server, "STREAM_TOTAL_TIMEOUT_SECONDS", GENEROUS_TIMEOUT)
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
    # A short stream total expires while preparation owns resources: Google's
    # lease (blocked platform lookup) or a native stream that is already open
    # (blocked stream_started write). The 40ms cap is armed on entry to that
    # owned phase; armed from request start it could expire before anything
    # was owned, and the old 0.25s wall-clock bound flaked on loaded runners.
    google = route == "google"
    state = setup_route(route, timeout=GENEROUS_TIMEOUT)
    monkeypatch.setattr(server, "STREAM_TOTAL_TIMEOUT_SECONDS", GENEROUS_TIMEOUT)
    unblock = threading.Event()
    entered = []
    async def scenario():
        arm_deadline = deadline_on_phase_entry(monkeypatch, timeout=0.04)
        if google:
            clients, contexts = stream_clients(monkeypatch, route)
            def platform():
                entered.append(time.monotonic())
                # This preparation step runs in a worker after lease acquisition.
                arm_deadline()
                unblock.wait(BLOCK_SECONDS)
                return "fixture"
            monkeypatch.setattr(server, "get_platform", platform)
        else:
            write_deadline = DiagnosticWriteDeadline(timeout=0.04).install(monkeypatch)
            def opened():
                entered.append(time.monotonic())
                arm_deadline(); write_deadline.arm()
            clients, contexts = stream_clients(monkeypatch, route, on_connect=opened)
            def blocked_writer(row):
                if write_deadline.armed.is_set():
                    unblock.wait(BLOCK_SECONDS)
            monkeypatch.setattr(server, "write_request_record", blocked_writer)
        try:
            with pytest.raises(HTTPException) as caught:
                await asyncio.wait_for(server.create_response(Request(route, stream=True)), HANG_GUARD_SECONDS)
            assert len(entered) == 1
            if google:
                assert state.acquire.await_count == 1
            else:
                assert len(clients) == len(contexts) == 1
            assert caught.value.status_code == 504
            # Returned while the owned step was still blocked.
            assert not unblock.is_set()
            assert time.monotonic() - entered[0] < BLOCK_SECONDS / 2
        finally:
            unblock.set()
        return clients, contexts
    clients, contexts = asyncio.run(scenario())
    assert all(client.closed == 1 for client in clients)
    assert all(context.closed == 1 for context in contexts)
    assert state.release.await_count == (1 if route == "google" else 0)


def test_owned_preparation_fault_survives_slow_body_read(monkeypatch, setup_route):
    read_json = Request.json
    async def delayed_json(request):
        await asyncio.sleep(0.12)
        return await read_json(request)
    monkeypatch.setattr(Request, "json", delayed_json)
    test_short_total_during_owned_preparation_releases_resources(monkeypatch, setup_route, "google")


def test_stream_handoff_never_extends_an_expired_preparation_budget():
    budget = budgets.RequestBudget(Request("google"), timeout=1, release_account=AsyncMock())
    budget.deadline = time.monotonic() - 0.001
    budget.stream_total = 1800
    with pytest.raises(budgets.RequestDeadlineExceeded): budget.start_stream()
    assert budget.failure_code == "request_deadline_exceeded"


def test_stream_total_override_does_not_limit_nonstream_preparation(monkeypatch, setup_route):
    # 1.05s of preparation outlives the 1s stream total but must stay well
    # inside the request timeout, even on a loaded runner.
    setup_route("openai", timeout=10)
    original = server.resolve_openai_auth
    monkeypatch.setattr(server, "resolve_openai_auth", lambda: (time.sleep(1.05), original())[1])
    class Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def post(self, *args, **kw): return success_response("openai")
    monkeypatch.setattr(server.httpx, "AsyncClient", lambda **kw: Client())
    request = Request("openai")
    request.payload["metadata"] = {"antigravity_request_timeout_seconds": 10,
                                   "antigravity_stream_total_timeout_seconds": 1}
    result = asyncio.run(server.create_response(request))
    assert result["status"] == "completed"


def test_expiry_between_header_check_and_dispatch_cannot_use_failure_grace(monkeypatch, setup_route):
    setup_route("openai", timeout=GENEROUS_TIMEOUT)
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
