import json
import os
import urllib.error
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import parse_qs, urlparse
from unittest.mock import patch

from codex_antigravity_auth.xai_oauth import (
    XAI_OAUTH_CLIENT_ID,
    XAI_OAUTH_REDIRECT_URI,
    XAI_OAUTH_SCOPE,
    _post_form,
    build_xai_authorize_url,
    get_xai_oauth_json_path,
    poll_xai_device_code_token,
    resolve_xai_oauth_access_token,
    request_xai_device_code,
    save_xai_oauth_token_response,
    xai_oauth_status,
)


class TestXaiOAuth(unittest.TestCase):
    def test_authorize_url_matches_grok_cli_oauth_contract(self):
        url = build_xai_authorize_url(
            {"challenge": "pkce-challenge"},
            state="state-value",
            nonce="nonce-value",
        )

        parsed = urlparse(url)
        params = parse_qs(parsed.query)

        self.assertEqual(f"{parsed.scheme}://{parsed.netloc}{parsed.path}", "https://auth.x.ai/oauth2/authorize")
        self.assertEqual(params["client_id"], [XAI_OAUTH_CLIENT_ID])
        self.assertEqual(params["redirect_uri"], [XAI_OAUTH_REDIRECT_URI])
        self.assertEqual(params["scope"], [XAI_OAUTH_SCOPE])
        self.assertEqual(params["response_type"], ["code"])
        self.assertEqual(params["code_challenge"], ["pkce-challenge"])
        self.assertEqual(params["code_challenge_method"], ["S256"])
        self.assertEqual(params["state"], ["state-value"])
        self.assertEqual(params["nonce"], ["nonce-value"])
        self.assertEqual(params["plan"], ["generic"])
        self.assertEqual(params["referrer"], ["codex-antigravity-auth"])

    def test_device_code_flow_handles_pending_slowdown_and_success(self):
        calls = []
        sleeps = []

        def fake_post(url, payload, timeout=15.0):
            calls.append((url, dict(payload)))
            if len(calls) == 1:
                return 400, {"error": "authorization_pending"}
            if len(calls) == 2:
                return 400, {"error": "slow_down"}
            return 200, {"access_token": "access", "refresh_token": "refresh", "expires_in": 3600}

        token = poll_xai_device_code_token(
            {"device_code": "device", "user_code": "ABCD", "verification_uri": "https://x.ai/device", "expires_in": 60, "interval": 1},
            post_form=fake_post,
            sleep=lambda seconds: sleeps.append(seconds),
            now_values=iter([0, 1, 2, 3]),
        )

        self.assertEqual(token["access_token"], "access")
        self.assertEqual([call[1]["grant_type"] for call in calls], ["urn:ietf:params:oauth:grant-type:device_code"] * 3)
        self.assertEqual(sleeps, [1.0, 6.0])

    def test_request_device_code_validates_required_fields(self):
        def fake_post(url, payload, timeout=15.0):
            return 200, {"device_code": "device", "user_code": "ABCD"}

        with self.assertRaisesRegex(RuntimeError, "verification_uri"):
            request_xai_device_code(post_form=fake_post)

    def test_post_form_normalizes_network_failures(self):
        with patch("codex_antigravity_auth.xai_oauth.urllib.request.urlopen", side_effect=urllib.error.URLError("network down api_key=sk-secret")):
            status, payload = _post_form("https://auth.x.ai/oauth2/token", {"client_id": XAI_OAUTH_CLIENT_ID})

        self.assertEqual(status, 0)
        self.assertEqual(payload["error"], "network_error")
        self.assertIn("network down", payload["error_description"])
        self.assertNotIn("sk-secret", payload["error_description"])

    def test_refresh_saves_rotated_refresh_token(self):
        with TemporaryDirectory() as tmp:
            with patch("codex_antigravity_auth.xai_oauth.get_codex_home", return_value=Path(tmp)):
                with patch.dict(os.environ, {"ANTIGRAVITY_STORAGE_KEY": "test-storage-key"}, clear=False):
                    save_xai_oauth_token_response(
                        {"access_token": "old-access", "refresh_token": "old-refresh", "expires_in": 1},
                        now=100,
                    )

                    def fake_post(url, payload, timeout=15.0):
                        self.assertEqual(payload["grant_type"], "refresh_token")
                        self.assertEqual(payload["refresh_token"], "old-refresh")
                        return 200, {
                            "access_token": "new-access",
                            "refresh_token": "new-refresh",
                            "expires_in": 3600,
                            "scope": XAI_OAUTH_SCOPE,
                            "token_type": "Bearer",
                        }

                    access = resolve_xai_oauth_access_token(now=200, post_form=fake_post)
                    status = xai_oauth_status(now=200)

                    self.assertEqual(access, "new-access")
                    self.assertTrue(status["ready"])
                    self.assertEqual(status["auth_mode"], "oauth")
                    raw_path = get_xai_oauth_json_path()
                    self.assertTrue(raw_path.is_file())
                    self.assertNotIn("new-refresh", raw_path.read_text(encoding="utf-8", errors="ignore"))
