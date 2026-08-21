from contextlib import asynccontextmanager
from types import SimpleNamespace
import json
import asyncio
import time
import unittest
import httpx
from unittest.mock import patch, MagicMock
from codex_antigravity_auth import server as server_module
from codex_antigravity_auth.server import (
    app,
    create_response,
    google_rotation_diagnostics,
    openai_compatible_sse_generator,
    stream_error_from_payload,
)
from fastapi.testclient import TestClient
from starlette.requests import Request

class TestServerStreaming(unittest.TestCase):
    def test_openai_compatible_sse_generator_emits_error_event_and_done(self):
        class FakeTransport:
            def __init__(self, **kwargs):
                pass

            def prepare_chat_request(self, codex_req, provider, provider_model, *, stream):
                return SimpleNamespace(
                    payload={"model": provider_model, "messages": [], "stream": stream},
                    url="https://example.invalid/v1/chat/completions",
                    headers={},
                    timeout=5,
                )

            async def stream_chat_events(self, *args, **kwargs):
                raise httpx.ConnectError("upstream refused connection")
                yield  # pragma: no cover - async generator shape

        with patch("codex_antigravity_auth.server.OpenAICompatibleTransport", FakeTransport):
            generator = openai_compatible_sse_generator(
                payload={"model": "nvidia/x"},
                url="https://openrouter.example/v1/chat/completions",
                headers={},
                timeout=5,
                provider={"id": "openrouter"},
                display_model="openrouter:nvidia/x",
            )
            chunks: list[str] = []

            async def consume():
                async for chunk in generator:
                    chunks.append(chunk)

            asyncio.run(consume())

        self.assertEqual(len(chunks), 2)
        self.assertIn("[DONE]", chunks[1])
        error_event = json.loads(chunks[0][len("data: ") :])
        self.assertEqual(error_event["error"]["code"], "connection_error")
        self.assertIn("upstream refused connection", error_event["error"]["message"])

    def test_request_backend_logs_unexpected_errors_instead_of_masking_them(self):
        fake_account = {"email": "test@gmail.com", "accessToken": "dummy_access"}

        class MockClient:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc_val, exc_tb):
                pass

            async def post(self, *args, **kwargs):
                raise KeyError("programming bug")

        with patch("codex_antigravity_auth.server.account_manager.acquire_account", side_effect=[fake_account, None]):
            with patch("codex_antigravity_auth.server.account_manager.release_account"):
                    with patch("codex_antigravity_auth.server.account_manager.mark_failure"):
                        with patch("codex_antigravity_auth.server.account_manager.record_attempt"):
                            with patch("codex_antigravity_auth.server.httpx.AsyncClient", MockClient):
                                with patch("codex_antigravity_auth.server.sys.stderr") as mock_stderr:
                                    response = TestClient(app, raise_server_exceptions=False).post(
                                        "/v1/responses",
                                        json={"model": "gemini-3.5-flash-high", "input": "hello"},
                                    )

        self.assertEqual(response.status_code, 500)
        stderr_text = "".join(call.args[0] for call in mock_stderr.write.call_args_list)
        self.assertIn("request_backend unexpected error", stderr_text)
        self.assertIn("KeyError", stderr_text)

    def test_google_streaming_same_email_rotation_releases_duplicate_lease(self):
        account = {"email": "solo@gmail.com", "accessToken": "token"}

        class ThrowingResponse:
            status_code = 200

            async def aiter_text(self):
                raise httpx.ConnectError("backend down")
                yield  # pragma: no cover - async generator shape

        class StreamContext:
            async def __aenter__(self):
                return ThrowingResponse()

            async def __aexit__(self, exc_type, exc_val, exc_tb):
                pass

        class MockClient:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc_val, exc_tb):
                pass

            def stream(self, *args, **kwargs):
                return StreamContext()

        with patch("codex_antigravity_auth.server.account_manager.acquire_account", side_effect=[account, account]) as acquire:
            with patch("codex_antigravity_auth.server.account_manager.release_account") as release:
                with patch("codex_antigravity_auth.server.account_manager.record_attempt"):
                    with patch("codex_antigravity_auth.server.httpx.AsyncClient", MockClient):
                        response = TestClient(app).post(
                            "/v1/responses",
                            json={"model": "gemini-3.5-flash-high", "input": "hello", "stream": True},
                        )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("response.failed", response.text)
        self.assertEqual(acquire.call_count, 2)
        # The duplicate same-email lease from rotation must be released in
        # addition to the tracked account's release in the finally block.
        self.assertEqual(
            [call.args[0] for call in release.call_args_list],
            ["solo@gmail.com", "solo@gmail.com"],
        )

    def test_byok_chat_stream_logs_finish_reason_success_and_error_events(self):
        provider = {
            "id": "matrix",
            "kind": "openai_chat",
            "baseUrl": "https://example.invalid/v1",
            "apiKey": "sk-test-matrix-key-1234567890",
            "models": ["model"],
        }

        class FakeTransport:
            def __init__(self, **kwargs):
                pass

            def prepare_chat_request(self, codex_req, provider, provider_model, *, stream):
                return SimpleNamespace(
                    payload={"model": provider_model, "messages": [], "stream": stream},
                    url="https://example.invalid/v1/chat/completions",
                    headers={},
                    timeout=5,
                )

            async def stream_chat_events(self, *args, **kwargs):
                yield {"id": "chatcmpl-1", "choices": [{"finish_reason": None, "delta": {"content": "ok"}}]}
                yield {"id": "chatcmpl-1", "choices": [{"finish_reason": "stop", "delta": {}}], "usage": {"total_tokens": 5}}
                yield "[DONE]"

        logged: list[dict] = []

        def fake_write_request_record(record):
            logged.append(record)

        with patch("codex_antigravity_auth.server.all_provider_configs", return_value={"matrix": provider}):
            with patch("codex_antigravity_auth.server.OpenAICompatibleTransport", FakeTransport):
                with patch("codex_antigravity_auth.server.write_request_record", side_effect=fake_write_request_record):
                    response = TestClient(app).post(
                        "/v1/responses",
                        json={"model": "matrix:model", "input": "hello", "stream": True},
                    )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("data: [DONE]", response.text)
        terminal = logged[-1]
        self.assertEqual(terminal["status"], "success")
        self.assertEqual(terminal["usage"], {"total_tokens": 5})

        # Error events must be recorded as failures too.
        class FailingTransport:
            def __init__(self, **kwargs):
                pass

            def prepare_chat_request(self, codex_req, provider, provider_model, *, stream):
                return SimpleNamespace(
                    payload={"model": provider_model, "messages": [], "stream": stream},
                    url="https://example.invalid/v1/chat/completions",
                    headers={},
                    timeout=5,
                )

            async def stream_chat_events(self, *args, **kwargs):
                yield {"error": {"message": "upstream rejected", "code": "bad_request"}}
                yield "[DONE]"

        logged.clear()
        with patch("codex_antigravity_auth.server.all_provider_configs", return_value={"matrix": provider}):
            with patch("codex_antigravity_auth.server.OpenAICompatibleTransport", FailingTransport):
                with patch("codex_antigravity_auth.server.write_request_record", side_effect=fake_write_request_record):
                    error_response = TestClient(app).post(
                        "/v1/responses",
                        json={"model": "matrix:model", "input": "hello", "stream": True},
                    )

        self.assertEqual(error_response.status_code, 200, error_response.text)
        self.assertIn("data: [DONE]", error_response.text)
        terminal = logged[-1]
        self.assertEqual(terminal["status"], "failed")
        self.assertEqual(terminal["error_class"], "bad_request")
        self.assertEqual(terminal["error"], "upstream rejected")

    def test_google_rotation_diagnostics_respects_family_scoped_cooldowns(self):
        now = 1_700_000_000
        data = {
            "accounts": [{"email": "test@example.com"}],
            "accountState": {
                "cooldowns": {"test@example.com": {"claude": now + 300}},
            },
        }
        with patch("codex_antigravity_auth.server.load_accounts_read_only", return_value=data):
            with patch("codex_antigravity_auth.server.time.time", return_value=now):
                claude = google_rotation_diagnostics("claude-sonnet-4-6")
                gemini = google_rotation_diagnostics("gemini-3.5-flash-high")

        self.assertEqual(claude["cooldown_count"], 1)
        self.assertEqual(gemini["cooldown_count"], 0)

    def test_google_rotation_diagnostics_normalizes_millisecond_cooldowns(self):
        data = {
            "accounts": [{"email": "test@example.com"}],
            "accountState": {"cooldowns": {"test@example.com": 1_700_000_000_000}},
        }
        with patch("codex_antigravity_auth.server.load_accounts_read_only", return_value=data):
            with patch("codex_antigravity_auth.server.time.time", return_value=1_700_000_001):
                diagnostics = google_rotation_diagnostics("claude-sonnet-4-6")

        self.assertEqual(diagnostics["cooldown_count"], 0)
        self.assertFalse(diagnostics["all_accounts_cooling_down"])

    def test_sse_generator_translation_output(self):
        with TestClient(app) as test_client:
            fake_account = {
                "email": "test@gmail.com",
                "accessToken": "dummy_access",
                "fingerprint": {
                    "deviceId": "dev_123",
                    "sessionToken": "session_123",
                    "userAgent": "Antigravity/2.0.0",
                    "apiClient": "google-cloud-sdk"
                }
            }
            
            codex_payload = {
                "model": "gemini-3.5-flash-high",
                "input": "Write a short story about AI",
                "stream": True
            }
            
            mock_response = MagicMock(spec=httpx.Response)
            mock_response.status_code = 200
            
            google_sse_chunks = [
                'data: {"candidates": [{"content": {"parts": [{"text": "Once"}]}}]}\n',
                'data: {"candidates": [{"content": {"parts": [{"text": " upon"}]}}]}\n',
                'data: {"candidates": [{"content": {"parts": [{"text": " a time"}]}}]}\n',
                'data: [DONE]\n'
            ]
            
            class AsyncAiterText:
                def __init__(self, chunks):
                    self.chunks = list(chunks)
                def __aiter__(self):
                    return self
                async def __anext__(self):
                    if not self.chunks:
                        raise StopAsyncIteration
                    return self.chunks.pop(0)
            
            mock_response.aiter_text = MagicMock(return_value=AsyncAiterText(google_sse_chunks))
            
            class StreamContext:
                async def __aenter__(self):
                    return mock_response
                async def __aexit__(self, exc_type, exc_val, exc_tb):
                    pass
            
            class MockClientInstance:
                def stream(self, *args, **kwargs):
                    return StreamContext()
                
                async def __aenter__(self):
                    return self
                async def __aexit__(self, exc_type, exc_val, exc_tb):
                    pass
            
            class CleanAsyncClientMock:
                def __init__(self, *args, **kwargs):
                    pass
                async def __aenter__(self):
                    return MockClientInstance()
                async def __aexit__(self, exc_type, exc_val, exc_tb):
                    pass
            
            with patch("codex_antigravity_auth.server.account_manager.acquire_account", return_value=fake_account):
                with patch("codex_antigravity_auth.server.httpx.AsyncClient", CleanAsyncClientMock):
                    response = test_client.post("/v1/responses", json=codex_payload)
                self.assertEqual(response.status_code, 200)
                
                lines = response.text.split("\n")
                
                created_lines = [l for l in lines if "response.created" in l]
                delta_lines = [l for l in lines if "response.output_text.delta" in l]
                done_lines = [l for l in lines if "response.completed" in l]
                
                self.assertTrue(len(created_lines) > 0, "Missing response.created event")
                self.assertTrue(len(delta_lines) > 0, "Missing response.output_text.delta event")
                self.assertTrue(len(done_lines) > 0, "Missing response.completed event")
                events = [
                    json.loads(line[6:])
                    for line in lines
                    if line.startswith("data: ") and line != "data: [DONE]"
                ]
                self.assertEqual([event["sequence_number"] for event in events], list(range(len(events))))
                text_done = [event for event in events if event.get("type") == "response.output_text.done"]
                self.assertEqual(text_done[0]["text"], "Once upon a time")
                text_deltas = [event for event in events if event.get("type") == "response.output_text.delta"]
                self.assertTrue(all("item_id" in event for event in text_deltas))
                completed = [event for event in events if event.get("type") == "response.completed"]
                completed_response = completed[0]["response"]
                self.assertEqual(completed_response["model"], "gemini-3.5-flash-high")
                self.assertEqual(completed_response["output"][0]["content"][0]["text"], "Once upon a time")
                self.assertIsInstance(completed_response["created_at"], int)

    def test_google_non_streaming_error_payload_fails_instead_of_completing(self):
        fake_account = {"email": "test@gmail.com", "accessToken": "dummy_access"}

        class MockClient:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc_val, exc_tb):
                pass

            async def post(self, *args, **kwargs):
                return httpx.Response(
                    200,
                    json={"error": {"status": "RESOURCE_EXHAUSTED", "message": "quota exhausted"}},
                )

        with patch("codex_antigravity_auth.server.account_manager.acquire_account", return_value=fake_account):
            with patch("codex_antigravity_auth.server.account_manager.record_attempt") as record:
                with patch("codex_antigravity_auth.server.httpx.AsyncClient", MockClient):
                    response = TestClient(app).post(
                        "/v1/responses",
                        json={"model": "gemini-3.5-flash-high", "input": "hello"},
                    )

        self.assertEqual(response.status_code, 429)
        detail = response.json()["detail"]
        self.assertIn("quota exhausted", detail["message"])
        self.assertEqual(detail["diagnostics"]["selected_account_family"], "gemini")
        self.assertIn("rotation_attempted", detail["diagnostics"])
        record.assert_called_once()
        self.assertEqual(record.call_args.args[2].category, "quota")

    def test_google_non_streaming_releases_acquired_account_on_backend_failure(self):
        fake_account = {"email": "test@gmail.com", "accessToken": "dummy_access"}

        class MockClient:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc_val, exc_tb):
                pass

            async def post(self, *args, **kwargs):
                raise httpx.ConnectError("backend down")

        with patch("codex_antigravity_auth.server.account_manager.acquire_account", side_effect=[fake_account, None]):
            with patch("codex_antigravity_auth.server.account_manager.release_account") as release:
                with patch("codex_antigravity_auth.server.account_manager.mark_failure"):
                    with patch("codex_antigravity_auth.server.account_manager.record_attempt"):
                        with patch("codex_antigravity_auth.server.httpx.AsyncClient", MockClient):
                            response = TestClient(app).post(
                                "/v1/responses",
                                json={"model": "gemini-3.5-flash-high", "input": "hello"},
                            )

        self.assertEqual(response.status_code, 502)
        diagnostics = response.json()["detail"]["diagnostics"]
        self.assertEqual(diagnostics["attempt_count"], 1)
        self.assertEqual(diagnostics["attempted_account_refs"], ["account-1"])
        self.assertEqual([call.args[0] for call in release.call_args_list], ["test@gmail.com"])

    def test_google_rotation_records_and_releases_every_attempted_account(self):
        first = {"email": "first@gmail.com", "accessToken": "first-token"}
        second = {"email": "second@gmail.com", "accessToken": "second-token"}

        class MockClient:
            responses = [
                httpx.Response(401, text="expired"),
                httpx.Response(
                    200,
                    json={
                        "candidates": [
                            {"finishReason": "STOP", "content": {"parts": [{"text": "ok"}]}}
                        ]
                    },
                ),
            ]

            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc_val, exc_tb):
                pass

            async def post(self, *args, **kwargs):
                return self.responses.pop(0)

        with patch("codex_antigravity_auth.server.account_manager.acquire_account", side_effect=[first, second]):
            with patch("codex_antigravity_auth.server.account_manager.release_account") as release:
                with patch("codex_antigravity_auth.server.account_manager.mark_failure"):
                    with patch("codex_antigravity_auth.server.account_manager.record_attempt") as record:
                        with patch("codex_antigravity_auth.server.httpx.AsyncClient", MockClient):
                            response = TestClient(app).post(
                                "/v1/responses",
                                json={"model": "gemini-3.5-flash-high", "input": "hello"},
                            )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual([call.args[0] for call in release.call_args_list], ["first@gmail.com", "second@gmail.com"])
        self.assertEqual([call.args[0] for call in record.call_args_list], ["first@gmail.com", "second@gmail.com"])
        self.assertEqual([call.args[2].category for call in record.call_args_list], ["auth", "success"])
        self.assertEqual(record.call_args_list[0].kwargs["error_class"], "auth")

    def test_google_stream_disconnect_records_cancellation_and_releases_lease(self):
        account = {"email": "cancelled@gmail.com", "accessToken": "token"}

        async def scenario():
            sent = False

            async def receive():
                nonlocal sent
                if sent:
                    return {"type": "http.disconnect"}
                sent = True
                return {
                    "type": "http.request",
                    "body": json.dumps(
                        {"model": "gemini-3.5-flash-high", "input": "hello", "stream": True}
                    ).encode(),
                    "more_body": False,
                }

            request = Request(
                {
                    "type": "http",
                    "method": "POST",
                    "path": "/v1/responses",
                    "headers": [],
                    "query_string": b"",
                    "client": ("testserver", 50000),
                    "server": ("testserver", 80),
                    "scheme": "http",
                },
                receive,
            )
            response = await create_response(request)
            iterator = response.body_iterator
            first_event = await iterator.__anext__()
            await iterator.aclose()
            return first_event

        class MockResponse:
            status_code = 200
            async def aiter_text(self):
                yield "data: {\"type\": \"response.created\", \"response\": {\"id\": \"resp-1\"}}\n\n"
                await asyncio.sleep(10)

        @asynccontextmanager
        async def mock_stream(request, lease):
            yield MockResponse()

        with patch("codex_antigravity_auth.google_transport.GoogleTransport.stream", side_effect=mock_stream):
            with patch("codex_antigravity_auth.server.account_manager.acquire_account", return_value=account):
                with patch("codex_antigravity_auth.server.account_manager.release_account") as release:
                    with patch("codex_antigravity_auth.server.account_manager.record_attempt") as record:
                        with patch("codex_antigravity_auth.server.write_request_record") as request_log:
                            first_event = asyncio.run(scenario())

        self.assertIn("response.created", first_event)
        release.assert_called_once_with("cancelled@gmail.com")
        record.assert_called_once()
        self.assertEqual(record.call_args.args[2].category, "cancelled")
        self.assertEqual(record.call_args.kwargs["error_class"], "cancelled")
        request_log.assert_called_once()
        self.assertTrue(request_log.call_args.args[0]["cancelled"])
        self.assertEqual(request_log.call_args.args[0]["outcome_category"], "cancelled")

    def test_google_request_log_records_terminal_attempt_rotation_and_usage(self):
        first = {"email": "first@gmail.com", "accessToken": "first-token"}
        second = {"email": "second@gmail.com", "accessToken": "second-token"}

        class MockClient:
            responses = [
                httpx.Response(429, headers={"Retry-After": "10"}, text="quota"),
                httpx.Response(
                    200,
                    json={
                        "candidates": [
                            {"finishReason": "MAX_TOKENS", "content": {"parts": [{"text": "partial"}]}}
                        ],
                        "usageMetadata": {"promptTokenCount": 2, "candidatesTokenCount": 3, "totalTokenCount": 5},
                    },
                ),
            ]

            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc_val, exc_tb):
                pass

            async def post(self, *args, **kwargs):
                return self.responses.pop(0)

        records = []
        with patch("codex_antigravity_auth.server.account_manager.acquire_account", side_effect=[first, second]):
            with patch("codex_antigravity_auth.server.account_manager.release_account"):
                with patch("codex_antigravity_auth.server.account_manager.mark_failure"):
                    with patch("codex_antigravity_auth.server.account_manager.record_attempt"):
                        with patch("codex_antigravity_auth.server.write_request_record", side_effect=records.append):
                            with patch("codex_antigravity_auth.server.httpx.AsyncClient", MockClient):
                                response = TestClient(app).post(
                                    "/v1/responses",
                                    json={
                                        "model": "gemini-3.5-flash-high",
                                        "input": "hello",
                                        "metadata": {"run_id": "anti-correlated-run"},
                                    },
                                )

        self.assertEqual(response.status_code, 200, response.text)
        terminal = records[-1]
        self.assertEqual(terminal["run_id"], "anti-correlated-run")
        self.assertEqual(terminal["terminal_kind"], "incomplete")
        self.assertEqual(terminal["terminal_reason"], "max_tokens")
        self.assertEqual(terminal["attempt_count"], 2)
        self.assertEqual(terminal["rotation_count"], 1)
        self.assertEqual(terminal["outcome_category"], "success")
        self.assertEqual(terminal["cooldown_scope"], "family")
        self.assertEqual(terminal["cooldown_category"], "rate_limit")
        self.assertEqual(terminal["usage"], {"input_tokens": 2, "output_tokens": 3, "total_tokens": 5})

    def test_google_non_streaming_terminal_matrix_has_one_attempt_and_release(self):
        account = {"email": "matrix@gmail.com", "accessToken": "token"}
        cases = [
            (
                "completed",
                {"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": "ok"}]}}]},
                "completed",
                "success",
            ),
            (
                "incomplete",
                {"candidates": [{"finishReason": "MAX_TOKENS", "content": {"parts": [{"text": "partial"}]}}]},
                "incomplete",
                "success",
            ),
            ("refusal", {"promptFeedback": {"blockReason": "SAFETY"}, "candidates": []}, "completed", "success"),
            ("empty", {"candidates": []}, "failed", "failure"),
            ("malformed", {"candidates": "bad"}, "failed", "failure"),
        ]

        for name, payload, expected_terminal, expected_attempt_status in cases:
            with self.subTest(name=name):
                class MockClient:
                    def __init__(self, *args, **kwargs):
                        pass

                    async def __aenter__(self):
                        return self

                    async def __aexit__(self, exc_type, exc_val, exc_tb):
                        pass

                    async def post(self, *args, **kwargs):
                        return httpx.Response(200, json=payload)

                with patch("codex_antigravity_auth.server.account_manager.acquire_account", return_value=account) as acquire:
                    with patch("codex_antigravity_auth.server.account_manager.release_account") as release:
                        with patch("codex_antigravity_auth.server.account_manager.record_attempt") as record:
                            with patch("codex_antigravity_auth.server.httpx.AsyncClient", MockClient):
                                response = TestClient(app).post(
                                    "/v1/responses",
                                    json={"model": "gemini-3.5-flash-high", "input": "hello"},
                                )

                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["status"], expected_terminal)
                acquire.assert_called_once()
                release.assert_called_once_with("matrix@gmail.com")
                record.assert_called_once()
                self.assertEqual(
                    record.call_args.args[2].category == "success",
                    expected_attempt_status == "success",
                )

    def test_byok_chat_non_streaming_terminal_matrix_uses_protocol_contract(self):
        provider = {
            "id": "matrix",
            "kind": "openai_chat",
            "baseUrl": "https://example.invalid/v1",
            "apiKey": "sk-test-matrix-key-1234567890",
            "models": ["model"],
        }
        cases = [
            (
                "completed",
                {"choices": [{"finish_reason": "stop", "message": {"content": "ok"}}]},
                "completed",
            ),
            (
                "incomplete",
                {"choices": [{"finish_reason": "length", "message": {"content": "partial"}}]},
                "incomplete",
            ),
            (
                "refusal",
                {"choices": [{"finish_reason": "content_filter", "message": {"content": None}}]},
                "completed",
            ),
            ("empty", {"choices": []}, "failed"),
            ("malformed", {"choices": "bad"}, "failed"),
        ]

        for name, payload, expected_terminal in cases:
            with self.subTest(name=name):
                class MockClient:
                    def __init__(self, *args, **kwargs):
                        pass

                    async def __aenter__(self):
                        return self

                    async def __aexit__(self, exc_type, exc_val, exc_tb):
                        pass

                    async def post(self, *args, **kwargs):
                        return httpx.Response(200, json=payload)

                with patch("codex_antigravity_auth.server.all_provider_configs", return_value={"matrix": provider}):
                    with patch("codex_antigravity_auth.server.httpx.AsyncClient", MockClient):
                        response = TestClient(app).post(
                            "/v1/responses",
                            json={"model": "matrix:model", "input": "hello"},
                        )

                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["status"], expected_terminal)

    def test_byok_chat_forwards_normalized_model_id_for_double_prefixed_request(self):
        provider = {
            "id": "openrouter",
            "kind": "openai_chat",
            "baseUrl": "https://openrouter.ai/api/v1",
            "apiKey": "sk-test-matrix-key-1234567890",
            "models": ["openrouter/nvidia/nemotron-3-ultra-550b-a55b:free"],
        }
        forwarded_models: list[str] = []

        class MockClient:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc_val, exc_tb):
                pass

            async def post(self, *args, **kwargs):
                forwarded_models.append(kwargs.get("json", {}).get("model"))
                return httpx.Response(
                    200,
                    json={"choices": [{"finish_reason": "stop", "message": {"content": "ok"}}]},
                )

        with patch("codex_antigravity_auth.server.all_provider_configs", return_value={"openrouter": provider}):
            with patch("codex_antigravity_auth.server.httpx.AsyncClient", MockClient):
                legacy = TestClient(app).post(
                    "/v1/responses",
                    json={"model": "openrouter:openrouter/nvidia/nemotron-3-ultra-550b-a55b:free", "input": "hello"},
                )
                normalized = TestClient(app).post(
                    "/v1/responses",
                    json={"model": "openrouter:nvidia/nemotron-3-ultra-550b-a55b:free", "input": "hello"},
                )

        self.assertEqual(legacy.status_code, 200, legacy.text)
        self.assertEqual(normalized.status_code, 200, normalized.text)
        self.assertEqual(
            forwarded_models,
            [
                "nvidia/nemotron-3-ultra-550b-a55b:free",
                "nvidia/nemotron-3-ultra-550b-a55b:free",
            ],
        )

    def test_google_streaming_terminal_matrix_records_one_attempt_and_release(self):
        account = {"email": "stream-matrix@gmail.com", "accessToken": "token"}
        cases = [
            (
                "incomplete",
                ['data: {"candidates":[{"finishReason":"MAX_TOKENS","content":{"parts":[{"text":"partial"}]}}]}\n', "data: [DONE]\n"],
                "response.incomplete",
                "success",
            ),
            (
                "refusal",
                ['data: {"promptFeedback":{"blockReason":"SAFETY"},"candidates":[]}\n', "data: [DONE]\n"],
                "response.completed",
                "success",
            ),
            ("empty", ["data: [DONE]\n"], "response.failed", "failure"),
        ]

        for name, chunks, terminal_event, attempt_status in cases:
            with self.subTest(name=name):
                class AsyncText:
                    def __init__(self):
                        self.items = list(chunks)

                    def __aiter__(self):
                        return self

                    async def __anext__(self):
                        if not self.items:
                            raise StopAsyncIteration
                        return self.items.pop(0)

                response_mock = MagicMock(spec=httpx.Response)
                response_mock.status_code = 200
                response_mock.aiter_text = MagicMock(return_value=AsyncText())

                class StreamContext:
                    async def __aenter__(self):
                        return response_mock

                    async def __aexit__(self, exc_type, exc_val, exc_tb):
                        pass

                class MockClient:
                    def __init__(self, *args, **kwargs):
                        pass

                    async def __aenter__(self):
                        return self

                    async def __aexit__(self, exc_type, exc_val, exc_tb):
                        pass

                    def stream(self, *args, **kwargs):
                        return StreamContext()

                with patch("codex_antigravity_auth.server.account_manager.acquire_account", return_value=account) as acquire:
                    with patch("codex_antigravity_auth.server.account_manager.release_account") as release:
                        with patch("codex_antigravity_auth.server.account_manager.record_attempt") as record:
                            with patch("codex_antigravity_auth.server.httpx.AsyncClient", MockClient):
                                response = TestClient(app).post(
                                    "/v1/responses",
                                    json={"model": "gemini-3.5-flash-high", "input": "hello", "stream": True},
                                )

                self.assertEqual(response.status_code, 200, response.text)
                self.assertIn(terminal_event, response.text)
                acquire.assert_called_once()
                release.assert_called_once_with("stream-matrix@gmail.com")
                record.assert_called_once()
                self.assertEqual(
                    record.call_args.args[2].category == "success",
                    attempt_status == "success",
                )

    def test_models_endpoint_returns_native_catalog_when_provider_catalog_blocks(self):
        def slow_provider_configs():
            time.sleep(0.2)
            return {
                "openrouter": {
                    "id": "openrouter",
                    "displayName": "OpenRouter",
                    "kind": "openai_chat",
                    "models": ["openrouter/auto"],
                    "apiKey": "sk-test1234567890",
                }
            }

        started = time.monotonic()
        with patch("codex_antigravity_auth.server.MODEL_CATALOG_PROVIDER_TIMEOUT_SECONDS", 0.01):
            with patch(
                "codex_antigravity_auth.server.all_provider_configs_read_only",
                side_effect=slow_provider_configs,
            ):
                response = TestClient(app).get("/v1/models")
        elapsed = time.monotonic() - started

        self.assertEqual(response.status_code, 200)
        ids = [model["id"] for model in response.json()["data"]]
        self.assertIn("claude-sonnet-4-6", ids)
        self.assertIn("claude-opus-4-6-thinking", ids)
        self.assertNotIn("openrouter:openrouter/auto", ids)
        self.assertLess(elapsed, 1.0)

    def test_google_streaming_invalid_json_chunk_fails_instead_of_completing(self):
        fake_account = {"email": "test@gmail.com", "accessToken": "dummy_access"}

        class AsyncAiterText:
            def __init__(self, chunks):
                self.chunks = list(chunks)

            def __aiter__(self):
                return self

            async def __anext__(self):
                if not self.chunks:
                    raise StopAsyncIteration
                return self.chunks.pop(0)

        mock_response = MagicMock(spec=httpx.Response)
        mock_response.status_code = 200
        mock_response.aiter_text = MagicMock(return_value=AsyncAiterText(['data: {"candidates": [}\n', "data: [DONE]\n"]))

        class StreamContext:
            async def __aenter__(self):
                return mock_response

            async def __aexit__(self, exc_type, exc_val, exc_tb):
                pass

        class MockClient:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc_val, exc_tb):
                pass

            def stream(self, *args, **kwargs):
                return StreamContext()

        with patch("codex_antigravity_auth.server.account_manager.acquire_account", return_value=fake_account):
            with patch("codex_antigravity_auth.server.httpx.AsyncClient", MockClient):
                response = TestClient(app).post(
                    "/v1/responses",
                    json={"model": "gemini-3.5-flash-high", "input": "hello", "stream": True},
                )

        self.assertIn("response.failed", response.text)
        self.assertIn("invalid_stream_chunk", response.text)
        self.assertNotIn("response.completed", response.text)

    def test_stream_error_from_payload_ignores_empty_error_shapes(self):
        for payload in (
            {"error": None},
            {"error": ""},
            {"error": {}},
            {"error": {"message": ""}},
        ):
            with self.subTest(payload=payload):
                self.assertIsNone(stream_error_from_payload(payload))

        self.assertEqual(
            stream_error_from_payload({"error": {"status": "RESOURCE_EXHAUSTED"}}),
            ("RESOURCE_EXHAUSTED", "RESOURCE_EXHAUSTED"),
        )

    def test_sse_generator_handling_wrapped_responses(self):
        with TestClient(app) as test_client:
            fake_account = {
                "email": "test@gmail.com",
                "accessToken": "dummy_access",
                "fingerprint": {
                    "deviceId": "dev_123",
                    "sessionToken": "session_123",
                    "userAgent": "Antigravity/2.0.0",
                    "apiClient": "google-cloud-sdk"
                }
            }
            codex_payload = {
                "model": "gemini-3.5-flash-high",
                "input": "Write a short story about AI",
                "stream": True
            }
            
            mock_response = MagicMock(spec=httpx.Response)
            mock_response.status_code = 200
            
            google_sse_chunks = [
                'data: {"response": {"candidates": [{"content": {"parts": [{"text": "Hello stream"}]}}]}}\n',
                'data: [DONE]\n'
            ]
            
            class AsyncAiterText:
                def __init__(self, chunks):
                    self.chunks = list(chunks)
                def __aiter__(self):
                    return self
                async def __anext__(self):
                    if not self.chunks:
                        raise StopAsyncIteration
                    return self.chunks.pop(0)
            
            mock_response.aiter_text = MagicMock(return_value=AsyncAiterText(google_sse_chunks))
            
            class StreamContext:
                async def __aenter__(self):
                    return mock_response
                async def __aexit__(self, exc_type, exc_val, exc_tb):
                    pass
            
            class MockClientInstance:
                def stream(self, *args, **kwargs):
                    return StreamContext()
                async def __aenter__(self):
                    return self
                async def __aexit__(self, exc_type, exc_val, exc_tb):
                    pass
            
            class CleanAsyncClientMock:
                def __init__(self, *args, **kwargs):
                    pass
                async def __aenter__(self):
                    return MockClientInstance()
                async def __aexit__(self, exc_type, exc_val, exc_tb):
                    pass
            
            with patch("codex_antigravity_auth.server.account_manager.acquire_account", return_value=fake_account):
                with patch("codex_antigravity_auth.server.httpx.AsyncClient", CleanAsyncClientMock):
                    response = test_client.post("/v1/responses", json=codex_payload)
                self.assertEqual(response.status_code, 200)
                
                lines = response.text.split("\n")
                delta_lines = [l for l in lines if "response.output_text.delta" in l]
                self.assertTrue(len(delta_lines) > 0, "Missing response.output_text.delta event for nested wrapped response")
                self.assertIn("Hello stream", delta_lines[0])

    def test_google_streaming_error_frame_fails_instead_of_completing(self):
        with TestClient(app) as test_client:
            fake_account = {
                "email": "test@gmail.com",
                "accessToken": "dummy_access",
                "fingerprint": {"userAgent": "Antigravity/2.0.0", "apiClient": "google-cloud-sdk"},
            }
            mock_response = MagicMock(spec=httpx.Response)
            mock_response.status_code = 200

            class AsyncAiterText:
                def __init__(self, chunks):
                    self.chunks = list(chunks)

                def __aiter__(self):
                    return self

                async def __anext__(self):
                    if not self.chunks:
                        raise StopAsyncIteration
                    return self.chunks.pop(0)

            mock_response.aiter_text = MagicMock(return_value=AsyncAiterText([
                'data: {"error": {"code": "rate_limit_exceeded", "message": "quota exhausted"}}\n',
                "data: [DONE]\n",
            ]))

            class StreamContext:
                async def __aenter__(self):
                    return mock_response

                async def __aexit__(self, exc_type, exc_val, exc_tb):
                    pass

            class MockClientInstance:
                def stream(self, *args, **kwargs):
                    return StreamContext()

                async def __aenter__(self):
                    return self

                async def __aexit__(self, exc_type, exc_val, exc_tb):
                    pass

            class CleanAsyncClientMock:
                def __init__(self, *args, **kwargs):
                    pass

                async def __aenter__(self):
                    return MockClientInstance()

                async def __aexit__(self, exc_type, exc_val, exc_tb):
                    pass

            with patch("codex_antigravity_auth.server.account_manager.acquire_account", return_value=fake_account):
                with patch("codex_antigravity_auth.server.account_manager.record_attempt") as record:
                    with patch("codex_antigravity_auth.server.httpx.AsyncClient", CleanAsyncClientMock):
                        response = test_client.post(
                            "/v1/responses",
                            json={"model": "gemini-3.5-flash-high", "input": "hello", "stream": True},
                        )

            self.assertEqual(response.status_code, 200)
            self.assertIn("response.failed", response.text)
            self.assertIn("rate_limit_exceeded", response.text)
            self.assertNotIn("response.completed", response.text)
            events = [
                json.loads(line[6:])
                for line in response.text.splitlines()
                if line.startswith("data: ") and line != "data: [DONE]"
            ]
            failed_events = [event for event in events if event.get("type") == "response.failed"]
            self.assertEqual(failed_events[0]["response"]["error"]["code"], "rate_limit_exceeded")
            record.assert_called_once()
            self.assertEqual(record.call_args.args[0], "test@gmail.com")
            self.assertEqual(record.call_args.args[2].category, "quota")

    def test_streaming_rotation_rebuilds_request_with_rotated_project(self):
        with TestClient(app) as test_client:
            first_account = {
                "email": "first@gmail.com",
                "accessToken": "first-access",
                "projectId": "project-first",
                "fingerprint": {"userAgent": "Antigravity/2.0.0", "apiClient": "google-cloud-sdk"},
            }
            second_account = {
                "email": "second@gmail.com",
                "accessToken": "second-access",
                "projectId": "project-second",
                "fingerprint": {"userAgent": "Antigravity/2.0.0", "apiClient": "google-cloud-sdk"},
            }
            requests = []

            class AsyncAiterText:
                def __init__(self, chunks):
                    self.chunks = list(chunks)

                def __aiter__(self):
                    return self

                async def __anext__(self):
                    if not self.chunks:
                        raise StopAsyncIteration
                    return self.chunks.pop(0)

            class StreamContext:
                def __init__(self, response):
                    self.response = response

                async def __aenter__(self):
                    return self.response

                async def __aexit__(self, exc_type, exc_val, exc_tb):
                    pass

            class MockClientInstance:
                def stream(self, method, url, json=None, headers=None):
                    requests.append({"json": json, "headers": headers})
                    if len(requests) == 1:
                        response = MagicMock(spec=httpx.Response)
                        response.status_code = 429
                        response.aread.return_value = b"rate limited"
                        return StreamContext(response)

                    response = MagicMock(spec=httpx.Response)
                    response.status_code = 200
                    response.aiter_text = MagicMock(return_value=AsyncAiterText([
                        'data: {"candidates": [{"content": {"parts": [{"text": "rotated ok"}]}}]}\n',
                        'data: [DONE]\n',
                    ]))
                    return StreamContext(response)

                async def __aenter__(self):
                    return self

                async def __aexit__(self, exc_type, exc_val, exc_tb):
                    pass

            class CleanAsyncClientMock:
                def __init__(self, *args, **kwargs):
                    pass

                async def __aenter__(self):
                    return MockClientInstance()

                async def __aexit__(self, exc_type, exc_val, exc_tb):
                    pass

            with patch(
                "codex_antigravity_auth.server.account_manager.acquire_account",
                side_effect=[first_account, second_account],
            ):
                with patch("codex_antigravity_auth.server.account_manager.release_account") as release:
                    with patch("codex_antigravity_auth.server.account_manager.mark_failure"):
                        with patch("codex_antigravity_auth.server.httpx.AsyncClient", CleanAsyncClientMock):
                            response = test_client.post(
                                "/v1/responses",
                                json={"model": "gemini-3.5-flash-high", "input": "hello", "stream": True},
                            )

            self.assertEqual(response.status_code, 200)
            self.assertIn("rotated ok", response.text)
            self.assertEqual([request["json"]["project"] for request in requests], ["project-first", "project-second"])
            self.assertEqual(
                [request["headers"]["Authorization"] for request in requests],
                ["Bearer first-access", "Bearer second-access"],
            )
            self.assertEqual(
                [call.args[0] for call in release.call_args_list],
                ["first@gmail.com", "second@gmail.com"],
            )

    def test_google_streaming_account_scoped_error_rotates_before_failing(self):
        with TestClient(app) as test_client:
            first_account = {
                "email": "first@gmail.com",
                "accessToken": "first-access",
                "projectId": "project-first",
                "fingerprint": {"userAgent": "Antigravity/2.0.0", "apiClient": "google-cloud-sdk"},
            }
            second_account = {
                "email": "second@gmail.com",
                "accessToken": "second-access",
                "projectId": "project-second",
                "fingerprint": {"userAgent": "Antigravity/2.0.0", "apiClient": "google-cloud-sdk"},
            }
            requests = []

            class AsyncAiterText:
                def __init__(self, chunks):
                    self.chunks = list(chunks)

                def __aiter__(self):
                    return self

                async def __anext__(self):
                    if not self.chunks:
                        raise StopAsyncIteration
                    return self.chunks.pop(0)

            class StreamContext:
                def __init__(self, response):
                    self.response = response

                async def __aenter__(self):
                    return self.response

                async def __aexit__(self, exc_type, exc_val, exc_tb):
                    pass

            class MockClientInstance:
                def stream(self, method, url, json=None, headers=None):
                    requests.append({"json": json, "headers": headers})
                    response = MagicMock(spec=httpx.Response)
                    response.status_code = 200
                    if len(requests) == 1:
                        response.aiter_text = MagicMock(return_value=AsyncAiterText([
                            'data: {"error": {"code": "RESOURCE_EXHAUSTED", "message": "quota exhausted"}}\n',
                            "data: [DONE]\n",
                        ]))
                    else:
                        response.aiter_text = MagicMock(return_value=AsyncAiterText([
                            'data: {"candidates": [{"content": {"parts": [{"text": "rotated ok"}]}}]}\n',
                            "data: [DONE]\n",
                        ]))
                    return StreamContext(response)

                async def __aenter__(self):
                    return self

                async def __aexit__(self, exc_type, exc_val, exc_tb):
                    pass

            class CleanAsyncClientMock:
                def __init__(self, *args, **kwargs):
                    pass

                async def __aenter__(self):
                    return MockClientInstance()

                async def __aexit__(self, exc_type, exc_val, exc_tb):
                    pass

            with patch(
                "codex_antigravity_auth.server.account_manager.acquire_account",
                side_effect=[first_account, second_account],
            ):
                with patch("codex_antigravity_auth.server.account_manager.record_attempt") as record:
                        with patch("codex_antigravity_auth.server.account_manager.release_account") as release:
                            with patch("codex_antigravity_auth.server.httpx.AsyncClient", CleanAsyncClientMock):
                                response = test_client.post(
                                    "/v1/responses",
                                    json={"model": "gemini-3.5-flash-high", "input": "hello", "stream": True},
                                )

            self.assertEqual(response.status_code, 200)
            self.assertIn("rotated ok", response.text)
            self.assertNotIn("response.failed", response.text)
            self.assertEqual([request["json"]["project"] for request in requests], ["project-first", "project-second"])
            self.assertEqual([call.args[0] for call in record.call_args_list], ["first@gmail.com", "second@gmail.com"])
            self.assertEqual(
                [call.args[2].category for call in record.call_args_list],
                ["quota", "success"],
            )
            self.assertEqual([call.args[0] for call in release.call_args_list], ["first@gmail.com", "second@gmail.com"])

    def test_google_streaming_rotation_discards_failed_attempt_usage(self):
        with TestClient(app) as test_client:
            first_account = {
                "email": "first@gmail.com",
                "accessToken": "first-access",
                "projectId": "project-first",
                "fingerprint": {"userAgent": "Antigravity/2.0.0", "apiClient": "google-cloud-sdk"},
            }
            second_account = {
                "email": "second@gmail.com",
                "accessToken": "second-access",
                "projectId": "project-second",
                "fingerprint": {"userAgent": "Antigravity/2.0.0", "apiClient": "google-cloud-sdk"},
            }
            attempts = []

            class AsyncAiterText:
                def __init__(self, chunks):
                    self.chunks = list(chunks)

                def __aiter__(self):
                    return self

                async def __anext__(self):
                    if not self.chunks:
                        raise StopAsyncIteration
                    return self.chunks.pop(0)

            class StreamContext:
                def __init__(self, response):
                    self.response = response

                async def __aenter__(self):
                    return self.response

                async def __aexit__(self, exc_type, exc_val, exc_tb):
                    pass

            class MockClientInstance:
                def stream(self, method, url, json=None, headers=None):
                    attempts.append(json["project"])
                    response = MagicMock(spec=httpx.Response)
                    response.status_code = 200
                    if len(attempts) == 1:
                        response.aiter_text = MagicMock(return_value=AsyncAiterText([
                            'data: {"usageMetadata": {"promptTokenCount": 99, "candidatesTokenCount": 88, "totalTokenCount": 187}}\n',
                            'data: {"error": {"code": "RESOURCE_EXHAUSTED", "message": "quota exhausted"}}\n',
                            "data: [DONE]\n",
                        ]))
                    else:
                        response.aiter_text = MagicMock(return_value=AsyncAiterText([
                            'data: {"candidates": [{"content": {"parts": [{"text": "rotated ok"}]}}]}\n',
                            "data: [DONE]\n",
                        ]))
                    return StreamContext(response)

                async def __aenter__(self):
                    return self

                async def __aexit__(self, exc_type, exc_val, exc_tb):
                    pass

            class CleanAsyncClientMock:
                def __init__(self, *args, **kwargs):
                    pass

                async def __aenter__(self):
                    return MockClientInstance()

                async def __aexit__(self, exc_type, exc_val, exc_tb):
                    pass

            with patch(
                "codex_antigravity_auth.server.account_manager.acquire_account",
                side_effect=[first_account, second_account],
            ):
                with patch("codex_antigravity_auth.server.account_manager.mark_failure"):
                    with patch("codex_antigravity_auth.server.httpx.AsyncClient", CleanAsyncClientMock):
                        response = test_client.post(
                            "/v1/responses",
                            json={"model": "gemini-3.5-flash-high", "input": "hello", "stream": True},
                        )

            events = [
                json.loads(line[6:])
                for line in response.text.splitlines()
                if line.startswith("data: ") and line != "data: [DONE]"
            ]
            completed = [event for event in events if event.get("type") == "response.completed"]

            self.assertEqual(attempts, ["project-first", "project-second"])
            self.assertIn("rotated ok", response.text)
            self.assertNotIn("response.failed", response.text)
            self.assertEqual(
                completed[0]["response"]["usage"],
                {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
            )

    def test_google_streaming_does_not_rotate_after_output_started(self):
        with TestClient(app) as test_client:
            first_account = {
                "email": "first@gmail.com",
                "accessToken": "first-access",
                "projectId": "project-first",
                "fingerprint": {"userAgent": "Antigravity/2.0.0", "apiClient": "google-cloud-sdk"},
            }
            attempts = []

            class AsyncAiterText:
                def __init__(self, chunks):
                    self.chunks = list(chunks)

                def __aiter__(self):
                    return self

                async def __anext__(self):
                    if not self.chunks:
                        raise StopAsyncIteration
                    return self.chunks.pop(0)

            class StreamContext:
                def __init__(self, response):
                    self.response = response

                async def __aenter__(self):
                    return self.response

                async def __aexit__(self, exc_type, exc_val, exc_tb):
                    pass

            class MockClientInstance:
                def stream(self, method, url, json=None, headers=None):
                    attempts.append(json["project"])
                    response = MagicMock(spec=httpx.Response)
                    response.status_code = 200
                    response.aiter_text = MagicMock(return_value=AsyncAiterText([
                        'data: {"candidates": [{"content": {"parts": [{"text": "partial"}]}}]}\n',
                        'data: {"error": {"code": "RESOURCE_EXHAUSTED", "message": "quota exhausted"}}\n',
                        "data: [DONE]\n",
                    ]))
                    return StreamContext(response)

                async def __aenter__(self):
                    return self

                async def __aexit__(self, exc_type, exc_val, exc_tb):
                    pass

            class CleanAsyncClientMock:
                def __init__(self, *args, **kwargs):
                    pass

                async def __aenter__(self):
                    return MockClientInstance()

                async def __aexit__(self, exc_type, exc_val, exc_tb):
                    pass

            with patch(
                "codex_antigravity_auth.server.account_manager.acquire_account",
                return_value=first_account,
            ) as mock_select:
                with patch("codex_antigravity_auth.server.account_manager.record_attempt") as record:
                        with patch("codex_antigravity_auth.server.httpx.AsyncClient", CleanAsyncClientMock):
                            response = test_client.post(
                                "/v1/responses",
                                json={"model": "gemini-3.5-flash-high", "input": "hello", "stream": True},
                            )

            self.assertEqual(response.status_code, 200)
            self.assertEqual(attempts, ["project-first"])
            self.assertEqual(mock_select.call_count, 1)
            record.assert_called_once()
            self.assertEqual(record.call_args.args[2].category, "quota")
            self.assertIn("partial", response.text)
            self.assertIn("response.failed", response.text)

if __name__ == "__main__":
    unittest.main()
