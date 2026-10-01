"""Logical request telemetry with synthetic Responses and log records."""

import asyncio
import copy
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from codex_antigravity_auth import observability as logs, server


@pytest.fixture
def records(monkeypatch):
    rows = []
    monkeypatch.setattr(server, "write_request_record", rows.append)
    return rows


def normalized_response(status):
    return {
        "id": "synthetic-response", "object": "response", "status": status,
        "output": [] if status == "failed" else [{"type": "message", "role": "assistant", "status": "completed", "content": [{"type": "output_text", "text": "synthetic output"}]}],
        "usage": {"input_tokens": 2, "output_tokens": 3, "total_tokens": 5},
        **({"incomplete_details": {"reason": "max_output_tokens"}} if status == "incomplete" else {}),
        **({"error": {"code": "empty_response", "message": "synthetic failure"}} if status == "failed" else {}),
    }


@pytest.fixture
def route_fixture(monkeypatch):
    provider = {"id": "fixture", "kind": "openai_chat", "baseUrl": "https://example.invalid/v1", "apiKey": "synthetic-key", "models": ["model"]}
    monkeypatch.setattr(server, "all_provider_configs", lambda: {"fixture": provider})
    monkeypatch.setattr(server, "resolve_openai_auth", lambda: SimpleNamespace(kind="api_key"))
    monkeypatch.setattr(server, "is_unified_mode_enabled", lambda: True)
    monkeypatch.setattr(server.account_manager, "acquire_account", lambda *args, **kwargs: {"email": "fixture@example.invalid", "accessToken": "synthetic-access", "projectId": "synthetic-project"})
    monkeypatch.setattr(server.account_manager, "release_account", MagicMock())
    monkeypatch.setattr(server.account_manager, "record_attempt", MagicMock())
    return provider


@pytest.mark.parametrize("route", ["google", "byok", "openai"])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("status", ["completed", "incomplete", "failed"])
def test_route_stream_matrix_has_one_semantic_terminal(monkeypatch, records, route_fixture, route, stream, status):
    response = normalized_response(status)
    monkeypatch.setattr(server, "classify_route", lambda *args, **kwargs: route)
    async def nonstream(*args, telemetry=None, **kwargs):
        if telemetry is not None:
            telemetry["http_status"] = 200
        return copy.deepcopy(response)
    async def opened(*args, telemetry=None, **kwargs):
        if telemetry is not None:
            telemetry["http_status"] = 200
        return (None, None, None)
    async def proxy_stream(*args, telemetry=None, **kwargs):
        if telemetry is not None:
            telemetry["http_status"] = 200
        yield "data: " + json.dumps({"type": f"response.{status}", "response": response}) + "\n\n"
        yield "data: [DONE]\n\n"
    async def google_post(*args, **kwargs):
        payload = {"usageMetadata": {"promptTokenCount": 2, "candidatesTokenCount": 3, "totalTokenCount": 5}, "candidates": []}
        if status != "failed":
            payload["candidates"] = [{"finishReason": "MAX_TOKENS" if status == "incomplete" else "STOP", "content": {"parts": [{"text": "synthetic output"}]}}]
        return httpx.Response(200, json=payload)
    async def google_stream(*args, telemetry=None, **kwargs):
        if telemetry is not None:
            telemetry["http_status"] = 200
        yield {"type": f"response.{status}", "response": response}
        yield "[DONE]"
    monkeypatch.setattr(server, "create_openai_upstream_response", nonstream)
    monkeypatch.setattr(server, "create_openai_compatible_response", nonstream)
    monkeypatch.setattr(server, "_open_openai_upstream_stream", opened)
    monkeypatch.setattr(server, "openai_upstream_sse_generator", proxy_stream)
    monkeypatch.setattr(server, "openai_compatible_sse_generator", proxy_stream)
    monkeypatch.setattr(server.GoogleTransport, "post", google_post)
    monkeypatch.setattr(server.GoogleTransport, "stream_events", google_stream)
    model = "fixture:model" if route == "byok" else "gemini-3.8-flash"
    result = TestClient(server.app).post("/v1/responses", json={"model": model, "input": "synthetic prompt secret", "stream": stream})
    assert result.status_code == 200
    terminal = [row for row in records if row["lifecycle_phase"] == "terminal"]
    assert len(terminal) == 1
    row = terminal[0]
    assert row["terminal_kind"] == status
    assert row["status"] == ("success" if status == "completed" else status)
    assert row["usage"] == {"input_tokens": 2, "output_tokens": 3, "total_tokens": 5}
    assert row["provider_accepted"] is True
    if status == "completed":
        assert row["terminal_reason"] in {"stop", "completed"}
    else:
        assert row["terminal_reason"] == {"incomplete": "max_output_tokens", "failed": "empty_response"}[status]
    assert sum(row["lifecycle_phase"] == "started" for row in records) == int(stream)
    assert len({row["request_id"] for row in records}) == 1
    assert "synthetic prompt secret" not in json.dumps(records)


@pytest.mark.parametrize("route", ["openai", "byok"])
def test_proxy_cancellation_records_one_cancelled_terminal(monkeypatch, records, route_fixture, route):
    monkeypatch.setattr(server, "classify_route", lambda *args, **kwargs: route)
    async def opened(*args, telemetry=None, **kwargs):
        if telemetry is not None:
            telemetry["http_status"] = 200
        return (None, None, None)
    async def stream(*args, telemetry=None, **kwargs):
        if telemetry is not None:
            telemetry["http_status"] = 200
        yield 'data: {"type":"response.output_text.delta","delta":"synthetic"}\n\n'
        await asyncio.Future()
    monkeypatch.setattr(server, "_open_openai_upstream_stream", opened)
    monkeypatch.setattr(server, "openai_upstream_sse_generator", stream)
    monkeypatch.setattr(server, "openai_compatible_sse_generator", stream)
    async def receive():
        return {"type": "http.request", "body": json.dumps({"model": "fixture:model" if route == "byok" else "gemini-3.8-flash", "input": "fixture", "stream": True}).encode(), "more_body": False}
    async def scenario():
        request = Request({"type": "http", "method": "POST", "path": "/v1/responses", "headers": [], "query_string": b"", "client": ("testserver", 80)}, receive)
        response = await server.create_response(request)
        await response.body_iterator.__anext__()
        await response.body_iterator.aclose()
    asyncio.run(scenario())
    assert len(records) == 2
    terminal = records[-1]
    assert terminal["lifecycle_phase"] == "terminal"
    assert terminal["status"] == "cancelled" and terminal["cancelled"] is True
    assert terminal["terminal_reason"] == "cancelled"


@pytest.mark.parametrize("body", [[], {"model": "gemini-3.8-flash", "stream": "yes"}, {"model": "gemini-3.8-flash", "previous_response_id": "synthetic-previous"}])
def test_boundary_validation_is_logged_without_request_content(records, body):
    result = TestClient(server.app).post("/v1/responses", json=body)
    assert result.status_code == 400
    assert len(records) == 1
    assert records[0]["lifecycle_phase"] == "terminal"
    assert records[0]["terminal_kind"] == "failed"
    assert records[0]["error_class"] == "invalid_request"
    assert records[0]["attempt_count"] == 0
    assert records[0]["provider_accepted"] is None
    assert records[0]["upstream_http_status"] is None


def event(request_id, status, **values):
    return {"request_id": request_id, "route": "byok", "provider": "fixture", "status": status, "timestamp": "2026-10-01T00:00:00Z", **values}


def test_summary_counts_terminal_once_and_keeps_open_partial_cancelled_separate(monkeypatch):
    completed = event("one", "success", latency_ms=100, usage={"total_tokens": 5}, provider_accepted=True)
    rows = [event("one", "stream_started", latency_ms=1), completed, copy.deepcopy(completed), event("one", "stream_started", latency_ms=2), event("two", "incomplete", terminal_reason="max_output_tokens", latency_ms=200), event("three", "failed", latency_ms=300), event("four", "cancelled", cancelled=True, latency_ms=400), event("five", "stream_started", latency_ms=0)]
    monkeypatch.setattr(logs, "iter_request_records", lambda: rows)
    report = logs.request_log_summary(since="all")
    group = report["groups"]["byok/fixture"]
    assert group["request_count"] == 5
    assert group["closed_request_count"] == 4
    assert group["open_count"] == group["incomplete_count"] == group["cancellation_count"] == group["failure_count"] == group["success_count"] == 1
    assert group["success_rate"] == 0.25
    assert group["p50_latency_ms"] == 200
    assert group["usage"]["total_tokens"] == 5
    assert group["attempt_count"] == 4
    assert group["terminal_counts"] == {"completed": 1, "incomplete": 1, "failed": 1, "cancelled": 1}
    assert group["terminal_reason_counts"]["max_output_tokens"] == 1
    assert report["included_records"] == 5 and report["included_event_records"] == 8


def test_legacy_rows_without_ids_are_not_falsely_deduplicated(monkeypatch):
    monkeypatch.setattr(logs, "iter_request_records", lambda: [{"status": "success"}, {"status": "success"}, {"status": "stream_started"}])
    group = logs.request_log_summary(since="all")["groups"]["unknown/unknown"]
    assert group["request_count"] == 3 and group["success_count"] == 2 and group["open_count"] == 1
    assert group["success_rate"] == 1.0


def test_open_only_summary_has_no_failure_or_latency_sample(monkeypatch):
    monkeypatch.setattr(logs, "iter_request_records", lambda: [event("open", "stream_started", latency_ms=7)])
    group = logs.request_log_summary(since="all")["groups"]["byok/fixture"]
    assert group["failure_count"] == 0
    assert group["success_rate"] is None and group["p50_latency_ms"] is None


def test_start_metrics_do_not_fill_missing_terminal_metrics(monkeypatch):
    monkeypatch.setattr(logs, "iter_request_records", lambda: [event("request", "stream_started", latency_ms=900, usage={"total_tokens": 500}, attempt_count=20), event("request", "success")])
    group = logs.request_log_summary(since="all")["groups"]["byok/fixture"]
    assert group["p50_latency_ms"] is None
    assert group["usage"]["total_tokens"] == 0 and group["attempt_count"] == 1


def test_new_lifecycle_fields_survive_sanitized_log_write(monkeypatch, tmp_path):
    monkeypatch.setattr(logs, "get_codex_home", lambda: tmp_path)
    logs.write_request_record(event("fixture", "incomplete", lifecycle_phase="terminal", terminal_kind="incomplete", terminal_reason="max_output_tokens", provider_accepted=True, body="must not persist"))
    record = list(logs.iter_request_records())[0]
    assert record["lifecycle_phase"] == "terminal" and record["provider_accepted"] is True
    assert "body" not in record


@pytest.mark.parametrize("upstream_status", [200, 429])
def test_real_chat_stream_telemetry_distinguishes_http_acceptance_from_generation(monkeypatch, records, route_fixture, upstream_status):
    monkeypatch.setattr(server, "classify_route", lambda *args, **kwargs: "byok")
    client_type = httpx.AsyncClient
    def respond(request):
        if upstream_status == 200:
            return httpx.Response(200, content=b'data: {"choices":[],"usage":{"total_tokens":5}}\n\ndata: [DONE]\n\n')
        return httpx.Response(upstream_status, json={"error": "synthetic throttle"})
    monkeypatch.setattr(server.httpx, "AsyncClient", lambda **kwargs: client_type(transport=httpx.MockTransport(respond), **kwargs))
    response = TestClient(server.app).post("/v1/responses", json={"model": "fixture:model", "input": "fixture", "stream": True})
    assert response.status_code == 200  # Downstream SSE envelope is distinct.
    terminal = records[-1]
    assert terminal["terminal_kind"] == "failed"
    assert terminal["http_status"] == upstream_status
    assert terminal["provider_accepted"] is (upstream_status == 200)
    if upstream_status == 200:
        assert terminal["usage"]["total_tokens"] == 5


def test_summary_tolerates_invalid_numeric_and_scalar_log_values(monkeypatch):
    row = event("malformed-fields", "failed", lifecycle_phase=[], terminal_kind={}, terminal_reason=[], latency_ms=float("inf"), usage={"total_tokens": float("nan")}, attempt_count=None, provider_accepted="unknown")
    monkeypatch.setattr(logs, "iter_request_records", lambda: [row])
    group = logs.request_log_summary(since="all")["groups"]["byok/fixture"]
    assert group["request_count"] == group["failure_count"] == group["provider_acceptance_unknown_count"] == 1
    assert group["usage"]["total_tokens"] == 0 and group["p50_latency_ms"] is None


def test_last_terminal_does_not_borrow_discarded_terminal_metrics(monkeypatch):
    rows = [event("same", "success", usage={"total_tokens": 5}, latency_ms=900, attempt_count=7, rotation_count=6, provider_accepted=True, upstream_http_status=200), event("same", "cancelled", cancelled=True, lifecycle_phase="terminal")]
    monkeypatch.setattr(logs, "iter_request_records", lambda: rows)
    group = logs.request_log_summary(since="all")["groups"]["byok/fixture"]
    assert group["request_count"] == group["cancellation_count"] == 1
    assert group["usage"]["total_tokens"] == group["rotation_count"] == 0
    assert group["p50_latency_ms"] is None and group["attempt_count"] == 1
    assert group["provider_accepted_count"] == 0 and group["provider_acceptance_unknown_count"] == 1


def test_legacy_gateway_status_does_not_certify_provider_acceptance(monkeypatch):
    monkeypatch.setattr(logs, "iter_request_records", lambda: [event("local", "failed", http_status=400)])
    group = logs.request_log_summary(since="all")["groups"]["byok/fixture"]
    assert group["provider_accepted_count"] == 0
    assert group["provider_acceptance_unknown_count"] == 1


@pytest.mark.parametrize("route", ["google", "byok", "openai", "openai_oauth"])
def test_upstream_http_200_is_preserved_when_gateway_maps_an_error(monkeypatch, records, route_fixture, route):
    monkeypatch.setattr(server, "classify_route", lambda *args, **kwargs: "openai" if route == "openai_oauth" else route)
    if route == "openai_oauth":
        monkeypatch.setattr(server, "resolve_openai_auth", lambda: SimpleNamespace(kind="codex_oauth"))
    monkeypatch.setattr(server, "openai_request_headers", lambda auth: {})
    monkeypatch.setattr(server, "openai_responses_url", lambda auth: "https://example.invalid/responses")
    original_client = httpx.AsyncClient
    def respond(request):
        if route == "google":
            return httpx.Response(200, json={"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "message": "synthetic quota"}})
        return httpx.Response(200, content=b"synthetic invalid JSON")
    monkeypatch.setattr(server.httpx, "AsyncClient", lambda **kwargs: original_client(transport=httpx.MockTransport(respond), **kwargs))
    model = "fixture:model" if route == "byok" else "gemini-3.8-flash"
    response = TestClient(server.app).post("/v1/responses", json={"model": model, "input": "synthetic", "stream": False})
    if route == "openai_oauth":
        # Native SSE authority returns a structured failed response even when
        # the upstream HTTP handshake succeeded (reviewed #78/#82 contract).
        assert response.status_code == 200 and response.json()["status"] == "failed"
    else:
        assert response.status_code >= 400
    terminal = records[-1]
    assert terminal["status"] == "failed"
    assert terminal["http_status"] == response.status_code
    assert terminal["upstream_http_status"] == 200 and terminal["provider_accepted"] is True


def test_google_stream_http_429_remains_a_known_rejection(monkeypatch, records, route_fixture):
    monkeypatch.setattr(server, "classify_route", lambda *args, **kwargs: "google")
    async def reject(*args, **kwargs):
        raise server.GoogleHTTPError(429, server.outcome_for_http_status(429), httpx.Response(429))
        yield  # Make this an asynchronous event source.
    monkeypatch.setattr(server.GoogleTransport, "stream_events", reject)
    response = TestClient(server.app).post("/v1/responses", json={"model": "gemini-3.8-flash", "input": "synthetic", "stream": True})
    assert response.status_code == 200
    terminal = records[-1]
    assert terminal["upstream_http_status"] == terminal["http_status"] == 429
    assert terminal["provider_accepted"] is False
    monkeypatch.setattr(logs, "iter_request_records", lambda: records)
    assert logs.request_log_summary(since="all")["groups"]["google/gemini"]["rate_limit_count"] == 1


@pytest.mark.parametrize("route", ["google", "openai", "byok"])
@pytest.mark.parametrize("status", ["completed", "incomplete", "failed"])
def test_disconnect_after_terminal_does_not_reclassify_it_as_cancelled(monkeypatch, records, route_fixture, route, status):
    monkeypatch.setattr(server, "classify_route", lambda *args, **kwargs: route)
    payload = normalized_response(status)
    async def opened(*args, telemetry=None, **kwargs):
        if telemetry is not None:
            telemetry["http_status"] = 200
        return (None, None, None)
    async def proxy(*args, telemetry=None, **kwargs):
        if telemetry is not None:
            telemetry["http_status"] = 200
        yield "data: " + json.dumps({"type": f"response.{status}", "response": payload}) + "\n\n"
        await asyncio.Future()
    async def google(*args, telemetry=None, **kwargs):
        if telemetry is not None:
            telemetry["http_status"] = 200
        yield {"type": f"response.{status}", "response": payload}
        await asyncio.Future()
    monkeypatch.setattr(server, "_open_openai_upstream_stream", opened)
    monkeypatch.setattr(server, "openai_upstream_sse_generator", proxy)
    monkeypatch.setattr(server, "openai_compatible_sse_generator", proxy)
    monkeypatch.setattr(server.GoogleTransport, "stream_events", google)
    async def receive():
        return {"type": "http.request", "body": json.dumps({"model": "fixture:model" if route == "byok" else "gemini-3.8-flash", "input": "fixture", "stream": True}).encode(), "more_body": False}
    async def scenario():
        request = Request({"type": "http", "method": "POST", "path": "/v1/responses", "headers": [], "query_string": b"", "client": ("testserver", 80)}, receive)
        response = await server.create_response(request)
        await response.body_iterator.__anext__()
        await response.body_iterator.aclose()
    asyncio.run(scenario())
    terminal = [record for record in records if record["lifecycle_phase"] == "terminal"]
    assert len(terminal) == 1
    assert terminal[0]["terminal_kind"] == status
    assert terminal[0]["status"] == ("success" if status == "completed" else status)
    assert terminal[0]["cancelled"] is False
    assert terminal[0]["usage"]["total_tokens"] == 5
