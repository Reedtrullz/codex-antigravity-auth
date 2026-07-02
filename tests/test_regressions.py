import json
import time
import unittest
from unittest.mock import MagicMock, patch
import urllib.error

import httpx
from fastapi.testclient import TestClient

from codex_antigravity_auth.accounts import AccountManager
from codex_antigravity_auth.cli import run_doctor
from codex_antigravity_auth.models import resolve_backend_model
from codex_antigravity_auth.oauth import (
    OAUTH_HTTP_TIMEOUT_SECONDS,
    _pkce_verifier_store,
    exchange_antigravity,
    get_pkce_verifier,
    refresh_access_token,
    token_expires_in_seconds,
)
from codex_antigravity_auth.schema import clean_json_schema
from codex_antigravity_auth.server import app, retry_after_seconds_from_response
from codex_antigravity_auth.transform import transform_request, transform_response


class TestRegressionFixes(unittest.TestCase):
    def test_hyphenated_codex_model_slug_resolves(self):
        self.assertEqual(resolve_backend_model("gemini-3-5-flash-high"), "gemini-3-flash-agent")
        self.assertEqual(resolve_backend_model("openai-responses/gemini-3-5-flash-high"), "gemini-3-flash-agent")

    def test_responses_tool_loop_input_round_trips_to_function_response(self):
        req = {
            "model": "gemini-3.5-flash-high",
            "input": [
                {
                    "type": "function_call",
                    "call_id": "call_123",
                    "name": "lookup",
                    "arguments": "{\"query\":\"answer\"}",
                },
                {
                    "type": "function_call_output",
                    "call_id": "call_123",
                    "output": "42",
                },
            ],
        }

        contents = transform_request(req)["request"]["contents"]

        self.assertEqual(contents[0]["role"], "model")
        self.assertEqual(contents[0]["parts"][0]["functionCall"]["name"], "lookup")
        self.assertEqual(contents[0]["parts"][0]["functionCall"]["args"], {"query": "answer"})
        self.assertEqual(contents[1]["role"], "user")
        self.assertEqual(contents[1]["parts"][0]["functionResponse"]["name"], "lookup")
        self.assertEqual(contents[1]["parts"][0]["functionResponse"]["response"]["content"], "42")

    def test_non_object_function_call_arguments_are_clamped_for_google(self):
        for arguments in ('["not", "object"]', '"string"', "42", "null", "true"):
            with self.subTest(arguments=arguments):
                req = {
                    "model": "gemini-3.5-flash-high",
                    "input": [
                        {
                            "type": "function_call",
                            "call_id": "call_123",
                            "name": "lookup",
                            "arguments": arguments,
                        },
                    ],
                }

                parts = transform_request(req)["request"]["contents"][0]["parts"]

                self.assertEqual(parts[0]["functionCall"]["args"], {})

    def test_json_object_tool_output_preserves_structured_google_response(self):
        req = {
            "model": "gemini-3.5-flash-high",
            "input": [
                {"type": "function_call", "call_id": "call_123", "name": "lookup", "arguments": "{}"},
                {"type": "function_call_output", "call_id": "call_123", "output": '{"ok": true, "items": [1, 2]}'},
            ],
        }

        response = transform_request(req)["request"]["contents"][1]["parts"][0]["functionResponse"]["response"]

        self.assertEqual(response, {"ok": True, "items": [1, 2]})

    def test_transform_request_honors_selected_account_project(self):
        req = {
            "model": "gemini-3.5-flash-high",
            "input": "hello",
        }

        transformed = transform_request(req, project_id="account-project-123")

        self.assertEqual(transformed["project"], "account-project-123")

    def test_rotated_google_account_rebuilds_request_with_rotated_project(self):
        first_account = {
            "email": "first@gmail.com",
            "accessToken": "first-access",
            "projectId": "project-first",
        }
        second_account = {
            "email": "second@gmail.com",
            "accessToken": "second-access",
            "projectId": "project-second",
        }
        requests = []

        class MockClient:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc_val, exc_tb):
                pass

            async def post(self, url, json=None, headers=None):
                requests.append({"json": json, "headers": headers})
                if len(requests) == 1:
                    return httpx.Response(429, json={"error": {"message": "rate limited"}})
                return httpx.Response(
                    200,
                    json={
                        "candidates": [
                            {
                                "content": {
                                    "role": "model",
                                    "parts": [{"text": "ok"}],
                                }
                            }
                        ],
                        "usageMetadata": {},
                    },
                )

        with patch(
            "codex_antigravity_auth.server.account_manager.select_active_account",
            side_effect=[first_account, second_account],
        ):
            with patch("codex_antigravity_auth.server.account_manager.mark_failure"):
                with patch("codex_antigravity_auth.server.httpx.AsyncClient", MockClient):
                    response = TestClient(app).post(
                        "/v1/responses",
                        json={"model": "gemini-3.5-flash-high", "input": "hello"},
                    )

        self.assertEqual(response.status_code, 200)
        self.assertEqual([request["json"]["project"] for request in requests], ["project-first", "project-second"])
        self.assertEqual(
            [request["headers"]["Authorization"] for request in requests],
            ["Bearer first-access", "Bearer second-access"],
        )

    def test_transform_request_can_use_project_environment_override(self):
        req = {
            "model": "gemini-3.5-flash-high",
            "input": "hello",
        }

        with patch.dict("os.environ", {"ANTIGRAVITY_PROJECT_ID": "env-project-123"}):
            transformed = transform_request(req)

        self.assertEqual(transformed["project"], "env-project-123")

    def test_forced_tool_choice_maps_to_google_tool_config(self):
        transformed = transform_request(
            {
                "model": "gemini-3.5-flash-high",
                "input": "Use the tool.",
                "tools": [{"type": "function", "name": "lookup", "parameters": {"type": "object", "properties": {}}}],
                "tool_choice": {"type": "function", "name": "lookup"},
            }
        )

        config = transformed["request"]["toolConfig"]["functionCallingConfig"]
        self.assertEqual(config["mode"], "ANY")
        self.assertEqual(config["allowedFunctionNames"], ["lookup"])

    def test_retry_after_seconds_supports_headers_and_google_retry_info(self):
        header_response = httpx.Response(429, headers={"Retry-After": "17"})
        self.assertEqual(retry_after_seconds_from_response(header_response), 17)

        retry_info_response = httpx.Response(
            429,
            json={
                "error": {
                    "details": [
                        {
                            "@type": "type.googleapis.com/google.rpc.RetryInfo",
                            "retryDelay": "3.5s",
                        }
                    ]
                }
            },
        )
        self.assertEqual(retry_after_seconds_from_response(retry_info_response), 3.5)

    def test_backend_function_call_is_top_level_response_output(self):
        gemini_resp = {
            "candidates": [
                {
                    "content": {
                        "role": "model",
                        "parts": [
                            {
                                "functionCall": {
                                    "id": "call_123",
                                    "name": "lookup",
                                    "args": {"query": "answer"},
                                }
                            }
                        ],
                    }
                }
            ]
        }

        output = transform_response(gemini_resp, "gemini-3.5-flash-high")["output"]

        self.assertEqual(len(output), 1)
        self.assertEqual(output[0]["type"], "function_call")
        self.assertEqual(output[0]["call_id"], "call_123")
        self.assertEqual(json.loads(output[0]["arguments"]), {"query": "answer"})

    def test_schema_refs_are_resolved_without_nested_placeholder_injection(self):
        raw_schema = {
            "type": "object",
            "$defs": {
                "payload": {
                    "type": "object",
                    "properties": {"name": {"type": "string", "minLength": 1}},
                }
            },
            "properties": {"payload": {"$ref": "#/$defs/payload"}},
            "required": ["payload"],
        }

        cleaned = clean_json_schema(raw_schema)

        payload = cleaned["properties"]["payload"]
        self.assertEqual(payload["type"], "object")
        self.assertNotIn("minLength", payload["properties"]["name"])
        self.assertNotIn("_placeholder", payload["properties"])

    @patch("codex_antigravity_auth.accounts.update_accounts")
    @patch("codex_antigravity_auth.accounts.refresh_access_token")
    def test_millisecond_expiry_is_normalized_and_refreshed(self, mock_refresh, mock_update):
        data = {
            "accounts": [
                {
                    "email": "primary@gmail.com",
                    "refreshToken": "refresh_1",
                    "accessToken": "old",
                    "expiresAt": int((time.time() - 30) * 1000),
                }
            ],
            "activeIndex": 0,
            "activeIndexByFamily": {"claude": 0, "gemini": 0},
        }
        mock_update.side_effect = lambda mutator: mutator(data)
        mock_refresh.return_value = {"access_token": "new", "expires_in": 3600}

        selected = AccountManager().select_active_account("gemini-3.5-flash-high")

        self.assertEqual(selected["accessToken"], "new")
        mock_refresh.assert_called_once_with("refresh_1")
        self.assertLess(selected["expiresAt"], 10_000_000_000)

    def test_token_expires_in_seconds_falls_back_for_malformed_success_payloads(self):
        for payload in (
            {},
            {"expires_in": None},
            {"expires_in": "not-a-number"},
            {"expires_in": -1},
            {"expires_in": 0},
        ):
            with self.subTest(payload=payload):
                self.assertEqual(token_expires_in_seconds(payload), 3600)
        self.assertEqual(token_expires_in_seconds({"expires_in": "1800"}), 1800)

    @patch("codex_antigravity_auth.accounts.update_accounts")
    @patch("codex_antigravity_auth.accounts.refresh_access_token")
    @patch("codex_antigravity_auth.accounts.time.time", return_value=1000)
    def test_malformed_refresh_expires_in_uses_default_lifetime(self, mock_time, mock_refresh, mock_update):
        data = {
            "accounts": [
                {
                    "email": "primary@gmail.com",
                    "refreshToken": "refresh_1",
                    "accessToken": "old",
                    "expiresAt": 900,
                }
            ],
            "activeIndex": 0,
            "activeIndexByFamily": {"claude": 0, "gemini": 0},
        }
        mock_update.side_effect = lambda mutator: mutator(data)
        mock_refresh.return_value = {"access_token": "new", "expires_in": "bad-value"}

        selected = AccountManager().select_active_account("gemini-3.5-flash-high")

        self.assertEqual(selected["accessToken"], "new")
        self.assertEqual(selected["expiresAt"], 4600)

    @patch("codex_antigravity_auth.accounts.update_accounts")
    def test_expired_account_without_refresh_token_is_skipped(self, mock_update):
        data = {
            "accounts": [
                {
                    "email": "expired@gmail.com",
                    "accessToken": "old",
                    "expiresAt": time.time() - 30,
                },
                {
                    "email": "healthy@gmail.com",
                    "accessToken": "ok",
                    "expiresAt": time.time() + 3600,
                },
            ],
            "activeIndex": 0,
            "activeIndexByFamily": {"claude": 0, "gemini": 0},
        }
        mock_update.side_effect = lambda mutator: mutator(data)

        selected = AccountManager().select_active_account("gemini-3.5-flash-high")

        self.assertEqual(selected["email"], "healthy@gmail.com")

    @patch("codex_antigravity_auth.accounts.update_accounts")
    @patch("codex_antigravity_auth.accounts.time.time", return_value=1000)
    def test_retry_after_hint_extends_cooldown(self, mock_time, mock_update):
        manager = AccountManager()

        manager.mark_failure("limited@gmail.com", "429", retry_after_seconds=600)

        self.assertEqual(manager._cooldowns["limited@gmail.com"], 1600)

    def test_pkce_verifier_expires(self):
        _pkce_verifier_store["expired_state"] = {
            "verifier": "secret",
            "createdAt": str(time.time() - 601),
        }

        self.assertIsNone(get_pkce_verifier("expired_state"))

    @patch("codex_antigravity_auth.oauth.require_credentials", return_value=("client-id", "client-secret"))
    @patch("urllib.request.urlopen")
    def test_oauth_exchange_and_refresh_use_timeout(self, mock_urlopen, mock_creds):
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"access_token":"access","expires_in":3600}'
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        exchange_antigravity("oauth-code", "verifier")
        refresh_access_token("refresh-token")

        self.assertEqual(mock_urlopen.call_args_list[0].kwargs["timeout"], OAUTH_HTTP_TIMEOUT_SECONDS)
        self.assertEqual(mock_urlopen.call_args_list[1].kwargs["timeout"], OAUTH_HTTP_TIMEOUT_SECONDS)

    def test_previous_response_id_is_rejected_before_backend_routing(self):
        response = TestClient(app).post(
            "/v1/responses",
            json={"model": "gemini-3.5-flash-high", "input": "hello", "previous_response_id": "resp_old"},
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("previous_response_id is not supported", response.json()["detail"])

    @patch("codex_antigravity_auth.cli.resolve_oauth_credentials")
    @patch("codex_antigravity_auth.cli.load_accounts")
    @patch("urllib.request.urlopen")
    def test_doctor_treats_auth_http_error_as_online(self, mock_urlopen, mock_load, mock_creds):
        mock_creds.return_value = ("client_id_val", "client_secret_val")
        mock_load.return_value = {"accounts": []}
        mock_urlopen.side_effect = urllib.error.HTTPError(
            url="https://cloudcode-pa.googleapis.com/v1internal:generateContent",
            code=401,
            msg="Unauthorized",
            hdrs=None,
            fp=None,
        )

        with patch("builtins.print") as mock_print:
            run_doctor()

        printed_text = "\n".join(call[0][0] for call in mock_print.call_args_list if call[0])
        self.assertIn("ONLINE (authentication required)", printed_text)

    def test_streaming_function_calls_use_stable_unique_output_items(self):
        fake_account = {
            "email": "test@gmail.com",
            "accessToken": "dummy_access",
            "fingerprint": {"userAgent": "Antigravity/2.0.0", "apiClient": "google-cloud-sdk"},
        }

        chunks = [
            'data: {"candidates": [{"content": {"parts": [{"functionCall": {"id": "call_a", "name": "a", "args": {"x": 1}}}, {"functionCall": {"id": "call_b", "name": "b", "args": {"y": 2}}}]}}]}\n',
            "data: [DONE]\n",
        ]

        class AsyncAiterText:
            def __init__(self, text_chunks):
                self.chunks = list(text_chunks)

            def __aiter__(self):
                return self

            async def __anext__(self):
                if not self.chunks:
                    raise StopAsyncIteration
                return self.chunks.pop(0)

        mock_response = MagicMock(spec=httpx.Response)
        mock_response.status_code = 200
        mock_response.aiter_text = MagicMock(return_value=AsyncAiterText(chunks))

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

        with patch("codex_antigravity_auth.server.account_manager.select_active_account", return_value=fake_account):
            with patch("codex_antigravity_auth.server.httpx.AsyncClient", CleanAsyncClientMock):
                response = TestClient(app).post(
                    "/v1/responses",
                    json={"model": "gemini-3.5-flash-high", "input": "call tools", "stream": True},
                )

        events = []
        for line in response.text.splitlines():
            if line.startswith("data: ") and line != "data: [DONE]":
                events.append(json.loads(line[6:]))

        added = [e for e in events if e.get("type") == "response.output_item.added" and e["item"]["type"] == "function_call"]
        arg_done = [e for e in events if e.get("type") == "response.function_call_arguments.done"]
        done = [e for e in events if e.get("type") == "response.output_item.done" and e["item"]["type"] == "function_call"]

        self.assertEqual([e["output_index"] for e in added], [1, 2])
        self.assertEqual([e["arguments"] for e in arg_done], ['{"x": 1}', '{"y": 2}'])
        self.assertEqual([e["output_index"] for e in done], [1, 2])
        self.assertEqual([e["item"]["id"] for e in added], [e["item"]["id"] for e in done])


if __name__ == "__main__":
    unittest.main()
