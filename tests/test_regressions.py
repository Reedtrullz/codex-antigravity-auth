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
from codex_antigravity_auth.oauth import _pkce_verifier_store, get_pkce_verifier
from codex_antigravity_auth.schema import clean_json_schema
from codex_antigravity_auth.server import app
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

    @patch("codex_antigravity_auth.accounts.save_accounts")
    @patch("codex_antigravity_auth.accounts.load_accounts")
    @patch("codex_antigravity_auth.accounts.refresh_access_token")
    def test_millisecond_expiry_is_normalized_and_refreshed(self, mock_refresh, mock_load, mock_save):
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
        mock_load.return_value = data
        mock_refresh.return_value = {"access_token": "new", "expires_in": 3600}

        selected = AccountManager().select_active_account("gemini-3.5-flash-high")

        self.assertEqual(selected["accessToken"], "new")
        mock_refresh.assert_called_once_with("refresh_1")
        self.assertLess(selected["expiresAt"], 10_000_000_000)

    @patch("codex_antigravity_auth.accounts.save_accounts")
    @patch("codex_antigravity_auth.accounts.load_accounts")
    def test_expired_account_without_refresh_token_is_skipped(self, mock_load, mock_save):
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
        mock_load.return_value = data

        selected = AccountManager().select_active_account("gemini-3.5-flash-high")

        self.assertEqual(selected["email"], "healthy@gmail.com")

    def test_pkce_verifier_expires(self):
        _pkce_verifier_store["expired_state"] = {
            "verifier": "secret",
            "createdAt": str(time.time() - 601),
        }

        self.assertIsNone(get_pkce_verifier("expired_state"))

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
        done = [e for e in events if e.get("type") == "response.output_item.done" and e["item"]["type"] == "function_call"]

        self.assertEqual([e["output_index"] for e in added], [1, 2])
        self.assertEqual([e["output_index"] for e in done], [1, 2])
        self.assertEqual([e["item"]["id"] for e in added], [e["item"]["id"] for e in done])


if __name__ == "__main__":
    unittest.main()
