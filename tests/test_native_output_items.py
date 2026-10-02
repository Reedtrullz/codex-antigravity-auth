"""Synthetic native continuation fixtures; no provider calls or real ciphertext."""
import asyncio
from copy import deepcopy
import json
from types import SimpleNamespace

import httpx
import pytest

from codex_antigravity_auth import native_output as native, server, unified
from codex_antigravity_auth.openai_transport import NativeResponsesStreamAdapter
from codex_antigravity_auth.response_protocol import meaningful_output_items

OPAQUE = "fixture-opaque-continuation-only-not-a-real-token"


def items():
    return [
        {"type": "reasoning", "id": "rs_fixture", "summary": [], "encrypted_content": OPAQUE},
        {"type": "web_search_call", "id": "ws_fixture", "status": "completed", "action": {
            "type": "search", "queries": ["fixture query"],
            "sources": [{"type": "url", "url": "https://example.invalid/source?q=fixture#fragment"}]}},
        {"type": "message", "id": "msg_fixture", "role": "assistant", "status": "completed", "phase": "final_answer",
         "content": [{"type": "output_text", "text": "Fixture citation.", "annotations": [
             {"type": "url_citation", "start_index": 0, "end_index": 7, "url": "https://example.invalid/", "title": "Fixture"}]}]},
        {"type": "function_call", "id": "fc_fixture", "call_id": "call_fixture", "name": "fixture",
         "arguments": '{"fixture":1}', "status": "completed", "namespace": "fixture_tools", "async": False,
         "caller": {"type": "direct"}},
        {"type": "custom_tool_call", "id": "ct_fixture", "call_id": "custom_fixture", "name": "fixture_custom",
         "input": "opaque tool syntax", "status": "completed", "caller": {"type": "program", "caller_id": "program_fixture"}},
    ]


def response(output=None, status="completed"):
    return {"id": "resp_fixture", "object": "response", "model": "upstream-fixture", "status": status,
            "output": items() if output is None else output,
            "usage": {"input_tokens": 2, "output_tokens": 3, "total_tokens": 5}}


def frame(event):
    return ("data: " + json.dumps(event) + "\n\n").encode()


def terminal(payload):
    return {"type": "response." + payload["status"], "response": payload}


def stream_events(output=None, terminal_output=None):
    output = items() if output is None else output
    events = [{"type": "response.created", "response": {"id": "resp_fixture", "status": "in_progress", "output": []}}]
    for index, item in enumerate(output):
        added = deepcopy(item)
        if added["type"] == "reasoning":
            added["encrypted_content"] = "fixture-incomplete-prefix"
        events.extend([
            {"type": "response.output_item.added", "output_index": index, "item": added},
            {"type": "response.output_item.done", "output_index": index, "item": item},
        ])
    events.append(terminal(response(output if terminal_output is None else terminal_output)))
    return events


def collect(events):
    adapter = NativeResponsesStreamAdapter(display_model="display-fixture")
    emitted = [event for value in events for event in adapter.consume_bytes(frame(value))]
    emitted.extend(adapter.finish())
    assert len([event for event in emitted if event["type"] in adapter._TERMINAL_TYPES]) == 1
    return emitted


def test_json_preserves_order_ids_phase_opaque_data_and_input_replay():
    payload = response()
    original = deepcopy(payload)
    normalized = native.validate_response(payload, display_model="display-fixture")
    assert normalized == {**original, "model": "display-fixture"}
    replay = unified.build_openai_payload({"input": normalized["output"]}, "upstream-fixture", stream=True)
    assert replay["input"] == original["output"]
    normalized["output"][0]["encrypted_content"] = "changed"
    assert payload == original
    # Translation heuristics are intentionally still separate.
    assert not meaningful_output_items([original["output"][0]])


@pytest.mark.parametrize("status", ["completed", "incomplete", "failed"])
def test_stream_and_buffered_routes_preserve_complete_mixed_output(status):
    events = stream_events()
    events[-1] = terminal(response(status=status))
    emitted = collect(events)
    buffered = server._collect_openai_sse_terminal(b"".join(map(frame, events)), "display-fixture")
    expected = native.validate_response(response(status=status), display_model="display-fixture")
    assert buffered == emitted[-1]["response"] == expected
    assert [event["item"] for event in emitted if event["type"] == "response.output_item.done"] == items()


@pytest.mark.parametrize("terminal_shape", ["empty", "missing_field", "null_field", "same"])
def test_only_completed_reasoning_snapshot_supplies_opaque_continuation(terminal_shape):
    output = [items()[0]]
    snapshot = deepcopy(output)
    if terminal_shape == "empty":
        snapshot = []
    elif terminal_shape == "missing_field":
        del snapshot[0]["encrypted_content"]
    elif terminal_shape == "null_field":
        snapshot[0]["encrypted_content"] = None
    final = collect(stream_events(output, snapshot))[-1]
    assert final["type"] == "response.completed"
    assert final["response"]["output"] == output


def test_added_only_reasoning_is_not_used_to_fill_missing_terminal_item():
    events = stream_events([items()[0]], [])
    del events[2]  # no output_item.done
    final = collect(events)[-1]
    assert final["response"]["error"]["code"] == "incomplete_native_output"
    assert OPAQUE not in json.dumps(final)


def test_completed_snapshot_does_not_alias_forwarded_item():
    adapter = NativeResponsesStreamAdapter(display_model="fixture")
    event = {"type": "response.output_item.done", "output_index": 0, "item": items()[0]}
    forwarded = adapter.consume_bytes(frame(event))
    forwarded[0]["item"]["encrypted_content"] = "caller-mutated"
    adapter.consume_bytes(frame(terminal(response([]))))
    assert adapter.finish()[0]["response"]["output"] == [items()[0]]


@pytest.mark.parametrize("mutate,code", [
    (lambda rows: rows[0].update(encrypted_content="conflicting-fixture"), "conflicting_native_item"),
    (lambda rows: rows.__setitem__(0, {"type": "message", "id": "rs_fixture", "content": []}), "conflicting_native_item"),
    (lambda rows: rows[0].update(id="rs_different"), "mismatched_item_id"),
])
def test_conflicting_terminal_snapshot_fails_without_exposing_opaque_data(mutate, code):
    snapshot = items()
    mutate(snapshot)
    final = collect(stream_events(terminal_output=snapshot))[-1]
    assert final["response"]["error"]["code"] == code
    assert OPAQUE not in json.dumps(final)


@pytest.mark.parametrize("mutate", [
    lambda rows: rows[0].update(type="future_tool_call"),
    lambda rows: rows[0].update(unknown_field="fixture-secret"),
    lambda rows: rows[0].update(encrypted_content={"invalid": "fixture-secret"}),
    lambda rows: rows[0].update(summary=[{"type": "summary_text", "text": 12}]),
    lambda rows: rows[0].update(id="\ud800"),
    lambda rows: rows[1].update(action={"type": "unsupported_action"}),
    lambda rows: rows[1].update(action={"type": "open_page", "url": "javascript:fixture"}),
    lambda rows: rows[1].update(status="unknown"),
    lambda rows: rows[2].update(role="user"),
    lambda rows: rows[2].update(phase="unknown"),
    lambda rows: rows[2]["content"][0].update(annotations=[{"type": "future_citation"}]),
    lambda rows: rows[2]["content"][0]["annotations"][0].update(start_index=True),
    lambda rows: rows[3].update(arguments={}),
    lambda rows: rows[4].update(input=None),
    lambda rows: rows[4].update(call_id=rows[3]["call_id"]),
    lambda rows: rows[4].update(id=rows[3]["id"]),
])
def test_malformed_or_unsupported_items_never_succeed_silently(mutate):
    output = items()
    mutate(output)
    direct = native.validate_response(response(output), display_model="fixture")
    assert direct["status"] == "failed" and direct["output"] == []
    assert "contract v1" in direct["error"]["message"]
    assert OPAQUE not in json.dumps(direct) and "fixture-secret" not in json.dumps(direct)
    streamed = collect([terminal(response(output))])[-1]
    assert streamed["type"] == "response.failed"
    assert OPAQUE not in json.dumps(streamed)


@pytest.mark.parametrize("name,limit", [("MAX_BYTES", 16), ("MAX_NODES", 5), ("MAX_DEPTH", 1), ("MAX_ITEMS", 2)])
def test_decoded_output_limits_fail_explicitly(monkeypatch, name, limit):
    monkeypatch.setattr(native, name, limit)
    result = native.validate_response(response(), display_model="fixture")
    assert result["error"]["code"] == "native_output_limit"


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), 2**100, "\ud800"])
def test_invalid_json_scalars_in_provider_envelope_are_rejected(bad):
    payload = response()
    payload["extra"] = bad
    assert native.validate_response(payload, display_model="fixture")["status"] == "failed"


@pytest.mark.parametrize("action", [
    {"type": "search", "query": "fixture"}, {"type": "open_page", "url": "https://example.invalid/page"},
    {"type": "find_in_page", "url": "https://example.invalid/page", "pattern": "fixture"},
])
def test_documented_search_actions_round_trip(action):
    item = {**items()[1], "action": action}
    assert native.validate_output([item]) == [item]


@pytest.mark.parametrize("event", [
    {"type": "response.reasoning_summary_part.added", "part": {"type": "summary_text", "text": ""}},
    {"type": "response.reasoning_summary_text.delta", "delta": "summary fixture"},
    {"type": "response.reasoning_text.done", "text": "reasoning fixture"},
    {"type": "response.web_search_call.searching"},
    {"type": "response.custom_tool_call_input.delta", "delta": "custom fixture"},
    {"type": "response.function_call_arguments.done", "arguments": "{}"},
])
def test_supported_structured_event_payloads_are_forwarded(event):
    assert collect([event])[0] == event


def test_unsupported_event_does_not_reach_client():
    event = {"type": "response.future_tool.delta", "delta": OPAQUE}
    emitted = collect([event, terminal(response())])
    assert len(emitted) == 1
    assert emitted[0]["response"]["error"]["code"] == "unsupported_native_event"
    assert OPAQUE not in json.dumps(emitted)


@pytest.mark.parametrize("mode", ["duplicate", "gap", "missing_tail"])
def test_completed_item_registry_rejects_incomplete_or_duplicate_state(mode):
    event = {"type": "response.output_item.done", "output_index": 0, "item": items()[0]}
    events = [event]
    if mode == "duplicate":
        events.append(event)
    elif mode == "gap":
        event["output_index"] = 1
    else:
        events = [{"type": "response.output_item.added", "output_index": 0, "item": items()[0]}]
    final = collect(events + [terminal(response([]))])[-1]
    assert final["type"] == "response.failed"


@pytest.mark.parametrize("kind", ["api_key", "codex_oauth"])
@pytest.mark.parametrize("unsupported", [False, True])
def test_actual_buffered_upstream_routes_use_native_validation(monkeypatch, kind, unsupported):
    payload = response()
    if unsupported:
        payload["output"][0]["type"] = "future_tool_call"
    sent = []
    class Client:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def post(self, url, **kwargs):
            sent.append(kwargs["json"])
            return httpx.Response(200, json=payload) if kind == "api_key" else httpx.Response(200, content=frame(terminal(payload)))
    monkeypatch.setattr(server.httpx, "AsyncClient", lambda **kwargs: Client())
    monkeypatch.setattr(server, "openai_responses_url", lambda auth: "https://example.invalid/responses")
    monkeypatch.setattr(server, "openai_request_headers", lambda auth: {"Authorization": "Bearer fixture-only"})
    request = {"input": items(), "store": False}
    result = asyncio.run(server.create_openai_upstream_response(request, "upstream-fixture", SimpleNamespace(kind=kind), "display-fixture"))
    assert sent[0]["input"] == request["input"]
    assert result["status"] == ("failed" if unsupported else "completed")
    assert result["output"] == ([] if unsupported else items())


def test_actual_stream_generator_preserves_complete_items_and_closes():
    class Client:
        closed = False
        async def aclose(self):
            self.closed = True
    class Context:
        closed = False
        async def __aexit__(self, *args):
            self.closed = True
    class Response:
        async def aiter_bytes(self):
            for event in stream_events():
                yield frame(event)
    client, context = Client(), Context()
    async def run():
        return [chunk async for chunk in server.openai_upstream_sse_generator(
            {}, "fixture", None, "display-fixture", stream_state=(client, context, Response()))]
    chunks = asyncio.run(run())
    emitted = [json.loads(line[6:]) for chunk in chunks for line in chunk.splitlines()
               if line.startswith("data: ") and line != "data: [DONE]"]
    assert emitted[-1]["response"] == native.validate_response(response(), display_model="display-fixture")
    assert client.closed and context.closed


@pytest.mark.parametrize("annotation", [
    {"type": "file_citation", "file_id": "file_fixture", "filename": "fixture.txt", "index": 0},
    {"type": "container_file_citation", "container_id": "container_fixture", "file_id": "file_fixture",
     "filename": "fixture.txt", "start_index": 0, "end_index": 7},
    {"type": "file_path", "file_id": "file_fixture", "index": 0},
])
def test_other_documented_citations_and_logprobs_remain_unchanged(annotation):
    item = items()[2]
    item["content"][0]["annotations"] = [annotation]
    item["content"][0]["logprobs"] = [{"token": "Fixture", "bytes": [70, 105], "logprob": -0.1,
                                       "top_logprobs": [{"token": "Other", "bytes": [79], "logprob": -2}]}]
    assert native.validate_output([item]) == [item]


def test_opaque_only_and_refusal_outputs_are_not_prose_filtered():
    for item in (items()[0], {**items()[2], "content": [{"type": "refusal", "refusal": "fixture refusal"}]}):
        final = native.validate_response(response([item]), display_model="fixture")
        assert final["status"] == "completed" and final["output"] == [item]
    assert native.validate_response(response([]), display_model="fixture")["error"]["code"] == "empty_response"


def test_aggregate_completed_snapshot_budget_is_bounded(monkeypatch):
    monkeypatch.setattr(native, "MAX_BYTES", 1500)
    output = [{"type": "reasoning", "id": f"rs_{index}", "summary": [], "encrypted_content": "x" * 600}
              for index in range(3)]
    events = [{"type": "response.output_item.done", "output_index": index, "item": item}
              for index, item in enumerate(output)]
    final = collect(events)[-1]
    assert final["response"]["error"]["code"] == "native_output_limit"


def test_provider_failure_keeps_safe_code_but_never_raw_message():
    payload = response([], "failed")
    payload["error"] = {"code": "server_error", "message": OPAQUE}
    result = native.validate_response(payload, display_model="fixture")
    assert result["error"]["code"] == "server_error"
    assert OPAQUE not in json.dumps(result)


@pytest.mark.parametrize("field,bad", [("input_tokens", 10**400), ("output_tokens", 10**400),
                                      ("total_tokens", 10**400), ("input_tokens", "9" * 1000)])
def test_validation_failure_handles_untrusted_usage_without_overflow(field, bad):
    payload = response()
    payload["output"][0]["type"] = "future_tool_call"
    payload["usage"][field] = bad
    direct = native.validate_response(payload, display_model="fixture")
    assert direct["status"] == "failed"
    assert direct["usage"][field] != bad
    assert all(type(value) is int and 0 <= value <= 2**63 - 1 for value in direct["usage"].values())
    streamed = collect([terminal(payload)])[-1]
    buffered = server._collect_openai_sse_terminal(frame(terminal(payload)), "fixture")
    assert streamed["type"] == "response.failed" and buffered["status"] == "failed"
    assert OPAQUE not in json.dumps(direct) + json.dumps(streamed) + json.dumps(buffered)


@pytest.mark.parametrize("omission", ["annotations", "logprobs", "queries", "sources", "source_url", "citation_title"])
def test_done_snapshots_supply_omitted_nested_fields(omission):
    output = items()
    output[2]["content"][0]["logprobs"] = [{"token": "Fixture", "bytes": [70], "logprob": -0.1}]
    snapshot = deepcopy(output)
    if omission in {"annotations", "logprobs"}:
        del snapshot[2]["content"][0][omission]
    elif omission == "queries":
        del snapshot[1]["action"]["queries"]
    elif omission == "sources":
        del snapshot[1]["action"]["sources"]
    elif omission == "source_url":
        del snapshot[1]["action"]["sources"][0]["url"]
    else:
        del snapshot[2]["content"][0]["annotations"][0]["title"]
    final = collect(stream_events(output, snapshot))[-1]
    assert final["type"] == "response.completed"
    assert final["response"]["output"] == output
    assert server._collect_openai_sse_terminal(b"".join(map(frame, stream_events(output, snapshot))), "fixture")["output"] == output


@pytest.mark.parametrize("mutate", [
    lambda output: output[2]["content"][0]["annotations"][0].update(title="conflicting title"),
    lambda output: output[2]["content"][0].update(annotations=[]),
    lambda output: output[1]["action"].update(queries=["conflicting query"]),
])
def test_nested_disagreement_still_fails(mutate):
    snapshot = items()
    mutate(snapshot)
    assert collect(stream_events(terminal_output=snapshot))[-1]["response"]["error"]["code"] == "conflicting_native_item"


def test_nullable_annotation_event_is_forwarded_without_loss():
    event = {"type": "response.output_text.annotation.added", "item_id": "msg_fixture", "output_index": 0,
             "content_index": 0, "annotation_index": 0, "annotation": None}
    output = [items()[2]]
    emitted = collect([event, terminal(response(output))])
    assert emitted[0] == event
    assert emitted[-1]["type"] == "response.completed"
    assert emitted[-1]["response"]["output"] == output
