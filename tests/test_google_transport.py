import unittest
from unittest.mock import patch
import math

from fastapi.testclient import TestClient

from codex_antigravity_auth.google_transport import (
    AccountLease,
    GoogleHTTPError,
    GoogleResponseAccumulator,
    GoogleTransport,
    outcome_for_backend_error,
)
from codex_antigravity_auth.response_protocol import TerminalKind
from codex_antigravity_auth.transform import transform_response


class TestGoogleResponseTranslation(unittest.TestCase):
    def setUp(self):
        self.transport = GoogleTransport(timeout=5)

    def test_backend_error_outcomes_are_typed_by_policy_scope(self):
        cases = [
            ("RESOURCE_EXHAUSTED", "quota exhausted", "family", "quota"),
            ("429", "rate limited", "family", "rate_limit"),
            ("UNAUTHENTICATED", "expired", "account", "auth"),
            ("INVALID_ARGUMENT", "bad request", "none", "invalid_request"),
            ("INTERNAL", "backend failed", "none", "transport"),
        ]
        for code, message, scope, category in cases:
            with self.subTest(code=code):
                outcome = outcome_for_backend_error(code, message)
                self.assertEqual((outcome.scope, outcome.category), (scope, category))

    def test_parses_wrapped_and_unwrapped_text_responses(self):
        candidate = {
            "candidates": [
                {
                    "finishReason": "STOP",
                    "content": {"role": "model", "parts": [{"text": "hello"}]},
                }
            ]
        }

        for payload in (candidate, {"response": candidate}):
            with self.subTest(wrapped="response" in payload):
                result = self.transport.parse_response(payload)
                self.assertEqual(result.terminal.kind, TerminalKind.COMPLETED)
                self.assertEqual(result.output[0]["content"][0]["text"], "hello")

    def test_preserves_thought_signature_text_as_normal_output(self):
        result = self.transport.parse_response(
            {
                "candidates": [
                    {
                        "finishReason": "STOP",
                        "content": {"parts": [{"text": "visible", "thoughtSignature": "opaque"}]},
                    }
                ]
            }
        )

        self.assertEqual(result.output[0]["type"], "message")
        self.assertEqual(result.output[0]["content"][0]["text"], "visible")

    def test_parses_reasoning_function_calls_and_usage(self):
        result = self.transport.parse_response(
            {
                "candidates": [
                    {
                        "finishReason": "STOP",
                        "content": {
                            "parts": [
                                {"thought": True, "text": "considering"},
                                {"functionCall": {"id": "call_1", "name": "lookup", "args": {"q": "x"}}},
                            ]
                        },
                    }
                ],
                "usageMetadata": {"promptTokenCount": 2, "candidatesTokenCount": 3, "totalTokenCount": 5},
            }
        )

        self.assertEqual([item["type"] for item in result.output], ["reasoning", "function_call"])
        self.assertEqual(result.output[1]["call_id"], "call_1")
        self.assertEqual(result.usage, {"input_tokens": 2, "output_tokens": 3, "total_tokens": 5})

    def test_empty_response_is_failed(self):
        result = self.transport.parse_response({"candidates": []})

        self.assertEqual(result.terminal.kind, TerminalKind.FAILED)
        self.assertEqual(result.terminal.error_code, "empty_response")

    def test_max_tokens_is_incomplete(self):
        result = self.transport.parse_response(
            {
                "candidates": [
                    {
                        "finishReason": "MAX_TOKENS",
                        "content": {"parts": [{"text": "partial"}]},
                    }
                ]
            }
        )

        self.assertEqual(result.terminal.kind, TerminalKind.INCOMPLETE)
        self.assertEqual(result.terminal.incomplete_reason, "max_output_tokens")

    def test_safety_prompt_feedback_becomes_completed_refusal(self):
        result = self.transport.parse_response(
            {"promptFeedback": {"blockReason": "SAFETY"}, "candidates": []}
        )

        self.assertEqual(result.terminal.kind, TerminalKind.COMPLETED)
        self.assertEqual(result.output[0]["content"][0]["type"], "refusal")

    def test_malformed_payload_is_failed(self):
        result = self.transport.parse_response({"candidates": "not-a-list"})

        self.assertEqual(result.terminal.kind, TerminalKind.FAILED)
        self.assertEqual(result.terminal.error_code, "malformed_provider_response")

    def test_skips_invalid_candidate_when_later_output_is_valid(self):
        result = self.transport.parse_response(
            {
                "candidates": [
                    "invalid",
                    {"finishReason": "STOP", "content": {"parts": [{"text": "valid"}]}},
                ]
            }
        )

        self.assertEqual(result.terminal.kind, TerminalKind.COMPLETED)
        self.assertEqual(result.output[0]["content"][0]["text"], "valid")

    def test_legacy_transform_response_wrapper_uses_terminal_contract(self):
        empty = transform_response({"candidates": []}, "test-model")
        truncated = transform_response(
            {"candidates": [{"finishReason": "MAX_TOKENS", "content": {"parts": [{"text": "partial"}]}}]},
            "test-model",
        )

        self.assertEqual(empty["status"], "failed")
        self.assertEqual(truncated["status"], "incomplete")


class TestGoogleStreamingAccumulator(unittest.TestCase):
    def test_empty_clean_eof_is_failed(self):
        result = GoogleResponseAccumulator().finalize()

        self.assertEqual(result.terminal.kind, TerminalKind.FAILED)
        self.assertEqual(result.terminal.error_code, "empty_response")

    def test_combines_streamed_text_reasoning_functions_and_usage(self):
        accumulator = GoogleResponseAccumulator()
        accumulator.consume(
            {
                "candidates": [
                    {"content": {"parts": [{"text": "hello "}, {"thought": True, "text": "think "}]}}
                ]
            }
        )
        accumulator.consume(
            {
                "candidates": [
                    {
                        "finishReason": "STOP",
                        "content": {
                            "parts": [
                                {"text": "world"},
                                {"thought": True, "text": "done"},
                                {"functionCall": {"id": "call_1", "name": "one", "args": {}}},
                                {"functionCall": {"id": "call_2", "name": "two", "args": {"x": 1}}},
                            ]
                        },
                    }
                ],
                "usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 4},
            }
        )

        result = accumulator.finalize()

        self.assertEqual(result.terminal.kind, TerminalKind.COMPLETED)
        self.assertEqual([item["type"] for item in result.output], ["reasoning", "message", "function_call", "function_call"])
        self.assertEqual(result.output[1]["content"][0]["text"], "hello world")
        self.assertEqual([item["call_id"] for item in result.output[2:]], ["call_1", "call_2"])
        self.assertEqual(result.usage["total_tokens"], 5)

    def test_finish_only_max_tokens_chunk_is_incomplete(self):
        accumulator = GoogleResponseAccumulator()
        accumulator.consume({"candidates": [{"content": {"parts": [{"text": "partial"}]}}]})
        accumulator.consume({"candidates": [{"finishReason": "MAX_TOKENS"}]})

        self.assertEqual(accumulator.finalize().terminal.kind, TerminalKind.INCOMPLETE)

    def test_output_without_provider_terminal_signal_is_failed(self):
        accumulator = GoogleResponseAccumulator()
        accumulator.consume({"candidates": [{"content": {"parts": [{"text": "partial"}]}}]})

        result = accumulator.finalize()

        self.assertEqual(result.terminal.kind, TerminalKind.FAILED)
        self.assertEqual(result.terminal.error_code, "missing_terminal_signal")

    def test_done_marker_is_an_explicit_terminal_signal(self):
        accumulator = GoogleResponseAccumulator()
        accumulator.consume({"candidates": [{"content": {"parts": [{"text": "complete"}]}}]})
        accumulator.mark_done()

        self.assertEqual(accumulator.finalize().terminal.kind, TerminalKind.COMPLETED)

    def test_malformed_chunk_fails_terminal_result(self):
        accumulator = GoogleResponseAccumulator()
        accumulator.consume({"candidates": [{"content": {"parts": [{"text": "visible"}]}}]})
        accumulator.mark_malformed()

        self.assertEqual(accumulator.finalize().terminal.kind, TerminalKind.FAILED)


class TestGoogleRequestConstruction(unittest.TestCase):
    def test_builds_account_specific_request_and_sanitized_headers(self):
        transport = GoogleTransport(timeout=5, platform_name="MACOS")
        lease = AccountLease(
            email="person@example.com",
            project_id="safe-project",
            access_token="token-value",
            fingerprint={"userAgent": "Agent/1", "apiClient": "client/1"},
        )

        envelope = transport.build_request({"model": "gemini-3.5-flash-high", "input": "hello"}, lease)
        headers = transport.build_headers(lease)

        self.assertEqual(envelope["project"], "safe-project")
        self.assertEqual(headers["Authorization"], "Bearer token-value")
        self.assertEqual(headers["User-Agent"], "Agent/1")
        self.assertNotIn("person@example.com", str(envelope))
        self.assertNotIn("person@example.com", str(headers))

    def test_drops_non_finite_fingerprint_metadata(self):
        transport = GoogleTransport(timeout=5, platform_name="MACOS")
        lease = AccountLease(
            email="person@example.com",
            project_id="safe-project",
            access_token="token-value",
            fingerprint={"clientMetadata": {"valid": 1, "invalid": math.inf}},
        )

        headers = transport.build_headers(lease)

        self.assertIn('"valid": 1', headers["Client-Metadata"])
        self.assertNotIn("Infinity", headers["Client-Metadata"])
        self.assertNotIn("invalid", headers["Client-Metadata"])


class TestGoogleHTTPExecution(unittest.IsolatedAsyncioTestCase):
    async def test_posts_non_streaming_request_through_transport(self):
        calls = []

        class Response:
            status_code = 200

        class Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc_value, traceback):
                return None

            async def post(self, url, **kwargs):
                calls.append((url, kwargs))
                return Response()

        transport = GoogleTransport(timeout=7, client_factory=lambda **kwargs: Client())
        lease = AccountLease("person@example.com", "safe-project", "token")

        response = await transport.post({"model": "gemini-3.5-flash-high", "input": "hello"}, lease)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(calls[0][0].endswith("/v1internal:generateContent"))
        self.assertEqual(calls[0][1]["headers"]["Authorization"], "Bearer token")
        self.assertEqual(calls[0][1]["json"]["project"], "safe-project")

    async def test_opens_streaming_request_through_transport(self):
        calls = []

        class Response:
            status_code = 200

        class StreamContext:
            async def __aenter__(self):
                return Response()

            async def __aexit__(self, exc_type, exc_value, traceback):
                return None

        class Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc_value, traceback):
                return None

            def stream(self, method, url, **kwargs):
                calls.append((method, url, kwargs))
                return StreamContext()

        transport = GoogleTransport(timeout=7, client_factory=lambda **kwargs: Client())
        lease = AccountLease("person@example.com", "safe-project", "token")

        async with transport.stream({"model": "gemini-3.5-flash-high", "input": "hello"}, lease) as response:
            self.assertEqual(response.status_code, 200)

        self.assertEqual(calls[0][0], "POST")
        self.assertTrue(calls[0][1].endswith("/v1internal:streamGenerateContent?alt=sse"))

    async def test_execute_returns_result_and_typed_http_failure(self):
        class Response:
            def __init__(self, status_code, payload=None):
                self.status_code = status_code
                self._payload = payload or {}
                self.text = "provider detail"
                self.headers = {}

            def json(self):
                return self._payload

        responses = [
            Response(200, {"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": "ok"}]}}]}),
            Response(429),
        ]

        class Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc_value, traceback):
                return None

            async def post(self, *args, **kwargs):
                return responses.pop(0)

        transport = GoogleTransport(timeout=7, client_factory=lambda **kwargs: Client())
        lease = AccountLease("person@example.com", "safe-project", "token")

        result = await transport.execute({"model": "gemini-3.5-flash-high", "input": "hello"}, lease, stream=False)
        self.assertEqual(result.terminal.kind, TerminalKind.COMPLETED)

        with self.assertRaises(GoogleHTTPError) as raised:
            await transport.execute({"model": "gemini-3.5-flash-high", "input": "hello"}, lease, stream=False)
        self.assertEqual(raised.exception.status_code, 429)
        self.assertEqual(raised.exception.outcome.scope, "family")
        self.assertEqual(raised.exception.outcome.category, "rate_limit")


class TestGoogleRouteTerminalFidelity(unittest.TestCase):
    @staticmethod
    def _post(payload: dict):
        from codex_antigravity_auth.server import app

        class Response:
            status_code = 200
            text = ""
            headers = {}

            def json(self):
                return payload

        class Client:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc_value, traceback):
                return None

            async def post(self, *args, **kwargs):
                return Response()

        account = {"email": "person@example.com", "accessToken": "token", "projectId": "safe-project"}
        with patch("codex_antigravity_auth.server.account_manager.acquire_account", return_value=account):
            with patch("codex_antigravity_auth.server.account_manager.release_account"):
                with patch("codex_antigravity_auth.server.account_manager.record_attempt"):
                    with patch("codex_antigravity_auth.server.httpx.AsyncClient", Client):
                        return TestClient(app).post(
                            "/v1/responses",
                            json={"model": "gemini-3.5-flash-high", "input": "hello"},
                        )

    def test_non_streaming_empty_200_returns_failed_response(self):
        response = self._post({"candidates": []})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "failed")
        self.assertEqual(response.json()["error"]["code"], "empty_response")

    def test_non_streaming_max_tokens_returns_incomplete_response(self):
        response = self._post(
            {"candidates": [{"finishReason": "MAX_TOKENS", "content": {"parts": [{"text": "partial"}]}}]}
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "incomplete")
        self.assertEqual(response.json()["incomplete_details"]["reason"], "max_output_tokens")

    def test_non_streaming_safety_block_returns_completed_refusal(self):
        response = self._post({"promptFeedback": {"blockReason": "SAFETY"}, "candidates": []})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "completed")
        self.assertEqual(response.json()["output"][0]["content"][0]["type"], "refusal")

    @staticmethod
    def _post_stream(chunks: list[str]):
        from codex_antigravity_auth.server import app

        class AsyncChunks:
            def __init__(self):
                self._chunks = list(chunks)

            def __aiter__(self):
                return self

            async def __anext__(self):
                if not self._chunks:
                    raise StopAsyncIteration
                return self._chunks.pop(0)

        class Response:
            status_code = 200
            headers = {}

            def aiter_text(self):
                return AsyncChunks()

        class StreamContext:
            async def __aenter__(self):
                return Response()

            async def __aexit__(self, exc_type, exc_value, traceback):
                return None

        class Client:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc_value, traceback):
                return None

            def stream(self, *args, **kwargs):
                return StreamContext()

        account = {"email": "person@example.com", "accessToken": "token", "projectId": "safe-project"}
        with patch("codex_antigravity_auth.server.account_manager.acquire_account", return_value=account):
            with patch("codex_antigravity_auth.server.account_manager.release_account"):
                with patch("codex_antigravity_auth.server.account_manager.record_attempt"):
                    with patch("codex_antigravity_auth.server.httpx.AsyncClient", Client):
                        return TestClient(app).post(
                            "/v1/responses",
                            json={"model": "gemini-3.5-flash-high", "input": "hello", "stream": True},
                        )

    @staticmethod
    def _events(response) -> list[dict]:
        import json

        return [
            json.loads(line[6:])
            for line in response.text.splitlines()
            if line.startswith("data: ") and line != "data: [DONE]"
        ]

    def test_streaming_empty_200_returns_failed_terminal(self):
        response = self._post_stream(["data: [DONE]\n"])

        terminal = [event for event in self._events(response) if event["type"].startswith("response.") and event["type"] in {"response.completed", "response.incomplete", "response.failed"}]
        self.assertEqual([event["type"] for event in terminal], ["response.failed"])
        self.assertEqual(response.text.count("data: [DONE]"), 1)

    def test_streaming_output_without_terminal_signal_returns_failed(self):
        response = self._post_stream(
            ['data: {"candidates":[{"content":{"parts":[{"text":"partial"}]}}]}\n']
        )

        terminal = [
            event
            for event in self._events(response)
            if event["type"] in {"response.completed", "response.incomplete", "response.failed"}
        ]
        self.assertEqual([event["type"] for event in terminal], ["response.failed"])
        self.assertEqual(terminal[0]["response"]["error"]["code"], "missing_terminal_signal")

    def test_streaming_max_tokens_returns_incomplete_terminal(self):
        response = self._post_stream(
            [
                'data: {"candidates":[{"content":{"parts":[{"text":"partial"}]}}]}\n',
                'data: {"candidates":[{"finishReason":"MAX_TOKENS"}]}\n',
                "data: [DONE]\n",
            ]
        )

        terminal = [event for event in self._events(response) if event["type"] in {"response.completed", "response.incomplete", "response.failed"}]
        self.assertEqual([event["type"] for event in terminal], ["response.incomplete"])
        self.assertEqual(terminal[0]["response"]["incomplete_details"]["reason"], "max_output_tokens")

    def test_streaming_safety_block_returns_refusal_lifecycle(self):
        response = self._post_stream(
            ['data: {"promptFeedback":{"blockReason":"SAFETY"},"candidates":[]}\n', "data: [DONE]\n"]
        )

        events = self._events(response)
        terminal = [event for event in events if event["type"] in {"response.completed", "response.incomplete", "response.failed"}]
        refusal_items = [
            event["item"]
            for event in events
            if event["type"] == "response.output_item.done"
            and event.get("item", {}).get("type") == "message"
            and event.get("item", {}).get("content", [{}])[0].get("type") == "refusal"
        ]
        self.assertEqual([event["type"] for event in terminal], ["response.completed"])
        self.assertEqual(len(refusal_items), 1)


if __name__ == "__main__":
    unittest.main()
