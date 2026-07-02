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
from codex_antigravity_auth.server import app, build_headers, retry_after_seconds_from_response
from codex_antigravity_auth.transform import (
    transform_chat_response,
    transform_request,
    transform_request_to_chat,
    transform_response,
)


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

    def test_request_transforms_drop_malformed_text_and_function_names(self):
        request = {
            "model": "gemini-3.5-flash-high",
            "input": [
                {"type": "message", "role": "system", "content": [{"type": "input_text", "text": ["bad"]}]},
                {"type": "message", "role": "user", "content": [{"type": "input_text", "text": ["bad"]}]},
                {"type": "function_call", "call_id": "call_bad", "name": ["bad"], "arguments": "{}"},
                {"type": "function_call", "call_id": "call_bad_name", "name": "bad name", "arguments": "{}"},
                {"type": "function_call", "call_id": "call_ok", "name": "lookup", "arguments": {"q": "x"}},
                {"type": "function_call_output", "call_id": ["bad"], "output": "result"},
                {"type": "function_call_output", "call_id": "bad call id", "output": "result"},
                {"type": "function_call_output", "call_id": "call_ok", "output": "ok"},
            ],
        }

        google = transform_request(request)
        google_parts = [part for content in google["request"]["contents"] for part in content["parts"]]

        self.assertNotIn({"text": ["bad"]}, google_parts)
        self.assertNotIn({"functionCall": {"name": ["bad"], "args": {}}}, google_parts)
        self.assertIn({"functionCall": {"name": "lookup", "args": {"q": "x"}}}, google_parts)

        byok = transform_request_to_chat({**request, "model": "deepseek:deepseek-chat"}, "deepseek-chat")

        rendered = json.dumps(byok)
        self.assertNotIn('["bad"]', rendered)
        self.assertNotIn("bad name", rendered)
        self.assertNotIn("bad call id", rendered)
        self.assertEqual(byok["messages"][0]["role"], "assistant")
        self.assertEqual(byok["messages"][0]["tool_calls"][0]["function"]["name"], "lookup")
        self.assertEqual(byok["messages"][1]["role"], "tool")
        self.assertEqual(byok["messages"][1]["tool_call_id"], "call_ok")

    def test_request_transforms_normalize_malformed_tool_metadata(self):
        request = {
            "model": "gemini-3.5-flash-high",
            "input": "hi",
            "tools": [
                {
                    "type": "function",
                    "name": "lookup",
                    "description": ["bad"],
                    "parameters": ["bad"],
                    "strict": "yes",
                },
                {
                    "type": "function",
                    "name": "bad name",
                    "description": "bad name should be dropped",
                    "parameters": {"type": "object"},
                },
                {
                    "type": "function",
                    "name": "x" * 65,
                    "description": "too long should be dropped",
                    "parameters": {"type": "object"},
                },
                {
                    "type": "function",
                    "function": {
                        "name": "nested_lookup",
                        "description": {"bad": True},
                        "parameters": ["bad"],
                        "strict": "yes",
                    },
                },
                {
                    "type": "function",
                    "function": {
                        "name": "bad\nnested",
                        "description": "control char should be dropped",
                        "parameters": {"type": "object"},
                    },
                },
            ],
        }

        google = transform_request(request)
        declarations = google["request"]["tools"][0]["functionDeclarations"]
        self.assertEqual(
            declarations,
            [
                {"name": "lookup", "description": "", "parameters": {}},
                {"name": "nested_lookup", "description": "", "parameters": {}},
            ],
        )

        byok = transform_request_to_chat({**request, "model": "deepseek:deepseek-chat"}, "deepseek-chat")
        chat_functions = [tool["function"] for tool in byok["tools"]]
        self.assertEqual(chat_functions, [{"name": "lookup", "parameters": {}}, {"name": "nested_lookup", "parameters": {}}])

    def test_response_transforms_drop_invalid_function_names(self):
        google = transform_response(
            {
                "candidates": [
                    {
                        "content": {
                            "role": "model",
                            "parts": [
                                {"functionCall": {"id": "call_bad", "name": "bad name", "args": {}}},
                                {"functionCall": {"id": "call_ok", "name": "lookup", "args": {"q": "x"}}},
                            ],
                        }
                    }
                ]
            },
            "gemini-3.5-flash-high",
        )
        google_calls = [item for item in google["output"] if item["type"] == "function_call"]
        self.assertEqual([call["name"] for call in google_calls], ["lookup"])

        byok = transform_chat_response(
            {
                "choices": [
                    {
                        "message": {
                            "tool_calls": [
                                {"id": "call_bad", "type": "function", "function": {"name": "bad name", "arguments": "{}"}},
                                {"id": "call_ok", "type": "function", "function": {"name": "lookup", "arguments": "{}"}},
                            ]
                        }
                    }
                ]
            },
            "deepseek:deepseek-chat",
        )
        byok_calls = [item for item in byok["output"] if item["type"] == "function_call"]
        self.assertEqual([call["name"] for call in byok_calls], ["lookup"])

    def test_google_request_transform_treats_developer_messages_as_system_instruction(self):
        transformed = transform_request(
            {
                "model": "gemini-3.5-flash-high",
                "input": [
                    {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": "Use terse output."}]},
                    {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Hi"}]},
                ],
            }
        )

        request = transformed["request"]
        system_text = request["systemInstruction"]["parts"][0]["text"]
        self.assertIn("Use terse output.", system_text)
        self.assertEqual(request["contents"], [{"role": "user", "parts": [{"text": "Hi"}]}])

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

        for retry_after in ("NaN", "Infinity", "1e999"):
            with self.subTest(retry_after=retry_after):
                self.assertIsNone(retry_after_seconds_from_response(httpx.Response(429, headers={"Retry-After": retry_after})))

        mixed_retry_info = httpx.Response(
            429,
            json={
                "error": {
                    "details": [
                        {"retryDelay": {"seconds": "Infinity", "nanos": 0}},
                        {"retryDelay": {"seconds": 4, "nanos": 500000000}},
                    ]
                }
            },
        )
        self.assertEqual(retry_after_seconds_from_response(mixed_retry_info), 4.5)

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

    def test_non_streaming_google_response_skips_malformed_backend_shapes(self):
        response = transform_response(
            {
                "usageMetadata": {"promptTokenCount": ["bad"], "candidatesTokenCount": "5", "totalTokenCount": -1},
                "candidates": [
                    "bad",
                    {"content": "bad"},
                    {"content": {"parts": "bad"}},
                    {
                        "content": {
                            "role": 123,
                            "parts": [
                                {"thought": True, "text": ["bad"]},
                                {"type": "thinking", "thinking": "reason"},
                                {"text": ["bad"]},
                                {"functionCall": "bad"},
                                {"functionCall": {"id": {"bad": "id"}, "name": ["bad"], "args": {"ignored": True}}},
                                {"functionCall": {"id": "call_123", "name": "lookup", "args": ["not", "object"]}},
                                {"text": "ok"},
                            ],
                        }
                    },
                ],
            },
            "gemini-3.5-flash-high",
        )

        output = response["output"]

        self.assertEqual(output[0]["type"], "reasoning")
        self.assertEqual(output[0]["step_by_step_summary"], "reason")
        self.assertEqual(output[1]["content"][0]["text"], "ok")
        self.assertEqual(output[2]["type"], "function_call")
        self.assertEqual(output[2]["call_id"], "call_123")
        self.assertEqual(output[2]["name"], "lookup")
        self.assertEqual(output[2]["arguments"], "{}")
        self.assertEqual(response["usage"]["input_tokens"], 0)
        self.assertEqual(response["usage"]["output_tokens"], 5)
        self.assertEqual(response["usage"]["total_tokens"], 0)

    def test_non_streaming_byok_response_skips_malformed_provider_shapes(self):
        response = transform_chat_response(
            {
                "created": "NaN",
                "usage": {"prompt_tokens": ["bad"], "completion_tokens": "5", "total_tokens": -1},
                "choices": [
                    "bad",
                    {"message": "bad"},
                    {
                        "message": {
                            "reasoning_content": ["bad"],
                            "content": ["bad"],
                            "tool_calls": "bad",
                        }
                    },
                    {
                        "message": {
                            "reasoning_content": "reason",
                            "content": [{"text": "ok"}, {"text": ["bad"]}],
                            "tool_calls": [
                                "bad",
                                {"id": {"bad": "id"}, "function": "bad"},
                                {"id": "call_bad", "function": {"name": ["bad"], "arguments": "{}"}},
                                {"id": "call_123", "function": {"name": "lookup", "arguments": {"q": "x"}}},
                            ],
                        }
                    },
                ],
            },
            "deepseek:deepseek-chat",
        )

        output = response["output"]

        self.assertEqual(output[0]["type"], "reasoning")
        self.assertEqual(output[0]["step_by_step_summary"], "reason")
        self.assertEqual(output[1]["content"][0]["text"], "ok")
        self.assertEqual(output[2]["type"], "function_call")
        self.assertEqual(output[2]["call_id"], "call_123")
        self.assertEqual(output[2]["name"], "lookup")
        self.assertEqual(json.loads(output[2]["arguments"]), {"q": "x"})
        self.assertEqual(response["usage"]["input_tokens"], 0)
        self.assertEqual(response["usage"]["output_tokens"], 5)
        self.assertEqual(response["usage"]["total_tokens"], 0)

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

    @patch("codex_antigravity_auth.accounts.update_accounts")
    @patch("codex_antigravity_auth.accounts.refresh_access_token")
    @patch("codex_antigravity_auth.accounts.time.time", return_value=1000)
    def test_non_finite_account_state_and_expiry_are_normalized(self, mock_time, mock_refresh, mock_update):
        data = {
            "accounts": [
                {
                    "email": "primary@gmail.com",
                    "refreshToken": "refresh_1",
                    "accessToken": "old",
                    "expiresAt": float("nan"),
                }
            ],
            "activeIndex": 0,
            "activeIndexByFamily": {"claude": 0, "gemini": 0},
            "accountState": {
                "failures": {
                    "primary@gmail.com": float("nan"),
                    "other@gmail.com": 2,
                    "bool@gmail.com": True,
                    "zero@gmail.com": 0,
                    "negative@gmail.com": -3,
                },
                "cooldowns": {
                    "primary@gmail.com": float("inf"),
                    "other@gmail.com": 2000,
                    "bool@gmail.com": False,
                    "zero@gmail.com": 0,
                    "negative@gmail.com": -3,
                },
            },
        }
        mock_update.side_effect = lambda mutator: mutator(data)
        mock_refresh.return_value = {"access_token": "new", "expires_in": 1800}

        manager = AccountManager()
        selected = manager.select_active_account("gemini-3.5-flash-high")

        self.assertEqual(selected["accessToken"], "new")
        self.assertEqual(selected["expiresAt"], 2800)
        self.assertEqual(manager._failures, {"other@gmail.com": 2})
        self.assertEqual(manager._cooldowns, {"other@gmail.com": 2000.0})

    def test_token_expires_in_seconds_falls_back_for_malformed_success_payloads(self):
        for payload in (
            {},
            {"expires_in": None},
            {"expires_in": "not-a-number"},
            {"expires_in": "NaN"},
            {"expires_in": "Infinity"},
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

        manager._failures["malformed@gmail.com"] = -5
        manager.mark_failure("malformed@gmail.com", "429", retry_after_seconds=True)

        self.assertEqual(manager._failures["malformed@gmail.com"], 1)
        self.assertEqual(manager._cooldowns["malformed@gmail.com"], 1120)

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

    def test_responses_endpoint_rejects_non_object_json_before_routing(self):
        client = TestClient(app)
        for body in ([], "hello", None):
            with self.subTest(body=body):
                response = client.post(
                    "/v1/responses",
                    content=json.dumps(body),
                    headers={"Content-Type": "application/json"},
                )

                self.assertEqual(response.status_code, 400)
                self.assertIn("JSON body must be an object", response.json()["detail"])

    def test_responses_endpoint_rejects_malformed_instructions_before_routing(self):
        client = TestClient(app)
        with (
            patch("codex_antigravity_auth.server.all_provider_configs") as mock_providers,
            patch("codex_antigravity_auth.server.account_manager.select_active_account") as mock_select_account,
        ):
            for model, instructions in (
                ("gemini-3.5-flash-high", {"leaked": "system prompt"}),
                ("deepseek:deepseek-chat", ["leaked system prompt"]),
            ):
                with self.subTest(model=model, instructions=instructions):
                    response = client.post(
                        "/v1/responses",
                        json={"model": model, "input": "hello", "instructions": instructions},
                    )

                    self.assertEqual(response.status_code, 400)
                    self.assertIn("instructions must be a string", response.json()["detail"])

        mock_providers.assert_not_called()
        mock_select_account.assert_not_called()

    def test_transform_helpers_do_not_stringify_malformed_instructions(self):
        google_payload = transform_request(
            {
                "model": "gemini-3.5-flash-high",
                "input": "hello",
                "instructions": {"leaked": "system prompt"},
            }
        )
        system_text = google_payload["request"]["systemInstruction"]["parts"][0]["text"]
        self.assertNotIn("leaked", system_text)

        chat_payload = transform_request_to_chat(
            {
                "model": "deepseek:deepseek-chat",
                "input": "hello",
                "instructions": ["leaked system prompt"],
            },
            "deepseek-chat",
        )
        self.assertNotEqual(chat_payload["messages"][0]["role"], "system")

    def test_responses_endpoint_rejects_malformed_stream_and_model_fields(self):
        client = TestClient(app)

        stream_response = client.post(
            "/v1/responses",
            json={"model": "gemini-3.5-flash-high", "input": "hello", "stream": "false"},
        )
        self.assertEqual(stream_response.status_code, 400)
        self.assertIn("stream must be a boolean", stream_response.json()["detail"])

        model_response = client.post(
            "/v1/responses",
            json={"model": "gemini bad", "input": "hello"},
        )
        self.assertEqual(model_response.status_code, 400)
        self.assertIn("model must not contain whitespace", model_response.json()["detail"])

        reasoning_response = client.post(
            "/v1/responses",
            json={"model": "gemini-3.5-flash-high", "input": "hello", "reasoning": "high"},
        )
        self.assertEqual(reasoning_response.status_code, 400)
        self.assertIn("reasoning must be an object", reasoning_response.json()["detail"])

    def test_responses_endpoint_rejects_malformed_generation_options_before_routing(self):
        client = TestClient(app)
        invalid_requests = [
            ({"temperature": "hot"}, "temperature must be a finite number"),
            ({"temperature": 3}, "temperature must be between 0 and 2"),
            ({"top_p": 2}, "top_p must be between 0 and 1"),
            ({"max_output_tokens": True}, "max_output_tokens must be a positive integer"),
            ({"max_output_tokens": 0}, "max_output_tokens must be a positive integer"),
            ({"stop": []}, "stop must be a string or a non-empty list of strings"),
            ({"stop": ["ok", "bad\nstop"]}, "stop values must be non-empty strings without control characters"),
        ]

        for extra, expected_detail in invalid_requests:
            with self.subTest(extra=extra):
                response = client.post(
                    "/v1/responses",
                    json={"model": "gemini-3.5-flash-high", "input": "hello", **extra},
                )
                self.assertEqual(response.status_code, 400)
                self.assertIn(expected_detail, response.json()["detail"])

        raw_invalid_requests = [
            ('{"model":"gemini-3.5-flash-high","input":"hello","temperature":NaN}', "temperature must be a finite number"),
            ('{"model":"gemini-3.5-flash-high","input":"hello","top_p":Infinity}', "top_p must be a finite number"),
        ]
        for body, expected_detail in raw_invalid_requests:
            with self.subTest(body=body):
                response = client.post(
                    "/v1/responses",
                    content=body,
                    headers={"Content-Type": "application/json"},
                )
                self.assertEqual(response.status_code, 400)
                self.assertIn(expected_detail, response.json()["detail"])

    def test_responses_endpoint_rejects_malformed_tool_choice_before_routing(self):
        client = TestClient(app)
        invalid_requests = [
            ({"tool_choice": "sometimes"}, "tool_choice must be auto, none, required, or a function choice object"),
            ({"tool_choice": ["bad"]}, "tool_choice must be auto, none, required, or a function choice object"),
            ({"tool_choice": {"type": "function", "name": ["bad"]}}, "tool_choice function name must contain only letters"),
            ({"tool_choice": {"type": "function", "function": {"name": "bad\nname"}}}, "tool_choice function name must contain only letters"),
            ({"tool_choice": {"type": "function", "function": {"name": "bad name"}}}, "tool_choice function name must contain only letters"),
            ({"tool_choice": {"type": "function", "function": {"name": "x" * 65}}}, "tool_choice function name must contain only letters"),
        ]

        for extra, expected_detail in invalid_requests:
            with self.subTest(extra=extra):
                response = client.post(
                    "/v1/responses",
                    json={"model": "gemini-3.5-flash-high", "input": "hello", **extra},
                )
                self.assertEqual(response.status_code, 400)
                self.assertIn(expected_detail, response.json()["detail"])

    def test_responses_endpoint_rejects_empty_provider_model_before_backend_routing(self):
        provider = {
            "id": "deepseek",
            "displayName": "DeepSeek",
            "kind": "openai_chat",
            "baseUrl": "https://api.deepseek.com",
            "apiKey": "secret",
            "models": ["deepseek-chat"],
        }
        with patch("codex_antigravity_auth.server.all_provider_configs", return_value={"deepseek": provider}):
            response = TestClient(app).post(
                "/v1/responses",
                json={"model": "deepseek:", "input": "hello"},
            )

        self.assertEqual(response.status_code, 400)
        self.assertIn("model id must be non-empty", response.json()["detail"])

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

    def test_google_headers_ignore_malformed_account_fingerprint_container(self):
        for fingerprint in (["bad"], "bad", 123):
            with self.subTest(fingerprint=fingerprint):
                headers = build_headers({"accessToken": "tok", "fingerprint": fingerprint})

                self.assertEqual(headers["Authorization"], "Bearer tok")
                self.assertIsInstance(headers["User-Agent"], str)
                self.assertIsInstance(headers["X-Goog-Api-Client"], str)
                self.assertEqual(json.loads(headers["Client-Metadata"])["ideType"], "ANTIGRAVITY")

    def test_google_headers_filter_malformed_account_fingerprint_values(self):
        headers = build_headers(
            {
                "accessToken": "tok",
                "fingerprint": {
                    "userAgent": ["not", "a", "string"],
                    "apiClient": "google-cloud-sdk\nbad",
                    "deviceId": "device-1",
                    "sessionToken": {"bad": "token"},
                    "clientMetadata": {
                        "ideType": "ANTIGRAVITY",
                        "nonAscii": "caf\u00e9",
                        "bad\nkey": "ignored",
                        "badValue": "ignored\nvalue",
                        "nested": {"ignored": True},
                        "floatNaN": float("nan"),
                        "count": 1,
                        "enabled": True,
                    },
                },
            }
        )

        metadata = json.loads(headers["Client-Metadata"])

        self.assertIsInstance(headers["User-Agent"], str)
        self.assertIn("Antigravity/2.0.0", headers["User-Agent"])
        self.assertEqual(headers["X-Goog-Api-Client"], "google-cloud-sdk vscode_cloudshelleditor/0.1")
        self.assertEqual(metadata["ideType"], "ANTIGRAVITY")
        self.assertEqual(metadata["deviceId"], "device-1")
        self.assertEqual(metadata["count"], 1)
        self.assertIs(metadata["enabled"], True)
        self.assertNotIn("bad\nkey", metadata)
        self.assertNotIn("badValue", metadata)
        self.assertNotIn("nonAscii", metadata)
        self.assertNotIn("nested", metadata)
        self.assertNotIn("floatNaN", metadata)
        self.assertNotIn("sessionToken", metadata)

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

    def test_google_streaming_skips_malformed_chunks_and_clamps_function_args(self):
        fake_account = {
            "email": "test@gmail.com",
            "accessToken": "dummy_access",
            "fingerprint": {"userAgent": "Antigravity/2.0.0", "apiClient": "google-cloud-sdk"},
        }

        chunks = [
            'data: {"usageMetadata": "bad", "candidates": "bad"}\n',
            'data: {"usageMetadata": {"promptTokenCount": ["bad"], "candidatesTokenCount": "5", "totalTokenCount": -1}, "candidates": []}\n',
            'data: {"candidates": ["bad", {"content": {"parts": ["bad", {"thought": true, "text": ["bad"]}, {"text": ["bad"]}, {"functionCall": {"id": {"bad": "id"}, "name": ["bad"], "args": {"ignored": true}}}, {"functionCall": {"id": "call_a", "name": "a", "args": ["not", "object"]}}, {"text": "ok"}]}}]}\n',
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

        self.assertNotIn("connection_error", response.text)
        arg_done = [e for e in events if e.get("type") == "response.function_call_arguments.done"]
        deltas = [e["delta"] for e in events if e.get("type") == "response.output_text.delta"]
        completed = [e for e in events if e.get("type") == "response.completed"]

        self.assertEqual([e["arguments"] for e in arg_done], ["{}"])
        self.assertEqual("".join(deltas), "ok")
        self.assertTrue(completed)
        self.assertEqual(completed[0]["response"]["usage"], {"input_tokens": 0, "output_tokens": 5, "total_tokens": 0})


if __name__ == "__main__":
    unittest.main()
