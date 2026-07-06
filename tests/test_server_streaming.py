import json
import unittest
import httpx
from unittest.mock import patch, MagicMock
from codex_antigravity_auth.server import app, google_rotation_diagnostics, stream_error_from_payload
from fastapi.testclient import TestClient

class TestServerStreaming(unittest.TestCase):
    def test_google_rotation_diagnostics_normalizes_millisecond_cooldowns(self):
        data = {
            "accounts": [{"email": "test@example.com"}],
            "accountState": {"cooldowns": {"test@example.com": 1_700_000_000_000}},
        }
        with patch("codex_antigravity_auth.server.load_accounts", return_value=data):
            with patch("codex_antigravity_auth.server.time.time", return_value=1_700_000_001):
                diagnostics = google_rotation_diagnostics("claude-3.5-sonnet")

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
            with patch("codex_antigravity_auth.server.account_manager.mark_failure") as mock_mark_failure:
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
        mock_mark_failure.assert_called_once()

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
                raise RuntimeError("backend down")

        with patch("codex_antigravity_auth.server.account_manager.acquire_account", return_value=fake_account):
            with patch("codex_antigravity_auth.server.account_manager.release_account") as release:
                with patch("codex_antigravity_auth.server.account_manager.mark_failure"):
                    with patch("codex_antigravity_auth.server.account_manager.record_request"):
                        with patch("codex_antigravity_auth.server.httpx.AsyncClient", MockClient):
                            response = TestClient(app).post(
                                "/v1/responses",
                                json={"model": "gemini-3.5-flash-high", "input": "hello"},
                            )

        self.assertEqual(response.status_code, 502)
        self.assertGreaterEqual(release.call_count, 1)
        self.assertEqual(release.call_args_list[-1].args[0], "test@gmail.com")

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
                with patch("codex_antigravity_auth.server.account_manager.mark_failure") as mock_mark_failure:
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
            mock_mark_failure.assert_called_once()
            self.assertEqual(mock_mark_failure.call_args.args[0], "test@gmail.com")
            self.assertIn("rate_limit_exceeded", mock_mark_failure.call_args.args[1])

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
                with patch("codex_antigravity_auth.server.account_manager.mark_failure") as mock_mark_failure:
                    with patch("codex_antigravity_auth.server.httpx.AsyncClient", CleanAsyncClientMock):
                        response = test_client.post(
                            "/v1/responses",
                            json={"model": "gemini-3.5-flash-high", "input": "hello", "stream": True},
                        )

            self.assertEqual(response.status_code, 200)
            self.assertIn("rotated ok", response.text)
            self.assertNotIn("response.failed", response.text)
            self.assertEqual([request["json"]["project"] for request in requests], ["project-first", "project-second"])
            mock_mark_failure.assert_called_once()
            self.assertEqual(mock_mark_failure.call_args.args[0], "first@gmail.com")

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
            self.assertNotIn("usage", completed[0]["response"])

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
                with patch("codex_antigravity_auth.server.account_manager.mark_failure") as mock_mark_failure:
                    with patch("codex_antigravity_auth.server.account_manager.record_request"):
                        with patch("codex_antigravity_auth.server.httpx.AsyncClient", CleanAsyncClientMock):
                            response = test_client.post(
                                "/v1/responses",
                                json={"model": "gemini-3.5-flash-high", "input": "hello", "stream": True},
                            )

            self.assertEqual(response.status_code, 200)
            self.assertEqual(attempts, ["project-first"])
            self.assertEqual(mock_select.call_count, 1)
            mock_mark_failure.assert_called_once()
            self.assertIn("partial", response.text)
            self.assertIn("response.failed", response.text)

if __name__ == "__main__":
    unittest.main()
