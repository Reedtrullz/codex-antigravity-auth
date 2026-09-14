"""Unified Codex model picker: OpenAI + Antigravity via one gateway (opt-in)."""

from __future__ import annotations

import json
import unittest
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient


class TestUnifiedRouting(unittest.TestCase):
    def test_claude_routes_to_antigravity(self):
        from codex_antigravity_auth.unified import classify_route

        self.assertEqual(classify_route("claude-sonnet-4-6", unified_enabled=True), "antigravity")
        self.assertEqual(classify_route("claude-opus-4-6-thinking", unified_enabled=True), "antigravity")
        # Aliases resolve through the native registry, not prefix heuristics.
        self.assertEqual(classify_route("opus", unified_enabled=True), "antigravity")
        self.assertEqual(classify_route("claude-3.5-sonnet", unified_enabled=True), "antigravity")

    def test_gemini_routes_to_antigravity(self):
        from codex_antigravity_auth.unified import classify_route

        self.assertEqual(classify_route("gemini-3.7-flash", unified_enabled=True), "antigravity")
        self.assertEqual(classify_route("gemini-3.1-pro", unified_enabled=True), "antigravity")

    def test_gpt_oss_antigravity_never_routes_to_openai(self):
        from codex_antigravity_auth.unified import classify_route, is_antigravity_model, is_openai_model

        # gpt-oss lives in Antigravity despite the gpt- prefix: exact registry match matters.
        self.assertTrue(is_antigravity_model("gpt-oss-120b-medium"))
        self.assertFalse(is_openai_model("gpt-oss-120b-medium"))
        self.assertEqual(classify_route("gpt-oss-120b-medium", unified_enabled=True), "antigravity")

    def test_openai_models_route_to_openai_when_unified(self):
        from codex_antigravity_auth.unified import classify_route

        self.assertEqual(classify_route("gpt-5.6", unified_enabled=True), "openai")
        self.assertEqual(classify_route("gpt-5.6-codex", unified_enabled=True), "openai")

    def test_openai_models_do_not_route_to_antigravity_when_classic(self):
        from codex_antigravity_auth.unified import classify_route

        # Classic must never silently send OpenAI ids to Google; callers hint at unified mode.
        self.assertEqual(classify_route("gpt-5.6", unified_enabled=False), "openai-disabled")
        self.assertEqual(classify_route("gpt-5.6-codex", unified_enabled=False), "openai-disabled")

    def test_byok_routes_to_existing_provider(self):
        from codex_antigravity_auth.unified import classify_route

        self.assertEqual(classify_route("deepseek:deepseek-chat", unified_enabled=True), "byok")
        self.assertEqual(classify_route("openrouter:deepseek/deepseek-chat", unified_enabled=True), "byok")
        self.assertEqual(classify_route("deepseek:deepseek-chat", unified_enabled=False), "byok")

    def test_unknown_model_is_strict_in_unified_passthrough_in_classic(self):
        from codex_antigravity_auth.unified import classify_route

        self.assertEqual(classify_route("totally-unknown-xyz", unified_enabled=True), "unknown")
        # Historical passthrough preserved when unified is off.
        self.assertEqual(classify_route("totally-unknown-xyz", unified_enabled=False), "antigravity")

    def test_no_accidental_cross_routing(self):
        from codex_antigravity_auth.unified import classify_route, list_openai_models
        from codex_antigravity_auth.models import all_native_models

        native_ids = {m.id.lower() for m in all_native_models()}
        native_ids |= {a.lower() for m in all_native_models() for a in m.aliases}
        native_ids |= {m.backend_id.lower() for m in all_native_models()}
        for openai_model in list_openai_models():
            route = classify_route(openai_model.id, unified_enabled=True)
            self.assertEqual(route, "openai", f"{openai_model.id} must route to openai, got {route}")
        for native_id in native_ids:
            # Native ids (incl. gpt-oss) must never route to openai.
            route = classify_route(native_id, unified_enabled=True)
            self.assertIn(route, {"antigravity", "byok", "unknown"}, f"{native_id} must not route to openai")

    def test_openai_registry_env_override(self):
        from codex_antigravity_auth.unified import is_openai_model
        import os

        with patch.dict(os.environ, {"ANTIGRAVITY_OPENAI_MODELS": "my-custom-gpt, gpt-5.6"}):
            self.assertTrue(is_openai_model("my-custom-gpt"))
            self.assertTrue(is_openai_model("gpt-5.6"))

    def test_unified_flag_env_parsing(self):
        import os
        from codex_antigravity_auth.unified import is_unified_mode_enabled

        with patch.dict(os.environ, {"ANTIGRAVITY_UNIFIED_MODEL_PICKER": "1"}):
            self.assertTrue(is_unified_mode_enabled())
        with patch.dict(os.environ, {"ANTIGRAVITY_UNIFIED_MODEL_PICKER": "true"}):
            self.assertTrue(is_unified_mode_enabled())
        with patch.dict(os.environ, {"ANTIGRAVITY_UNIFIED_MODEL_PICKER": ""}):
            self.assertFalse(is_unified_mode_enabled())
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ANTIGRAVITY_UNIFIED_MODEL_PICKER", None)
            self.assertFalse(is_unified_mode_enabled())


class TestUnifiedCatalog(unittest.TestCase):
    def test_models_classic_has_no_openai(self):
        import os
        from codex_antigravity_auth import server

        with patch.dict(os.environ, {"ANTIGRAVITY_UNIFIED_MODEL_PICKER": "0"}):
            with patch("codex_antigravity_auth.server.all_provider_configs_read_only", return_value={}):
                response = TestClient(server.app).get("/v1/models")
        self.assertEqual(response.status_code, 200)
        ids = {m["id"] for m in response.json()["data"]}
        self.assertNotIn("gpt-5.6", ids)
        self.assertIn("claude-sonnet-4-6", ids)

    def test_models_unified_includes_openai_antigravity_and_byok(self):
        import os
        from codex_antigravity_auth import server

        providers = {
            "deepseek": {
                "id": "deepseek",
                "kind": "openai_chat",
                "displayName": "DeepSeek",
                "baseUrl": "https://api.deepseek.com",
                "apiKeyEnv": "DEEPSEEK_API_KEY",
                "apiKey": "sk-test-key-1234567890",
                "models": ["deepseek-chat"],
            }
        }
        with patch.dict(os.environ, {"ANTIGRAVITY_UNIFIED_MODEL_PICKER": "1"}):
            with patch("codex_antigravity_auth.server.all_provider_configs_read_only", return_value=providers):
                response = TestClient(server.app).get("/v1/models")
        self.assertEqual(response.status_code, 200)
        ids = {m["id"] for m in response.json()["data"]}
        self.assertIn("gpt-5.6", ids)
        self.assertIn("gpt-5.6-codex", ids)
        self.assertIn("claude-sonnet-4-6", ids)
        self.assertIn("gemini-3.7-flash", ids)
        self.assertIn("deepseek:deepseek-chat", ids)

    def test_models_unified_does_not_hardcode_only_static_without_registry(self):
        from codex_antigravity_auth.unified import list_openai_models, openai_catalog

        # Registry is the single source of truth for the OpenAI slice of the catalog.
        registry_ids = {m.id for m in list_openai_models()}
        catalog_ids = {e["id"] for e in openai_catalog()}
        self.assertEqual(registry_ids, catalog_ids)
        self.assertIn("gpt-5.6", catalog_ids)


class TestUnifiedResponsesRouting(unittest.TestCase):
    def _post(self, model, env_unified="1"):
        import os
        from codex_antigravity_auth import server

        with patch.dict(os.environ, {"ANTIGRAVITY_UNIFIED_MODEL_PICKER": env_unified}):
            with patch("codex_antigravity_auth.server.all_provider_configs", return_value={}):
                return TestClient(server.app).post("/v1/responses", json={"model": model, "input": "hi"})

    def test_openai_request_without_auth_is_401_not_antigravity_error(self):
        import os

        env = {"ANTIGRAVITY_UNIFIED_MODEL_PICKER": "1"}
        env.pop("OPENAI_API_KEY", None)
        with patch.dict(os.environ, env, clear=False):
            os.environ.pop("OPENAI_API_KEY", None)
            os.environ.pop("ANTIGRAVITY_OPENAI_USE_CODEX_AUTH", None)
            response = self._post("gpt-5.6")
        self.assertEqual(response.status_code, 401)
        body = json.dumps(response.json())
        self.assertIn("OpenAI", body)
        self.assertNotIn("Antigravity", body)
        self.assertNotIn("sk-", body)

    def test_unknown_model_is_clean_404_in_unified(self):
        response = self._post("nope-not-a-model-xyz")
        self.assertEqual(response.status_code, 404)
        body = json.dumps(response.json())
        self.assertIn("Unknown model", body)

    def test_openai_disabled_in_classic_hints_at_unified(self):
        response = self._post("gpt-5.6", env_unified="0")
        # Must not silently go to Google: either explicit hint or Google auth error,
        # but never a misleading success. Unified-disabled hint is the contract.
        self.assertIn(response.status_code, {404, 500})
        body = json.dumps(response.json())
        # No OpenAI token material may leak, and the hint must name unified mode when 404.
        self.assertNotIn("sk-", body)
        if response.status_code == 404:
            self.assertIn("unified", body.lower())

    def test_openai_success_does_not_touch_google_accounts(self):
        import os
        from codex_antigravity_auth import server
        from codex_antigravity_auth.unified import OpenAIAuth

        fake_payload = {
            "id": "resp_123",
            "object": "response",
            "created_at": 123,
            "model": "gpt-5.6",
            "output": [
                {
                    "type": "message",
                    "id": "msg_1",
                    "status": "completed",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "hello", "annotations": []}],
                }
            ],
            "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
        }

        class FakeResp:
            status_code = 200

            def json(self):
                return fake_payload

            text = "{}"

        class FakeClient:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def post(self, *a, **k):
                return FakeResp()

        with patch.dict(
            os.environ,
            {"ANTIGRAVITY_UNIFIED_MODEL_PICKER": "1", "OPENAI_API_KEY": "sk-test-1234567890abcdef"},
        ):
            with patch("codex_antigravity_auth.server.all_provider_configs", return_value={}):
                with patch("codex_antigravity_auth.server.httpx.AsyncClient", FakeClient):
                    with patch(
                        "codex_antigravity_auth.server.acquire_active_account_for_request"
                    ) as mock_acquire:
                        mock_acquire.side_effect = AssertionError("OpenAI must not select Google accounts")
                        response = TestClient(server.app).post(
                            "/v1/responses", json={"model": "gpt-5.6", "input": "hi"}
                        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["model"], "gpt-5.6")

    def test_openai_streaming_preserves_http_failures_and_closes_successfully(self):
        import os
        from codex_antigravity_auth import server

        class FakeResponse:
            def __init__(self, status_code, *, body="", headers=None, chunks=()):
                self.status_code = status_code
                self.headers = headers or {}
                self._body = body.encode("utf-8") if isinstance(body, str) else body
                self._chunks = list(chunks)
                self.closed = False
                self.read = False

            async def aread(self):
                self.read = True
                return self._body

            def aiter_text(self):
                async def iterator():
                    for chunk in self._chunks:
                        yield chunk

                return iterator()

        class FakeStreamContext:
            def __init__(self, response):
                self.response = response
                self.entered = False
                self.exited = False

            async def __aenter__(self):
                self.entered = True
                return self.response

            async def __aexit__(self, exc_type, exc_val, exc_tb):
                self.exited = True
                self.response.closed = True
                return False

        class FakeClient:
            def __init__(self, response):
                self.response = response
                self.context = FakeStreamContext(response)
                self.closed = False

            def stream(self, *args, **kwargs):
                return self.context

            async def aclose(self):
                self.closed = True

        for status_code, headers in ((429, {"retry-after": "30"}), (401, {})):
            with self.subTest(status_code=status_code):
                upstream = FakeResponse(status_code, body='{"error":{"message":"upstream failure"}}', headers=headers)
                client = FakeClient(upstream)
                with patch.dict(os.environ, {"ANTIGRAVITY_UNIFIED_MODEL_PICKER": "1", "OPENAI_API_KEY": "test-key"}):
                    with patch("codex_antigravity_auth.server.all_provider_configs", return_value={}):
                        with patch("codex_antigravity_auth.server.httpx.AsyncClient", return_value=client):
                            with patch("codex_antigravity_auth.server.write_request_record"):
                                response = TestClient(server.app).post(
                                    "/v1/responses",
                                    json={"model": "gpt-5.6", "input": "hi", "stream": True},
                                )
                self.assertEqual(response.status_code, status_code)
                self.assertEqual(response.headers.get("retry-after"), headers.get("retry-after"))
                self.assertTrue(upstream.read)
                self.assertTrue(upstream.closed)
                self.assertTrue(client.context.exited)
                self.assertTrue(client.closed)
                self.assertNotIn("response.failed", response.text)

        chunks = (
            'data: {"type":"response.created","response":{"id":"resp_1","model":"gpt-5.6"}}\n\n',
            'data: {"type":"response.completed","response":{"id":"resp_1","model":"gpt-5.6","usage":{"total_tokens":2}}}\n\n',
            "data: [DONE]\n\n",
        )
        upstream = FakeResponse(200, chunks=chunks)
        client = FakeClient(upstream)
        with patch.dict(os.environ, {"ANTIGRAVITY_UNIFIED_MODEL_PICKER": "1", "OPENAI_API_KEY": "test-key"}):
            with patch("codex_antigravity_auth.server.all_provider_configs", return_value={}):
                with patch("codex_antigravity_auth.server.httpx.AsyncClient", return_value=client):
                    with patch("codex_antigravity_auth.server.write_request_record"):
                        response = TestClient(server.app).post(
                            "/v1/responses",
                            json={"model": "gpt-5.6", "input": "hi", "stream": True},
                        )
        self.assertEqual(response.status_code, 200, response.text)
        # A native terminal without meaningful output is normalized to a
        # failed Responses terminal instead of advertising a false success.
        self.assertIn("response.failed", response.text)
        self.assertIn("empty_response", response.text)
        self.assertTrue(upstream.closed)
        self.assertTrue(client.context.exited)
        self.assertTrue(client.closed)

    def test_antigravity_success_does_not_touch_openai(self):
        import os
        from codex_antigravity_auth import server

        with patch.dict(os.environ, {"ANTIGRAVITY_UNIFIED_MODEL_PICKER": "1"}):
            with patch("codex_antigravity_auth.server.all_provider_configs", return_value={}):
                with patch(
                    "codex_antigravity_auth.server.acquire_active_account_for_request",
                    return_value={"email": "a@example.com", "accessToken": "t", "projectId": "p"},
                ):
                    with patch("codex_antigravity_auth.server.GoogleTransport") as mock_transport_cls:
                        instance = mock_transport_cls.return_value
                        import asyncio

                        async def fake_post(req, lease):
                            class R:
                                status_code = 200
                                text = "{}"

                                def json(self):
                                    return {
                                        "response": {
                                            "candidates": [
                                                {"content": {"parts": [{"text": "hi"}]}, "finishReason": "STOP"}
                                            ],
                                            "usageMetadata": {},
                                        }
                                    }

                            return R()

                        instance.post.side_effect = fake_post
                        with patch(
                            "codex_antigravity_auth.unified.resolve_openai_auth"
                        ) as mock_oai:
                            mock_oai.side_effect = AssertionError("Antigravity must not resolve OpenAI auth")
                            response = TestClient(server.app).post(
                                "/v1/responses", json={"model": "claude-sonnet-4-6", "input": "hi"}
                            )
        self.assertEqual(response.status_code, 200)


class TestUnifiedCodexConfig(unittest.TestCase):
    def test_classic_config_uses_antigravity_provider(self):
        from codex_antigravity_auth.cli import (
            DEFAULT_CODEX_PROVIDER_ID,
            merge_codex_config,
            render_codex_config_snippet,
        )

        snippet = render_codex_config_snippet(model="claude-sonnet-4-6", activate=True)
        self.assertIn(f'model_provider = "{DEFAULT_CODEX_PROVIDER_ID}"', snippet)
        self.assertIn(f"[model_providers.{DEFAULT_CODEX_PROVIDER_ID}]", snippet)

    def test_unified_config_uses_unified_provider(self):
        from codex_antigravity_auth.cli import (
            DEFAULT_UNIFIED_CODEX_PROVIDER_ID,
            merge_codex_config,
            render_codex_config_snippet,
        )

        snippet = render_codex_config_snippet(
            model="gpt-5.6", provider_id=DEFAULT_UNIFIED_CODEX_PROVIDER_ID, activate=True
        )
        self.assertIn(f'model_provider = "{DEFAULT_UNIFIED_CODEX_PROVIDER_ID}"', snippet)
        self.assertIn(f"[model_providers.{DEFAULT_UNIFIED_CODEX_PROVIDER_ID}]", snippet)
        merged = merge_codex_config(
            "", model="gpt-5.6", provider_id=DEFAULT_UNIFIED_CODEX_PROVIDER_ID, activate=True
        )
        self.assertIn("gpt-5.6", merged)

    def test_classic_config_preserved_when_unified_off(self):
        from codex_antigravity_auth.cli import DEFAULT_CODEX_PROVIDER_ID, merge_codex_config

        existing = 'model = "claude-sonnet-4-6"\nmodel_provider = "antigravity"\n'
        merged = merge_codex_config(existing, model="claude-sonnet-4-6", provider_id=DEFAULT_CODEX_PROVIDER_ID)
        self.assertIn('[model_providers.antigravity]', merged)
        self.assertNotIn("antigravity-unified", merged)


class TestUnifiedAuth(unittest.TestCase):
    def test_missing_openai_auth_is_401_without_secrets(self):
        import os
        from codex_antigravity_auth.unified import OpenAIUpstreamAuthError, resolve_openai_auth

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("OPENAI_API_KEY", None)
            os.environ.pop("ANTIGRAVITY_OPENAI_USE_CODEX_AUTH", None)
            # Ensure file path does not provide a key in this hermetic env.
            with patch("codex_antigravity_auth.unified._read_json_file", return_value=None):
                try:
                    resolve_openai_auth()
                    self.fail("expected OpenAIUpstreamAuthError")
                except OpenAIUpstreamAuthError as exc:
                    self.assertEqual(exc.status_code, 401)
                    self.assertNotIn("sk-", str(exc))
                    self.assertIn("OPENAI_API_KEY", str(exc))

    def test_api_key_from_env_wins_and_never_logs_value(self):
        import os
        from codex_antigravity_auth.unified import openai_request_headers, resolve_openai_auth

        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test-1234567890abcdef"}):
            with patch("codex_antigravity_auth.unified._read_json_file", return_value=None):
                auth = resolve_openai_auth()
        self.assertEqual(auth.kind, "api_key")
        headers = openai_request_headers(auth)
        self.assertTrue(headers["Authorization"].startswith("Bearer "))
        # Redaction helper must mask bearer material in free text.
        from codex_antigravity_auth.redaction import redact_secret_text

        redacted = redact_secret_text(f"header {headers['Authorization']}")
        self.assertNotIn("sk-test", redacted)

    def test_codex_oauth_reuse_is_explicit_opt_in(self):
        import os
        from codex_antigravity_auth.unified import resolve_openai_auth

        fake_auth = {"OPENAI_API_KEY": {"access_token": "tok123", "account_id": "acc1"}}
        with patch.dict(os.environ, {"ANTIGRAVITY_OPENAI_USE_CODEX_AUTH": "1"}):
            with patch("codex_antigravity_auth.unified._read_json_file", return_value=fake_auth):
                auth = resolve_openai_auth()
        self.assertEqual(auth.kind, "codex_oauth")
        self.assertEqual(auth.account_id, "acc1")
    def test_codex_oauth_reads_standard_tokens_shape(self):
        import os
        from codex_antigravity_auth.unified import resolve_openai_auth

        fake_auth = {
            "auth_mode": "chatgpt",
            "OPENAI_API_KEY": None,
            "tokens": {
                "access_token": "test-token",
                "account_id": "test-account",
            },
        }
        with patch.dict(os.environ, {"ANTIGRAVITY_OPENAI_USE_CODEX_AUTH": "1", "OPENAI_API_KEY": ""}):
            with patch("codex_antigravity_auth.unified._read_json_file", return_value=fake_auth):
                auth = resolve_openai_auth()
        self.assertEqual(auth.kind, "codex_oauth")
        self.assertEqual(auth.access_token, "test-token")
        self.assertEqual(auth.account_id, "test-account")


if __name__ == "__main__":
    unittest.main()
