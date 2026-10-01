"""Native authority is committed once; all upstreams and credentials are synthetic."""

import asyncio
import json

import httpx
import pytest

from codex_antigravity_auth import server
from codex_antigravity_auth.openai_transport import NativeResponsesStreamAdapter


TERMINALS = {"response.completed", "response.incomplete", "response.failed"}
DONE = b"data: [DONE]\n\n"


def wire(event):
    return ("data: " + json.dumps(event) + "\n\n").encode()


def created():
    return {"type": "response.created", "sequence_number": 1,
            "response": {"id": "resp-fixture", "status": "in_progress", "output": []}}


def delta(**extra):
    return {"type": "response.output_text.delta", "sequence_number": 3,
            "item_id": "msg-fixture", "output_index": 0, "delta": "fixture answer", **extra}


def completed(**extra):
    return {"type": "response.completed", "sequence_number": 4, "response": {
        "id": "resp-fixture", "status": "completed", "output": [{
            "id": "msg-fixture", "type": "message", "role": "assistant", "status": "completed",
            "content": [{"type": "output_text", "text": "fixture answer"}],
        }],
    }, **extra}


def terminal_result(chunks):
    adapter = NativeResponsesStreamAdapter(display_model="fixture-model")
    emitted = []
    for chunk in chunks:
        events = adapter.consume_bytes(chunk)
        assert not any(event["type"] in TERMINALS for event in events)
        emitted.extend(events)
    final = adapter.finish()
    assert len(final) == 1 and final[0]["type"] in TERMINALS
    assert adapter.finish() == []
    # EOF commitment is immutable; callers cannot reopen the finished stream.
    assert adapter.consume_bytes(DONE) == []
    assert adapter.finish() == []
    return emitted, final[0]


@pytest.mark.parametrize("tail,error", [
    (b"", None), (DONE, None), (DONE + DONE, "duplicate_done"),
    (DONE + wire(delta(sequence_number=5)), "output_after_done"),
    (wire(delta(sequence_number=5)), "output_after_terminal"),
    (wire(completed(sequence_number=5)), "duplicate_terminal"),
    (b"data: {not-json}\n\n", "invalid_stream_chunk"),
    (b"data: {", "invalid_stream_chunk"),
])
def test_stream_and_buffered_collection_agree_after_terminal(tail, error):
    chunks = [wire(created()), wire(delta()), wire(completed()), tail]
    emitted, final = terminal_result(chunks)
    assert any(event.get("delta") == "fixture answer" for event in emitted)
    buffered = server._collect_openai_sse_terminal(b"".join(chunks), "fixture-model")
    assert final["response"]["id"] == "resp-fixture"
    assert buffered["status"] == final["response"]["status"]
    if error:
        assert final["type"] == "response.failed"
        assert final["response"]["error"]["code"] == buffered["error"]["code"] == error
    else:
        assert final["type"] == "response.completed"


def test_early_protocol_failure_is_not_overwritten_by_later_terminal():
    _, final = terminal_result([wire(created()), b"data: {broken}\n\n", wire(completed()), DONE])
    assert final["response"]["error"]["code"] == "invalid_stream_chunk"


def test_provider_error_event_cannot_be_hidden_by_later_success():
    _, final = terminal_result([wire(created()), wire({"type": "error", "message": "synthetic-private-error"}), wire(completed()), DONE])
    assert final["response"]["error"]["code"] == "provider_error"
    assert "synthetic-private-error" not in json.dumps(final)


def test_pathological_trailing_json_produces_a_protocol_failure():
    depth = 2000
    deeply_nested = b'data: {"type":"fixture","value":' + b'[' * depth + b'0' + b']' * depth + b'}\n\n'
    _, final = terminal_result([wire(created()), wire(completed()), deeply_nested])
    # JSON implementations have different nesting limits. Parsed or rejected,
    # this trailing payload must never leave the earlier success authoritative.
    assert final["type"] == "response.failed"
    assert final["response"]["error"]["code"] in {"invalid_stream_chunk", "output_after_terminal"}


@pytest.mark.parametrize("status", ["completed", "incomplete", "failed"])
def test_each_native_terminal_kind_remains_authoritative_at_clean_eof(status):
    event = completed()
    event["type"] = f"response.{status}"
    event["response"]["status"] = status
    _, final = terminal_result([wire(event)])
    assert final["type"] == f"response.{status}"
    assert final["response"]["status"] == status


@pytest.mark.parametrize("chunks", [[], [DONE], [wire(created()), wire(delta())]])
def test_missing_terminal_fails_once(chunks):
    _, final = terminal_result(chunks)
    assert final["response"]["error"]["code"] == "missing_terminal_signal"


@pytest.mark.parametrize("mutation,error", [
    (lambda event: event["response"].update(id="resp-other"), "mismatched_response_id"),
    (lambda event: event.update(response_id="resp-other"), "mismatched_response_id"),
    (lambda event: event["response"]["output"][0].update(id="msg-other"), "mismatched_item_id"),
])
def test_explicit_identity_contradictions_fail(mutation, error):
    terminal = completed()
    mutation(terminal)
    _, final = terminal_result([wire(created()), wire(delta()), wire(terminal), DONE])
    assert final["response"]["error"]["code"] == error


@pytest.mark.parametrize("event_type", ["response.created", "response.in_progress"])
@pytest.mark.parametrize("item_id", ["msg-fixture", "msg-other"])
def test_every_supplied_response_snapshot_binds_item_identity(event_type, item_id):
    snapshot = created()
    snapshot["type"] = event_type
    snapshot["response"]["output"] = [{"id": item_id, "type": "message", "content": []}]
    chunks = [wire(snapshot), wire(completed()), DONE]
    _, final = terminal_result(chunks)
    buffered = server._collect_openai_sse_terminal(b"".join(chunks), "fixture-model")
    if item_id == "msg-fixture":
        assert final["type"] == "response.completed"
        assert buffered["status"] == "completed"
    else:
        assert final["type"] == "response.failed"
        assert final["response"]["error"]["code"] == buffered["error"]["code"] == "mismatched_item_id"


@pytest.mark.parametrize("event,error", [
    (delta(item_id="msg-other", sequence_number=4), "mismatched_item_id"),
    (delta(output_index=1, sequence_number=4), "mismatched_item_id"),
    (delta(output_index=-1, sequence_number=4), "invalid_output_index"),
    (delta(output_index=True, sequence_number=4), "invalid_output_index"),
    (delta(item_id={}, sequence_number=4), "invalid_stream_identity"),
    (delta(sequence_number=3), "invalid_stream_sequence"),
    (delta(sequence_number=2), "invalid_stream_sequence"),
    (delta(sequence_number=True), "invalid_stream_sequence"),
    (delta(sequence_number="4"), "invalid_stream_sequence"),
])
def test_supplied_item_and_sequence_fields_are_consistent(event, error):
    _, final = terminal_result([wire(created()), wire(delta()), wire(event), wire(completed(sequence_number=5))])
    assert final["response"]["error"]["code"] == error


def test_sequence_gaps_and_missing_optional_lifecycle_fields_remain_compatible():
    first = {"type": "response.output_text.delta", "delta": "fixture"}
    terminal = completed(sequence_number=100)
    terminal["response"].pop("id")
    _, final = terminal_result([wire(first), wire(terminal)])
    assert final["type"] == "response.completed"
    _, final = terminal_result([wire(created()), wire(delta(sequence_number=80)), wire(completed(sequence_number=100))])
    assert final["type"] == "response.completed"


def test_identity_bookkeeping_is_bounded():
    event = delta(item_id="x" * 65537)
    _, final = terminal_result([wire(event)])
    assert final["response"]["error"]["code"] == "stream_identity_limit"


class Client:
    def __init__(self):
        self.closed = 0

    async def aclose(self):
        self.closed += 1


class Context:
    def __init__(self):
        self.closed = 0

    async def __aexit__(self, *args):
        self.closed += 1


def parsed_events(chunks):
    return [json.loads(line[6:]) for chunk in chunks for line in chunk.splitlines()
            if line.startswith("data: ") and line != "data: [DONE]"]


@pytest.mark.parametrize("mode,error", [("eof", None), ("duplicate", "duplicate_done"),
                                       ("interrupted", "stream_interrupted"), ("timeout", "stream_timeout"),
                                       ("keepalive", "stream_timeout")])
def test_live_generator_commits_at_eof_or_bounded_failure_and_closes(monkeypatch, mode, error):
    monkeypatch.setattr(server, "OPENAI_UPSTREAM_TIMEOUT_SECONDS", 0.015)
    sent_comments = []
    class Response:
        async def aiter_bytes(self):
            yield wire(created()) + wire(delta()) + wire(completed())
            if mode == "duplicate":
                yield DONE
                yield DONE
            elif mode == "interrupted":
                raise httpx.ReadError("synthetic-private-error")
            elif mode == "timeout":
                await asyncio.sleep(5)
            elif mode == "keepalive":
                for _ in range(100):
                    await asyncio.sleep(0.001)
                    sent_comments.append(1)
                    yield b": keepalive\n\n"
    client, context = Client(), Context()
    async def collect():
        return [chunk async for chunk in server.openai_upstream_sse_generator(
            {}, "fixture-model", None, "fixture-model", stream_state=(client, context, Response()))]
    async def bounded():
        return await asyncio.wait_for(collect(), 1)
    chunks = asyncio.run(bounded())
    terminals = [event for event in parsed_events(chunks) if event["type"] in TERMINALS]
    assert len(terminals) == 1
    assert sum(chunk == "data: [DONE]\n\n" for chunk in chunks) == 1
    assert client.closed == context.closed == 1
    assert "synthetic-private-error" not in "".join(chunks)
    if error:
        assert terminals[0]["type"] == "response.failed"
        assert terminals[0]["response"]["error"]["code"] == error
    else:
        assert terminals[0]["type"] == "response.completed"
    if mode == "keepalive":
        assert len(sent_comments) < 100


def test_downstream_close_releases_resources_without_waiting_for_terminal():
    class Response:
        async def aiter_bytes(self):
            yield wire(delta())
            await asyncio.sleep(5)
    client, context = Client(), Context()
    async def exercise():
        stream = server.openai_upstream_sse_generator({}, "fixture", None, "fixture", stream_state=(client, context, Response()))
        first = await anext(stream)
        assert parsed_events([first])[0]["delta"] == "fixture answer"
        await stream.aclose()
    asyncio.run(exercise())
    assert client.closed == context.closed == 1
