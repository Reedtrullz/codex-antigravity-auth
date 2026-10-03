"""Boundaries and overload use synthetic bytes, transports and credentials only."""
import asyncio
import base64
from dataclasses import replace
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import HTTPException
from starlette.requests import ClientDisconnect

from codex_antigravity_auth import resource_limits as limits_module, server
from codex_antigravity_auth.resource_limits import Admission, ResourceLimits, ResourceLimitError, json_loads_limited, read_request_json
from codex_antigravity_auth.schema import clean_json_schema
from codex_antigravity_auth.sse import SSEDecoder, SSELimitError
from codex_antigravity_auth.openai_transport import NativeResponsesStreamAdapter
from codex_antigravity_auth.request_budget import RequestDeadlineExceeded
from codex_antigravity_auth.tool_calls import ToolCallError
from codex_antigravity_auth.transform import function_call_arguments_string

REAL_NATIVE_RESPONSE = server.create_openai_upstream_response

NATIVE = {"id": "resp_fixture", "status": "completed", "output": [{"type": "message", "id": "msg_fixture",
          "role": "assistant", "status": "completed", "content": [{"type": "output_text", "text": "fixture"}]}]}


class RawRequest:
    def __init__(self, payload=None, *, chunks=None, headers=None):
        self.chunks = chunks if chunks is not None else [json.dumps(payload or {"model": "gpt-5.2", "input": "fixture"}).encode()]
        self.headers = headers or {}
        self.read_chunks = 0
        self.disconnected = False
    async def stream(self):
        for chunk in self.chunks:
            self.read_chunks += 1
            yield chunk
            await asyncio.sleep(0)
    async def is_disconnected(self): return self.disconnected
    async def json(self): raise AssertionError("unbounded JSON reader must not be used")


@pytest.fixture
def policy(monkeypatch):
    def configure(**values):
        configured = replace(ResourceLimits(), **values)
        monkeypatch.setattr(ResourceLimits, "from_env", classmethod(lambda cls: configured))
        admission = Admission()
        monkeypatch.setattr(server, "ADMISSION", admission)
        monkeypatch.setattr(server, "is_unified_mode_enabled", lambda: True)
        monkeypatch.setattr(server, "classify_route", lambda model, **kw: "byok" if model.startswith("fixture:") else "openai")
        monkeypatch.setattr(server, "resolve_openai_auth", lambda: SimpleNamespace(kind="api_key"))
        monkeypatch.setattr(server, "all_provider_configs", lambda: {"fixture": {"id": "fixture", "kind": "openai_chat",
            "baseUrl": "https://example.invalid/v1", "apiKey": "synthetic-only", "models": ["model"]}})
        monkeypatch.setattr(server, "schedule_refresh_accounts_ahead", lambda **kw: False)
        monkeypatch.setattr(server, "write_request_record", lambda row: None)
        native = AsyncMock(return_value=NATIVE)
        byok = AsyncMock(return_value=NATIVE)
        monkeypatch.setattr(server, "create_openai_upstream_response", native)
        monkeypatch.setattr(server, "create_openai_compatible_response", byok)
        return configured, admission, native, byok
    return configure


@pytest.mark.parametrize("declared", [None, "9999", "1"])
def test_oversized_body_is_never_clipped_or_dispatched(policy, declared):
    _, admission, native, _ = policy(body_bytes=128)
    raw = b'{"model":"gpt-5.2","input":"' + b'x' * 200 + b'"}'
    request = RawRequest(chunks=[raw[:90], raw[90:180], raw[180:]], headers={} if declared is None else {"content-length": declared})
    with pytest.raises(HTTPException) as caught:
        asyncio.run(server.create_response(request))
    assert caught.value.status_code == 413
    assert caught.value.detail["code"] == "request_body_limit"
    assert not native.called and admission.total == 0 and not admission.routes
    assert request.read_chunks == (0 if declared == "9999" else 2)


@pytest.mark.parametrize("length", ["-1", "1.5", "+5", "fixture-secret", ""])
def test_invalid_content_length_is_rejected_without_echo(policy, length):
    policy()
    request = RawRequest(headers={"content-length": length})
    with pytest.raises(HTTPException) as caught: asyncio.run(server.create_response(request))
    assert caught.value.status_code == 400 and request.read_chunks == 0
    assert "fixture-secret" not in str(caught.value.detail)


def test_declared_length_mismatch_cannot_be_accepted_as_complete(policy):
    policy()
    with pytest.raises(HTTPException) as caught:
        asyncio.run(server.create_response(RawRequest(headers={"content-length": "1000"})))
    assert caught.value.detail["code"] == "content_length_mismatch"


def test_exact_body_limit_accepts_the_original_payload(policy):
    payload = {"model": "gpt-5.2", "input": "é fixture"}
    wire = json.dumps(payload, ensure_ascii=False).encode()
    _, admission, native, _ = policy(body_bytes=len(wire))
    result = asyncio.run(server.create_response(RawRequest(chunks=[wire[:9], wire[9:]], headers={"content-length": str(len(wire))})))
    assert result["status"] == "completed" and native.call_args.args[0]["input"] == payload["input"]
    assert admission.total == 0


@pytest.mark.parametrize("wire,code", [
    ('{"x":' + '[' * 20 + '0' + ']' * 20 + '}', "json_depth_limit"),
    ('[' + ','.join('0' for _ in range(200)) + ']', "json_node_limit"),
    ('{"x":' + '1' * 1200 + '}', "json_number_limit"),
])
def test_json_amplification_is_rejected_before_json_loads(monkeypatch, wire, code):
    configured = replace(ResourceLimits(), json_depth=8, json_nodes=100)
    if code != "json_number_limit":
        monkeypatch.setattr(limits_module.json, "loads", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("must reject before decode")))
    with pytest.raises(ResourceLimitError) as caught: json_loads_limited(wire, limits=configured)
    assert caught.value.code == code


def test_json_scanner_does_not_count_braces_or_escaped_quotes_inside_strings():
    value = {"value": '\\"' + '[' * 1000 + '}' * 1000}
    assert json_loads_limited(json.dumps(value), limits=replace(ResourceLimits(), json_depth=2)) == value


def test_unpaired_unicode_and_nonfinite_values_fail_before_dispatch(policy):
    _, _, native, _ = policy()
    for wire in (b'{"input":"\\ud800"}', b'{"input":NaN}', b'{"input":1e9999}'):
        with pytest.raises(HTTPException) as caught:
            asyncio.run(server.create_response(RawRequest(chunks=[wire])))
        assert caught.value.status_code == 400
    assert not native.called


def image(data):
    return {"type": "input_image", "image_url": "data:image/png;base64," + base64.b64encode(data).decode()}


@pytest.mark.parametrize("parts", [[image(b'12345')], [image(b'1234'), image(b'5678')]])
def test_decoded_attachment_limits_apply_before_decoding(monkeypatch, parts):
    configured = replace(ResourceLimits(), attachment_bytes=4, attachments_bytes=6)
    original = limits_module.base64.b64decode
    decoded = []
    def track(value, **kw):
        decoded.append(value)
        return original(value, **kw)
    monkeypatch.setattr(limits_module.base64, "b64decode", track)
    request = RawRequest({"input": [{"role": "user", "content": parts}]})
    with pytest.raises(ResourceLimitError) as caught: asyncio.run(read_request_json(request, configured))
    assert caught.value.code == "attachment_size_limit"
    assert len(decoded) == (0 if len(parts) == 1 else 1)


def test_attachment_error_paths_do_not_echo_arbitrary_user_keys():
    payload = {"private-sentinel-key": {"type": "input_image", "image_url": "data:image/png;base64,not-base64"}}
    with pytest.raises(ResourceLimitError) as caught:
        limits_module.check_request_structure(payload, ResourceLimits())
    assert caught.value.code == "invalid_attachment_encoding"
    assert "private-sentinel-key" not in str(caught.value)
    assert "not-base64" not in str(caught.value)


def test_inline_attachment_exact_boundary_and_remote_urls_are_unchanged():
    configured = replace(ResourceLimits(), attachment_bytes=4, attachments_bytes=4)
    payload = {"input": [{"role": "user", "content": [image(b'1234'), {"type": "input_image", "image_url": "https://example.invalid/large.png"}]}]}
    assert asyncio.run(read_request_json(RawRequest(payload), configured)) == payload


def test_schema_depth_is_distinct_from_generic_json_depth(policy):
    policy(schema_depth=3, json_depth=32)
    schema = {"type": "object", "properties": {"one": {"type": "array", "items": {"type": "array", "items": {"type": "string"}}}}}
    payload = {"model": "gpt-5.2", "input": "fixture", "tools": [{"type": "function", "name": "fixture", "parameters": schema}]}
    with pytest.raises(HTTPException) as caught: asyncio.run(server.create_response(RawRequest(payload)))
    assert caught.value.status_code == 413 and caught.value.detail["code"] == "schema_depth_limit"
    # Arbitrary tool-result data named schema remains under the general bound.
    result = {"input": [{"type": "function_call_output", "call_id": "fixture", "output": {"schema": schema}}]}
    asyncio.run(read_request_json(RawRequest(result), replace(ResourceLimits(), schema_depth=3)))


def test_compact_ref_graph_has_bounded_expansion(policy):
    configured, _, _, _ = policy(body_bytes=1024)
    schema = {"type": "object", "$defs": {"leaf": {"type": "string", "description": "x" * 180}},
              "properties": {str(index): {"$ref": "#/$defs/leaf"} for index in range(10)}}
    assert len(json.dumps(schema)) < configured.body_bytes
    with pytest.raises(ResourceLimitError) as caught: clean_json_schema(schema)
    assert caught.value.code == "schema_expansion_limit"


def test_tool_argument_serialization_prechecks_size_then_rejects_duplicate_keys():
    wire = '{"outer":{"value":1,"value":2}}'
    with pytest.raises(ToolCallError):
        function_call_arguments_string(wire)
    valid = '{ "outer": {"value": 1} }'
    assert function_call_arguments_string(valid) == valid
    deep = '{"value":' + '[' * 70 + '0' + ']' * 70 + '}'
    with pytest.raises(ResourceLimitError) as caught:
        function_call_arguments_string(deep)
    assert caught.value.code == "json_depth_limit"


@pytest.mark.parametrize("failure", [
    ResourceLimitError("provider_body_limit", status=502),
    RequestDeadlineExceeded(),
    ClientDisconnect(),
])
def test_byok_nonstream_limited_post_preserves_typed_control_errors(monkeypatch, failure):
    class Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
    monkeypatch.setattr(server, "prepare_openai_compatible_request", lambda *args, **kwargs: ({}, "https://example.invalid/v1", {}, 1))
    monkeypatch.setattr(server.httpx, "AsyncClient", lambda **kwargs: Client())
    async def rejected(*args, **kwargs): raise failure
    monkeypatch.setattr(server, "limited_post", rejected)
    with pytest.raises(type(failure)) as caught:
        asyncio.run(server.create_openai_compatible_response({}, {"id": "fixture"}, "model", "display"))
    assert caught.value is failure


@pytest.mark.parametrize("failure", [
    ResourceLimitError("provider_body_limit", status=502),
    RequestDeadlineExceeded(),
    ClientDisconnect(),
])
def test_native_non200_body_read_preserves_typed_control_errors(monkeypatch, failure):
    class Response:
        status_code = 429
        headers = {"retry-after": "1"}
    class StreamContext:
        async def __aenter__(self): return Response()
        async def __aexit__(self, *args): pass
    class Client:
        async def aclose(self): self.closed = True
        def stream(self, *args, **kwargs): return StreamContext()
    client = Client()
    monkeypatch.setattr(server, "openai_responses_url", lambda _auth: "https://example.invalid/v1/responses")
    monkeypatch.setattr(server, "openai_request_headers", lambda _auth: {})
    monkeypatch.setattr(server.httpx, "AsyncClient", lambda **kwargs: client)
    async def rejected(*args, **kwargs): raise failure
    monkeypatch.setattr(server, "read_response_bytes", rejected)
    auth = SimpleNamespace(kind="api_key")
    with pytest.raises(type(failure)) as caught:
        asyncio.run(server._open_openai_upstream_stream({}, "model", auth))
    assert caught.value is failure and client.closed


def test_native_non200_body_read_io_failure_keeps_upstream_http_status(monkeypatch):
    class Response:
        status_code = 429
        headers = {"retry-after": "1"}
    class StreamContext:
        async def __aenter__(self): return Response()
        async def __aexit__(self, *args): pass
    class Client:
        async def aclose(self): self.closed = True
        def stream(self, *args, **kwargs): return StreamContext()
    client = Client()
    monkeypatch.setattr(server, "openai_responses_url", lambda _auth: "https://example.invalid/v1/responses")
    monkeypatch.setattr(server, "openai_request_headers", lambda _auth: {})
    monkeypatch.setattr(server.httpx, "AsyncClient", lambda **kwargs: client)
    async def unreadable(*args, **kwargs): raise OSError("synthetic body-read failure")
    monkeypatch.setattr(server, "read_response_bytes", unreadable)
    with pytest.raises(server.OpenAIUpstreamHTTPError) as caught:
        asyncio.run(server._open_openai_upstream_stream({}, "model", SimpleNamespace(kind="api_key")))
    assert caught.value.status_code == 429 and caught.value.body == ""
    assert caught.value.retry_after == "1" and client.closed


def test_stream_schema_expansion_rejection_is_local_nonretryable_and_releases_permit(monkeypatch, policy):
    _, admission, _, _ = policy(body_bytes=1024)
    monkeypatch.setattr(server, "classify_route", lambda *_args, **_kwargs: "antigravity")
    account = {"email": "fixture@example.invalid", "accessToken": "synthetic-only", "projectId": "fixture"}
    acquire = AsyncMock(return_value=account)
    release = AsyncMock()
    record = AsyncMock()
    monkeypatch.setattr(server, "acquire_active_account_for_request", acquire)
    monkeypatch.setattr(server, "release_account_for_request", release)
    monkeypatch.setattr(server, "record_attempt_outcome", record)

    schema = {"type": "object", "properties": {"value": {"type": "string"}}}
    clean_json_schema(schema)  # Each inline schema fits; generated placeholders share the request budget.
    tools = [{"type": "function", "name": f"f_{index}", "parameters": schema} for index in range(8)]
    payload = {"model": "gemini-3.8-flash", "input": "fixture", "stream": True,
               "tools": tools}
    assert len(json.dumps(payload)) < 1024
    dispatched = []
    clients = []
    real_client = httpx.AsyncClient

    def client_factory(**kwargs):
        client = real_client(transport=httpx.MockTransport(lambda request: dispatched.append(request)), **kwargs)
        clients.append(client)
        return client

    monkeypatch.setattr(server.httpx, "AsyncClient", client_factory)

    async def scenario():
        response = await server.create_response(RawRequest(payload))
        wire = "".join([chunk async for chunk in response.body_iterator])
        events = [json.loads(line[6:]) for line in wire.splitlines()
                  if line.startswith("data: ") and line != "data: [DONE]"]
        failed = [event for event in events if event.get("type") == "response.failed"]
        assert len(failed) == 1
        assert failed[0]["response"]["error"]["code"] == "schema_expansion_limit"
        assert "connection_error" not in wire and "response.completed" not in wire
        assert "data: [DONE]" in wire

    asyncio.run(scenario())
    assert acquire.await_count == 1  # Initial account lease only; the error did not rotate.
    assert release.await_count == 1
    record.assert_awaited_once()
    assert record.await_args.args[2].category == "invalid_request"
    assert not dispatched and not clients  # The real Google transport failed before opening HTTPX.
    assert admission.total == 0 and not admission.routes


def test_runtime_inflight_growth_is_capped_at_lifespan_startup_ceiling():
    admission = Admission()
    admission.set_startup_ceiling(2)
    limits = replace(ResourceLimits(), inflight=8, route_inflight=8)
    first = admission.acquire(limits)
    second = admission.acquire(limits)
    try:
        with pytest.raises(ResourceLimitError) as caught:
            admission.acquire(limits)
        assert caught.value.code == "gateway_overloaded" and caught.value.status == 503
    finally:
        first.release()
        second.release()
    assert admission.total == 0


@pytest.mark.parametrize("chunks", [1, 7, 10000])
def test_cumulative_sse_limit_preserves_prior_events_independent_of_chunking(chunks):
    first = 'data: {"text":"é fixture"}\n\n'.encode()
    wire = first + b'data: {"text":"' + b'x' * 200 + b'"}\n\n'
    decoder = SSEDecoder(max_buffer_chars=1000, max_total_bytes=len(first) + 4)
    output = []
    with pytest.raises(SSELimitError):
        for offset in range(0, len(wire), chunks): output.extend(decoder.feed(wire[offset:offset + chunks]))
    assert output == ['{"text":"é fixture"}']
    assert decoder.buffered_chars == 0


def test_never_terminated_frame_and_many_small_chunks_remain_bounded():
    decoder = SSEDecoder(max_buffer_chars=64, max_total_bytes=1000)
    with pytest.raises(SSELimitError):
        for _ in range(100): list(decoder.feed(b'x'))
    assert decoder.buffered_chars == 0


def test_native_limit_failure_retains_only_validated_complete_items(policy):
    policy(sse_total_bytes=500)
    item = NATIVE["output"][0]
    adapter = NativeResponsesStreamAdapter(display_model="fixture")
    wire = ("data: " + json.dumps({"type": "response.output_item.done", "output_index": 0, "item": item}) + "\n\n").encode()
    assert adapter.consume_bytes(wire)[0]["item"] == item
    adapter.consume_bytes(b'data: {"type":"fixture","data":"' + b'x' * 1000)
    final = adapter.finish()[0]["response"]
    assert final["status"] == "failed" and final["error"]["code"] == "provider_output_limit"
    assert final["output"] == [item]


def test_global_admission_rejects_without_reading_and_returns_on_cancel(policy):
    _, admission, _, _ = policy(inflight=2, route_inflight=2)
    async def scenario():
        entered = 0
        both = asyncio.Event()
        async def provider(*args, **kw):
            nonlocal entered
            entered += 1
            if entered == 2: both.set()
            await asyncio.Future()
        server.create_openai_upstream_response.side_effect = provider
        tasks = [asyncio.create_task(server.create_response(RawRequest())) for _ in range(2)]
        await both.wait()
        rejected = RawRequest()
        with pytest.raises(HTTPException) as caught: await server.create_response(rejected)
        assert caught.value.status_code == 503 and caught.value.headers["Retry-After"] == "1"
        assert caught.value.detail["code"] == "gateway_overloaded" and rejected.read_chunks == 0
        for task in tasks: task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        assert admission.total == 0 and not admission.routes
    asyncio.run(scenario())


def test_route_admission_does_not_block_another_route(policy):
    _, admission, native, _ = policy(inflight=3, route_inflight=1)
    async def scenario():
        entered = asyncio.Event()
        async def provider(*args, **kw):
            entered.set()
            await asyncio.Future()
        native.side_effect = provider
        task = asyncio.create_task(server.create_response(RawRequest()))
        await entered.wait()
        with pytest.raises(HTTPException) as caught: await server.create_response(RawRequest())
        assert caught.value.detail["code"] == "route_overloaded"
        result = await server.create_response(RawRequest({"model": "fixture:model", "input": "fixture"}))
        assert result["status"] == "completed" and admission.total == 1
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert admission.total == 0 and not admission.routes
    asyncio.run(scenario())


@pytest.mark.parametrize("failure", [HTTPException(401, "fixture"), RuntimeError("fixture"), ClientDisconnect()])
def test_permits_return_on_every_provider_error(policy, failure):
    _, admission, native, _ = policy(inflight=1)
    native.side_effect = failure
    with pytest.raises(type(failure)): asyncio.run(server.create_response(RawRequest()))
    assert admission.total == 0 and not admission.routes


def test_permit_release_is_idempotent_and_route_rejection_does_not_underflow():
    admission = Admission(); configured = replace(ResourceLimits(), inflight=2, route_inflight=1)
    one, two = admission.acquire(configured), admission.acquire(configured)
    one.bind_route("google")
    with pytest.raises(ResourceLimitError): two.bind_route("google")
    two.release(); two.release(); one.release(); one.release()
    assert admission.total == 0 and not admission.routes


@pytest.mark.parametrize("name,value", [("BODY_BYTES", "fixture-secret"), ("JSON_DEPTH", "0"), ("INFLIGHT", "9999"),
                                       ("SSE_TOTAL_BYTES", "-1"), ("BODY_BYTES", "9" * 1000)])
def test_invalid_operator_limits_fail_without_echo(monkeypatch, name, value):
    monkeypatch.setenv("ANTIGRAVITY_MAX_" + name, value)
    with pytest.raises(ValueError) as caught: ResourceLimits.from_env()
    assert value not in str(caught.value)


def wire(event):
    return ("data: " + json.dumps(event) + "\n\n").encode()


def install_stream_client(monkeypatch, chunks, *, headers=None):
    closed = []
    class Response:
        status_code = 200
        def __init__(self): self.headers = headers or {}
        async def aiter_bytes(self):
            for chunk in chunks:
                if chunk is None: await asyncio.Future()
                yield chunk
        async def aread(self): raise AssertionError("unbounded read forbidden")
    class Context:
        async def __aenter__(self): return Response()
        async def __aexit__(self, *args): closed.append("response")
    class Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): closed.append("client")
        async def aclose(self): closed.append("client")
        def stream(self, *args, **kwargs): return Context()
        async def post(self, *args, **kwargs): raise AssertionError("buffered HTTP post forbidden")
    monkeypatch.setattr(server.httpx, "AsyncClient", lambda **kw: Client())
    monkeypatch.setattr(server, "openai_responses_url", lambda auth: "https://example.invalid/responses")
    monkeypatch.setattr(server, "openai_request_headers", lambda auth: {"Authorization": "Bearer synthetic-only"})
    return closed


def test_stream_permit_is_held_until_body_closes(policy, monkeypatch):
    _, admission, _, _ = policy(inflight=1)
    closed = install_stream_client(monkeypatch, [wire({"type": "response.created", "response": {"id": "fixture", "status": "in_progress", "output": []}}), None])
    async def scenario():
        response = await server.create_response(RawRequest({"model": "gpt-5.2", "input": "fixture", "stream": True}))
        assert admission.total == 1
        await anext(response.body_iterator)
        with pytest.raises(HTTPException) as caught: await server.create_response(RawRequest())
        assert caught.value.status_code == 503
        await response.body_iterator.aclose()
        assert admission.total == 0 and not admission.routes
    asyncio.run(scenario())
    assert sorted(closed) == ["client", "response"]


def test_preiteration_disconnect_returns_stream_permit(policy, monkeypatch):
    _, admission, _, _ = policy(inflight=1)
    closed = install_stream_client(monkeypatch, [None])
    async def scenario():
        response = await server.create_response(RawRequest({"model": "gpt-5.2", "input": "fixture", "stream": True}))
        async def receive(): return {"type": "http.disconnect"}
        async def send(message): pass
        await response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)
        assert admission.total == 0
    asyncio.run(scenario())
    assert sorted(closed) == ["client", "response"]


def test_oauth_collection_is_incremental_and_stops_on_stream_limit(policy, monkeypatch):
    # Exercise the real collector while using only the synthetic HTTP client.
    policy(sse_total_bytes=500)
    completed = wire({"type": "response.output_item.done", "output_index": 0, "item": NATIVE["output"][0]})
    closed = install_stream_client(monkeypatch, [completed, b'data: {"padding":"' + b'x' * 1000, None])
    result = asyncio.run(REAL_NATIVE_RESPONSE({}, "fixture", SimpleNamespace(kind="codex_oauth"), "fixture"))
    assert result["status"] == "failed" and result["error"]["code"] == "provider_output_limit"
    assert result["output"] == NATIVE["output"]
    assert sorted(closed) == ["client", "response"]


@pytest.mark.parametrize("declared", [True, False])
def test_provider_json_body_is_bounded_before_collection(policy, monkeypatch, declared):
    policy(provider_body_bytes=128)
    closed = install_stream_client(monkeypatch, [b'{"x":"' + b'x' * 500 + b'"}', None],
                                   headers={"content-length": "507"} if declared else {})
    with pytest.raises(ResourceLimitError) as caught:
        asyncio.run(REAL_NATIVE_RESPONSE({}, "fixture", SimpleNamespace(kind="api_key"), "fixture"))
    assert caught.value.status == 502 and caught.value.code == "provider_body_limit"
    assert sorted(closed) == ["client", "response"]


def test_compressed_provider_body_is_not_decoded_twice(policy, monkeypatch):
    policy()
    closed = install_stream_client(monkeypatch, [json.dumps(NATIVE).encode()], headers={"content-encoding": "gzip"})
    result = asyncio.run(REAL_NATIVE_RESPONSE({}, "fixture", SimpleNamespace(kind="api_key"), "fixture"))
    assert result["status"] == "completed"
    assert sorted(closed) == ["client", "response"]


def test_google_output_limit_preserves_visible_text_without_rotation(policy, monkeypatch):
    _, admission, _, _ = policy(sse_total_bytes=200)
    monkeypatch.setattr(server, "classify_route", lambda *a, **kw: "google")
    acquire = AsyncMock(return_value={"email": "fixture@example.invalid", "accessToken": "synthetic-only"})
    release = AsyncMock()
    monkeypatch.setattr(server, "acquire_active_account_for_request", acquire)
    monkeypatch.setattr(server, "release_account_for_request", release)
    monkeypatch.setattr(server, "record_attempt_outcome", AsyncMock())
    first = wire({"candidates": [{"content": {"parts": [{"text": "kept fixture"}]}}]})
    closed = install_stream_client(monkeypatch, [first, b'data: {"padding":"' + b'x' * 1000, None])
    async def scenario():
        response = await server.create_response(RawRequest({"model": "gemini-3.8-flash", "input": "fixture", "stream": True}))
        return [chunk async for chunk in response.body_iterator]
    chunks = asyncio.run(scenario())
    events = [json.loads(line[6:]) for chunk in chunks for line in chunk.splitlines()
              if line.startswith("data: ") and line != "data: [DONE]"]
    final = next(event["response"] for event in events if event["type"] == "response.failed")
    assert final["error"]["code"] == "provider_output_limit"
    assert any(part.get("text") == "kept fixture" for item in final["output"] for part in item.get("content", []))
    assert acquire.await_count == release.await_count == 1
    assert admission.total == 0 and sorted(closed) == ["client", "response"]


def test_schema_expansion_budget_is_shared_across_all_tools(policy):
    from codex_antigravity_auth.transform import transform_request
    policy(body_bytes=1400)
    schema = {"type": "object", "properties": {"value": {"type": "string"}}}
    clean_json_schema(schema)  # One inline schema fits; generated placeholders share the aggregate budget.
    request = {"model": "gemini-3.8-flash", "input": "fixture", "tools": [
        {"type": "function", "name": f"fixture_{index}", "parameters": schema} for index in range(10)
    ]}
    assert len(json.dumps(request)) < 1400
    with pytest.raises(ResourceLimitError) as caught: transform_request(request)
    assert caught.value.code == "schema_expansion_limit"


def test_body_disconnect_and_malformed_json_return_the_global_permit(policy):
    _, admission, native, _ = policy(inflight=1)
    class Disconnected(RawRequest):
        async def stream(self):
            yield b'{"input":'
            raise ClientDisconnect()
    with pytest.raises(ClientDisconnect): asyncio.run(server.create_response(Disconnected()))
    assert admission.total == 0
    with pytest.raises(HTTPException) as caught: asyncio.run(server.create_response(RawRequest(chunks=[b'{broken'])))
    assert caught.value.status_code == 400 and admission.total == 0 and not native.called


def test_provider_depth_limit_becomes_explicit_upstream_failure(policy, monkeypatch):
    policy(json_depth=8)
    closed = install_stream_client(monkeypatch, [b'{"x":' + b'[' * 20 + b'0' + b']' * 20 + b'}'])
    with pytest.raises(ResourceLimitError) as caught:
        asyncio.run(REAL_NATIVE_RESPONSE({}, "fixture", SimpleNamespace(kind="api_key"), "fixture"))
    assert caught.value.status == 502 and caught.value.code == "json_depth_limit"
    assert sorted(closed) == ["client", "response"]


def test_impossible_reference_path_is_rejected_before_splitting(policy):
    policy(schema_depth=8)
    with pytest.raises(ResourceLimitError): clean_json_schema({"$ref": "#/" + "/" * 10000})


def test_invalid_policy_refuses_gateway_startup(monkeypatch):
    monkeypatch.setenv("ANTIGRAVITY_MAX_INFLIGHT", "fixture-secret")
    started = []
    monkeypatch.setattr(server, "_RefreshAheadOwner", lambda: started.append(True))
    async def scenario():
        with pytest.raises(ValueError, match="ANTIGRAVITY_MAX_INFLIGHT"):
            async with server.gateway_lifespan(server.app):
                raise AssertionError("invalid policy must not serve")
    asyncio.run(scenario())
    assert not started


def test_native_limit_failure_survives_conflicting_complete_item_aggregate(policy):
    policy(sse_total_bytes=1000)
    adapter = NativeResponsesStreamAdapter(display_model="fixture")
    for index in range(2):
        item = {"type": "function_call", "id": f"fc_{index}", "call_id": "same_call", "name": "fixture", "arguments": "{}"}
        adapter.consume_bytes(wire({"type": "response.output_item.done", "output_index": index, "item": item}))
    adapter.consume_bytes(b"data: " + b"x" * 2000)
    result = adapter.finish()
    assert len(result) == 1 and result[0]["type"] == "response.failed"
    assert result[0]["response"]["error"]["code"] == "provider_output_limit"
    assert result[0]["response"]["output"] == []


class AppendOnlyDelta(str):
    def __add__(self, other):
        raise AssertionError("delta assembly copied a growing string prefix")
    def __radd__(self, other):
        raise AssertionError("delta assembly copied a growing string prefix")


def test_event_builder_tiny_deltas_are_deferred_without_prefix_copying():
    from codex_antigravity_auth.response_protocol import ResponseEventBuilder
    with pytest.raises(AssertionError):
        _ = "" + AppendOnlyDelta("x")  # Calibrate the non-timing complexity guard.
    builder = ResponseEventBuilder(response_id="fixture", model="fixture", created_at=1)
    builder.created()
    for _ in range(2000):
        builder.add_text_delta(AppendOnlyDelta("é"))
        builder.add_reasoning_delta(AppendOnlyDelta("r"))
    text = builder.finish_text()
    reasoning = builder.finish_reasoning()
    assert text[0]["text"] == "é" * 2000
    assert reasoning[0]["text"] == "r" * 2000
    assert text[-1]["item"]["content"][0]["text"] == "é" * 2000


@pytest.mark.parametrize("provider", ["google", "chat"])
def test_provider_accumulators_do_not_copy_prefixes_for_tiny_deltas(provider):
    from codex_antigravity_auth.google_transport import GoogleResponseAccumulator
    from codex_antigravity_auth.openai_transport import ChatResponseAccumulator
    accumulator = GoogleResponseAccumulator() if provider == "google" else ChatResponseAccumulator()
    for index in range(2000):
        if provider == "google":
            accumulator.consume({"candidates": [{"content": {"parts": [
                {"text": AppendOnlyDelta("x")}, {"thought": True, "text": AppendOnlyDelta("r")}]} }]})
        else:
            arguments = '{"x":"' if index == 0 else "x"
            call = {"index": 0, "function": {
                "name": AppendOnlyDelta("lookup") if index == 0 else "",
                "arguments": AppendOnlyDelta(arguments),
            }}
            if index == 0:
                call["id"] = "call_fixture"
            accumulator.consume({"choices": [{"delta": {"content": AppendOnlyDelta("x"), "reasoning_content": AppendOnlyDelta("r"),
                "tool_calls": [call]}}]})
    if provider == "chat":
        accumulator.consume({"choices": [{"delta": {"tool_calls": [{
            "index": 0, "function": {"arguments": AppendOnlyDelta('"}')}
        }]}}]})
    accumulator.mark_done()
    result = accumulator.finalize()
    text = next(item for item in result.output if item["type"] == "message")
    reasoning = next(item for item in result.output if item["type"] == "reasoning")
    assert text["content"][0]["text"] == "x" * 2000
    assert reasoning["step_by_step_summary"] == "r" * 2000
    if provider == "chat":
        call = next(item for item in result.output if item["type"] == "function_call")
        assert call["name"] == "lookup" and call["call_id"] == "call_fixture"
        assert call["arguments"] == '{"x":"' + "x" * 1999 + '"}'


@pytest.mark.parametrize("provider", ["google", "chat"])
def test_empty_fragments_still_produce_no_output(provider):
    from codex_antigravity_auth.google_transport import GoogleResponseAccumulator
    from codex_antigravity_auth.openai_transport import ChatResponseAccumulator
    accumulator = GoogleResponseAccumulator() if provider == "google" else ChatResponseAccumulator()
    payload = {"candidates": [{"content": {"parts": [{"text": ""}, {"thought": True, "text": ""}]}}]} if provider == "google" else {
        "choices": [{"delta": {"content": "", "reasoning_content": "", "tool_calls": [{"index": 0, "function": {"name": "", "arguments": ""}}]}}]}
    accumulator.consume(payload)
    accumulator.mark_done()
    assert accumulator.finalize().output == ()


def test_byok_stream_tool_assembly_avoids_prefix_copying(monkeypatch):
    from codex_antigravity_auth import openai_transport as transport_module
    original = transport_module.parse_sse_payload
    def guarded(data, **kwargs):
        payload = original(data, **kwargs)
        for choice in payload.get("choices", []):
            delta = choice.get("delta", {})
            for field in ("content", "reasoning_content"):
                if isinstance(delta.get(field), str): delta[field] = AppendOnlyDelta(delta[field])
            for call in delta.get("tool_calls", []):
                function = call.get("function", {})
                for field in ("name", "arguments"):
                    if isinstance(function.get(field), str): function[field] = AppendOnlyDelta(function[field])
        return payload
    monkeypatch.setattr(transport_module, "parse_sse_payload", guarded)
    class Response:
        status_code = 200
        headers = {}
        async def aiter_bytes(self):
            yield wire({"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_fixture", "function": {"name": "lookup", "arguments": '{"x":"'}}]}}]})
            for _ in range(2000):
                yield wire({"choices": [{"delta": {"content": "x", "reasoning_content": "r", "tool_calls": [{"index": 0, "function": {"arguments": "x"}}]}}]})
            yield wire({"choices": [{"finish_reason": "tool_calls", "delta": {"tool_calls": [{"index": 0, "function": {"arguments": '"}'}}]}}]})
            yield b"data: [DONE]\n\n"
    class Context:
        async def __aenter__(self): return Response()
        async def __aexit__(self, *args): pass
    class Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        def stream(self, *args, **kwargs): return Context()
    transport = transport_module.OpenAICompatibleTransport(timeout=1, client_factory=lambda **kw: Client())
    prepared = transport_module.PreparedOpenAIRequest({"tools": [{"type": "function", "function": {
        "name": "lookup", "parameters": {"type": "object", "properties": {"x": {"type": "string"}}},
    }}]}, "https://example.invalid", {}, 1)
    async def scenario():
        return [event async for event in transport.stream_chat_events(prepared, response_id="fixture", display_model="fixture")]
    events = asyncio.run(scenario())
    final = next(event["response"] for event in events if isinstance(event, dict) and event.get("type") == "response.completed")
    call = next(item for item in final["output"] if item["type"] == "function_call")
    assert call["arguments"] == '{"x":"' + 'x' * 2000 + '"}'
    assert call["call_id"] == "call_fixture"


def test_legacy_multiline_framing_parses_a_complete_root_once(monkeypatch):
    from codex_antigravity_auth import sse
    original = sse._complete_legacy_payload
    calls = []
    def counted(value, limits=None):
        calls.append(len(value))
        return original(value, limits)
    monkeypatch.setattr(sse, "_complete_legacy_payload", counted)
    decoder = SSEDecoder(legacy_json_lines=True)
    wire_bytes = b'data: {"fixture":\n' + b'data:  \n' * 2000 + b'data: 1}\n'
    output = [value for byte in wire_bytes for value in decoder.feed(bytes([byte]))]
    assert len(output) == 1 and json.loads(output[0]) == {"fixture": 1}
    assert len(calls) == 1


def test_legacy_invalid_balanced_prefix_is_not_reparsed_for_each_line(monkeypatch):
    from codex_antigravity_auth import sse
    original = sse._complete_legacy_payload
    calls = []
    def counted(value, limits=None):
        calls.append(1)
        return original(value, limits)
    monkeypatch.setattr(sse, "_complete_legacy_payload", counted)
    decoder = SSEDecoder(legacy_json_lines=True)
    output = list(decoder.feed(b'data: invalid\n' * 2000 + b'\n'))
    assert len(output) == 1 and len(calls) == 1
    with pytest.raises(json.JSONDecodeError): json.loads(output[0])


def test_legacy_lexical_scan_ignores_string_braces_and_resets_between_roots():
    decoder = SSEDecoder(legacy_json_lines=True)
    values = [{"text": 'braces }[ and escaped " quote \\'}, {"next": [1, 2]}]
    output = list(decoder.feed(b''.join(b'data: ' + json.dumps(value).encode() + b'\n' for value in values)))
    assert list(map(json.loads, output)) == values
