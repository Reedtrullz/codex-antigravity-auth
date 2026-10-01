"""One logical response must never merge provider answer alternatives."""

import asyncio
import itertools
import json
from contextlib import asynccontextmanager

import pytest

from codex_antigravity_auth.google_transport import GoogleStreamEventAdapter, GoogleStreamPayloadError, GoogleTransport
from codex_antigravity_auth.openai_transport import OpenAICompatibleTransport, PreparedOpenAIRequest
from codex_antigravity_auth.response_protocol import PrimaryAlternativeSelector, TerminalKind


USAGE = {"input_tokens": 2, "output_tokens": 7, "total_tokens": 9}


def google(index, text, reason="STOP", tool=None):
    parts = [{"text": text}] if text else []
    if tool:
        parts.append({"functionCall": {"name": tool, "args": {}, "id": f"call_{index}"}})
    return {"index": index, "finishReason": reason, "content": {"parts": parts}}


def chat(index, text, reason="stop", tool=None, stream=False):
    message = {"content": text}
    if tool:
        message["tool_calls"] = [{"index": 0, "id": f"call_{index}", "function": {"name": tool, "arguments": "{}"}}]
    return {"index": index, "finish_reason": reason, "delta" if stream else "message": message}


def chat_stream(frames):
    class Response:
        status_code = 200
        async def aiter_text(self):
            for frame in frames:
                yield "data: " + json.dumps(frame) + "\n\n"
            yield "data: [DONE]\n\n"

    class Client:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        @asynccontextmanager
        async def stream(self, *args, **kwargs):
            yield Response()

    transport = OpenAICompatibleTransport(timeout=1, client_factory=lambda **kwargs: Client())
    async def collect():
        request = PreparedOpenAIRequest(payload={}, url="https://example.invalid", headers={}, timeout=1)
        return [event async for event in transport.stream_chat_events(request, response_id="fixture", display_model="fixture")]
    return asyncio.run(collect())


def terminal_response(events):
    return next(event["response"] for event in events if isinstance(event, dict) and event.get("type") in {"response.completed", "response.incomplete", "response.failed"})


@pytest.mark.parametrize("provider", ["chat", "google"])
def test_permutations_choose_zero_and_preserve_partial_status_tools_and_aggregate_usage(provider):
    choices = [chat(0, "primary", "length", "selected_tool"), chat(1, "alternate", "stop", "unselected_tool")] if provider == "chat" else [google(0, "primary", "MAX_TOKENS", "selected_tool"), google(1, "alternate", "STOP", "unselected_tool")]
    choices.append(dict(choices[1]))  # Duplicated secondary alternatives are irrelevant.
    for permutation in itertools.permutations(choices):
        if provider == "chat":
            result = OpenAICompatibleTransport(timeout=1).parse_chat_response({"choices": list(permutation), "usage": {"prompt_tokens": 2, "completion_tokens": 7, "total_tokens": 9}})
            stream_choices = [{**item, "delta": item["message"]} for item in permutation]
            events = chat_stream([{"choices": stream_choices, "usage": {"prompt_tokens": 2, "completion_tokens": 7, "total_tokens": 9}}])
        else:
            payload = {"candidates": list(permutation), "usageMetadata": {"promptTokenCount": 2, "candidatesTokenCount": 7, "totalTokenCount": 9}}
            result = GoogleTransport(timeout=1).parse_response({"response": payload})
            adapter = GoogleStreamEventAdapter(response_id="fixture", display_model="fixture")
            events = [adapter.created(), *adapter.consume({"response": payload})]
            adapter.mark_done()
            events += adapter.finish()
        assert result.terminal.kind is TerminalKind.INCOMPLETE
        assert result.usage == USAGE
        assert "primary" in json.dumps(result.output)
        assert "selected_tool" in json.dumps(result.output)
        assert "alternate" not in json.dumps(result.output)
        assert "unselected_tool" not in json.dumps(result.output)
        response = terminal_response(events)
        assert response["status"] == "incomplete"
        assert response["usage"] == USAGE
        assert "primary" in json.dumps(events)
        assert "unselected_tool" not in json.dumps(events)
        assert "alternate" not in json.dumps(events)


@pytest.mark.parametrize("provider", ["chat", "google"])
def test_stream_interleaving_never_leaks_alternative_text_or_finish_reason(provider):
    if provider == "chat":
        events = chat_stream([
            {"choices": [chat(1, "ignore-first", None, stream=True)]},
            {"choices": [chat(0, "A", None, stream=True)]},
            {"choices": [chat(1, "ignore-last", "stop", stream=True)]},
            {"choices": [chat(0, "B", "length", stream=True)]},
        ])
    else:
        adapter = GoogleStreamEventAdapter(response_id="fixture", display_model="fixture")
        events = [adapter.created()]
        for candidates in [[google(1, "ignore-first", None)], [google(0, "A", None)], [google(1, "ignore-last", "STOP")], [google(0, "B", "MAX_TOKENS")]]:
            events += adapter.consume({"candidates": candidates})
        adapter.mark_done()
        events += adapter.finish()
    assert "ignore-" not in json.dumps(events)
    assert terminal_response(events)["status"] == "incomplete"
    assert "".join(event["delta"] for event in events if isinstance(event, dict) and event.get("type") == "response.output_text.delta") == "AB"


@pytest.mark.parametrize("choices", [
    [{}, {}], [{"index": 0}, {}], [{"index": 0}, {"index": 0}],
    [{"index": False}], [{"index": "0"}], [{"index": -1}], [{"index": None}], ["bad", {}],
])
def test_ambiguous_alternatives_fail_without_emitting_output(choices):
    chat_result = OpenAICompatibleTransport(timeout=1).parse_chat_response({"choices": choices})
    google_result = GoogleTransport(timeout=1).parse_response({"candidates": choices})
    assert chat_result.terminal.kind is google_result.terminal.kind is TerminalKind.FAILED
    assert chat_result.output == google_result.output == ()
    assert terminal_response(chat_stream([{"choices": choices}]))["status"] == "failed"
    adapter = GoogleStreamEventAdapter(response_id="fixture", display_model="fixture")
    adapter.created()
    with pytest.raises(GoogleStreamPayloadError, match="ambiguous|indices|objects"):
        adapter.consume({"candidates": choices})


@pytest.mark.parametrize("unindexed_first", [False, True])
def test_mixing_unindexed_stream_chunks_with_nonprimary_indices_is_rejected(unindexed_first):
    selector = PrimaryAlternativeSelector()
    first, second = ([{}], [{"index": 1}]) if unindexed_first else ([{"index": 1}], [{}])
    selector.select(first)
    with pytest.raises(ValueError, match="ambiguous"):
        selector.select(second)


def test_legacy_unindexed_single_answer_and_index_zero_remain_compatible():
    selector = PrimaryAlternativeSelector()
    assert selector.select([{"text": "A"}]) == [{"text": "A"}]
    assert selector.select([{"index": 0, "text": "B"}]) == [{"index": 0, "text": "B"}]
    assert selector.select([]) == []


def test_alternate_cannot_override_primary_safety_outcome():
    google_result = GoogleTransport(timeout=1).parse_response({"candidates": [google(0, "", "SAFETY"), google(1, "unsafe-alternative", "STOP")]})
    assert google_result.terminal.kind is TerminalKind.FAILED
    assert google_result.output == ()
    chat_result = OpenAICompatibleTransport(timeout=1).parse_chat_response({"choices": [chat(0, "", "content_filter"), chat(1, "unsafe-alternative", "stop")]})
    assert chat_result.output[0]["content"][0]["type"] == "refusal"
    assert "unsafe-alternative" not in json.dumps(chat_result.output)


@pytest.mark.parametrize("provider", ["chat", "google"])
def test_nonstream_ambiguous_alternatives_keep_reported_usage(provider):
    invalid = [{"index": 0}, {"index": 0}]
    if provider == "chat":
        result = OpenAICompatibleTransport(timeout=1).parse_chat_response({"choices": invalid, "usage": {"prompt_tokens": 2, "completion_tokens": 7, "total_tokens": 9}})
    else:
        result = GoogleTransport(timeout=1).parse_response({"candidates": invalid, "usageMetadata": {"promptTokenCount": 2, "candidatesTokenCount": 7, "totalTokenCount": 9}})
    assert result.terminal.kind is TerminalKind.FAILED
    assert result.usage == USAGE


@pytest.mark.parametrize("provider", ["chat", "google"])
@pytest.mark.parametrize("placement", ["earlier", "invalid-frame", "both"])
def test_stream_validation_failure_keeps_latest_reported_usage(provider, placement):
    invalid = [{"index": 0}, {"index": 0}]
    if provider == "chat":
        first = {"choices": [chat(0, "prefix", None, stream=True)]}
        last = {"choices": invalid}
        if placement in {"earlier", "both"}:
            first["usage"] = {"prompt_tokens": 2, "completion_tokens": 7, "total_tokens": 9}
        if placement in {"invalid-frame", "both"}:
            last["usage"] = {"prompt_tokens": 5, "completion_tokens": 10, "total_tokens": 15}
        events = chat_stream([first, last])
    else:
        first = {"candidates": [google(0, "prefix", None)]}
        last = {"candidates": invalid}
        if placement in {"earlier", "both"}:
            first["usageMetadata"] = {"promptTokenCount": 2, "candidatesTokenCount": 7, "totalTokenCount": 9}
        if placement in {"invalid-frame", "both"}:
            last["usageMetadata"] = {"promptTokenCount": 5, "candidatesTokenCount": 10, "totalTokenCount": 15}
        adapter = GoogleStreamEventAdapter(response_id="fixture", display_model="fixture")
        events = [adapter.created(), *adapter.consume(first)]
        with pytest.raises(GoogleStreamPayloadError) as exc:
            adapter.consume(last)
        events += adapter.fail(exc.value.code, exc.value.message)
    result = terminal_response(events)
    assert result["status"] == "failed"
    assert result["usage"] == (USAGE if placement == "earlier" else {"input_tokens": 5, "output_tokens": 10, "total_tokens": 15})
