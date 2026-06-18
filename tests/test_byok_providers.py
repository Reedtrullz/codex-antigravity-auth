import json
import unittest
from unittest.mock import MagicMock, patch

import httpx
from fastapi.testclient import TestClient

from codex_antigravity_auth.byok import PROVIDER_PRESETS, split_provider_model
from codex_antigravity_auth.server import app
from codex_antigravity_auth.transform import transform_chat_response, transform_request_to_chat


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
        arg_deltas = [e["delta"] for e in events if e.get("type") == "response.function_call_arguments.delta"]
        arg_done = [e for e in events if e.get("type") == "response.function_call_arguments.done"]
        tool_done = [e for e in events if e.get("type") == "response.output_item.done" and e["item"]["type"] == "function_call"]
        done = [e for e in events if e.get("type") == "response.completed"]

        self.assertEqual("".join(deltas), "Hello")
        self.assertEqual(arg_deltas, ['{"q":"x"}'])
        self.assertEqual(arg_done[0]["arguments"], '{"q":"x"}')
        self.assertEqual(tool_done[0]["item"]["call_id"], "call_1")
        self.assertEqual(tool_done[0]["item"]["name"], "lookup")
        self.assertEqual(tool_done[0]["item"]["arguments"], '{"q":"x"}')
        self.assertEqual(done[0]["response"]["usage"]["total_tokens"], 3)


if __name__ == "__main__":
    unittest.main()
