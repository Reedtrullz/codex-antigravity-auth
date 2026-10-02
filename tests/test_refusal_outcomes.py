"""Synthetic provider outcomes preserve refusals independently of answer prefixes."""

import asyncio
import json
from contextlib import asynccontextmanager

import pytest

from codex_antigravity_auth.cli_doctor import _generation_probe_outcome
from codex_antigravity_auth.google_transport import GoogleStreamEventAdapter, GoogleTransport
from codex_antigravity_auth.openai_transport import OpenAICompatibleTransport, PreparedOpenAIRequest
from codex_antigravity_auth.response_protocol import (
    TerminalKind, classify_terminal, refusal_item, response_from_result,
)


REFUSAL = 'Fixture refusal: "verbatim".\nSecond line.'


def semantic_output(output):
    result = {"text": [], "refusal": [], "tools": [], "reasoning": []}
    for item in output:
        if item["type"] == "message":
            for part in item["content"]:
                if part["type"] == "output_text":
                    result["text"].append(part["text"])
                elif part["type"] == "refusal":
                    result["refusal"].append(part["refusal"])
        elif item["type"] == "function_call":
            result["tools"].append((item["name"], json.loads(item["arguments"])))
        elif item["type"] == "reasoning":
            result["reasoning"].append(item["step_by_step_summary"])
    return result


def chat_stream(payloads):
    class Response:
        status_code = 200
        async def aiter_bytes(self):
            for payload in payloads:
                yield ("data: " + json.dumps(payload) + "\n").encode()
            yield b"data: [DONE]\n"
    class Client:
        def __init__(self, **kwargs):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            return None
        @asynccontextmanager
        async def stream(self, *args, **kwargs):
            yield Response()
    transport = OpenAICompatibleTransport(timeout=1, client_factory=Client)
    async def collect():
        prepared = PreparedOpenAIRequest({}, "https://example.invalid/chat/completions", {}, 1)
        return [event async for event in transport.stream_chat_events(prepared, response_id="fixture", display_model="fixture")]
    events = asyncio.run(collect())
    terminal = [event for event in events if isinstance(event, dict) and event.get("type") in {"response.completed", "response.incomplete", "response.failed"}]
    assert len(terminal) == 1 and events[-1] == "[DONE]"
    return terminal[0]["response"], events


@pytest.mark.parametrize("reason,text,refusal,tool,expected", [
    ("stop", "prefix", REFUSAL, False, "completed"),
    ("stop", "", REFUSAL, False, "completed"),
    ("content_filter", "prefix", REFUSAL, False, "incomplete"),
    ("content_filter", "", REFUSAL, False, "completed"),
    ("content_filter", "", "", False, "completed"),
    ("content_filter", "", "", True, "incomplete"),
    ("length", "prefix", "", False, "incomplete"),
    ("length", "", REFUSAL, False, "incomplete"),
    ("tool_calls", "prefix", "", True, "completed"),
    ("new-finish-reason", "prefix", REFUSAL, True, "failed"),
])
def test_chat_stream_and_nonstream_preserve_siblings_and_finish_semantics(reason, text, refusal, tool, expected):
    message = {"content": text, "refusal": refusal or None}
    if tool:
        message["tool_calls"] = [{"id": "fixture-call", "type": "function", "function": {"name": "lookup", "arguments": '{"q":"fixture"}'}}]
    usage = {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5}
    result = OpenAICompatibleTransport(timeout=1).parse_chat_response({
        "choices": [{"index": 0, "message": message, "finish_reason": reason}], "usage": usage,
    })
    first = {**message, "refusal": refusal[:10] or None}
    payloads = [{"choices": [{"index": 0, "delta": first}]},
                {"choices": [{"index": 0, "delta": {"refusal": refusal[10:] or None}, "finish_reason": reason}], "usage": usage}]
    streamed, _ = chat_stream(payloads)
    assert result.terminal.kind.value == streamed["status"] == expected
    assert semantic_output(result.output) == semantic_output(streamed["output"])
    content = semantic_output(result.output)
    assert content["text"] == ([text] if text else [])
    assert bool(content["tools"]) is tool
    if refusal:
        assert content["refusal"] == [REFUSAL]
    elif reason == "content_filter":
        assert len(content["refusal"]) == 1
    assert result.usage["total_tokens"] == streamed["usage"]["total_tokens"] == 5
    response = response_from_result(result, response_id="fixture", model="fixture", created_at=1)
    ready = _generation_probe_outcome(response)
    if refusal or expected != "completed" or reason == "content_filter":
        assert ready[0] != "completed"
    if expected == "incomplete":
        assert streamed["incomplete_details"]["reason"] == ("max_output_tokens" if reason == "length" else "content_filter")


@pytest.mark.parametrize("reason", [
    "safety", "recitation", "blocklist", "prohibited_content", "spii",
    "image_safety", "image_prohibited_content", "image_recitation", "escalation",
])
@pytest.mark.parametrize("ordinary", [False, True])
def test_google_policy_finishes_preserve_supported_output_and_refusal(reason, ordinary):
    parts = [{"thought": True, "text": "fixture reasoning"}]
    if ordinary:
        parts += [{"text": "prefix"}, {"functionCall": {"id": "fixture-call", "name": "lookup", "args": {"q": "fixture"}}}]
    payload = {"candidates": [{"index": 0, "content": {"parts": parts}, "finishReason": reason.upper()}],
               "usageMetadata": {"promptTokenCount": 2, "candidatesTokenCount": 3, "totalTokenCount": 5}}
    result = GoogleTransport(timeout=1).parse_response(payload)
    adapter = GoogleStreamEventAdapter(response_id="fixture", display_model="fixture")
    adapter.created()
    events = adapter.consume(payload) + adapter.finish()
    streamed = next(event["response"] for event in events if isinstance(event, dict) and event.get("type") in {"response.completed", "response.incomplete", "response.failed"})
    expected = "incomplete" if ordinary else "completed"
    assert result.terminal.kind.value == streamed["status"] == expected
    content = semantic_output(result.output)
    assert content == semantic_output(streamed["output"])
    assert len(content["refusal"]) == 1
    assert content["reasoning"] == ["fixture reasoning"]
    if ordinary:
        assert content["text"] == ["prefix"] and len(content["tools"]) == 1
        assert streamed["incomplete_details"]["reason"] == "content_filter"
    assert _generation_probe_outcome(streamed)[0] != "completed"


@pytest.mark.parametrize("reason,expected,detail", [
    ("STOP", "completed", None), ("MAX_TOKENS", "incomplete", "max_output_tokens"),
    ("MALFORMED_FUNCTION_CALL", "failed", "provider_finish_malformed_function_call"),
    ("UNEXPECTED_TOOL_CALL", "failed", "provider_finish_unexpected_tool_call"),
    ("LANGUAGE", "failed", "provider_finish_language"),
    ("OTHER", "failed", "unknown_finish_reason"),
    ("unknown-fixture-value", "failed", "unknown_finish_reason"),
])
def test_google_known_partial_errors_and_unknown_reasons_are_explicit(reason, expected, detail):
    payload = {"candidates": [{"index": 0, "content": {"parts": [{"text": "prefix"}]}, "finishReason": reason}]}
    result = GoogleTransport(timeout=1).parse_response(payload)
    assert result.terminal.kind.value == expected
    assert semantic_output(result.output)["text"] == ["prefix"]
    adapter = GoogleStreamEventAdapter(response_id="fixture", display_model="fixture")
    adapter.created()
    events = adapter.consume(payload) + adapter.finish()
    streamed = next(event["response"] for event in events if isinstance(event, dict) and event.get("type") in {"response.completed", "response.incomplete", "response.failed"})
    assert streamed["status"] == expected
    assert semantic_output(streamed["output"]) == semantic_output(result.output)
    assert (_generation_probe_outcome(streamed)[0] == "completed") is (expected == "completed")
    if expected == "incomplete":
        assert result.terminal.incomplete_reason == detail
    elif expected == "failed":
        assert result.terminal.error_code == detail
        assert "unknown-fixture-value" not in result.terminal.error_message


def test_prompt_block_metadata_does_not_disappear_behind_text_or_clean_finish():
    result = GoogleTransport(timeout=1).parse_response({
        "promptFeedback": {"blockReason": "SAFETY"},
        "candidates": [{"content": {"parts": [{"text": "prefix"}]}, "finishReason": "STOP"}],
    })
    assert result.terminal.kind is TerminalKind.INCOMPLETE
    assert semantic_output(result.output)["refusal"]


def test_safety_ratings_without_an_explicit_block_are_not_a_refusal():
    payload = {"promptFeedback": {"safetyRatings": [{"category": "fixture", "probability": "NEGLIGIBLE"}]},
               "candidates": [{"content": {"parts": [{"text": "answer"}]}, "finishReason": "STOP"}]}
    result = GoogleTransport(timeout=1).parse_response(payload)
    assert result.terminal.kind is TerminalKind.COMPLETED
    assert semantic_output(result.output)["refusal"] == []


def test_refusal_content_parts_and_scalar_forms_are_preserved():
    message = {"content": [{"type": "text", "text": "prefix"}, {"type": "refusal", "refusal": REFUSAL}]}
    result = OpenAICompatibleTransport(timeout=1).parse_chat_response({"choices": [{"message": message, "finish_reason": "stop"}]})
    streamed, _ = chat_stream([{"choices": [{"delta": message, "finish_reason": "stop"}]}])
    assert semantic_output(result.output) == semantic_output(streamed["output"])
    assert semantic_output(result.output)["refusal"] == [REFUSAL]


def test_generic_policy_metadata_stays_sanitized_while_explicit_refusal_is_verbatim():
    assert "private-fixture" not in str(refusal_item({"blockReason": "private-fixture\nunsafe"}))
    assert refusal_item(refusal_text=REFUSAL)["content"][0]["refusal"] == REFUSAL
    terminal = classify_terminal(output=[refusal_item(refusal_text=REFUSAL)], finish_reason="stop", safety_block=None)
    assert terminal.kind is TerminalKind.COMPLETED


@pytest.mark.parametrize("reason", [True, 123, {}, [], "", " \t\n"])
def test_invalid_finish_scalar_is_not_treated_as_an_absent_legacy_reason(reason):
    payload = {"choices": [{"message": {"content": "prefix"}, "finish_reason": reason}]}
    chat = OpenAICompatibleTransport(timeout=1).parse_chat_response(payload)
    streamed, _ = chat_stream([{"choices": [{"delta": {"content": "prefix"}, "finish_reason": reason}]}])
    google_payload = {"candidates": [{"content": {"parts": [{"text": "prefix"}]}, "finishReason": reason}]}
    google = GoogleTransport(timeout=1).parse_response(google_payload)
    adapter = GoogleStreamEventAdapter(response_id="fixture", display_model="fixture")
    adapter.created()
    adapter.consume(google_payload)
    google_streamed = next(event["response"] for event in adapter.finish() if isinstance(event, dict) and event.get("type") == "response.failed")
    for result in (chat, google):
        assert result.terminal.kind is TerminalKind.FAILED
        assert semantic_output(result.output)["text"] == ["prefix"]
    for result in (streamed, google_streamed):
        assert result["status"] == "failed"
        assert semantic_output(result["output"])["text"] == ["prefix"]


@pytest.mark.parametrize("reasons,expected", [(["", "stop"], "completed"), ([" \t", "stop"], "completed"), (["stop", ""], "failed")])
def test_a_blank_interim_finish_requires_a_subsequent_valid_terminal(reasons, expected):
    chat_payloads = [{"choices": [{"delta": {"content": "prefix" if index == 0 else ""}, "finish_reason": reason}]}
                     for index, reason in enumerate(reasons)]
    chat, _ = chat_stream(chat_payloads)
    adapter = GoogleStreamEventAdapter(response_id="fixture", display_model="fixture")
    adapter.created()
    for index, reason in enumerate(reasons):
        adapter.consume({"candidates": [{"content": {"parts": [{"text": "prefix" if index == 0 else ""}]}, "finishReason": reason.upper()}]})
    adapter.mark_done()
    google = next(event["response"] for event in adapter.finish() if isinstance(event, dict) and event.get("type") in {"response.completed", "response.failed"})
    for result in (chat, google):
        assert result["status"] == expected
        assert semantic_output(result["output"])["text"] == ["prefix"]


@pytest.mark.parametrize("explicit_null", [False, True])
def test_absent_and_null_legacy_finishes_keep_their_existing_end_behavior(explicit_null):
    chat_choice = {"message": {"content": "answer"}}
    google_candidate = {"content": {"parts": [{"text": "answer"}]}}
    if explicit_null:
        chat_choice["finish_reason"] = None
        google_candidate["finishReason"] = None
    chat = OpenAICompatibleTransport(timeout=1).parse_chat_response({"choices": [chat_choice]})
    google = GoogleTransport(timeout=1).parse_response({"candidates": [google_candidate]})
    streamed, _ = chat_stream([{"choices": [{"delta": chat_choice["message"], **({"finish_reason": None} if explicit_null else {})}]}])
    adapter = GoogleStreamEventAdapter(response_id="fixture", display_model="fixture")
    adapter.created()
    adapter.consume({"candidates": [google_candidate]})
    adapter.mark_done()
    google_streamed = next(event["response"] for event in adapter.finish() if isinstance(event, dict) and event.get("type") == "response.completed")
    assert chat.terminal.kind is google.terminal.kind is TerminalKind.COMPLETED
    assert streamed["status"] == google_streamed["status"] == "completed"
