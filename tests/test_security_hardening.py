import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
from fastapi.testclient import TestClient

from codex_antigravity_auth.redaction import REDACTED, redact_secret_text, redact_secrets
from codex_antigravity_auth.server import app


class TestRedaction(unittest.TestCase):
    def test_redacts_nested_tokens_and_free_form_api_keys(self):
        raw = {
            "Authorization": "Bearer raw-access-token",
            "tokens": {
                "accessToken": "raw-access-token",
                "refresh_token": "raw-refresh-token",
                "accessTokenExpiresAt": 123,
            },
            "snapshot": {
                "access_token_cached": True,
                "access_token_expires_at": 456,
            },
            "Set-Cookie": "session=raw-cookie",
            "url": "https://example.test/callback?code=oauth-code-secret&client_secret=client-secret",
            "body": '{"apiKey":"json-api-key"} x-goog-api-key: header-api-key authorization=form-secret Cookie: inline-cookie',
        }

        redacted = redact_secrets(raw)
        rendered = str(redacted)

        for secret in (
            "raw-access-token",
            "raw-refresh-token",
            "oauth-code-secret",
            "client-secret",
            "json-api-key",
            "header-api-key",
            "raw-cookie",
            "form-secret",
            "inline-cookie",
        ):
            self.assertNotIn(secret, rendered)
        self.assertEqual(redacted["tokens"]["accessToken"], REDACTED)
        self.assertEqual(redacted["tokens"]["refresh_token"], REDACTED)
        self.assertEqual(redacted["tokens"]["accessTokenExpiresAt"], 123)
        self.assertTrue(redacted["snapshot"]["access_token_cached"])
        self.assertEqual(redacted["snapshot"]["access_token_expires_at"], 456)

    def test_redacts_python_repr_secret_shapes(self):
        rendered = redact_secret_text("{'refreshToken': 'refresh-secret', 'client_secret': 'client-secret'}")

        self.assertNotIn("refresh-secret", rendered)
        self.assertNotIn("client-secret", rendered)
        self.assertIn(REDACTED, rendered)

    def test_redacts_custom_provider_token_headers(self):
        rendered = redact_secret_text("X-Api-Token: provider-token\nX-Credential: provider-credential")

        self.assertNotIn("provider-token", rendered)
        self.assertNotIn("provider-credential", rendered)
        self.assertIn(REDACTED, rendered)

    def test_redacts_cookie_and_authorization_form_values(self):
        rendered = redact_secret_text("authorization=form-secret&cookie=cookie-secret")

        self.assertNotIn("form-secret", rendered)
        self.assertNotIn("cookie-secret", rendered)
        self.assertIn(f"authorization={REDACTED}", rendered)
        self.assertIn(f"cookie={REDACTED}", rendered)


class TestCredentialResolution(unittest.TestCase):
    def test_partial_env_credentials_merge_with_file_and_repair_permissions(self):
        with tempfile.TemporaryDirectory() as tmp:
            creds_path = Path(tmp) / "antigravity-credentials.json"
            creds_path.write_text(
                json.dumps({"client_id": "file-id", "client_secret": "file-secret"}),
                encoding="utf-8",
            )
            os.chmod(creds_path, 0o644)

            with patch("codex_antigravity_auth.constants.CREDENTIALS_FILE", str(creds_path)):
                with patch.dict("os.environ", {"ANTIGRAVITY_CLIENT_ID": "env-id"}, clear=True):
                    from codex_antigravity_auth.constants import resolve_oauth_credentials

                    self.assertEqual(resolve_oauth_credentials(), ("env-id", "file-secret"))

            self.assertEqual(stat.S_IMODE(creds_path.stat().st_mode), 0o600)

    def test_symlinked_oauth_credentials_file_is_ignored_without_chmodding_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            target_path = Path(tmp) / "target-credentials.json"
            target_path.write_text(
                json.dumps({"client_id": "file-id", "client_secret": "file-secret"}),
                encoding="utf-8",
            )
            os.chmod(target_path, 0o644)
            symlink_path = Path(tmp) / "antigravity-credentials.json"
            symlink_path.symlink_to(target_path)

            with patch("codex_antigravity_auth.constants.CREDENTIALS_FILE", str(symlink_path)):
                with patch.dict("os.environ", {}, clear=True):
                    from codex_antigravity_auth.constants import resolve_oauth_credentials

                    self.assertEqual(resolve_oauth_credentials(), (None, None))

            self.assertEqual(stat.S_IMODE(target_path.stat().st_mode), 0o644)


class TestProviderStorage(unittest.TestCase):
    def test_provider_config_write_uses_private_temp_and_cleans_failed_replace(self):
        from codex_antigravity_auth.byok import save_provider_config

        with tempfile.TemporaryDirectory() as tmp:
            providers_path = Path(tmp) / "providers.json"
            observed_modes = []

            def fail_replace(src, dst):
                observed_modes.append(stat.S_IMODE(os.stat(src).st_mode))
                raise RuntimeError("replace failed")

            with patch("codex_antigravity_auth.byok.get_providers_json_path", return_value=providers_path):
                with patch("codex_antigravity_auth.storage.encrypt_payload", return_value=b"encrypted"):
                    with patch("codex_antigravity_auth.storage.os.replace", side_effect=fail_replace):
                        with self.assertRaises(RuntimeError):
                            save_provider_config({"providers": {"deepseek": {"apiKey": "secret"}}})

            self.assertEqual(observed_modes, [0o600])
            self.assertFalse(providers_path.exists())
            self.assertEqual(list(Path(tmp).glob(".providers.json.*.tmp")), [])

    def test_plaintext_provider_config_is_migrated_to_encrypted_private_file(self):
        from codex_antigravity_auth.byok import load_provider_config

        with tempfile.TemporaryDirectory() as tmp:
            providers_path = Path(tmp) / "providers.json"
            providers_path.write_text(
                json.dumps({"providers": {"deepseek": {"apiKey": "provider-secret", "models": ["deepseek-chat"]}}}),
                encoding="utf-8",
            )
            os.chmod(providers_path, 0o644)

            with patch("codex_antigravity_auth.byok.get_providers_json_path", return_value=providers_path):
                loaded = load_provider_config()

            self.assertEqual(loaded["providers"]["deepseek"]["apiKey"], "provider-secret")
            self.assertEqual(stat.S_IMODE(providers_path.stat().st_mode), 0o600)
            with self.assertRaises(json.JSONDecodeError):
                json.loads(providers_path.read_text(encoding="utf-8"))

    def test_provider_config_normalizes_malformed_provider_map(self):
        from codex_antigravity_auth.byok import all_provider_configs, load_provider_config

        with tempfile.TemporaryDirectory() as tmp:
            providers_path = Path(tmp) / "providers.json"
            providers_path.write_text(json.dumps({"providers": []}), encoding="utf-8")

            with patch("codex_antigravity_auth.byok.get_providers_json_path", return_value=providers_path):
                loaded = load_provider_config()
                providers = all_provider_configs(include_env_enabled=False)

            self.assertEqual(loaded["providers"], {})
            self.assertEqual(providers, {})


class TestGatewayRemoteAccess(unittest.TestCase):
    def test_loopback_responses_reject_browser_plain_text_posts(self):
        with patch("codex_antigravity_auth.server.account_manager.select_active_account") as mock_select:
            response = TestClient(app).post(
                "/v1/responses",
                content='{"model":"gemini-3.5-flash-high","input":"hello"}',
                headers={"Content-Type": "text/plain"},
            )

        self.assertEqual(response.status_code, 415)
        self.assertIn("application/json", response.json()["detail"])
        mock_select.assert_not_called()

    def test_loopback_responses_reject_cross_site_browser_origin(self):
        with patch("codex_antigravity_auth.server.account_manager.select_active_account") as mock_select:
            response = TestClient(app).post(
                "/v1/responses",
                json={"model": "gemini-3.5-flash-high", "input": "hello"},
                headers={"Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site"},
            )

        self.assertEqual(response.status_code, 403)
        self.assertIn("Cross-site", response.json()["detail"])
        mock_select.assert_not_called()

    def test_loopback_responses_reject_dns_rebinding_host(self):
        with patch("codex_antigravity_auth.server.account_manager.select_active_account") as mock_select:
            response = TestClient(app).post(
                "/v1/responses",
                json={"model": "gemini-3.5-flash-high", "input": "hello"},
                headers={"Host": "attacker.example:51122", "Origin": "http://attacker.example:51122"},
            )

        self.assertEqual(response.status_code, 403)
        self.assertIn("loopback Host", response.json()["detail"])
        mock_select.assert_not_called()

    def test_loopback_responses_reject_testserver_host_from_real_loopback_client(self):
        with patch("codex_antigravity_auth.server.account_manager.select_active_account") as mock_select:
            response = TestClient(app, client=("127.0.0.1", 50000)).post(
                "/v1/responses",
                json={"model": "gemini-3.5-flash-high", "input": "hello"},
                headers={"Host": "testserver:51122", "Origin": "http://testserver:51122"},
            )

        self.assertEqual(response.status_code, 403)
        self.assertIn("loopback Host", response.json()["detail"])
        mock_select.assert_not_called()

    def test_non_loopback_clients_require_opt_in_bearer_token(self):
        with patch("codex_antigravity_auth.server.all_provider_configs", return_value={}):
            with patch.dict(os.environ, {}, clear=True):
                response = TestClient(app, client=("203.0.113.10", 50000)).get("/v1/models")

        self.assertEqual(response.status_code, 403)

    def test_non_loopback_clients_can_use_configured_bearer_token(self):
        with patch("codex_antigravity_auth.server.all_provider_configs", return_value={}):
            with patch.dict(
                os.environ,
                {"ANTIGRAVITY_ALLOW_REMOTE": "1", "ANTIGRAVITY_GATEWAY_TOKEN": "x" * 32},
                clear=True,
            ):
                response = TestClient(app, client=("203.0.113.10", 50000)).get(
                    "/v1/models",
                    headers={"Authorization": f"Bearer {'x' * 32}"},
                )

        self.assertEqual(response.status_code, 200)

    def test_non_loopback_clients_reject_weak_remote_token_even_when_supplied(self):
        with patch("codex_antigravity_auth.server.all_provider_configs", return_value={}):
            with patch.dict(
                os.environ,
                {"ANTIGRAVITY_ALLOW_REMOTE": "1", "ANTIGRAVITY_GATEWAY_TOKEN": "short"},
                clear=True,
            ):
                response = TestClient(app, client=("203.0.113.10", 50000)).get(
                    "/v1/models",
                    headers={"Authorization": "Bearer short"},
                )

        self.assertEqual(response.status_code, 403)
        self.assertIn("at least 32", response.json()["detail"])


class TestServerErrorRedaction(unittest.TestCase):
    def test_byok_non_streaming_error_detail_is_redacted(self):
        provider = {
            "id": "deepseek",
            "displayName": "DeepSeek",
            "kind": "openai_chat",
            "baseUrl": "https://api.deepseek.com",
            "apiKey": "provider-secret",
            "models": ["deepseek-chat"],
        }

        class MockClient:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc_val, exc_tb):
                pass

            async def post(self, url, json=None, headers=None):
                return httpx.Response(
                    401,
                    content=b'{"error":"bad","authorization":"Bearer raw-token","api_key":"raw-api-key"}',
                )

        with patch("codex_antigravity_auth.server.all_provider_configs", return_value={"deepseek": provider}):
            with patch("codex_antigravity_auth.server.httpx.AsyncClient", MockClient):
                response = TestClient(app).post(
                    "/v1/responses",
                    json={"model": "deepseek:deepseek-chat", "input": "hello"},
                )

        detail = response.json()["detail"]
        self.assertEqual(response.status_code, 401)
        self.assertNotIn("raw-token", detail)
        self.assertNotIn("raw-api-key", detail)
        self.assertIn(REDACTED, detail)

    def test_byok_streaming_error_event_is_redacted(self):
        provider = {
            "id": "xai",
            "displayName": "xAI",
            "kind": "openai_chat",
            "baseUrl": "https://api.x.ai/v1",
            "apiKey": "provider-secret",
            "models": ["grok-code-fast-1"],
        }

        mock_response = MagicMock(spec=httpx.Response)
        mock_response.status_code = 401
        mock_response.aread.return_value = b"api_key=raw-api-key Authorization: Bearer raw-token"

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

        self.assertNotIn("raw-token", response.text)
        self.assertNotIn("raw-api-key", response.text)
        self.assertIn(REDACTED, response.text)


if __name__ == "__main__":
    unittest.main()
