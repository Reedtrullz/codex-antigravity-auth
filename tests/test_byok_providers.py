import json
import unittest
from unittest.mock import MagicMock, patch

import httpx
from fastapi.testclient import TestClient

from codex_antigravity_auth.byok import (
    PROVIDER_PRESETS,
    all_provider_configs,
    normalize_provider_config,
    normalize_provider_entry,
    set_provider_config,
    split_provider_model,
    validate_http_base_url,
    validate_provider_api_key,
    validate_provider_api_key_env,
    validate_provider_display_name,
    validate_provider_id,
    validate_provider_model_id,
)
from codex_antigravity_auth.server import app
from codex_antigravity_auth.transform import transform_chat_response, transform_request, transform_request_to_chat


class TestBYOKProviders(unittest.TestCase):
    def test_named_provider_presets_exist(self):
        for provider_id in ("openrouter", "deepseek", "xai", "kimi", "ollama", "opencode"):
            self.assertIn(provider_id, PROVIDER_PRESETS)
            self.assertEqual(PROVIDER_PRESETS[provider_id]["kind"], "openai_chat")

    def test_provider_model_prefix_parsing_preserves_slashy_models(self):
        self.assertEqual(split_provider_model("deepseek:deepseek-chat"), ("deepseek", "deepseek-chat"))
        self.assertEqual(split_provider_model("openrouter:deepseek/deepseek-chat"), ("openrouter", "deepseek/deepseek-chat"))
        self.assertEqual(split_provider_model("openrouter:openrouter/auto"), ("openrouter", "openrouter/auto"))
        self.assertIn("openrouter/auto", PROVIDER_PRESETS["openrouter"]["models"])

    def test_byok_provider_id_validation_reserves_model_separators(self):
        self.assertEqual(validate_provider_id("my-provider_1"), "my-provider_1")
        for provider_id in ("bad:provider", "bad/provider", "bad.provider", "bad provider", ""):
            with self.subTest(provider_id=provider_id):
                with self.assertRaisesRegex(ValueError, "provider id"):
                    validate_provider_id(provider_id)

    def test_http_base_url_validation_requires_absolute_http_url(self):
        self.assertEqual(validate_http_base_url(" https://api.example.com/v1/ "), "https://api.example.com/v1")
        self.assertEqual(validate_http_base_url("http://[::1]:11434/v1/"), "http://[::1]:11434/v1")
        invalid_cases = [
            ("localhost:8000/v1", "absolute http\\(s\\) URL"),
            ("ftp://example.com/v1", "absolute http\\(s\\) URL"),
            ("", "non-empty absolute http\\(s\\) URL"),
            (123, "non-empty absolute http\\(s\\) URL"),
            ("http://local host:8000/v1", "whitespace or control characters"),
            ("http://localhost:8000/v1\nHeader: x", "whitespace or control characters"),
            ("http://localhost:8000/v1\tbad", "whitespace or control characters"),
            ("http://localhost:8000/v1?x=y", "query strings or fragments"),
            ("http://localhost:8000/v1#frag", "query strings or fragments"),
            ("http://example.com:bad/v1", "valid port"),
            ("http://[::1", "absolute http\\(s\\) URL"),
            ("http://[::1]bad/v1", "absolute http\\(s\\) URL"),
            ("https://user:pass@example.com/v1", "username or password"),
        ]
        for base_url, expected_error in invalid_cases:
            with self.subTest(base_url=base_url):
                with self.assertRaisesRegex(ValueError, expected_error):
                    validate_http_base_url(base_url, label="BYOK provider base URL")

    def test_set_provider_config_rejects_unroutable_provider_id_before_write(self):
        with patch("codex_antigravity_auth.byok.update_secure_json_file") as mock_update:
            with self.assertRaisesRegex(ValueError, "provider id"):
                set_provider_config("bad:provider", models=["model"])

        mock_update.assert_not_called()

    def test_set_provider_config_rejects_invalid_base_url_before_write(self):
        for base_url, expected_error in (
            ("localhost:8000/v1", "absolute http\\(s\\) URL"),
            ("http://localhost:8000/v1?x=y", "query strings or fragments"),
            ("http://local host:8000/v1", "whitespace or control characters"),
        ):
            with self.subTest(base_url=base_url):
                with patch("codex_antigravity_auth.byok.update_secure_json_file") as mock_update:
                    with self.assertRaisesRegex(ValueError, expected_error):
                        set_provider_config("custom-one", base_url=base_url, models=["model"])

                mock_update.assert_not_called()

    def test_set_provider_config_requires_base_url_for_new_custom_providers(self):
        with patch("codex_antigravity_auth.byok.load_provider_config", return_value={"providers": {}}):
            with patch("codex_antigravity_auth.byok.update_secure_json_file") as mock_update:
                with self.assertRaisesRegex(ValueError, "base URL is required"):
                    set_provider_config("custom-one", models=["model"])

        mock_update.assert_not_called()

    def test_set_provider_config_rejects_reserved_or_malformed_headers_before_write(self):
        invalid_headers = [
            ({"Authorization": "Bearer override"}, "must not override"),
            ({"Content-Type": "text/plain"}, "must not override"),
            ({"Bad Header": "value"}, "valid HTTP header names"),
            ({"X-Empty": ""}, "non-empty"),
            ({"X-Bad": "line\nbreak"}, "control characters"),
            ({"X-Bad": "bad\x00value"}, "control characters"),
            ({"X-Bad": "bad\x7fvalue"}, "control characters"),
        ]
        for headers, expected_error in invalid_headers:
            with self.subTest(headers=headers):
                with patch("codex_antigravity_auth.byok.update_secure_json_file") as mock_update:
                    with self.assertRaisesRegex(ValueError, expected_error):
                        set_provider_config("deepseek", headers=headers)

                mock_update.assert_not_called()

    def test_set_provider_config_rejects_malformed_api_key_before_write(self):
        invalid_api_keys = ["secret\nbad", "secret\rbad", "secret\x00bad"]
        for api_key in invalid_api_keys:
            with self.subTest(api_key=repr(api_key)):
                with patch("codex_antigravity_auth.byok.update_secure_json_file") as mock_update:
                    with self.assertRaisesRegex(ValueError, "API key must not contain control characters"):
                        set_provider_config("deepseek", api_key=api_key)

                mock_update.assert_not_called()

    def test_set_provider_config_rejects_malformed_picker_fields_before_write(self):
        invalid_cases = [
            ({"models": ["bad\nmodel"]}, "model ids must not contain whitespace or control characters"),
            ({"models": ["bad model"]}, "model ids must not contain whitespace or control characters"),
            ({"display_name": "Deep\nSeek"}, "display name must not contain control characters"),
            ({"api_key_env": "BAD\nENV"}, "env var name"),
            ({"api_key_env": "1BAD"}, "must not start with a number"),
            ({"api_key_env": "BAD-ENV"}, "env var name"),
        ]
        for kwargs, expected_error in invalid_cases:
            with self.subTest(kwargs=kwargs):
                with patch("codex_antigravity_auth.byok.update_secure_json_file") as mock_update:
                    with self.assertRaisesRegex(ValueError, expected_error):
                        set_provider_config("deepseek", **kwargs)

                mock_update.assert_not_called()

    def test_empty_api_key_clears_stored_key_after_normalization(self):
        captured = {}

        def fake_update_secure_json_file(path, default_factory, mutate, **kwargs):
            data = {"providers": {"deepseek": {"apiKey": "old-secret"}}}
            mutate(data)
            normalized = kwargs["normalize"](data)
            captured.update(normalized["providers"]["deepseek"])

        with patch("codex_antigravity_auth.byok.update_secure_json_file", side_effect=fake_update_secure_json_file):
            provider = set_provider_config("deepseek", api_key="")

        self.assertNotIn("apiKey", captured)
        self.assertNotIn("apiKey", provider)

    def test_set_provider_config_preserves_existing_custom_base_url(self):
        stored = {"providers": {"custom-one": {"baseUrl": "http://localhost:8000/v1", "models": ["old"]}}}
        captured = {}

        def fake_update_secure_json_file(path, default_factory, mutate, **kwargs):
            data = {"providers": {"custom-one": {"baseUrl": "http://localhost:8000/v1", "models": ["old"]}}}
            mutate(data)
            captured.update(data["providers"]["custom-one"])

        with patch("codex_antigravity_auth.byok.load_provider_config", return_value=stored):
            with patch("codex_antigravity_auth.byok.update_secure_json_file", side_effect=fake_update_secure_json_file):
                provider = set_provider_config("custom-one", models=["new"])

        self.assertEqual(captured["baseUrl"], "http://localhost:8000/v1")
        self.assertEqual(captured["models"], ["new"])
        self.assertEqual(provider["baseUrl"], "http://localhost:8000/v1")

    def test_legacy_invalid_provider_ids_are_not_advertised_or_routed(self):
        stored = {
            "providers": {
                "bad:provider": {"kind": "openai_chat", "baseUrl": "http://localhost:8000/v1", "models": ["m"]},
                "bad/provider": {"kind": "openai_chat", "baseUrl": "http://localhost:8001/v1", "models": ["m"]},
                "good-provider": {"kind": "openai_chat", "baseUrl": "http://localhost:8002/v1", "models": ["ok"]},
                "custom-missing-base": {"kind": "openai_chat", "models": ["ghost"]},
                "custom-invalid-base": {"kind": "openai_chat", "baseUrl": "localhost:8000/v1", "models": ["ghost"]},
            }
        }

        normalized = normalize_provider_config(stored)
        self.assertEqual(set(normalized["providers"]), {"good-provider"})

        with patch("codex_antigravity_auth.byok.load_provider_config", return_value=normalized):
            providers = all_provider_configs(include_env_enabled=False)
            self.assertEqual(set(providers), {"good-provider"})
            self.assertEqual(split_provider_model("good-provider:ok"), ("good-provider", "ok"))
            self.assertEqual(split_provider_model("bad:provider:m"), (None, "bad:provider:m"))

            model_ids = [model["id"] for model in TestClient(app).get("/v1/models").json()["data"]]
            self.assertIn("good-provider:ok", model_ids)
            self.assertNotIn("bad:provider:m", model_ids)
            self.assertNotIn("bad/provider:m", model_ids)
            self.assertNotIn("custom-missing-base:ghost", model_ids)
            self.assertNotIn("custom-invalid-base:ghost", model_ids)

    def test_legacy_malformed_provider_fields_are_normalized_before_runtime_use(self):
        stored = {
            "providers": {
                "deepseek": {
                    "kind": " openai_chat ",
                    "displayName": " DeepSeek Custom ",
                    "baseUrl": 123,
                    "models": "deepseek-chat",
                    "headers": ["bad"],
                    "apiKey": "secret",
                    "apiKeyEnvAliases": "DEEPSEEK_ALT_KEY",
                    "timeout": "slow",
                    "apiKeyOptional": "yes",
                },
                "custom-one": {
                    "kind": [],
                    "displayName": 42,
                    "baseUrl": " http://localhost:9999/v1/ ",
                    "models": [
                        "",
                        None,
                        {"id": " custom-model ", "displayName": "Custom Model"},
                        {"id": "bad\nmodel", "displayName": "Bad Model"},
                        {"id": "bad-dict", "displayName": "Bad\nDisplay", "context_window": "huge"},
                        {"id": "good-dict", "displayName": " Good Dict ", "contextWindow": 4096},
                        {"displayName": "missing id"},
                    ],
                    "headers": {
                        "X-Test": 1,
                        " X-Spaced ": " ok ",
                        "Bad Header": "nope",
                        "X-Bad": "line\nbreak",
                        "X-Nul": "bad\x00value",
                        "X-Del": "bad\x7fvalue",
                        "X-Empty": None,
                    },
                    "apiKey": " secret ",
                    "timeout": 0,
                },
                "custom-two": {
                    "kind": "unknown",
                    "displayName": "Bad\nName",
                    "baseUrl": "http://localhost:9998/v1",
                    "models": [{"id": "ok", "display_name": "Ok\nBad", "context_window": 0}],
                    "apiKeyEnv": "BAD-ENV",
                    "apiKeyEnvAliases": ["GOOD_ENV", "BAD\nENV", "2BAD"],
                },
            }
        }

        normalized = normalize_provider_config(stored)

        deepseek = normalized["providers"]["deepseek"]
        self.assertEqual(deepseek["kind"], "openai_chat")
        self.assertEqual(deepseek["displayName"], "DeepSeek Custom")
        self.assertNotIn("baseUrl", deepseek)
        self.assertEqual(deepseek["models"], ["deepseek-chat"])
        self.assertNotIn("headers", deepseek)
        self.assertEqual(deepseek["apiKeyEnvAliases"], ["DEEPSEEK_ALT_KEY"])
        self.assertNotIn("timeout", deepseek)
        self.assertNotIn("apiKeyOptional", deepseek)

        custom = normalized["providers"]["custom-one"]
        self.assertNotIn("kind", custom)
        self.assertNotIn("displayName", custom)
        self.assertEqual(custom["baseUrl"], "http://localhost:9999/v1")
        self.assertEqual(
            custom["models"],
            [
                {"id": "custom-model", "displayName": "Custom Model"},
                {"id": "bad-dict"},
                {"id": "good-dict", "displayName": "Good Dict", "contextWindow": 4096},
            ],
        )
        self.assertEqual(custom["headers"], {"X-Test": "1", "X-Spaced": "ok"})
        self.assertEqual(custom["apiKey"], "secret")
        self.assertNotIn("timeout", custom)

        custom_two = normalized["providers"]["custom-two"]
        self.assertNotIn("kind", custom_two)
        self.assertNotIn("displayName", custom_two)
        self.assertEqual(custom_two["models"], [{"id": "ok"}])
        self.assertNotIn("apiKeyEnv", custom_two)
        self.assertEqual(custom_two["apiKeyEnvAliases"], ["GOOD_ENV"])

        malformed_key = normalize_provider_entry({"apiKey": "secret\nbad"})
        self.assertNotIn("apiKey", malformed_key)

        reserved = normalize_provider_entry(
            {"headers": {"Authorization": "Bearer override", "Content-Type": "text/plain", "Host": "example.com", "X-Ok": "yes"}}
        )
        self.assertEqual(reserved["headers"], {"X-Ok": "yes"})

        with patch("codex_antigravity_auth.byok.load_provider_config", return_value=normalized):
            providers = all_provider_configs(include_env_enabled=False)
            self.assertEqual(providers["deepseek"]["baseUrl"], PROVIDER_PRESETS["deepseek"]["baseUrl"])
            self.assertEqual(providers["deepseek"]["models"], ["deepseek-chat"])
            self.assertEqual(providers["custom-one"]["kind"], "openai_chat")

    def test_single_string_legacy_models_are_not_split_into_characters(self):
        normalized = normalize_provider_entry({"models": "abc"})

        self.assertEqual(normalized["models"], ["abc"])

    def test_validate_provider_api_key_strips_valid_keys_and_rejects_control_characters(self):
        self.assertEqual(validate_provider_api_key(" secret "), "secret")
        self.assertEqual(validate_provider_api_key(""), "")
        self.assertIsNone(validate_provider_api_key(None))
        for api_key in ("secret\nbad", "secret\rbad", "secret\x7fbad"):
            with self.subTest(api_key=repr(api_key)):
                with self.assertRaisesRegex(ValueError, "control characters"):
                    validate_provider_api_key(api_key)

    def test_provider_picker_field_validators_reject_control_characters(self):
        self.assertEqual(validate_provider_model_id(" openrouter/auto "), "openrouter/auto")
        self.assertEqual(validate_provider_model_id("ollama:gpt-oss:20b"), "ollama:gpt-oss:20b")
        self.assertEqual(validate_provider_display_name(" DeepSeek Chat "), "DeepSeek Chat")
        self.assertEqual(validate_provider_api_key_env(" DEEPSEEK_API_KEY "), "DEEPSEEK_API_KEY")
        for model_id in ("bad model", "bad\nmodel", "bad\tmodel"):
            with self.subTest(model_id=repr(model_id)):
                with self.assertRaisesRegex(ValueError, "model ids"):
                    validate_provider_model_id(model_id)
        for display_name in ("Bad\nName", "Bad\x00Name"):
            with self.subTest(display_name=repr(display_name)):
                with self.assertRaisesRegex(ValueError, "display name"):
                    validate_provider_display_name(display_name)
        for env_name in ("BAD-ENV", "1BAD", "BAD ENV", "BAD\nENV"):
            with self.subTest(env_name=repr(env_name)):
                with self.assertRaisesRegex(ValueError, "env var name"):
                    validate_provider_api_key_env(env_name)

    def test_models_endpoint_does_not_advertise_legacy_malformed_picker_fields(self):
        stored = {
            "providers": {
                "deepseek": {
                    "displayName": "Deep\nSeek",
                    "models": ["ok", "bad\nmodel", {"id": "bad dict"}, {"id": "good", "displayName": "Good\nBad"}],
                }
            }
        }

        normalized = normalize_provider_config(stored)

        with patch("codex_antigravity_auth.byok.load_provider_config", return_value=normalized):
            with patch("codex_antigravity_auth.server.all_provider_configs", wraps=all_provider_configs):
                response = TestClient(app).get("/v1/models")

        self.assertEqual(response.status_code, 200)
        byok_models = [model for model in response.json()["data"] if model["owned_by"] == "deepseek"]
        self.assertEqual([model["id"] for model in byok_models], ["deepseek:ok", "deepseek:good"])
        rendered = json.dumps(byok_models)
        self.assertNotIn("\\n", rendered)
        self.assertNotIn("bad", rendered)

    def test_transform_responses_to_chat_completions(self):
        payload = transform_request_to_chat(
            {
                "model": "deepseek:deepseek-chat",
                "instructions": "Be concise.",
                "input": [
                    {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Hi"}]},
                    {"type": "function_call", "call_id": "call_1", "name": "lookup", "arguments": "{\"q\":\"x\"}"},
                    {"type": "function_call_output", "call_id": "call_1", "output": "result"},
                ],
                "tools": [
                    {
                        "type": "function",
                        "function": {
                            "name": "lookup",
                            "parameters": {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]},
                        },
                    }
                ],
                "max_output_tokens": 100,
            },
            "deepseek-chat",
        )

        self.assertEqual(payload["model"], "deepseek-chat")
        self.assertEqual(payload["messages"][0]["role"], "system")
        self.assertEqual(payload["messages"][1], {"role": "user", "content": "Hi"})
        self.assertEqual(payload["messages"][2]["tool_calls"][0]["id"], "call_1")
        self.assertEqual(payload["messages"][3]["role"], "tool")
        self.assertEqual(payload["tools"][0]["function"]["name"], "lookup")
        self.assertEqual(payload["max_tokens"], 100)

    def test_byok_function_call_output_serializes_structured_tool_content(self):
        payload = transform_request_to_chat(
            {
                "model": "deepseek:deepseek-chat",
                "input": [
                    {"type": "function_call", "call_id": "call_1", "name": "lookup", "arguments": "{}"},
                    {"type": "function_call_output", "call_id": "call_1", "output": {"ok": True, "items": [1, 2]}},
                ],
            },
            "deepseek-chat",
        )

        self.assertEqual(payload["messages"][1]["role"], "tool")
        self.assertEqual(payload["messages"][1]["content"], '{"ok": true, "items": [1, 2]}')

    def test_flat_responses_function_tools_transform_for_google_and_byok(self):
        flat_tool = {
            "type": "function",
            "name": "lookup",
            "description": "Lookup a value",
            "parameters": {
                "type": "object",
                "properties": {"q": {"type": "string"}},
                "required": ["q"],
            },
            "strict": True,
        }

        google = transform_request({"model": "gemini-3.5-flash-high", "input": "hi", "tools": [flat_tool]})
        declaration = google["request"]["tools"][0]["functionDeclarations"][0]
        self.assertEqual(declaration["name"], "lookup")
        self.assertEqual(declaration["parameters"]["required"], ["q"])

        byok = transform_request_to_chat({"model": "deepseek:deepseek-chat", "input": "hi", "tools": [flat_tool]}, "deepseek-chat")
        chat_fn = byok["tools"][0]["function"]
        self.assertEqual(chat_fn["name"], "lookup")
        self.assertTrue(chat_fn["strict"])

    def test_transform_text_format_json_object_to_chat_response_format(self):
        payload = transform_request_to_chat(
            {
                "model": "deepseek:deepseek-chat",
                "input": "Return JSON.",
                "text": {"format": {"type": "json_object"}},
            },
            "deepseek-chat",
        )

        self.assertEqual(payload["response_format"], {"type": "json_object"})

    def test_transform_text_format_json_schema_to_chat_response_format(self):
        payload = transform_request_to_chat(
            {
                "model": "deepseek:deepseek-chat",
                "input": "Return JSON.",
                "text": {
                    "format": {
                        "type": "json_schema",
                        "name": "answer",
                        "strict": True,
                        "schema": {
                            "type": "object",
                            "properties": {"answer": {"type": "string"}},
                            "required": ["answer"],
                        },
                    }
                },
            },
            "deepseek-chat",
        )

        self.assertEqual(payload["response_format"]["type"], "json_schema")
        self.assertEqual(payload["response_format"]["json_schema"]["name"], "answer")
        self.assertTrue(payload["response_format"]["json_schema"]["strict"])
        self.assertEqual(payload["response_format"]["json_schema"]["schema"]["required"], ["answer"])

    def test_byok_json_schema_preserves_strict_schema_keywords(self):
        payload = transform_request_to_chat(
            {
                "model": "deepseek:deepseek-chat",
                "input": "Return JSON.",
                "text": {
                    "format": {
                        "type": "json_schema",
                        "name": "answer",
                        "strict": True,
                        "schema": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {"answer": {"type": "string", "minLength": 1}},
                            "required": ["answer"],
                        },
                    }
                },
            },
            "deepseek-chat",
        )

        schema = payload["response_format"]["json_schema"]["schema"]
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(schema["properties"]["answer"]["minLength"], 1)

    def test_forced_responses_tool_choice_maps_to_chat_shape(self):
        payload = transform_request_to_chat(
            {
                "model": "deepseek:deepseek-chat",
                "input": "Use the tool.",
                "tools": [{"type": "function", "name": "lookup", "parameters": {"type": "object", "properties": {}}}],
                "tool_choice": {"type": "function", "name": "lookup"},
            },
            "deepseek-chat",
        )

        self.assertEqual(payload["tool_choice"], {"type": "function", "function": {"name": "lookup"}})

    def test_transform_text_format_unsupported_type_fails(self):
        with self.assertRaisesRegex(ValueError, "Unsupported Responses text.format type"):
            transform_request_to_chat(
                {
                    "model": "deepseek:deepseek-chat",
                    "input": "Return XML.",
                    "text": {"format": {"type": "xml"}},
                },
                "deepseek-chat",
            )

    def test_transform_chat_response_to_responses(self):
        response = transform_chat_response(
            {
                "created": 123,
                "choices": [
                    {
                        "message": {
                            "content": "hello",
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {"name": "lookup", "arguments": "{\"q\":\"x\"}"},
                                }
                            ],
                        }
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
            },
            "deepseek:deepseek-chat",
        )

        self.assertEqual(response["output"][0]["content"][0]["text"], "hello")
        self.assertEqual(response["output"][1]["type"], "function_call")
        self.assertEqual(response["output"][1]["call_id"], "call_1")
        self.assertEqual(response["usage"]["total_tokens"], 3)

    def test_transform_chat_response_preserves_reasoning_content(self):
        response = transform_chat_response(
            {
                "created": 123,
                "choices": [{"message": {"reasoning_content": "thinking", "content": "answer"}}],
            },
            "deepseek:deepseek-reasoner",
        )

        self.assertEqual(response["output"][0]["type"], "reasoning")
        self.assertEqual(response["output"][0]["step_by_step_summary"], "thinking")
        self.assertEqual(response["output"][1]["content"][0]["text"], "answer")

    def test_replayed_reasoning_content_attaches_to_chat_tool_call_turn(self):
        payload = transform_request_to_chat(
            {
                "model": "deepseek:deepseek-reasoner",
                "input": [
                    {"type": "reasoning", "step_by_step_summary": "need a lookup"},
                    {"type": "function_call", "call_id": "call_1", "name": "lookup", "arguments": {"q": "x"}},
                ],
            },
            "deepseek-reasoner",
        )

        self.assertEqual(payload["messages"][0]["role"], "assistant")
        self.assertEqual(payload["messages"][0]["reasoning_content"], "need a lookup")
        self.assertEqual(payload["messages"][0]["tool_calls"][0]["function"]["name"], "lookup")

    def test_models_endpoint_includes_configured_byok_models(self):
        provider = {
            "id": "deepseek",
            "displayName": "DeepSeek",
            "kind": "openai_chat",
            "baseUrl": "https://api.deepseek.com",
            "apiKey": "secret",
            "models": ["deepseek-chat"],
        }

        with patch("codex_antigravity_auth.server.all_provider_configs", return_value={"deepseek": provider}):
            response = TestClient(app).get("/v1/models")

        self.assertEqual(response.status_code, 200)
        model_ids = [m["id"] for m in response.json()["data"]]
        self.assertIn("deepseek:deepseek-chat", model_ids)

    def test_models_endpoint_includes_documented_builtin_aliases(self):
        with patch("codex_antigravity_auth.server.all_provider_configs", return_value={}):
            response = TestClient(app).get("/v1/models")

        self.assertEqual(response.status_code, 200)
        model_ids = [m["id"] for m in response.json()["data"]]
        self.assertIn("gemini-3.5-flash-high", model_ids)
        self.assertIn("gemini-3.5-flash-medium", model_ids)
        self.assertIn("gemini-3.1-pro-high", model_ids)
        self.assertIn("claude-3.5-sonnet", model_ids)
        self.assertIn("claude-opus-4-6", model_ids)

    def test_non_streaming_byok_route_posts_chat_completion(self):
        provider = {
            "id": "deepseek",
            "displayName": "DeepSeek",
            "kind": "openai_chat",
            "baseUrl": "https://api.deepseek.com",
            "apiKey": "secret",
            "models": ["deepseek-chat"],
        }
        captured = {}

        class MockClient:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc_val, exc_tb):
                pass

            async def post(self, url, json=None, headers=None):
                captured["url"] = url
                captured["json"] = json
                captured["headers"] = headers
                return httpx.Response(
                    200,
                    json={
                        "choices": [{"message": {"content": "hello"}}],
                        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                    },
                )

        with patch("codex_antigravity_auth.server.all_provider_configs", return_value={"deepseek": provider}):
            with patch("codex_antigravity_auth.server.httpx.AsyncClient", MockClient):
                response = TestClient(app).post(
                    "/v1/responses",
                    json={"model": "deepseek:deepseek-chat", "input": "hello"},
                )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(captured["url"], "https://api.deepseek.com/chat/completions")
        self.assertEqual(captured["headers"]["Authorization"], "Bearer secret")
        self.assertEqual(captured["json"]["model"], "deepseek-chat")
        self.assertEqual(response.json()["output"][0]["content"][0]["text"], "hello")

    def test_streaming_byok_route_translates_content_and_tool_calls(self):
        provider = {
            "id": "xai",
            "displayName": "xAI",
            "kind": "openai_chat",
            "baseUrl": "https://api.x.ai/v1",
            "apiKey": "secret",
            "models": ["grok-code-fast-1"],
        }

        chunks = [
            'data: {"choices":[{"delta":{"reasoning_content":"Think "}}]}\n',
            'data: {"choices":[{"delta":{"content":"Hel"}}]}\n',
            'data: {"choices":[{"delta":{"content":"lo"}}]}\n',
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_1","function":{"name":"lookup","arguments":"{\\"q\\":"}}]}}]}\n',
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"\\"x\\"}"}}]}}]}\n',
            'data: {"usage":{"prompt_tokens":1,"completion_tokens":2,"total_tokens":3},"choices":[]}\n',
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

        class MockClient:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc_val, exc_tb):
                pass

            def stream(self, *args, **kwargs):
                return StreamContext()

        with patch("codex_antigravity_auth.server.all_provider_configs", return_value={"xai": provider}):
            with patch("codex_antigravity_auth.server.httpx.AsyncClient", MockClient):
                response = TestClient(app).post(
                    "/v1/responses",
                    json={"model": "xai:grok-code-fast-1", "input": "hello", "stream": True},
                )

        events = []
        for line in response.text.splitlines():
            if line.startswith("data: ") and line != "data: [DONE]":
                events.append(json.loads(line[6:]))

        deltas = [e["delta"] for e in events if e.get("type") == "response.output_text.delta"]
        reasoning_deltas = [e["delta"] for e in events if e.get("type") == "response.reasoning_text.delta"]
        arg_deltas = [e["delta"] for e in events if e.get("type") == "response.function_call_arguments.delta"]
        tool_added = [e for e in events if e.get("type") == "response.output_item.added" and e["item"]["type"] == "function_call"]
        arg_done = [e for e in events if e.get("type") == "response.function_call_arguments.done"]
        tool_done = [e for e in events if e.get("type") == "response.output_item.done" and e["item"]["type"] == "function_call"]
        done = [e for e in events if e.get("type") == "response.completed"]

        self.assertEqual("".join(deltas), "Hello")
        self.assertEqual("".join(reasoning_deltas), "Think ")
        self.assertEqual("".join(arg_deltas), '{"q":"x"}')
        self.assertLess(events.index(tool_added[0]), events.index(arg_done[0]))
        self.assertEqual(tool_added[0]["item"]["name"], "lookup")
        self.assertEqual(arg_done[0]["arguments"], '{"q":"x"}')
        self.assertEqual(tool_done[0]["item"]["call_id"], "call_1")
        self.assertEqual(tool_done[0]["item"]["name"], "lookup")
        self.assertEqual(tool_done[0]["item"]["arguments"], '{"q":"x"}')
        self.assertEqual(done[0]["response"]["usage"]["total_tokens"], 3)

    def test_streaming_byok_missing_api_key_fails_before_sse_starts(self):
        provider = {
            "id": "deepseek",
            "displayName": "DeepSeek",
            "kind": "openai_chat",
            "baseUrl": "https://api.deepseek.com",
            "models": ["deepseek-chat"],
        }

        with patch("codex_antigravity_auth.server.all_provider_configs", return_value={"deepseek": provider}):
            response = TestClient(app).post(
                "/v1/responses",
                json={"model": "deepseek:deepseek-chat", "input": "hello", "stream": True},
            )

        self.assertEqual(response.status_code, 401)
        self.assertNotIn("response.created", response.text)

    def test_streaming_byok_invalid_base_url_fails_before_sse_starts(self):
        invalid_cases = [
            ("localhost:8000/v1", "absolute http(s) URL"),
            ("http://localhost:8000/v1?x=y", "query strings or fragments"),
            ("http://local host:8000/v1", "whitespace or control characters"),
        ]
        for base_url, expected_error in invalid_cases:
            with self.subTest(base_url=base_url):
                provider = {
                    "id": "deepseek",
                    "displayName": "Custom",
                    "kind": "openai_chat",
                    "baseUrl": base_url,
                    "apiKey": "secret",
                    "models": ["model"],
                }

                with patch("codex_antigravity_auth.server.all_provider_configs", return_value={"deepseek": provider}):
                    response = TestClient(app).post(
                        "/v1/responses",
                        json={"model": "deepseek:model", "input": "hello", "stream": True},
                    )

                self.assertEqual(response.status_code, 400)
                self.assertIn(expected_error, response.json()["detail"])
                self.assertNotIn("response.created", response.text)

    def test_streaming_byok_invalid_timeout_fails_before_sse_starts(self):
        provider = {
            "id": "deepseek",
            "displayName": "Custom",
            "kind": "openai_chat",
            "baseUrl": "http://localhost:8000/v1",
            "apiKey": "secret",
            "timeout": -1,
            "models": ["model"],
        }

        with patch("codex_antigravity_auth.server.all_provider_configs", return_value={"deepseek": provider}):
            response = TestClient(app).post(
                "/v1/responses",
                json={"model": "deepseek:model", "input": "hello", "stream": True},
            )

        self.assertEqual(response.status_code, 400)
        self.assertIn("timeout must be a positive number", response.json()["detail"])
        self.assertNotIn("response.created", response.text)

    def test_streaming_byok_reserved_header_fails_before_sse_starts(self):
        provider = {
            "id": "deepseek",
            "displayName": "Custom",
            "kind": "openai_chat",
            "baseUrl": "http://localhost:8000/v1",
            "apiKey": "secret",
            "headers": {"Authorization": "Bearer override"},
            "models": ["model"],
        }

        with patch("codex_antigravity_auth.server.all_provider_configs", return_value={"deepseek": provider}):
            response = TestClient(app).post(
                "/v1/responses",
                json={"model": "deepseek:model", "input": "hello", "stream": True},
            )

        self.assertEqual(response.status_code, 400)
        self.assertIn("must not override", response.json()["detail"])
        self.assertNotIn("response.created", response.text)

    def test_streaming_byok_malformed_api_key_fails_before_sse_starts(self):
        provider = {
            "id": "deepseek",
            "displayName": "Custom",
            "kind": "openai_chat",
            "baseUrl": "http://localhost:8000/v1",
            "apiKey": "secret\nbad",
            "models": ["model"],
        }

        with patch("codex_antigravity_auth.server.all_provider_configs", return_value={"deepseek": provider}):
            response = TestClient(app).post(
                "/v1/responses",
                json={"model": "deepseek:model", "input": "hello", "stream": True},
            )

        self.assertEqual(response.status_code, 400)
        self.assertIn("API key must not contain control characters", response.json()["detail"])
        self.assertNotIn("response.created", response.text)


if __name__ == "__main__":
    unittest.main()
