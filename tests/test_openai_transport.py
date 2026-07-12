import unittest
import json
from unittest.mock import patch

from codex_antigravity_auth.openai_transport import ChatResponseAccumulator, OpenAICompatibleTransport
from codex_antigravity_auth.response_protocol import TerminalKind
from codex_antigravity_auth.transform import transform_chat_response, transform_request_to_chat


class TestOpenAIRequestTranslation(unittest.TestCase):
    def test_forwards_parallel_tool_calls_true_and_false(self):
        for value in (True, False):
            with self.subTest(value=value):
                payload = transform_request_to_chat(
                    {
                        "model": "custom:model",
                        "input": "hello",
                        "parallel_tool_calls": value,
                        "tools": [{"type": "function", "name": "lookup", "parameters": {"type": "object"}}],
                    },
                    "model",
                )
                self.assertIs(payload["parallel_tool_calls"], value)


class TestOpenAIResponseTranslation(unittest.TestCase):
    def setUp(self):
        self.transport = OpenAICompatibleTransport(timeout=5)

    def test_empty_response_is_failed(self):
        result = self.transport.parse_chat_response({"choices": []})
        self.assertEqual(result.terminal.kind, TerminalKind.FAILED)
        self.assertEqual(result.terminal.error_code, "empty_response")

    def test_length_finish_is_incomplete(self):
        result = self.transport.parse_chat_response(
            {"choices": [{"finish_reason": "length", "message": {"content": "partial"}}]}
        )
        self.assertEqual(result.terminal.kind, TerminalKind.INCOMPLETE)

    def test_content_filter_becomes_refusal(self):
        result = self.transport.parse_chat_response(
            {"choices": [{"finish_reason": "content_filter", "message": {"refusal": "declined"}}]}
        )
        self.assertEqual(result.terminal.kind, TerminalKind.COMPLETED)
        self.assertEqual(result.output[0]["content"][0]["type"], "refusal")

    def test_preserves_text_reasoning_tools_and_usage(self):
        result = self.transport.parse_chat_response(
            {
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": "answer",
                            "reasoning_content": "thought",
                            "tool_calls": [
                                {"id": "call_1", "function": {"name": "lookup", "arguments": "{}"}}
                            ],
                        },
                    }
                ],
                "usage": {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5},
            }
        )
        self.assertEqual([item["type"] for item in result.output], ["reasoning", "message", "function_call"])
        self.assertEqual(result.usage["total_tokens"], 5)

    def test_legacy_wrapper_uses_terminal_contract(self):
        self.assertEqual(transform_chat_response({"choices": []}, "model")["status"], "failed")

    def test_native_responses_empty_completion_is_normalized_to_failed(self):
        response = self.transport.validate_native_response(
            {"id": "resp_native", "object": "response", "status": "completed", "output": []},
            display_model="xai-oauth:model",
        )

        self.assertEqual(response["status"], "failed")
        self.assertEqual(response["error"]["code"], "empty_response")
        self.assertEqual(response["model"], "xai-oauth:model")

    def test_native_responses_rejects_invalid_terminal_status(self):
        with self.assertRaisesRegex(ValueError, "status"):
            self.transport.validate_native_response(
                {"object": "response", "status": "mystery", "output": []},
                display_model="xai-oauth:model",
            )


class TestChatResponseAccumulator(unittest.TestCase):
    def test_clean_eof_without_done_is_failed(self):
        accumulator = ChatResponseAccumulator()
        accumulator.consume({"choices": [{"delta": {"content": "partial"}}]})
        result = accumulator.finalize()
        self.assertEqual(result.terminal.kind, TerminalKind.FAILED)
        self.assertEqual(result.terminal.error_code, "missing_terminal_signal")

    def test_done_with_text_is_completed(self):
        accumulator = ChatResponseAccumulator()
        accumulator.consume({"choices": [{"delta": {"content": "done"}}]})
        accumulator.mark_done()
        self.assertEqual(accumulator.finalize().terminal.kind, TerminalKind.COMPLETED)

    def test_stream_finish_length_is_incomplete(self):
        accumulator = ChatResponseAccumulator()
        accumulator.consume({"choices": [{"delta": {"content": "partial"}, "finish_reason": "length"}]})
        self.assertEqual(accumulator.finalize().terminal.kind, TerminalKind.INCOMPLETE)


class TestOpenAIStreamingRoute(unittest.IsolatedAsyncioTestCase):
    async def _events(self, chunks):
        from codex_antigravity_auth.server import openai_compatible_sse_generator

        class AsyncChunks:
            def __init__(self):
                self.chunks = list(chunks)

            def __aiter__(self):
                return self

            async def __anext__(self):
                if not self.chunks:
                    raise StopAsyncIteration
                return self.chunks.pop(0)

        class Response:
            status_code = 200

            def aiter_text(self):
                return AsyncChunks()

        class StreamContext:
            async def __aenter__(self):
                return Response()

            async def __aexit__(self, *args):
                return None

        class Client:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return None

            def stream(self, *args, **kwargs):
                return StreamContext()

        output = []
        with patch("codex_antigravity_auth.server.httpx.AsyncClient", Client):
            async for chunk in openai_compatible_sse_generator(
                {}, "https://provider.example/v1/chat/completions", {}, 5, {"id": "custom"}, "custom:model"
            ):
                output.append(chunk)
        return [
            json.loads(line[6:])
            for chunk in output
            for line in chunk.splitlines()
            if line.startswith("data: ") and line != "data: [DONE]"
        ]

    async def test_empty_done_stream_is_failed(self):
        events = await self._events(["data: [DONE]\n"])
        terminal = [event["type"] for event in events if event["type"] in {"response.completed", "response.incomplete", "response.failed"}]
        self.assertEqual(terminal, ["response.failed"])

    async def test_length_stream_is_incomplete(self):
        events = await self._events(
            [
                'data: {"choices":[{"delta":{"content":"partial"},"finish_reason":"length"}]}\n',
                "data: [DONE]\n",
            ]
        )
        terminal = [event for event in events if event["type"] in {"response.completed", "response.incomplete", "response.failed"}]
        self.assertEqual([event["type"] for event in terminal], ["response.incomplete"])

    async def test_content_filter_stream_emits_refusal_item(self):
        events = await self._events(
            [
                'data: {"choices":[{"delta":{"refusal":"provider detail"},"finish_reason":"content_filter"}]}\n',
                "data: [DONE]\n",
            ]
        )
        terminal = [event for event in events if event["type"] in {"response.completed", "response.incomplete", "response.failed"}]
        refusals = [
            event["item"]
            for event in events
            if event["type"] == "response.output_item.done"
            and event.get("item", {}).get("content", [{}])[0].get("type") == "refusal"
        ]
        self.assertEqual([event["type"] for event in terminal], ["response.completed"])
        self.assertEqual(len(refusals), 1)
        self.assertNotIn("provider detail", str(refusals))


class TestNativeResponsesRoute(unittest.IsolatedAsyncioTestCase):
    async def test_xai_native_empty_completion_is_failed(self):
        from codex_antigravity_auth.server import create_xai_oauth_response

        async def prepare(*args, **kwargs):
            return {}, "https://provider.example/v1/responses", {}, 5

        class Response:
            status_code = 200
            text = ""

            def json(self):
                return {"id": "resp_native", "object": "response", "status": "completed", "output": []}

        class Client:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return None

            async def post(self, *args, **kwargs):
                return Response()

        with patch("codex_antigravity_auth.server.prepare_xai_oauth_responses_request", prepare):
            with patch("codex_antigravity_auth.server.httpx.AsyncClient", Client):
                response = await create_xai_oauth_response(
                    {}, {"id": "xai-oauth"}, "grok-model", "xai-oauth:grok-model"
                )

        self.assertEqual(response["status"], "failed")
        self.assertEqual(response["error"]["code"], "empty_response")


if __name__ == "__main__":
    unittest.main()
