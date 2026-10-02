"""Synthetic byte streams exercise framing independently of live providers."""

import asyncio
import json
from contextlib import asynccontextmanager

import pytest

from codex_antigravity_auth import sse
from codex_antigravity_auth.google_transport import AccountLease, GoogleTransport
from codex_antigravity_auth.openai_transport import NativeResponsesStreamAdapter
from codex_antigravity_auth.server import _collect_openai_sse_terminal


class Response:
    status_code = 200

    def __init__(self, chunks):
        self.chunks = chunks

    async def aiter_bytes(self):
        for chunk in self.chunks:
            yield chunk


def decode(chunks, **kwargs):
    decoder = sse.SSEDecoder(**kwargs)
    output = [value for chunk in chunks for value in decoder.feed(chunk)]
    output.extend(decoder.finish())
    assert decoder.buffered_chars == 0
    return output


def native(chunks):
    adapter = NativeResponsesStreamAdapter(display_model="fixture-model")
    output = [event for chunk in chunks for event in adapter.consume_bytes(chunk)]
    output.extend(adapter.finish())
    return output


def test_every_byte_split_preserves_unicode_bom_multiline_data_and_native_events():
    wire = (
        '\ufeff: fixture comment\r\nevent: response.output_text.delta\r\n'
        'data: {"type":"response.output_text.delta",\r\n'
        'data: "delta":"é漢💡"}\r\n\r\n'
        'data: {"type":"response.completed","response":{"status":"completed","output":[]}}\n\n'
        'data: [DONE]\r\r'
    ).encode()
    expected = decode([wire])
    expected_native = native([wire])
    assert expected_native[0]["delta"] == "é漢💡"
    assert expected_native[-1]["type"] == "response.completed"
    for split in range(len(wire) + 1):
        chunks = [wire[:split], wire[split:]]
        assert decode(chunks) == expected
        assert native(chunks) == expected_native
    assert decode([bytes([value]) for value in wire]) == expected
    assert native([bytes([value]) for value in wire]) == expected_native


def test_general_iterator_and_native_reader_agree_on_multiline_unicode_bytes():
    wire = 'data: {"type":"response.output_text.delta",\ndata: "delta":"café 💡"}\n\n'.encode()
    chunks = [bytes([value]) for value in wire]
    async def collect():
        return [value async for value in sse.iter_sse_data(Response(chunks))]
    parsed = [json.loads(value) for value in asyncio.run(collect())]
    assert parsed == native(chunks)[:-1]  # Native adds its missing-terminal failure.
    assert parsed[0]["delta"] == "café 💡"


def test_buffered_collection_uses_the_same_unicode_multiline_event_boundaries():
    wire = ('\ufeffdata: {"type":"response.completed",\r\n'
            'data: "response":{"status":"completed","output_text":"café 💡"}}\r\n\r\n').encode()
    result = _collect_openai_sse_terminal(wire, "fixture")
    assert result["output_text"] == "café 💡" and result["model"] == "fixture"
    with pytest.raises(sse.SSELineError, match="incomplete"):
        _collect_openai_sse_terminal(wire[:-4], "fixture")


@pytest.mark.parametrize("ending", [b"\n", b"\r", b"\r\n"])
def test_delimiters_field_rules_and_data_whitespace(ending):
    lines = [b": comment", b"event: ignored", b"id: ignored", b"retry: 5", b"DATA: ignored", b" data: ignored",
             b"data:  first ", b"data:second", b"", b"data", b"", b"data: \xef\xbb\xbfkeep-bom", b""]
    wire = ending.join(lines) + ending
    for split in range(len(wire) + 1):
        assert decode([wire[:split], wire[split:]]) == [" first \nsecond", "", "\ufeffkeep-bom"]


def test_standard_mode_waits_for_blank_boundary_even_after_valid_json():
    decoder = sse.SSEDecoder()
    assert list(decoder.feed(b'data: {"a":1}\n')) == []
    assert list(decoder.feed(b'data: {"b":2}\n')) == []
    assert list(decoder.feed(b'\n')) == ['{"a":1}\n{"b":2}']
    assert list(decoder.finish()) == []


def test_legacy_json_lines_is_explicit_and_emits_deltas_before_completion():
    decoder = sse.SSEDecoder(legacy_json_lines=True)
    assert list(decoder.feed(b'data: {"a":1}\n')) == ['{"a":1}']
    assert list(decoder.feed(b'data: {"b":\n')) == []
    assert list(decoder.feed(b'data: 2}\n')) == ['{"b":\n2}']
    assert list(decoder.feed(b'data: [DONE]\n')) == ['[DONE]']
    assert list(decoder.finish()) == []


def test_legacy_sentinel_normalization_preserves_standard_data_and_pending_json():
    wire = b"data:  [DONE] \t\n"
    assert decode([wire], legacy_json_lines=True) == ["[DONE]"]
    assert decode([wire + b"\n"]) == [" [DONE] \t"]
    assert decode([b"data: {\n" + wire], legacy_json_lines=True) == ["{", "[DONE]"]


def test_native_delta_is_visible_at_event_boundary_without_waiting_for_eof():
    adapter = NativeResponsesStreamAdapter(display_model="fixture")
    assert adapter.consume_bytes(b'data: {"type":"response.output_text.delta","delta":"first"}\n') == []
    events = adapter.consume_bytes(b'\n')
    assert len(events) == 1 and events[0]["delta"] == "first"


@pytest.mark.parametrize("wire", [b'data: {}', b'data: {}\n', b'data: {"x":"\xe2'])
def test_standard_mode_rejects_unterminated_data_at_eof(wire):
    decoder = sse.SSEDecoder()
    assert list(decoder.feed(wire)) == []
    with pytest.raises(sse.SSELineError, match="incomplete"):
        list(decoder.finish())
    assert decoder.buffered_chars == 0
    assert list(decoder.finish()) == []


def test_legacy_mode_also_rejects_an_unterminated_physical_data_line():
    with pytest.raises(sse.SSELineError, match="incomplete"):
        decode([b'data: {}'], legacy_json_lines=True)


def test_eof_discards_comments_and_nondata_fields():
    assert decode([b': comment']) == []
    assert decode([b'event: unused']) == []


def test_malformed_utf8_is_replaced_consistently_at_every_split():
    wire = b'data: {"type":"response.output_text.delta","delta":"\xff\xe2\x82!"}\n\n'
    expected = decode([wire])
    assert json.loads(expected[0])["delta"] == "\ufffd\ufffd!"
    for split in range(len(wire) + 1):
        assert decode([wire[:split], wire[split:]]) == expected
        assert native([wire[:split], wire[split:]])[0]["delta"] == "\ufffd\ufffd!"


@pytest.mark.parametrize("chunks", [
    [b"x" * 80], [b"x" * 10] * 8, [b"data: " + b"x" * 18 + b"\n"] * 4,
])
def test_line_and_event_buffers_are_bounded_and_cleared_after_failure(chunks):
    decoder = sse.SSEDecoder(max_buffer_chars=64)
    with pytest.raises(sse.SSELineError, match="buffer limit"):
        for chunk in chunks:
            list(decoder.feed(chunk))
    assert decoder.buffered_chars == 0
    with pytest.raises(sse.SSELineError, match="closed"):
        list(decoder.feed(b"ignored"))


def test_data_line_count_is_bounded_even_for_empty_values(monkeypatch):
    monkeypatch.setattr(sse, "MAX_SSE_DATA_LINES", 2)
    with pytest.raises(sse.SSELineError, match="data-line limit"):
        decode([b"data:\ndata:\ndata:\n"])


def test_large_chunk_with_many_small_events_does_not_accumulate_stream_history():
    assert decode([b"data: {}\n\n" * 10000], max_buffer_chars=64) == ["{}"] * 10000


@pytest.mark.parametrize("sentinel", ["[DONE]", " [DONE]", "[DONE] \t", " \t[DONE] \t"])
def test_google_compatibility_path_uses_byte_framing(monkeypatch, sentinel):
    payload = {"candidates": [{"content": {"parts": [{"text": "café 💡"}]}, "finishReason": "STOP"}]}
    wire = ("data: " + json.dumps(payload, ensure_ascii=False) + "\r\n" + f"data: {sentinel}\r\n").encode()
    @asynccontextmanager
    async def stream(*args):
        yield Response([bytes([value]) for value in wire])
    transport = GoogleTransport(timeout=1)
    monkeypatch.setattr(transport, "stream", stream)
    async def collect():
        return [event async for event in transport.stream_events({}, AccountLease("fixture@example.invalid", "fixture-project", "synthetic-token"), response_id="fixture-response", display_model="fixture-model")]
    events = asyncio.run(collect())
    assert any(isinstance(event, dict) and event.get("delta") == "café 💡" for event in events)
    assert events[-1] == "[DONE]"
