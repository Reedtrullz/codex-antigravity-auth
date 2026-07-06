import unittest
import json
import os
import signal
import stat
import sys
import threading
import time
from argparse import Namespace
from pathlib import Path
from tempfile import TemporaryDirectory
import urllib.error
import urllib.request
from urllib.parse import parse_qs, urlparse
from unittest.mock import patch, MagicMock
from codex_antigravity_auth.oauth import authorize_antigravity, encode_state
from codex_antigravity_auth.cli import (
    OAuthCallbackHandler,
    OAuthServer,
    account_rotation_lines,
    configure_codex_write_command,
    codex_ready_report,
    gateway_generate_probe,
    gateway_model_ids,
    gateway_process_command,
    gateway_start_command,
    gateway_status_info,
    install_codex_skill,
    inspect_codex_gateway_config,
    main,
    merge_codex_config,
    normalize_epoch_seconds,
    provider_key_status,
    process_is_running,
    remove_google_account,
    render_codex_config_snippet,
    require_safe_gateway_host,
    run_configure_codex,
    reset_google_account_state,
    run_doctor,
    run_gateway_status,
    run_install_skill,
    run_login,
    run_local_oauth_flow,
    run_setup,
    run_setup_v2,
    run_setup_google,
    start_gateway_background,
    stop_gateway,
    upsert_google_account,
    validate_codex_model_id,
    validate_codex_provider_name,
    version_check_result,
    write_codex_config,
)
from codex_antigravity_auth.constants import save_oauth_credentials
from codex_antigravity_auth.models import canonical_model_id, native_model_catalog, resolve_backend_model
from codex_antigravity_auth.observability import iter_request_records, request_log_summary, write_request_record
from codex_antigravity_auth.service import install_service, render_linux_systemd_unit, render_macos_launch_agent, service_command, service_status


def assert_mode_if_posix(testcase: unittest.TestCase, path: Path, expected: int) -> None:
    if os.name != "nt":
        testcase.assertEqual(stat.S_IMODE(path.stat().st_mode), expected)


def write_ready_codex_config(
    path: Path,
    *,
    base_url: str = "http://localhost:51122/v1",
    model: str = "claude-3.5-sonnet",
    provider_id: str = "antigravity",
    provider_name: str = "Google Antigravity",
) -> None:
    path.write_text(
        render_codex_config_snippet(
            model=model,
            provider_id=provider_id,
            provider_name=provider_name,
            base_url=base_url,
        ),
        encoding="utf-8",
    )

class TestCliDoctor(unittest.TestCase):
    def setUp(self):
        self._version_check_env = patch.dict(os.environ, {"CODEX_ANTIGRAVITY_NO_UPDATE_CHECK": "1"})
        self._version_check_env.start()

    def tearDown(self):
        self._version_check_env.stop()

    @patch("codex_antigravity_auth.cli.resolve_oauth_credentials")
    @patch("codex_antigravity_auth.cli.load_accounts")
    @patch("urllib.request.urlopen")
    def test_run_doctor_displays_accurate_information(self, mock_urlopen, mock_load, mock_creds):
        mock_creds.return_value = ("client_id_val", "client_secret_val")
        mock_load.return_value = {"accounts": [{"email": "test@example.com", "expiresAt": 9_999_999_999}]}
        
        # Mock successful network check
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        with TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            write_ready_codex_config(config_path)
            with patch("codex_antigravity_auth.cli.all_provider_configs", return_value={}):
                with patch("builtins.print") as mock_print:
                    self.assertTrue(run_doctor(config=str(config_path)))
            
            # Extract printed strings
            printed_args = [call[0][0] for call in mock_print.call_args_list if call[0]]
            printed_text = "\n".join(printed_args)
            
            self.assertIn("Configured", printed_text)
            self.assertIn("Token Storage Encryption", printed_text)

    @patch("codex_antigravity_auth.cli.resolve_oauth_credentials")
    @patch("codex_antigravity_auth.cli.load_accounts")
    @patch("urllib.request.urlopen")
    def test_main_doctor_exits_nonzero_on_hard_failure(self, mock_urlopen, mock_load, mock_creds):
        mock_creds.return_value = (None, None)
        mock_load.return_value = {"accounts": []}
        mock_urlopen.side_effect = TimeoutError("offline")

        with TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            write_ready_codex_config(config_path)
            with patch.object(sys, "argv", ["codex-antigravity", "doctor", "--config", str(config_path)]):
                with patch("codex_antigravity_auth.cli.all_provider_configs", return_value={}):
                    with patch("builtins.print"):
                        with self.assertRaises(SystemExit) as raised:
                            main()

        self.assertEqual(raised.exception.code, 1)

    @patch("codex_antigravity_auth.cli.resolve_oauth_credentials")
    @patch("codex_antigravity_auth.cli.load_accounts")
    @patch("urllib.request.urlopen")
    def test_main_doctor_exits_nonzero_without_google_accounts(self, mock_urlopen, mock_load, mock_creds):
        mock_creds.return_value = ("client_id_val", "client_secret_val")
        mock_load.return_value = {"accounts": []}
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        with TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            write_ready_codex_config(config_path)
            with patch.object(sys, "argv", ["codex-antigravity", "doctor", "--config", str(config_path)]):
                with patch("codex_antigravity_auth.cli.all_provider_configs", return_value={}):
                    with patch("builtins.print"):
                        with self.assertRaises(SystemExit) as raised:
                            main()

        self.assertEqual(raised.exception.code, 1)

    @patch("codex_antigravity_auth.cli.resolve_oauth_credentials")
    @patch("codex_antigravity_auth.cli.load_accounts")
    @patch("urllib.request.urlopen")
    def test_run_doctor_reports_malformed_byok_key_without_secret(self, mock_urlopen, mock_load, mock_creds):
        mock_creds.return_value = ("client_id_val", "client_secret_val")
        mock_load.return_value = {"accounts": []}
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        provider = {
            "displayName": "DeepSeek",
            "baseUrl": "https://api.deepseek.com",
            "apiKey": "secret\nbad",
            "models": ["deepseek-chat"],
        }
        with TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            write_ready_codex_config(config_path)
            with patch("codex_antigravity_auth.cli.all_provider_configs", return_value={"deepseek": provider}):
                with patch("builtins.print") as mock_print:
                    self.assertFalse(run_doctor(config=str(config_path)))

        printed_args = [call[0][0] for call in mock_print.call_args_list if call[0]]
        printed_text = "\n".join(printed_args)
        self.assertIn("malformed key", printed_text)
        self.assertNotIn("secret", printed_text)

    @patch("codex_antigravity_auth.cli.resolve_oauth_credentials")
    @patch("codex_antigravity_auth.cli.load_accounts")
    @patch("urllib.request.urlopen")
    def test_run_doctor_byok_only_skips_google_checks(self, mock_urlopen, mock_load, mock_creds):
        provider = {
            "displayName": "DeepSeek",
            "baseUrl": "https://api.deepseek.com",
            "apiKey": "secret",
            "models": ["deepseek-chat"],
        }
        with TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            write_ready_codex_config(config_path)
            with patch("codex_antigravity_auth.cli.all_provider_configs", return_value={"deepseek": provider}):
                with patch("builtins.print") as mock_print:
                    self.assertTrue(run_doctor(byok_only=True, config=str(config_path)))

        printed_args = [call[0][0] for call in mock_print.call_args_list if call[0]]
        printed_text = "\n".join(printed_args)
        self.assertIn("skipped (--byok-only)", printed_text)
        self.assertIn("BYOK Providers: 1 configured, env-enabled, or local", printed_text)
        mock_creds.assert_not_called()
        mock_load.assert_not_called()
        mock_urlopen.assert_not_called()

    def test_doctor_rejects_gateway_url_in_comment_for_inactive_provider(self):
        content = """model = "gpt-5"
model_provider = "openai"
# base_url = "http://localhost:51122/v1"

[model_providers.antigravity]
name = "Google Antigravity"
base_url = "http://localhost:51122/v1"
wire_api = "responses"
"""

        ready, reason = inspect_codex_gateway_config(
            content,
            provider_id="antigravity",
            expected_base_url="http://localhost:51122/v1",
        )

        self.assertFalse(ready)
        self.assertIn("active model_provider", reason)

    def test_run_doctor_byok_only_fails_for_missing_provider_key(self):
        provider = {
            "displayName": "DeepSeek",
            "baseUrl": "https://api.deepseek.com",
            "apiKeyEnv": "MISSING_DEEPSEEK_KEY",
            "models": ["deepseek-chat"],
        }

        with TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            write_ready_codex_config(config_path)
            with patch.dict(os.environ, {}, clear=True):
                with patch("codex_antigravity_auth.cli.all_provider_configs", return_value={"deepseek": provider}):
                    with patch("builtins.print") as mock_print:
                        self.assertFalse(run_doctor(byok_only=True, config=str(config_path)))

        printed_text = "\n".join(call[0][0] for call in mock_print.call_args_list if call[0])
        self.assertIn("[FAIL] BYOK Providers", printed_text)
        self.assertIn("missing key", printed_text)

    def test_run_doctor_byok_only_fails_when_selected_provider_is_missing(self):
        ollama = {
            "id": "ollama",
            "displayName": "Ollama",
            "baseUrl": "http://localhost:11434/v1",
            "apiKeyOptional": True,
            "defaultApiKey": "ollama",
            "models": ["gpt-oss:20b"],
        }
        with TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            write_ready_codex_config(config_path, model="deepseek:deepseek-chat")
            with patch("codex_antigravity_auth.cli.all_provider_configs", return_value={"ollama": ollama}):
                with patch("builtins.print") as mock_print:
                    self.assertFalse(run_doctor(byok_only=True, config=str(config_path)))

        printed_text = "\n".join(call[0][0] for call in mock_print.call_args_list if call[0])
        self.assertIn("Selected BYOK model", printed_text)
        self.assertIn("deepseek", printed_text)

    def test_run_doctor_byok_only_fails_when_selected_model_is_not_listed(self):
        provider = {
            "displayName": "DeepSeek",
            "baseUrl": "https://api.deepseek.com",
            "apiKey": "secret",
            "models": ["deepseek-chat"],
        }
        with TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            write_ready_codex_config(config_path, model="deepseek:deepseek-reasoner")
            with patch("codex_antigravity_auth.cli.all_provider_configs", return_value={"deepseek": provider}):
                with patch("builtins.print") as mock_print:
                    self.assertFalse(run_doctor(byok_only=True, config=str(config_path)))

        printed_text = "\n".join(call[0][0] for call in mock_print.call_args_list if call[0])
        self.assertIn("[FAIL] Selected BYOK model", printed_text)
        self.assertIn("exact model is not listed", printed_text)

    def test_run_doctor_byok_only_fails_when_provider_store_cannot_load(self):
        with TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            write_ready_codex_config(config_path, model="deepseek:deepseek-chat")
            with patch(
                "codex_antigravity_auth.cli.all_provider_configs",
                side_effect=RuntimeError("api_key=sk-testsecret1234567890"),
            ):
                with patch("builtins.print") as mock_print:
                    self.assertFalse(run_doctor(byok_only=True, config=str(config_path)))

        printed_text = "\n".join(call[0][0] for call in mock_print.call_args_list if call[0])
        self.assertIn("[FAIL] BYOK Providers: could not load provider config", printed_text)
        self.assertNotIn("sk-testsecret1234567890", printed_text)

    @patch("codex_antigravity_auth.cli.resolve_oauth_credentials")
    @patch("codex_antigravity_auth.cli.load_accounts")
    @patch("urllib.request.urlopen")
    def test_run_doctor_reports_account_store_load_failure(self, mock_urlopen, mock_load, mock_creds):
        mock_creds.return_value = ("client_id_val", "client_secret_val")
        mock_load.side_effect = RuntimeError("access_token=ya29.secret")
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        with TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            write_ready_codex_config(config_path)
            with patch("codex_antigravity_auth.cli.all_provider_configs", return_value={}):
                with patch("builtins.print") as mock_print:
                    self.assertFalse(run_doctor(config=str(config_path)))

        printed_text = "\n".join(call[0][0] for call in mock_print.call_args_list if call[0])
        self.assertIn("[FAIL] Authenticated Accounts: could not load account store", printed_text)
        self.assertNotIn("ya29.secret", printed_text)

    @patch("codex_antigravity_auth.cli.resolve_oauth_credentials")
    @patch("codex_antigravity_auth.cli.load_accounts")
    @patch("urllib.request.urlopen")
    def test_run_doctor_accepts_custom_codex_provider_id(self, mock_urlopen, mock_load, mock_creds):
        mock_creds.return_value = ("client_id_val", "client_secret_val")
        mock_load.return_value = {"accounts": [{"email": "test@example.com", "expiresAt": 9_999_999_999}]}
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        with TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            write_ready_codex_config(config_path, provider_id="ag-local", provider_name="AG Local")
            with patch("codex_antigravity_auth.cli.all_provider_configs", return_value={}):
                self.assertTrue(run_doctor(config=str(config_path), provider_id="ag-local"))

    @patch("codex_antigravity_auth.cli.resolve_oauth_credentials")
    @patch("codex_antigravity_auth.cli.load_accounts")
    @patch("urllib.request.urlopen")
    def test_run_doctor_reports_env_storage_key_as_configured(self, mock_urlopen, mock_load, mock_creds):
        mock_creds.return_value = ("client_id_val", "client_secret_val")
        mock_load.return_value = {"accounts": [{"email": "test@example.com", "expiresAt": 9_999_999_999}]}
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        with TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            write_ready_codex_config(config_path)
            with patch.dict(os.environ, {"ANTIGRAVITY_STORAGE_KEY": "test-storage-key"}, clear=True):
                with patch("codex_antigravity_auth.cli.all_provider_configs", return_value={}):
                    with patch("builtins.print") as mock_print:
                        self.assertTrue(run_doctor(config=str(config_path)))

        printed_text = "\n".join(call[0][0] for call in mock_print.call_args_list if call[0])
        self.assertIn("ANTIGRAVITY_STORAGE_KEY configured", printed_text)

    def test_normalize_epoch_seconds_treats_non_finite_values_as_expired(self):
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=repr(value)):
                self.assertEqual(normalize_epoch_seconds(value), 0)

    def test_normalize_epoch_seconds_accepts_seconds_and_milliseconds(self):
        self.assertEqual(normalize_epoch_seconds(1_700_000_000), 1_700_000_000)
        self.assertEqual(normalize_epoch_seconds(1_700_000_000_000), 1_700_000_000)
        self.assertAlmostEqual(normalize_epoch_seconds(10_000_000_001), 10_000_000.001)


class TestCliStartSafety(unittest.TestCase):
    def test_loopback_host_is_allowed_without_remote_token(self):
        with patch.dict("os.environ", {}, clear=True):
            require_safe_gateway_host("127.0.0.1", allow_remote=False)
            require_safe_gateway_host("localhost", allow_remote=False)

    def test_non_loopback_host_requires_explicit_remote_opt_in(self):
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaisesRegex(SystemExit, "Refusing to bind"):
                require_safe_gateway_host("0.0.0.0", allow_remote=False)
            with self.assertRaisesRegex(SystemExit, "ANTIGRAVITY_GATEWAY_TOKEN"):
                require_safe_gateway_host("0.0.0.0", allow_remote=True)

    def test_remote_opt_in_sets_runtime_guard_flag(self):
        with patch.dict("os.environ", {"ANTIGRAVITY_GATEWAY_TOKEN": "x" * 32}, clear=True):
            require_safe_gateway_host("0.0.0.0", allow_remote=True)
            self.assertEqual(os.environ["ANTIGRAVITY_ALLOW_REMOTE"], "1")

    def test_remote_opt_in_rejects_weak_gateway_token(self):
        with patch.dict("os.environ", {"ANTIGRAVITY_GATEWAY_TOKEN": "short"}, clear=True):
            with self.assertRaisesRegex(SystemExit, "at least 32"):
                require_safe_gateway_host("0.0.0.0", allow_remote=True)


class TestGoogleAccountSetup(unittest.TestCase):
    @patch("codex_antigravity_auth.oauth.require_credentials", return_value=("client-id", "client-secret"))
    def test_authorize_antigravity_can_force_google_account_chooser(self, _mock_credentials):
        auth_info = authorize_antigravity(select_account=True)
        query = parse_qs(urlparse(auth_info["url"]).query)

        self.assertEqual(query["prompt"], ["consent select_account"])

    def test_upsert_google_account_adds_to_rotation_and_clears_stale_state(self):
        data = {
            "accounts": [
                {
                    "email": "old@example.com",
                    "refreshToken": "old-refresh",
                    "accessToken": "old-access",
                    "expiresAt": 100,
                }
            ],
            "accountState": {
                "failures": {"old@example.com": 3, "other@example.com": 1},
                "cooldowns": {"old@example.com": 9000, "other@example.com": 8000},
            },
        }

        result = upsert_google_account(
            data,
            {
                "email": "old@example.com",
                "refreshToken": "new-refresh",
                "accessToken": "new-access",
                "expiresAt": 200,
            },
        )

        self.assertEqual(result, {"email": "old@example.com", "created": False, "account_count": 1})
        self.assertEqual(data["accounts"][0]["refreshToken"], "new-refresh")
        self.assertNotIn("old@example.com", data["accountState"]["failures"])
        self.assertNotIn("old@example.com", data["accountState"]["cooldowns"])
        self.assertIn("other@example.com", data["accountState"]["failures"])
        self.assertIn("other@example.com", data["accountState"]["cooldowns"])

    def test_account_rotation_lines_show_active_families_and_cooldowns(self):
        data = {
            "accounts": [
                {"email": "first@example.com", "expiresAt": 5000},
                {"email": "second@example.com", "expiresAt": 900},
            ],
            "activeIndexByFamily": {"gemini": 0, "claude": 1},
            "accountState": {
                "failures": {"second@example.com": 2},
                "cooldowns": {"second@example.com": 1120},
            },
        }

        with patch("codex_antigravity_auth.cli.time.time", return_value=1000):
            lines = account_rotation_lines(data)

        self.assertIn("2 account(s)", lines[0])
        self.assertIn("first@example.com [gemini active] - token OK, available", lines[1])
        self.assertIn("second@example.com [claude active] - will refresh, cooldown 120s, failures=2", lines[2])

    @patch("codex_antigravity_auth.cli.print_account_rotation_summary")
    @patch("codex_antigravity_auth.cli.run_local_oauth_flow")
    def test_run_login_count_forces_google_account_chooser(self, mock_login, mock_summary):
        run_login(Namespace(count=2, select_account=False))

        self.assertEqual(mock_login.call_count, 2)
        for call in mock_login.call_args_list:
            self.assertEqual(call.kwargs, {"select_account": True})
        mock_summary.assert_called_once()

    @patch("codex_antigravity_auth.cli.run_doctor")
    @patch("codex_antigravity_auth.cli.run_login")
    @patch("codex_antigravity_auth.cli.run_configure_codex")
    def test_setup_google_writes_codex_config_and_logs_accounts_in(self, mock_configure, mock_login, mock_doctor):
        with patch("codex_antigravity_auth.cli.resolve_oauth_credentials", return_value=("client-id", "client-secret")):
            run_setup_google(
                Namespace(
                    accounts=3,
                    skip_codex_config=False,
                    skip_doctor=False,
                    config="/tmp/codex.toml",
                    model="gemini-3.5-flash-high",
                    provider="antigravity",
                    provider_name="Google Antigravity",
                    base_url="http://localhost:51122/v1",
                    port=51122,
                )
            )

        configure_args = mock_configure.call_args.args[0]
        self.assertTrue(configure_args.write)
        self.assertEqual(configure_args.config, "/tmp/codex.toml")
        mock_login.assert_called_once()
        login_args = mock_login.call_args.args[0]
        self.assertEqual(login_args.count, 3)
        self.assertTrue(login_args.select_account)
        mock_doctor.assert_called_once()
        self.assertEqual(mock_doctor.call_args.kwargs["provider_id"], "antigravity")

    @patch("codex_antigravity_auth.cli.run_doctor")
    @patch("codex_antigravity_auth.cli.run_login")
    @patch("codex_antigravity_auth.cli.run_configure_codex")
    def test_setup_google_derives_base_url_from_custom_port(self, mock_configure, mock_login, mock_doctor):
        with patch("codex_antigravity_auth.cli.resolve_oauth_credentials", return_value=("client-id", "client-secret")):
            run_setup_google(
                Namespace(
                    accounts=1,
                    skip_codex_config=False,
                    skip_doctor=False,
                    config="/tmp/codex.toml",
                    model="gemini-3.5-flash-high",
                    provider="antigravity",
                    provider_name="Google Antigravity",
                    base_url=None,
                    port=51123,
                )
            )

        configure_args = mock_configure.call_args.args[0]
        self.assertEqual(configure_args.base_url, "http://localhost:51123/v1")
        self.assertEqual(mock_doctor.call_args.kwargs["expected_base_url"], "http://localhost:51123/v1")

    @patch("codex_antigravity_auth.cli.run_doctor")
    @patch("codex_antigravity_auth.cli.run_login")
    @patch("codex_antigravity_auth.cli.run_configure_codex")
    def test_setup_google_validates_codex_config_options_before_login(self, mock_configure, mock_login, mock_doctor):
        with patch("codex_antigravity_auth.cli.resolve_oauth_credentials", return_value=("client-id", "client-secret")):
            with self.assertRaisesRegex(SystemExit, "absolute http\\(s\\) URL"):
                run_setup_google(
                    Namespace(
                        accounts=1,
                        skip_codex_config=False,
                        skip_doctor=False,
                        config="/tmp/codex.toml",
                        model="gemini-3.5-flash-high",
                        provider="antigravity",
                        provider_name="Google Antigravity",
                        base_url="localhost:51122/v1",
                        port=51122,
                    )
                )

        mock_login.assert_not_called()
        mock_configure.assert_not_called()
        mock_doctor.assert_not_called()

    @patch("codex_antigravity_auth.cli.run_doctor")
    @patch("codex_antigravity_auth.cli.run_login")
    @patch("codex_antigravity_auth.cli.run_configure_codex")
    def test_setup_google_preflights_oauth_credentials_before_codex_config(self, mock_configure, mock_login, mock_doctor):
        with patch("codex_antigravity_auth.cli.resolve_oauth_credentials", return_value=(None, None)):
            with self.assertRaisesRegex(SystemExit, "OAuth client credentials"):
                run_setup_google(
                    Namespace(
                        accounts=1,
                        skip_codex_config=False,
                        skip_doctor=False,
                        config="/tmp/codex.toml",
                        model="gemini-3.5-flash-high",
                        provider="antigravity",
                        provider_name="Google Antigravity",
                        base_url=None,
                        port=51122,
                    )
                )

        mock_configure.assert_not_called()
        mock_login.assert_not_called()
        mock_doctor.assert_not_called()

    @patch("codex_antigravity_auth.cli.run_doctor", return_value=False)
    @patch("codex_antigravity_auth.cli.run_login")
    @patch("codex_antigravity_auth.cli.run_configure_codex")
    def test_setup_google_exits_nonzero_when_post_setup_doctor_fails(self, mock_configure, mock_login, mock_doctor):
        with patch("codex_antigravity_auth.cli.resolve_oauth_credentials", return_value=("client-id", "client-secret")):
            with self.assertRaisesRegex(SystemExit, "doctor found hard failures"):
                run_setup_google(
                    Namespace(
                        accounts=1,
                        skip_codex_config=False,
                        skip_doctor=False,
                        config="/tmp/codex.toml",
                        model="gemini-3.5-flash-high",
                        provider="antigravity",
                        provider_name="Google Antigravity",
                        base_url=None,
                        port=51122,
                    )
                )

        mock_configure.assert_called_once()
        mock_login.assert_called_once()
        mock_doctor.assert_called_once()

    @patch("codex_antigravity_auth.cli.run_login", side_effect=SystemExit("OAuth callback port 51121 is already in use"))
    @patch("codex_antigravity_auth.cli.run_configure_codex")
    def test_setup_google_does_not_write_codex_config_when_oauth_start_fails(self, mock_configure, mock_login):
        with patch("codex_antigravity_auth.cli.resolve_oauth_credentials", return_value=("client-id", "client-secret")):
            with self.assertRaisesRegex(SystemExit, "OAuth callback port"):
                run_setup_google(
                    Namespace(
                        accounts=1,
                        skip_codex_config=False,
                        skip_doctor=False,
                        config="/tmp/codex.toml",
                        model="gemini-3.5-flash-high",
                        provider="antigravity",
                        provider_name="Google Antigravity",
                        base_url=None,
                        port=51122,
                    )
                )

        mock_login.assert_called_once()
        mock_configure.assert_not_called()

    def test_oauth_callback_port_conflict_reports_actionable_error(self):
        with patch("codex_antigravity_auth.cli.resolve_oauth_credentials", return_value=("client-id", "client-secret")):
            with patch(
                "codex_antigravity_auth.cli.authorize_antigravity",
                return_value={"url": "https://accounts.google.example/auth", "state_id": "state"},
            ):
                with patch("codex_antigravity_auth.cli.OAuthServer", side_effect=OSError("address in use")):
                    with self.assertRaisesRegex(SystemExit, "port 51121"):
                        run_local_oauth_flow()

    def test_oauth_callback_rejects_state_mismatch_before_storing_code(self):
        server = OAuthServer(("127.0.0.1", 0), OAuthCallbackHandler)
        server.timeout = 2
        server.expected_state_id = "good-state"
        host, port = server.server_address

        def request_once(path: str):
            thread = threading.Thread(target=server.handle_request)
            thread.start()
            try:
                return urllib.request.urlopen(f"http://{host}:{port}{path}", timeout=2)
            finally:
                thread.join(timeout=2)

        try:
            bad_state = encode_state({"id": "bad-state"})
            with self.assertRaises(urllib.error.HTTPError) as raised:
                request_once(f"/oauth-callback?code=evil-code&state={bad_state}")
            self.assertEqual(raised.exception.code, 400)
            self.assertIsNone(server.auth_code)
            self.assertIsNone(server.auth_state)

            good_state = encode_state({"id": "good-state"})
            with request_once(f"/oauth-callback?code=good-code&state={good_state}") as response:
                self.assertEqual(response.status, 200)
            self.assertEqual(server.auth_code, "good-code")
            self.assertEqual(server.auth_state, good_state)
        finally:
            server.server_close()


class TestConfigureCodex(unittest.TestCase):
    def test_render_codex_config_snippet_contains_gateway_provider(self):
        snippet = render_codex_config_snippet()

        self.assertIn('model = "claude-3.5-sonnet"', snippet)
        self.assertIn('model_provider = "antigravity"', snippet)
        self.assertIn("[model_providers.antigravity]", snippet)
        self.assertIn('base_url = "http://localhost:51122/v1"', snippet)
        self.assertIn('wire_api = "responses"', snippet)

    def test_claude_aliases_resolve_for_native_codex_setup(self):
        self.assertEqual(validate_codex_model_id("sonnet"), "claude-3.5-sonnet")
        self.assertEqual(validate_codex_model_id("claude-sonnet"), "claude-3.5-sonnet")
        self.assertEqual(validate_codex_model_id("opus"), "claude-opus-4-6")
        self.assertEqual(validate_codex_model_id("claude-opus"), "claude-opus-4-6")
        self.assertEqual(canonical_model_id("openai-responses/sonnet"), "claude-3.5-sonnet")
        self.assertEqual(resolve_backend_model("sonnet"), "claude-sonnet-4-6")
        self.assertEqual(resolve_backend_model("opus"), "claude-opus-4-6-thinking")
        self.assertEqual(resolve_backend_model("gemini-3.5-flash-low"), "gemini-3.5-flash-low")
        self.assertEqual(resolve_backend_model("gemini-3.1-pro"), "gemini-3.1-pro-low")

    def test_configure_codex_write_command_preserves_custom_options(self):
        command = configure_codex_write_command(
            Namespace(
                config="/tmp/codex config.toml",
                model="deepseek:deepseek-chat",
                provider="ag-local",
                provider_name="AG Local",
                base_url="http://127.0.0.1:51123/v1",
            )
        )

        self.assertIn("--write", command)
        self.assertIn("--config '/tmp/codex config.toml'", command)
        self.assertIn("--model deepseek:deepseek-chat", command)
        self.assertIn("--provider ag-local", command)
        self.assertIn("--provider-name 'AG Local'", command)
        self.assertIn("--base-url http://127.0.0.1:51123/v1", command)

    def test_gateway_start_command_matches_configured_port(self):
        self.assertEqual(gateway_start_command("http://localhost:51122/v1"), "codex-antigravity start")
        self.assertEqual(
            gateway_start_command("http://localhost:51123/v1"),
            "codex-antigravity start --port 51123",
        )

    def test_configure_codex_rejects_unsafe_provider_id(self):
        with self.assertRaisesRegex(ValueError, "provider id"):
            render_codex_config_snippet(provider_id='bad"]\n[evil]')

        with TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            with self.assertRaisesRegex(ValueError, "provider id"):
                write_codex_config(config_path, provider_id="bad.provider")

    def test_configure_codex_rejects_malformed_model_or_provider_name(self):
        self.assertEqual(validate_codex_model_id("deepseek:deepseek-chat"), "deepseek:deepseek-chat")
        self.assertEqual(validate_codex_provider_name(" Google Antigravity "), "Google Antigravity")
        invalid_values = [
            ({"model": "bad\nmodel"}, "model id"),
            ({"model": "bad model"}, "model id"),
            ({"provider_name": "Bad\nProvider"}, "provider name"),
            ({"provider_name": ""}, "provider name"),
        ]
        for kwargs, expected_error in invalid_values:
            with self.subTest(kwargs=kwargs):
                with self.assertRaisesRegex(ValueError, expected_error):
                    render_codex_config_snippet(**kwargs)

                with TemporaryDirectory() as tmp:
                    config_path = Path(tmp) / "config.toml"
                    with self.assertRaisesRegex(ValueError, expected_error):
                        write_codex_config(config_path, **kwargs)
                    self.assertFalse(config_path.exists())

    def test_configure_codex_rejects_invalid_base_url_before_write(self):
        with self.assertRaisesRegex(ValueError, "absolute http\\(s\\) URL"):
            render_codex_config_snippet(base_url="localhost:51122/v1")

        for base_url, expected_error in (
            ("ftp://localhost:51122/v1", "absolute http\\(s\\) URL"),
            ("http://localhost:51122/v1?x=y", "query strings or fragments"),
            ("http://local host:51122/v1", "whitespace or control characters"),
        ):
            with self.subTest(base_url=base_url):
                with TemporaryDirectory() as tmp:
                    config_path = Path(tmp) / "config.toml"
                    with self.assertRaisesRegex(ValueError, expected_error):
                        write_codex_config(config_path, base_url=base_url)
                    self.assertFalse(config_path.exists())

    def test_run_configure_codex_reports_unsafe_provider_id_without_traceback(self):
        args = Namespace(
            config="~/.codex/config.toml",
            model="gemini-3.5-flash-high",
            provider="bad.provider",
            provider_name="Bad Provider",
            base_url="http://localhost:51122/v1",
            write=False,
        )

        with self.assertRaisesRegex(SystemExit, "provider id"):
            run_configure_codex(args)

    def test_run_configure_codex_reports_invalid_base_url_without_traceback(self):
        args = Namespace(
            config="~/.codex/config.toml",
            model="gemini-3.5-flash-high",
            provider="antigravity",
            provider_name="Google Antigravity",
            base_url="localhost:51122/v1",
            write=False,
        )

        with self.assertRaisesRegex(SystemExit, "absolute http\\(s\\) URL"):
            run_configure_codex(args)

    def test_run_configure_codex_reports_malformed_model_without_traceback(self):
        args = Namespace(
            config="~/.codex/config.toml",
            model="bad\nmodel",
            provider="antigravity",
            provider_name="Google Antigravity",
            base_url="http://localhost:51122/v1",
            write=False,
        )

        with self.assertRaisesRegex(SystemExit, "model id"):
            run_configure_codex(args)

    def test_run_configure_codex_reports_write_failure_without_traceback(self):
        args = Namespace(
            config="~/.codex/config.toml",
            model="gemini-3.5-flash-high",
            provider="antigravity",
            provider_name="Google Antigravity",
            base_url="http://localhost:51122/v1",
            write=True,
        )

        with patch("codex_antigravity_auth.cli.write_codex_config", side_effect=RuntimeError("disk full")):
            with self.assertRaisesRegex(SystemExit, "disk full"):
                run_configure_codex(args)

    def test_merge_codex_config_preserves_unrelated_sections(self):
        existing = "\n".join(
            [
                "# user settings",
                'model = "gpt-5"',
                "",
                "[profiles.work]",
                'approval_policy = "never"',
                "",
                "[model_providers.antigravity] # managed gateway",
                'name = "Old Gateway"',
                'base_url = "http://127.0.0.1:1234/v1"',
            ]
        )

        merged = merge_codex_config(existing)

        self.assertIn("# user settings", merged)
        self.assertIn("[profiles.work]", merged)
        self.assertIn('approval_policy = "never"', merged)
        self.assertIn('model = "claude-3.5-sonnet"', merged)
        self.assertIn('model_provider = "antigravity"', merged)
        self.assertIn("[model_providers.antigravity] # managed gateway", merged)
        self.assertIn('name = "Google Antigravity"', merged)
        self.assertIn('base_url = "http://localhost:51122/v1"', merged)
        self.assertIn('wire_api = "responses"', merged)

    def test_write_codex_config_creates_backup_and_is_idempotent(self):
        with TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            config_path.write_text('model = "gpt-5"\n', encoding="utf-8")
            os.chmod(config_path, 0o644)

            changed, backup_path = write_codex_config(config_path)

            self.assertTrue(changed)
            self.assertIsNotNone(backup_path)
            self.assertEqual(backup_path.read_text(encoding="utf-8"), 'model = "gpt-5"\n')
            assert_mode_if_posix(self, backup_path, 0o600)
            self.assertIn('model_provider = "antigravity"', config_path.read_text(encoding="utf-8"))
            assert_mode_if_posix(self, config_path, 0o600)

            changed_again, backup_again = write_codex_config(config_path)

            self.assertFalse(changed_again)
            self.assertIsNone(backup_again)

    def test_write_codex_config_creates_private_new_file(self):
        with TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "nested" / "config.toml"

            changed, backup_path = write_codex_config(config_path)

            self.assertTrue(changed)
            self.assertIsNone(backup_path)
            self.assertIn('model_provider = "antigravity"', config_path.read_text(encoding="utf-8"))
            assert_mode_if_posix(self, config_path, 0o600)

    def test_write_codex_config_does_not_overwrite_same_second_backup(self):
        with TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            config_path.write_text('model = "gpt-5"\n', encoding="utf-8")

            with patch("codex_antigravity_auth.cli.time.strftime", return_value="20260702140000"):
                changed_first, first_backup = write_codex_config(config_path, model="gemini-3.5-flash-high")
                changed_second, second_backup = write_codex_config(config_path, model="gemini-3.5-flash-medium")

            self.assertTrue(changed_first)
            self.assertTrue(changed_second)
            self.assertNotEqual(first_backup, second_backup)
            self.assertEqual(first_backup.name, "config.toml.bak-20260702140000")
            self.assertEqual(second_backup.name, "config.toml.bak-20260702140000-2")
            self.assertEqual(first_backup.read_text(encoding="utf-8"), 'model = "gpt-5"\n')
            self.assertIn('model = "gemini-3.5-flash-high"', second_backup.read_text(encoding="utf-8"))
            assert_mode_if_posix(self, first_backup, 0o600)
            assert_mode_if_posix(self, second_backup, 0o600)

    def test_write_codex_config_keeps_original_when_replace_fails(self):
        with TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            config_path.write_text('model = "gpt-5"\n', encoding="utf-8")
            os.chmod(config_path, 0o644)
            original_replace = os.replace
            observed_config_temp_modes = []

            def fail_config_replace(src, dst):
                if Path(dst) == config_path:
                    observed_config_temp_modes.append(stat.S_IMODE(os.stat(src).st_mode))
                    raise RuntimeError("replace failed")
                return original_replace(src, dst)

            with patch("codex_antigravity_auth.cli.os.replace", side_effect=fail_config_replace):
                with self.assertRaises(RuntimeError):
                    write_codex_config(config_path)

            self.assertEqual(config_path.read_text(encoding="utf-8"), 'model = "gpt-5"\n')
            assert_mode_if_posix(self, config_path, 0o644)
            if os.name != "nt":
                self.assertEqual(observed_config_temp_modes, [0o600])
            backups = list(Path(tmp).glob("config.toml.bak-*"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_text(encoding="utf-8"), 'model = "gpt-5"\n')
            assert_mode_if_posix(self, backups[0], 0o600)
            self.assertEqual(list(Path(tmp).glob(".config.toml.*.tmp")), [])

    def test_write_codex_config_preserves_symlinked_config_path(self):
        with TemporaryDirectory() as tmp:
            target_path = Path(tmp) / "dotfiles" / "config.toml"
            target_path.parent.mkdir()
            target_path.write_text('model = "gpt-5"\n', encoding="utf-8")
            config_path = Path(tmp) / "config.toml"
            try:
                os.symlink(target_path, config_path)
            except (AttributeError, NotImplementedError, OSError) as e:
                self.skipTest(f"symlink unavailable: {e}")

            changed, backup_path = write_codex_config(config_path)

            self.assertTrue(changed)
            self.assertTrue(config_path.is_symlink())
            self.assertEqual(config_path.resolve(), target_path.resolve())
            self.assertIn('model_provider = "antigravity"', target_path.read_text(encoding="utf-8"))
            assert_mode_if_posix(self, target_path, 0o600)
            self.assertIsNotNone(backup_path)
            self.assertEqual(backup_path.parent.resolve(), target_path.parent.resolve())
            self.assertEqual(backup_path.read_text(encoding="utf-8"), 'model = "gpt-5"\n')
            assert_mode_if_posix(self, backup_path, 0o600)


class TestInstallSkill(unittest.TestCase):
    def test_install_codex_skill_copies_bundled_anti_skill(self):
        with TemporaryDirectory() as tmp:
            action, destination, backup_path = install_codex_skill(Path(tmp))

            self.assertEqual(action, "installed")
            self.assertIsNone(backup_path)
            self.assertTrue((destination / "SKILL.md").is_file())
            self.assertTrue((destination / "agents" / "openai.yaml").is_file())
            self.assertTrue((destination / "scripts" / "anti.py").is_file())
            self.assertTrue((destination / "tests" / "test_anti.py").is_file())
            assert_mode_if_posix(self, destination / "scripts" / "anti.py", 0o755)
            self.assertIn("$anti", (destination / "SKILL.md").read_text(encoding="utf-8"))

            action_again, destination_again, backup_again = install_codex_skill(Path(tmp))
            self.assertEqual(action_again, "unchanged")
            self.assertEqual(destination_again, destination)
            self.assertIsNone(backup_again)

    def test_install_codex_skill_requires_force_before_replacing_existing_skill(self):
        with TemporaryDirectory() as tmp:
            skill_dir = Path(tmp)
            existing = skill_dir / "anti"
            existing.mkdir()
            (existing / "SKILL.md").write_text("local edit\n", encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "--force"):
                install_codex_skill(skill_dir)

            action, destination, backup_path = install_codex_skill(skill_dir, force=True)

            self.assertEqual(action, "replaced")
            self.assertEqual(destination, existing)
            self.assertIsNotNone(backup_path)
            self.assertEqual(backup_path.parent, skill_dir.with_name(f"{skill_dir.name}-backups"))
            self.assertFalse(backup_path.parent.is_relative_to(skill_dir))
            self.assertEqual(list(skill_dir.glob("anti.backup-*")), [])
            self.assertEqual((backup_path / "SKILL.md").read_text(encoding="utf-8"), "local edit\n")
            self.assertIn("$anti", (destination / "SKILL.md").read_text(encoding="utf-8"))

    def test_install_codex_skill_does_not_follow_symlinked_existing_files(self):
        with TemporaryDirectory() as tmp:
            skill_dir = Path(tmp)
            existing = skill_dir / "anti"
            existing.mkdir()
            secret_path = Path(tmp) / "outside-secret.txt"
            secret_path.write_text("do not read me\n", encoding="utf-8")
            try:
                os.symlink(secret_path, existing / "SKILL.md")
            except (AttributeError, NotImplementedError, OSError) as e:
                self.skipTest(f"symlink unavailable: {e}")

            with self.assertRaisesRegex(RuntimeError, "--force"):
                install_codex_skill(skill_dir)

    def test_install_codex_skill_dry_run_does_not_write(self):
        with TemporaryDirectory() as tmp:
            action, destination, backup_path = install_codex_skill(Path(tmp), dry_run=True)

            self.assertEqual(action, "installed")
            self.assertIsNone(backup_path)
            self.assertFalse(destination.exists())

    def test_run_install_skill_prints_install_location(self):
        with TemporaryDirectory() as tmp:
            args = Namespace(skill_dir=tmp, force=False, dry_run=False, verify=False)
            with patch("builtins.print") as mock_print:
                run_install_skill(args)

            printed_text = "\n".join(call[0][0] for call in mock_print.call_args_list if call[0])
            self.assertIn("Installed Codex Anti skill", printed_text)
            self.assertIn("Skill chip: Anti", printed_text)
            self.assertIn("$anti review this diff with opus", printed_text)

    def test_run_install_skill_verify_runs_installed_skill_tests(self):
        with TemporaryDirectory() as tmp:
            args = Namespace(skill_dir=tmp, force=False, dry_run=False, verify=True)
            with patch("codex_antigravity_auth.cli.verify_codex_skill", return_value=True) as verify:
                with patch("builtins.print"):
                    run_install_skill(args)

            verify.assert_called_once()
            self.assertTrue((Path(tmp) / "anti" / "SKILL.md").is_file())

    def test_main_install_skill_command_uses_temp_skill_dir(self):
        with TemporaryDirectory() as tmp:
            argv = ["codex-antigravity", "install-skill", "--skill-dir", tmp]
            with patch.object(sys, "argv", argv):
                with patch("builtins.print"):
                    main()

            self.assertTrue((Path(tmp) / "anti" / "SKILL.md").is_file())

    def test_setup_v2_preflight_does_not_write_without_write_flag(self):
        with TemporaryDirectory() as tmp:
            args = Namespace(
                skill_dir=tmp,
                base_url="http://127.0.0.1:51122/v1",
                timeout=0.01,
                write=False,
                force=False,
                verify_skill=False,
                check_google=False,
                check_byok=False,
            )
            with patch("codex_antigravity_auth.cli.gateway_model_ids", return_value={"claude-opus-4-6", "claude-3.5-sonnet"}):
                with patch("codex_antigravity_auth.cli.all_provider_configs") as providers:
                    with patch("builtins.print") as mock_print:
                        run_setup_v2(args)

            self.assertFalse((Path(tmp) / "anti").exists())
            providers.assert_not_called()
            printed_text = "\n".join(call[0][0] for call in mock_print.call_args_list if call[0])
            self.assertIn("Bundled Anti skill", printed_text)
            self.assertIn("Gateway /v1/models", printed_text)
            self.assertIn("BYOK provider checks skipped", printed_text)

    def test_setup_v2_warns_when_installed_skill_differs_from_bundled(self):
        with TemporaryDirectory() as tmp:
            skill_dir = Path(tmp)
            installed = skill_dir / "anti"
            installed.mkdir()
            (installed / "SKILL.md").write_text("stale local skill\n", encoding="utf-8")
            args = Namespace(
                skill_dir=tmp,
                base_url="http://127.0.0.1:51122/v1",
                timeout=0.01,
                write=False,
                force=False,
                verify_skill=False,
                check_google=False,
                check_byok=False,
            )
            with patch("codex_antigravity_auth.cli.gateway_model_ids", return_value={"claude-opus-4-6", "claude-3.5-sonnet"}):
                with patch("builtins.print") as mock_print:
                    run_setup_v2(args)

            printed_text = "\n".join(call[0][0] for call in mock_print.call_args_list if call[0])
            self.assertIn("differs from bundled V2 skill", printed_text)
            self.assertIn("--write --force", printed_text)

    def test_setup_v2_check_byok_reports_gateway_model_mismatch(self):
        provider = {
            "displayName": "DeepSeek",
            "baseUrl": "https://api.deepseek.com",
            "apiKey": "secret",
            "models": ["deepseek-chat"],
        }
        with TemporaryDirectory() as tmp:
            args = Namespace(
                skill_dir=tmp,
                base_url="http://127.0.0.1:51122/v1",
                timeout=0.01,
                write=False,
                force=False,
                verify_skill=False,
                check_google=False,
                check_byok=True,
            )
            with patch("codex_antigravity_auth.cli.gateway_model_ids", return_value={"claude-opus-4-6", "claude-3.5-sonnet"}):
                with patch("codex_antigravity_auth.cli.all_provider_configs", return_value={"deepseek": provider}):
                    with patch("codex_antigravity_auth.cli.load_provider_config", return_value={"providers": {"deepseek": provider}}):
                        with patch("builtins.print") as mock_print:
                            run_setup_v2(args)

            printed_text = "\n".join(call[0][0] for call in mock_print.call_args_list if call[0])
            self.assertIn("deepseek:deepseek-chat", printed_text)
            self.assertIn("not advertised by gateway", printed_text)

    def test_setup_v2_check_byok_reports_provider_load_failure_without_secret(self):
        with TemporaryDirectory() as tmp:
            args = Namespace(
                skill_dir=tmp,
                base_url="http://127.0.0.1:51122/v1",
                timeout=0.01,
                write=False,
                force=False,
                verify_skill=False,
                check_google=False,
                check_byok=True,
            )
            with patch("codex_antigravity_auth.cli.gateway_model_ids", return_value={"claude-opus-4-6", "claude-3.5-sonnet"}):
                with patch("codex_antigravity_auth.cli.all_provider_configs", side_effect=RuntimeError("api_key=sk-testsecret1234567890")):
                    with patch("builtins.print") as mock_print:
                        run_setup_v2(args)

            printed_text = "\n".join(call[0][0] for call in mock_print.call_args_list if call[0])
            self.assertIn("could not load provider config", printed_text)
            self.assertNotIn("sk-testsecret1234567890", printed_text)

    def test_gateway_model_ids_sends_bearer_token_from_env(self):
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["auth"] = req.get_header("Authorization")
            response = MagicMock()
            response.read.return_value = b'{"data": [{"id": "claude-opus-4-6"}]}'
            response.__enter__ = lambda self_: response
            response.__exit__ = lambda self_, *exc: False
            return response

        with patch.dict(os.environ, {"TEST_GATEWAY_TOKEN": "unit-test-token-value"}):
            with patch("urllib.request.urlopen", side_effect=fake_urlopen):
                ids = gateway_model_ids("https://gateway.example/v1", token_env="TEST_GATEWAY_TOKEN")

        self.assertEqual(ids, {"claude-opus-4-6"})
        self.assertEqual(captured["auth"], "Bearer unit-test-token-value")

    def test_gateway_model_ids_omits_auth_header_when_env_missing(self):
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["auth"] = req.get_header("Authorization")
            response = MagicMock()
            response.read.return_value = b'{"data": [{"id": "claude-opus-4-6"}]}'
            response.__enter__ = lambda self_: response
            response.__exit__ = lambda self_, *exc: False
            return response

        env = {key: value for key, value in os.environ.items() if key != "TEST_GATEWAY_TOKEN"}
        with patch.dict(os.environ, env, clear=True):
            with patch("urllib.request.urlopen", side_effect=fake_urlopen):
                gateway_model_ids("https://gateway.example/v1", token_env="TEST_GATEWAY_TOKEN")

        self.assertIsNone(captured["auth"])

    def test_gateway_generate_probe_posts_responses_request_with_bearer_token(self):
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured["timeout"] = timeout
            captured["auth"] = req.get_header("Authorization")
            captured["body"] = json.loads(req.data.decode("utf-8"))
            response = MagicMock()
            response.status = 200
            response.read.return_value = b'{"output":[{"content":[{"type":"output_text","text":"ready"}]}]}'
            response.__enter__ = lambda self_: response
            response.__exit__ = lambda self_, *exc: False
            return response

        with patch.dict(os.environ, {"TEST_GATEWAY_TOKEN": "token-value"}, clear=False):
            with patch("urllib.request.urlopen", side_effect=fake_urlopen):
                probe = gateway_generate_probe(
                    "https://gateway.example/v1",
                    "claude-3.5-sonnet",
                    timeout=12,
                    token_env="TEST_GATEWAY_TOKEN",
                )

        self.assertTrue(probe["ok"])
        self.assertEqual(captured["url"], "https://gateway.example/v1/responses")
        self.assertEqual(captured["timeout"], 12)
        self.assertEqual(captured["auth"], "Bearer token-value")
        self.assertEqual(captured["body"]["input"], "Reply with the single word: ready")
        self.assertEqual(captured["body"]["max_output_tokens"], 16)
        self.assertEqual(probe["output_preview"], "ready")

    def test_gateway_generate_probe_folds_http_errors_into_result(self):
        response = MagicMock()
        response.read.return_value = b'{"api_key":"sk-secret-value"}'
        error = urllib.error.HTTPError("https://gateway.example/v1/responses", 502, "Bad Gateway", {}, response)

        with patch("urllib.request.urlopen", side_effect=error):
            probe = gateway_generate_probe(
                "https://gateway.example/v1",
                "claude-3.5-sonnet",
                timeout=1,
                token_env="TEST_GATEWAY_TOKEN",
            )

        self.assertFalse(probe["ok"])
        self.assertEqual(probe["http_status"], 502)
        self.assertIn("HTTP 502", probe["error"])
        self.assertNotIn("sk-secret-value", probe["error"])


class TestV3NativeSetup(unittest.TestCase):
    def setUp(self):
        self._version_check_env = patch.dict(os.environ, {"CODEX_ANTIGRAVITY_NO_UPDATE_CHECK": "1"})
        self._version_check_env.start()

    def tearDown(self):
        self._version_check_env.stop()

    def setup_args(self, **overrides):
        args = Namespace(
            check=False,
            json=False,
            write=False,
            no_input=False,
            accounts=1,
            model="claude-3.5-sonnet",
            provider="antigravity",
            provider_name="Google Antigravity",
            base_url="http://localhost:51122/v1",
            config="~/.codex/config.toml",
            install_skill=False,
            skill_dir="~/.codex/skills",
            force=False,
            verify_skill=False,
            start=False,
            port=51122,
            host="127.0.0.1",
            allow_remote=False,
            gateway_timeout=0.01,
            gateway_token_env="ANTIGRAVITY_GATEWAY_TOKEN",
            live=False,
            live_model=None,
            live_timeout=30.0,
        )
        for key, value in overrides.items():
            setattr(args, key, value)
        return args

    def test_setup_check_does_not_mutate_state(self):
        with TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            args = self.setup_args(check=True, config=str(config_path), skill_dir=tmp)
            with patch("codex_antigravity_auth.cli.resolve_oauth_credentials", return_value=("client-id", "secret")):
                with patch("codex_antigravity_auth.cli.run_login") as login:
                    with patch("codex_antigravity_auth.cli.run_configure_codex") as configure:
                        with patch("codex_antigravity_auth.cli.run_install_skill") as install:
                            with patch("codex_antigravity_auth.cli.start_gateway_background") as start:
                                with patch("codex_antigravity_auth.cli.gateway_model_ids", return_value={"claude-3.5-sonnet"}):
                                    with patch("codex_antigravity_auth.cli.load_accounts", return_value={"accounts": []}):
                                        with patch("builtins.print"):
                                            report = run_setup(args)

            self.assertFalse(report["ok"])
            self.assertFalse(config_path.exists())
            login.assert_not_called()
            configure.assert_not_called()
            install.assert_not_called()
            start.assert_not_called()

    def test_plain_setup_reports_read_only_mode(self):
        with TemporaryDirectory() as tmp:
            args = self.setup_args(config=str(Path(tmp) / "config.toml"), skill_dir=tmp)
            with patch("codex_antigravity_auth.cli.resolve_oauth_credentials", return_value=("client-id", "secret")):
                with patch("codex_antigravity_auth.cli.gateway_model_ids", return_value={"claude-3.5-sonnet"}):
                    with patch("codex_antigravity_auth.cli.load_accounts", return_value={"accounts": []}):
                        with patch("builtins.print") as mock_print:
                            report = run_setup(args)

        printed_text = "\n".join(call[0][0] for call in mock_print.call_args_list if call[0])
        self.assertEqual(report["mode"], "check")
        self.assertIn("read-only", printed_text)
        self.assertIn("pass --write", printed_text)

    def test_setup_write_runs_login_config_skill_start_and_readiness_in_order(self):
        events = []

        def record(name):
            def _inner(*args, **kwargs):
                events.append(name)
                if name == "doctor":
                    return {
                        "ok": True,
                        "checks": [{"name": "codex_config", "status": "pass", "detail": "ready"}],
                        "next_command": "codex",
                    }
                if name == "gateway":
                    return {"claude-3.5-sonnet", "claude-opus-4-6"}
                return None
            return _inner

        args = self.setup_args(write=True, install_skill=True, start=True, config="/tmp/codex.toml")
        with patch("codex_antigravity_auth.cli.resolve_oauth_credentials", return_value=("client-id", "secret")):
            with patch("codex_antigravity_auth.cli.run_login", side_effect=record("login")):
                with patch("codex_antigravity_auth.cli.run_configure_codex", side_effect=record("config")):
                    with patch("codex_antigravity_auth.cli.run_install_skill", side_effect=record("skill")):
                        with patch("codex_antigravity_auth.cli.start_gateway_background", side_effect=record("start")):
                            with patch("codex_antigravity_auth.cli.gateway_model_ids", side_effect=record("gateway")):
                                with patch("codex_antigravity_auth.cli.codex_ready_report", side_effect=record("doctor")):
                                    with patch("builtins.print"):
                                        report = run_setup(args)

        self.assertTrue(report["ok"])
        self.assertEqual(events, ["login", "config", "skill", "start", "gateway", "doctor"])

    def test_setup_derives_base_url_from_custom_port(self):
        with TemporaryDirectory() as tmp:
            args = self.setup_args(check=True, base_url=None, port=6000, config=str(Path(tmp) / "config.toml"))
            with patch("codex_antigravity_auth.cli.resolve_oauth_credentials", return_value=("client-id", "secret")):
                with patch(
                    "codex_antigravity_auth.cli.codex_ready_report",
                    return_value={"ok": True, "checks": [], "next_command": "codex"},
                ) as readiness:
                    with patch("builtins.print"):
                        report = run_setup(args)

        self.assertEqual(report["base_url"], "http://localhost:6000/v1")
        self.assertEqual(readiness.call_args.kwargs["expected_base_url"], "http://localhost:6000/v1")

    def test_setup_rejects_start_port_base_url_mismatch_before_config_write(self):
        args = self.setup_args(write=True, start=True, port=6000, base_url="http://localhost:51122/v1")
        with patch("codex_antigravity_auth.cli.resolve_oauth_credentials") as creds:
            with patch("codex_antigravity_auth.cli.run_configure_codex") as configure:
                with patch("builtins.print") as mock_print:
                    with self.assertRaises(SystemExit):
                        run_setup(args)

        creds.assert_not_called()
        configure.assert_not_called()
        printed_text = "\n".join(call[0][0] for call in mock_print.call_args_list if call[0])
        self.assertIn("--port 6000", printed_text)
        self.assertIn("--base-url points at port 51122", printed_text)

    def test_setup_invalid_model_reports_without_oauth_or_config_write(self):
        args = self.setup_args(write=True, model="bad model")
        with patch("codex_antigravity_auth.cli.resolve_oauth_credentials") as creds:
            with patch("codex_antigravity_auth.cli.run_configure_codex") as configure:
                with patch("builtins.print") as mock_print:
                    with self.assertRaises(SystemExit):
                        run_setup(args)

        creds.assert_not_called()
        configure.assert_not_called()
        printed_text = "\n".join(call[0][0] for call in mock_print.call_args_list if call[0])
        self.assertIn("Codex model id must not contain whitespace", printed_text)

    def test_setup_write_preflights_oauth_before_config_write(self):
        args = self.setup_args(write=True)
        with patch("codex_antigravity_auth.cli.resolve_oauth_credentials", return_value=(None, None)):
            with patch("codex_antigravity_auth.cli.run_configure_codex") as configure:
                with patch("codex_antigravity_auth.cli.run_login") as login:
                    with patch("builtins.print"):
                        with self.assertRaisesRegex(SystemExit, "OAuth client credentials"):
                            run_setup(args)

        login.assert_not_called()
        configure.assert_not_called()

    def test_setup_write_prompts_for_missing_oauth_credentials_on_tty(self):
        with TemporaryDirectory() as tmp:
            credentials_path = Path(tmp) / "antigravity-credentials.json"
            args = self.setup_args(write=True, config=str(Path(tmp) / "config.toml"))
            stdin = MagicMock()
            stdin.isatty.return_value = True
            with patch("codex_antigravity_auth.constants.CREDENTIALS_FILE", str(credentials_path)):
                with patch("codex_antigravity_auth.cli.sys.stdin", stdin):
                    with patch("codex_antigravity_auth.cli.resolve_oauth_credentials", return_value=(None, None)):
                        with patch("builtins.input", return_value="client.apps.googleusercontent.com"):
                            with patch("codex_antigravity_auth.cli.getpass.getpass", return_value="client-secret"):
                                with patch(
                                    "codex_antigravity_auth.cli.validate_oauth_credentials_with_google",
                                    return_value=("pass", "valid"),
                                ):
                                    with patch("codex_antigravity_auth.cli.run_login") as login:
                                        with patch("codex_antigravity_auth.cli.run_configure_codex") as configure:
                                            with patch("codex_antigravity_auth.cli.gateway_model_ids", return_value={"claude-3.5-sonnet"}):
                                                with patch(
                                                    "codex_antigravity_auth.cli.codex_ready_report",
                                                    return_value={"ok": True, "checks": [], "next_command": "codex"},
                                                ):
                                                    with patch("builtins.print"):
                                                        report = run_setup(args)

            self.assertTrue(report["ok"])
            login.assert_called_once()
            configure.assert_called_once()
            assert_mode_if_posix(self, credentials_path, 0o600)
            saved = json.loads(credentials_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["client_id"], "client.apps.googleusercontent.com")
            self.assertEqual(saved["client_secret"], "client-secret")

    def test_setup_no_input_fails_fast_when_oauth_credentials_missing(self):
        args = self.setup_args(write=True, no_input=True)
        stdin = MagicMock()
        stdin.isatty.return_value = True
        with patch("codex_antigravity_auth.cli.sys.stdin", stdin):
            with patch("codex_antigravity_auth.cli.resolve_oauth_credentials", return_value=(None, None)):
                with patch("builtins.input") as prompt:
                    with patch("codex_antigravity_auth.cli.run_configure_codex") as configure:
                        with patch("builtins.print"):
                            with self.assertRaisesRegex(SystemExit, "OAuth client credentials"):
                                run_setup(args)

        prompt.assert_not_called()
        configure.assert_not_called()

    def test_save_oauth_credentials_refuses_symlink(self):
        with TemporaryDirectory() as tmp:
            target = Path(tmp) / "target.json"
            link = Path(tmp) / "antigravity-credentials.json"
            target.write_text("{}", encoding="utf-8")
            link.symlink_to(target)
            with patch("codex_antigravity_auth.constants.CREDENTIALS_FILE", str(link)):
                with self.assertRaisesRegex(RuntimeError, "symlink"):
                    save_oauth_credentials("client.apps.googleusercontent.com", "client-secret")

    def test_setup_write_preflights_byok_provider_before_config_write(self):
        args = self.setup_args(write=True, model="deepseek:deepseek-chat")
        with patch("codex_antigravity_auth.cli.resolve_oauth_credentials", return_value=(None, None)):
            with patch("codex_antigravity_auth.cli.all_provider_configs", return_value={}):
                with patch("codex_antigravity_auth.cli.run_configure_codex") as configure:
                    with patch("codex_antigravity_auth.cli.run_login") as login:
                        with patch("builtins.print") as mock_print:
                            with self.assertRaisesRegex(SystemExit, "BYOK provider is not ready"):
                                run_setup(args)

        login.assert_not_called()
        configure.assert_not_called()
        printed_text = "\n".join(call[0][0] for call in mock_print.call_args_list if call[0])
        self.assertIn("BYOK provider 'deepseek' is not configured", printed_text)
        self.assertIn("codex-antigravity provider set deepseek", printed_text)

    def test_setup_write_allows_ready_byok_provider_before_config_write(self):
        provider = {
            "id": "deepseek",
            "displayName": "DeepSeek",
            "baseUrl": "https://api.deepseek.com",
            "apiKey": "secret",
            "models": ["deepseek-chat"],
        }
        args = self.setup_args(write=True, model="deepseek:deepseek-chat")
        with patch("codex_antigravity_auth.cli.resolve_oauth_credentials", return_value=(None, None)):
            with patch("codex_antigravity_auth.cli.all_provider_configs", return_value={"deepseek": provider}):
                with patch("codex_antigravity_auth.cli.run_configure_codex") as configure:
                    with patch("codex_antigravity_auth.cli.gateway_model_ids", return_value={"deepseek:deepseek-chat"}):
                        with patch(
                            "codex_antigravity_auth.cli.codex_ready_report",
                            return_value={"ok": True, "checks": [], "next_command": "codex"},
                        ):
                            with patch("builtins.print"):
                                report = run_setup(args)

        self.assertTrue(report["ok"])
        configure.assert_called_once()

    def test_setup_write_start_failure_reports_next_command(self):
        args = self.setup_args(write=True, start=True)
        with patch("codex_antigravity_auth.cli.resolve_oauth_credentials", return_value=("client-id", "secret")):
            with patch("codex_antigravity_auth.cli.run_login"):
                with patch("codex_antigravity_auth.cli.run_configure_codex"):
                    with patch("codex_antigravity_auth.cli.start_gateway_background", side_effect=SystemExit("boom")):
                        with patch("codex_antigravity_auth.cli.gateway_model_ids") as gateway:
                            with patch("builtins.print") as mock_print:
                                with self.assertRaisesRegex(SystemExit, "boom"):
                                    run_setup(args)

        gateway.assert_not_called()
        printed_text = "\n".join(call[0][0] for call in mock_print.call_args_list if call[0])
        self.assertIn("codex-antigravity start --background --port 51122", printed_text)

    def test_setup_write_start_waits_for_gateway_models(self):
        provider_models = iter([RuntimeError("booting"), {"claude-3.5-sonnet", "claude-opus-4-6"}])

        def fake_models(*args, **kwargs):
            result = next(provider_models)
            if isinstance(result, Exception):
                raise result
            return result

        args = self.setup_args(write=True, start=True)
        with patch("codex_antigravity_auth.cli.resolve_oauth_credentials", return_value=("client-id", "secret")):
            with patch("codex_antigravity_auth.cli.run_login"):
                with patch("codex_antigravity_auth.cli.run_configure_codex"):
                    with patch("codex_antigravity_auth.cli.start_gateway_background"):
                        with patch("codex_antigravity_auth.cli.gateway_model_ids", side_effect=fake_models) as models:
                            with patch("codex_antigravity_auth.cli.time.sleep"):
                                with patch(
                                    "codex_antigravity_auth.cli.codex_ready_report",
                                    return_value={"ok": True, "checks": [], "next_command": "codex"},
                                ):
                                    with patch("builtins.print") as mock_print:
                                        report = run_setup(args)

        self.assertTrue(report["ok"])
        self.assertEqual(models.call_count, 2)
        printed_text = "\n".join(call[0][0] for call in mock_print.call_args_list if call[0])
        self.assertIn("/v1/models is reachable", printed_text)

    def test_setup_write_start_reports_gateway_wait_failure(self):
        args = self.setup_args(write=True, start=True)
        with patch("codex_antigravity_auth.cli.resolve_oauth_credentials", return_value=("client-id", "secret")):
            with patch("codex_antigravity_auth.cli.run_login"):
                with patch("codex_antigravity_auth.cli.run_configure_codex"):
                    with patch("codex_antigravity_auth.cli.start_gateway_background"):
                        with patch("codex_antigravity_auth.cli.wait_for_gateway_model_ids", side_effect=RuntimeError("still booting")):
                            with patch("builtins.print") as mock_print:
                                with self.assertRaisesRegex(SystemExit, "Gateway did not become ready"):
                                    run_setup(args)

        printed_text = "\n".join(call[0][0] for call in mock_print.call_args_list if call[0])
        self.assertIn("still booting", printed_text)
        self.assertIn("codex-antigravity status --port 51122", printed_text)

    def test_setup_check_reports_ignored_action_flags(self):
        with TemporaryDirectory() as tmp:
            args = self.setup_args(
                check=True,
                install_skill=True,
                start=True,
                config=str(Path(tmp) / "config.toml"),
                skill_dir=tmp,
            )
            with patch("codex_antigravity_auth.cli.resolve_oauth_credentials", return_value=("client-id", "secret")):
                with patch(
                    "codex_antigravity_auth.cli.codex_ready_report",
                    return_value={"ok": True, "checks": [], "next_command": "codex"},
                ):
                    with patch("builtins.print") as mock_print:
                        report = run_setup(args)

        self.assertTrue(report["ok"])
        printed_text = "\n".join(call[0][0] for call in mock_print.call_args_list if call[0])
        self.assertIn("--install-skill is only applied when --write is used", printed_text)
        self.assertIn("--start is only applied when --write is used", printed_text)

    def test_setup_json_is_read_only(self):
        with self.assertRaisesRegex(SystemExit, "read-only"):
            run_setup(self.setup_args(write=True, json=True))

    def test_codex_ready_report_passes_with_selected_claude_model(self):
        with TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            write_ready_codex_config(config_path, model="sonnet")
            with patch("codex_antigravity_auth.cli.gateway_model_ids", return_value={"claude-3.5-sonnet", "claude-opus-4-6"}):
                with patch("codex_antigravity_auth.cli.load_accounts", return_value={"accounts": [{"email": "a@example.com"}]}):
                    report = codex_ready_report(
                        config=str(config_path),
                        provider_id="antigravity",
                        expected_base_url="http://localhost:51122/v1",
                        include_version_check=False,
                    )

        self.assertTrue(report["ok"])
        self.assertEqual(report["canonical_model"], "claude-3.5-sonnet")
        self.assertEqual(report["route"], "google")

    def test_codex_ready_report_fails_when_selected_model_missing_from_gateway(self):
        with TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            write_ready_codex_config(config_path, model="opus")
            with patch("codex_antigravity_auth.cli.gateway_model_ids", return_value={"claude-3.5-sonnet"}):
                with patch("codex_antigravity_auth.cli.load_accounts", return_value={"accounts": [{"email": "a@example.com"}]}):
                    report = codex_ready_report(
                        config=str(config_path),
                        provider_id="antigravity",
                        expected_base_url="http://localhost:51122/v1",
                    )

        self.assertFalse(report["ok"])
        failed = [check["name"] for check in report["checks"] if check["status"] == "fail"]
        self.assertIn("model_catalog", failed)
        self.assertIn("doctor --codex-ready", report["next_command"])

    def test_codex_ready_report_handles_account_load_failure(self):
        with TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            write_ready_codex_config(config_path, model="sonnet")
            with patch("codex_antigravity_auth.cli.gateway_model_ids", return_value={"claude-3.5-sonnet"}):
                with patch("codex_antigravity_auth.cli.load_accounts", side_effect=RuntimeError("account-store boom")):
                    report = codex_ready_report(
                        config=str(config_path),
                        provider_id="antigravity",
                        expected_base_url="http://localhost:51122/v1",
                    )

        self.assertFalse(report["ok"])
        rotation = next(check for check in report["checks"] if check["name"] == "google_rotation")
        self.assertEqual(rotation["status"], "fail")
        self.assertIn("Could not load Google account rotation state", rotation["detail"])
        self.assertIn("setup-google", report["next_command"])

    def test_codex_ready_report_handles_byok_provider_load_failure(self):
        with TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            write_ready_codex_config(config_path, model="deepseek:deepseek-chat")
            with patch("codex_antigravity_auth.cli.gateway_model_ids", return_value={"deepseek:deepseek-chat"}):
                with patch("codex_antigravity_auth.cli.all_provider_configs", side_effect=RuntimeError("providers boom")):
                    report = codex_ready_report(
                        config=str(config_path),
                        provider_id="antigravity",
                        expected_base_url="http://localhost:51122/v1",
                    )

        self.assertFalse(report["ok"])
        route_checks = [check for check in report["checks"] if check["name"] == "model_route"]
        self.assertEqual(len(route_checks), 1)
        self.assertEqual(route_checks[0]["status"], "fail")
        self.assertIn("Could not load BYOK provider configuration", route_checks[0]["detail"])

    def test_codex_ready_report_includes_live_generation_when_requested(self):
        with TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            write_ready_codex_config(config_path, model="sonnet")
            probe = {
                "ok": True,
                "model": "claude-3.5-sonnet",
                "latency_ms": 12,
                "output_preview": "ready",
                "http_status": 200,
                "error": None,
            }
            with patch("codex_antigravity_auth.cli.gateway_model_ids", return_value={"claude-3.5-sonnet"}):
                with patch("codex_antigravity_auth.cli.load_accounts", return_value={"accounts": [{"email": "a@example.com"}]}):
                    with patch("codex_antigravity_auth.cli.gateway_generate_probe", return_value=probe) as live_probe:
                        with patch("codex_antigravity_auth.cli.version_check_result", return_value={"status": "skip", "detail": "skip", "installed": None, "latest": None}):
                            report = codex_ready_report(
                                config=str(config_path),
                                provider_id="antigravity",
                                expected_base_url="http://localhost:51122/v1",
                                live=True,
                                live_timeout=7,
                            )

        live_probe.assert_called_once_with(
            "http://localhost:51122/v1",
            "claude-3.5-sonnet",
            timeout=7,
            token_env="ANTIGRAVITY_GATEWAY_TOKEN",
        )
        live_check = next(check for check in report["checks"] if check["name"] == "live_generation")
        self.assertEqual(live_check["status"], "pass")
        self.assertEqual(live_check["probe"], probe)

    def test_codex_ready_report_fails_live_generation_on_empty_output(self):
        with TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            write_ready_codex_config(config_path, model="sonnet")
            probe = {
                "ok": True,
                "model": "claude-3.5-sonnet",
                "latency_ms": 12,
                "output_preview": "",
                "http_status": 200,
                "error": None,
            }
            with patch("codex_antigravity_auth.cli.gateway_model_ids", return_value={"claude-3.5-sonnet"}):
                with patch("codex_antigravity_auth.cli.load_accounts", return_value={"accounts": [{"email": "a@example.com"}]}):
                    with patch("codex_antigravity_auth.cli.gateway_generate_probe", return_value=probe):
                        with patch("codex_antigravity_auth.cli.version_check_result", return_value={"status": "skip", "detail": "skip", "installed": None, "latest": None}):
                            report = codex_ready_report(
                                config=str(config_path),
                                provider_id="antigravity",
                                expected_base_url="http://localhost:51122/v1",
                                live=True,
                            )

        live_check = next(check for check in report["checks"] if check["name"] == "live_generation")
        self.assertEqual(live_check["status"], "fail")
        self.assertIn("empty output", live_check["detail"])

    def test_codex_ready_report_rejects_byok_live_model_before_generation(self):
        with TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            write_ready_codex_config(config_path, model="deepseek:deepseek-chat")
            provider = {
                "id": "deepseek",
                "displayName": "DeepSeek",
                "baseUrl": "https://api.deepseek.com",
                "apiKey": "secret",
                "models": ["deepseek-chat"],
            }
            with patch("codex_antigravity_auth.cli.gateway_model_ids", return_value={"deepseek:deepseek-chat"}):
                with patch("codex_antigravity_auth.cli.all_provider_configs", return_value={"deepseek": provider}):
                    with patch("codex_antigravity_auth.cli.gateway_generate_probe") as live_probe:
                        with patch("codex_antigravity_auth.cli.version_check_result", return_value={"status": "skip", "detail": "skip", "installed": None, "latest": None}):
                            report = codex_ready_report(
                                config=str(config_path),
                                provider_id="antigravity",
                                expected_base_url="http://localhost:51122/v1",
                                live=True,
                            )

        live_probe.assert_not_called()
        live_check = next(check for check in report["checks"] if check["name"] == "live_generation")
        self.assertEqual(live_check["status"], "fail")
        self.assertIn("Google Antigravity models only", live_check["detail"])

    def test_gateway_status_reports_stale_pid(self):
        with TemporaryDirectory() as tmp:
            pid_file = Path(tmp) / "antigravity-gateway-51122.pid"
            pid_file.write_text("999999\n", encoding="utf-8")
            with patch("codex_antigravity_auth.cli.get_codex_home", return_value=Path(tmp)):
                with patch("codex_antigravity_auth.cli.process_is_running", return_value=False):
                    info = gateway_status_info(51122)

        self.assertEqual(info["status"], "stale")
        self.assertFalse(info["running"])

    def test_gateway_status_reports_foreign_pid(self):
        with TemporaryDirectory() as tmp:
            pid_file = Path(tmp) / "antigravity-gateway-51122.pid"
            pid_file.write_text("12345\n", encoding="utf-8")
            with patch("codex_antigravity_auth.cli.get_codex_home", return_value=Path(tmp)):
                with patch("codex_antigravity_auth.cli.process_is_running", return_value=True):
                    with patch("codex_antigravity_auth.cli.gateway_pid_matches", return_value=False):
                        info = gateway_status_info(51122)

        self.assertEqual(info["status"], "foreign")
        self.assertFalse(info["running"])
        self.assertTrue(info["process_running"])
        self.assertFalse(info["process_matches"])

    def test_run_gateway_status_reports_unmanaged_reachable_gateway(self):
        with TemporaryDirectory() as tmp:
            with patch("codex_antigravity_auth.cli.get_codex_home", return_value=Path(tmp)):
                with patch("codex_antigravity_auth.cli.gateway_model_ids", return_value={"claude-3.5-sonnet"}):
                    with patch("codex_antigravity_auth.cli.service_status", return_value={"installed": False, "active": False}):
                        with patch("codex_antigravity_auth.cli.request_log_info", return_value={"path": "requests.jsonl"}):
                            with patch("builtins.print"):
                                info = run_gateway_status(Namespace(port=51122, json=False))

        self.assertEqual(info["status"], "unmanaged")
        self.assertTrue(info["reachable"])
        self.assertEqual(info["reachable_model_count"], 1)

    def test_codex_ready_treats_unmanaged_reachable_gateway_as_process_ready(self):
        with TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            write_ready_codex_config(config_path, model="claude-3.5-sonnet")
            with patch("codex_antigravity_auth.cli.get_codex_home", return_value=Path(tmp)):
                with patch("codex_antigravity_auth.cli.gateway_model_ids", return_value={"claude-3.5-sonnet"}):
                    with patch("codex_antigravity_auth.cli.service_status", return_value={"installed": False, "active": False}):
                        with patch("codex_antigravity_auth.cli.load_accounts", return_value={"accounts": [{"email": "a@example.com"}]}):
                            with patch("codex_antigravity_auth.cli.version_check_result", return_value={"status": "skip", "detail": "skip", "installed": None, "latest": None}):
                                report = codex_ready_report(
                                    config=str(config_path),
                                    provider_id="antigravity",
                                    expected_base_url="http://localhost:51122/v1",
                                )

        process_check = next(check for check in report["checks"] if check["name"] == "gateway_process")
        self.assertEqual(process_check["status"], "pass")
        self.assertTrue(process_check["reachable"])
        self.assertIn("without a managed pid", process_check["detail"])

    def test_stop_gateway_removes_stale_pid_without_killing_unrelated_process(self):
        with TemporaryDirectory() as tmp:
            pid_file = Path(tmp) / "antigravity-gateway-51122.pid"
            pid_file.write_text("999999\n", encoding="utf-8")
            with patch("codex_antigravity_auth.cli.get_codex_home", return_value=Path(tmp)):
                with patch("codex_antigravity_auth.cli.process_is_running", return_value=False):
                    with patch("codex_antigravity_auth.cli.os.kill") as kill:
                        with patch("builtins.print"):
                            info = stop_gateway(Namespace(port=51122))

                        self.assertEqual(info["status"], "stopped")
                        self.assertFalse(pid_file.exists())
                        kill.assert_not_called()

    def test_stop_gateway_refuses_foreign_pid(self):
        with TemporaryDirectory() as tmp:
            pid_file = Path(tmp) / "antigravity-gateway-51122.pid"
            pid_file.write_text("12345\n", encoding="utf-8")
            with patch("codex_antigravity_auth.cli.get_codex_home", return_value=Path(tmp)):
                with patch("codex_antigravity_auth.cli.process_is_running", return_value=True):
                    with patch("codex_antigravity_auth.cli.gateway_pid_matches", return_value=False):
                        with patch("codex_antigravity_auth.cli.os.kill") as kill:
                            with self.assertRaisesRegex(SystemExit, "Refusing"):
                                stop_gateway(Namespace(port=51122))
                            self.assertTrue(pid_file.exists())
                            kill.assert_not_called()

    @unittest.skipIf(sys.platform == "win32", "POSIX signal stop behavior")
    def test_stop_gateway_handles_pid_exiting_before_sigterm(self):
        with TemporaryDirectory() as tmp:
            pid_file = Path(tmp) / "antigravity-gateway-51122.pid"
            pid_file.write_text("12345\n", encoding="utf-8")
            with patch("codex_antigravity_auth.cli.get_codex_home", return_value=Path(tmp)):
                with patch("codex_antigravity_auth.cli.process_is_running", return_value=True):
                    with patch("codex_antigravity_auth.cli.gateway_pid_matches", return_value=True):
                        with patch("codex_antigravity_auth.cli.os.kill", side_effect=ProcessLookupError):
                            with patch("builtins.print"):
                                info = stop_gateway(Namespace(port=51122))
                            self.assertEqual(info["status"], "stopped")
                            self.assertFalse(pid_file.exists())

    @unittest.skipIf(sys.platform == "win32", "POSIX signal stop behavior")
    def test_stop_gateway_preserves_pid_file_on_permission_error(self):
        with TemporaryDirectory() as tmp:
            pid_file = Path(tmp) / "antigravity-gateway-51122.pid"
            pid_file.write_text("12345\n", encoding="utf-8")
            with patch("codex_antigravity_auth.cli.get_codex_home", return_value=Path(tmp)):
                with patch("codex_antigravity_auth.cli.process_is_running", return_value=True):
                    with patch("codex_antigravity_auth.cli.gateway_pid_matches", return_value=True):
                        with patch("codex_antigravity_auth.cli.os.kill", side_effect=PermissionError):
                            with self.assertRaisesRegex(SystemExit, "permission"):
                                stop_gateway(Namespace(port=51122))
                            self.assertTrue(pid_file.exists())

    @unittest.skipIf(sys.platform == "win32", "POSIX signal stop behavior")
    def test_stop_gateway_sends_sigterm_for_running_pid(self):
        with TemporaryDirectory() as tmp:
            pid_file = Path(tmp) / "antigravity-gateway-51122.pid"
            pid_file.write_text("12345\n", encoding="utf-8")
            states = iter([True, False, False])

            def fake_running(pid):
                return next(states)

            with patch("codex_antigravity_auth.cli.get_codex_home", return_value=Path(tmp)):
                with patch("codex_antigravity_auth.cli.process_is_running", side_effect=fake_running):
                    with patch("codex_antigravity_auth.cli.gateway_pid_matches", return_value=True):
                        with patch("codex_antigravity_auth.cli.os.kill") as kill:
                            with patch("builtins.print"):
                                info = stop_gateway(Namespace(port=51122))

        self.assertEqual(info["status"], "stopped")
        self.assertFalse(pid_file.exists())
        kill.assert_called_once_with(12345, signal.SIGTERM)

    def test_start_background_refuses_foreign_pid_file(self):
        with TemporaryDirectory() as tmp:
            pid_file = Path(tmp) / "antigravity-gateway-51122.pid"
            pid_file.write_text("12345\n", encoding="utf-8")
            with patch("codex_antigravity_auth.cli.get_codex_home", return_value=Path(tmp)):
                with patch("codex_antigravity_auth.cli.process_is_running", return_value=True):
                    with patch("codex_antigravity_auth.cli.gateway_pid_matches", return_value=False):
                        with patch("codex_antigravity_auth.cli.subprocess.Popen") as popen:
                            with self.assertRaisesRegex(SystemExit, "Refusing"):
                                start_gateway_background(Namespace(host="127.0.0.1", port=51122, allow_remote=False))
                            self.assertTrue(pid_file.exists())
                            popen.assert_not_called()

    def test_start_background_refuses_already_reachable_gateway_without_pid_file(self):
        with TemporaryDirectory() as tmp:
            with patch("codex_antigravity_auth.cli.get_codex_home", return_value=Path(tmp)):
                with patch("codex_antigravity_auth.cli.gateway_model_ids", return_value={"claude-3.5-sonnet"}):
                    with patch("codex_antigravity_auth.cli.subprocess.Popen") as popen:
                        with self.assertRaisesRegex(SystemExit, "already reachable"):
                            start_gateway_background(Namespace(host="127.0.0.1", port=51122, allow_remote=False))
                        popen.assert_not_called()

    def test_start_background_writes_pid_and_log_paths(self):
        proc = MagicMock()
        proc.pid = 12345
        proc.poll.return_value = None
        with TemporaryDirectory() as tmp:
            with patch("codex_antigravity_auth.cli.get_codex_home", return_value=Path(tmp)):
                with patch("codex_antigravity_auth.cli.gateway_model_ids", side_effect=RuntimeError("not ready")):
                    with patch("codex_antigravity_auth.cli.wait_for_gateway_model_ids", return_value={"claude-3.5-sonnet"}):
                        with patch("codex_antigravity_auth.cli.subprocess.Popen", return_value=proc) as popen:
                            with patch("codex_antigravity_auth.cli.process_is_running", return_value=True):
                                with patch("codex_antigravity_auth.cli.gateway_pid_matches", return_value=True):
                                    with patch("codex_antigravity_auth.cli.time.sleep"):
                                        with patch("builtins.print"):
                                            info = start_gateway_background(
                                                Namespace(host="127.0.0.1", port=51122, allow_remote=False)
                                            )

            pid_file = Path(tmp) / "antigravity-gateway-51122.pid"
            log_file = Path(tmp) / "antigravity-gateway-51122.log"
            self.assertEqual(pid_file.read_text(encoding="utf-8"), "12345\n")
            assert_mode_if_posix(self, log_file, 0o600)
            self.assertEqual(info["pid_file"], str(pid_file))
            self.assertEqual(info["log_file"], str(log_file))
            popen.assert_called_once()

    def test_start_background_removes_pid_and_terminates_when_readiness_fails(self):
        proc = MagicMock()
        proc.pid = 12345
        proc.poll.return_value = None
        with TemporaryDirectory() as tmp:
            pid_file = Path(tmp) / "antigravity-gateway-51122.pid"
            with patch("codex_antigravity_auth.cli.get_codex_home", return_value=Path(tmp)):
                with patch("codex_antigravity_auth.cli.gateway_model_ids", side_effect=RuntimeError("not ready")):
                    with patch("codex_antigravity_auth.cli.wait_for_gateway_model_ids", side_effect=RuntimeError("still not ready")):
                        with patch("codex_antigravity_auth.cli.subprocess.Popen", return_value=proc):
                            with patch("codex_antigravity_auth.cli.time.sleep"):
                                with self.assertRaisesRegex(SystemExit, "did not become ready"):
                                    start_gateway_background(
                                        Namespace(host="127.0.0.1", port=51122, allow_remote=False)
                                    )
            self.assertFalse(pid_file.exists())
            proc.terminate.assert_called_once()

    def test_start_background_wraps_gateway_with_onepassword_env_file(self):
        proc = MagicMock()
        proc.pid = 12345
        proc.poll.return_value = None
        with TemporaryDirectory() as tmp:
            env_file = Path(tmp) / "antigravity.env"
            env_file.write_text("OPENROUTER_API_KEY=op://Private/OpenRouter/sk\n", encoding="utf-8")
            with patch("codex_antigravity_auth.cli.get_codex_home", return_value=Path(tmp)):
                with patch("codex_antigravity_auth.onepassword.shutil.which", return_value="/usr/local/bin/op"):
                    with patch("codex_antigravity_auth.cli.gateway_model_ids", side_effect=RuntimeError("not ready")):
                        with patch("codex_antigravity_auth.cli.wait_for_gateway_model_ids", return_value={"claude-3.5-sonnet"}):
                            with patch("codex_antigravity_auth.cli.subprocess.Popen", return_value=proc) as popen:
                                with patch("codex_antigravity_auth.cli.process_is_running", return_value=True):
                                    with patch("codex_antigravity_auth.cli.gateway_pid_matches", return_value=True):
                                        with patch("codex_antigravity_auth.cli.time.sleep"):
                                            with patch("builtins.print"):
                                                start_gateway_background(
                                                    Namespace(
                                                        host="127.0.0.1",
                                                        port=51122,
                                                        allow_remote=False,
                                                        op_env_file=str(env_file),
                                                        op_environment=None,
                                                    )
                                                )

            cmd = popen.call_args.args[0]
            self.assertEqual(cmd[:4], ["/usr/local/bin/op", "run", "--env-file", str(env_file)])
            self.assertIn("--", cmd)
            self.assertIn("uvicorn", cmd)
            self.assertIn("codex_antigravity_auth.server:app", cmd)

    def test_start_background_rejects_onepassword_when_op_missing_before_popen(self):
        proc = MagicMock()
        with TemporaryDirectory() as tmp:
            env_file = Path(tmp) / "antigravity.env"
            env_file.write_text("OPENROUTER_API_KEY=op://Private/OpenRouter/sk\n", encoding="utf-8")
            with patch("codex_antigravity_auth.cli.get_codex_home", return_value=Path(tmp)):
                with patch("codex_antigravity_auth.onepassword.shutil.which", return_value=None):
                    with patch("codex_antigravity_auth.cli.gateway_model_ids", side_effect=RuntimeError("not ready")):
                        with patch("codex_antigravity_auth.cli.subprocess.Popen", return_value=proc) as popen:
                            with self.assertRaisesRegex(SystemExit, "1Password CLI"):
                                start_gateway_background(
                                    Namespace(
                                        host="127.0.0.1",
                                        port=51122,
                                        allow_remote=False,
                                        op_env_file=str(env_file),
                                        op_environment=None,
                                    )
                                )

            self.assertFalse((Path(tmp) / "antigravity-gateway-51122.pid").exists())
            popen.assert_not_called()

    def test_start_background_rejects_conflicting_onepassword_modes(self):
        proc = MagicMock()
        with TemporaryDirectory() as tmp:
            env_file = Path(tmp) / "antigravity.env"
            env_file.write_text("OPENROUTER_API_KEY=op://Private/OpenRouter/sk\n", encoding="utf-8")
            with patch("codex_antigravity_auth.cli.get_codex_home", return_value=Path(tmp)):
                with patch("codex_antigravity_auth.cli.gateway_model_ids", side_effect=RuntimeError("not ready")):
                    with patch("codex_antigravity_auth.cli.subprocess.Popen", return_value=proc) as popen:
                        with self.assertRaisesRegex(SystemExit, "Use only one"):
                            start_gateway_background(
                                Namespace(
                                    host="127.0.0.1",
                                    port=51122,
                                    allow_remote=False,
                                    op_env_file=str(env_file),
                                    op_environment="abcdefgh",
                                )
                            )
            popen.assert_not_called()


class TestProviderCli(unittest.TestCase):
    def test_provider_key_status_validates_keys_without_rendering_secrets(self):
        self.assertEqual(provider_key_status({"apiKey": " secret "}, configured_label="configured"), "configured")
        self.assertEqual(provider_key_status({}, configured_label="configured"), "missing key")
        self.assertEqual(provider_key_status({"apiKey": "secret\nbad"}, configured_label="configured"), "malformed key")

    def test_provider_list_reports_malformed_api_key_without_secret(self):
        argv = [
            "codex-antigravity",
            "provider",
            "list",
        ]
        provider = {
            "displayName": "DeepSeek",
            "baseUrl": "https://api.deepseek.com",
            "apiKey": "secret\nbad",
            "models": ["deepseek-chat"],
        }
        with patch.object(sys, "argv", argv):
            with patch("codex_antigravity_auth.cli.all_provider_configs", return_value={"deepseek": provider}):
                with patch("builtins.print") as mock_print:
                    main()

        printed_args = [call[0][0] for call in mock_print.call_args_list if call[0]]
        printed_text = "\n".join(printed_args)
        self.assertIn("malformed key", printed_text)
        self.assertNotIn("secret", printed_text)

    def test_provider_list_labels_implicit_loopback_provider_as_local_preset(self):
        argv = ["codex-antigravity", "provider", "list"]
        provider = {
            "id": "ollama",
            "displayName": "Ollama",
            "baseUrl": "http://localhost:11434/v1",
            "apiKeyOptional": True,
            "defaultApiKey": "ollama",
            "models": ["gpt-oss:20b"],
        }

        with patch.object(sys, "argv", argv):
            with patch("codex_antigravity_auth.cli.all_provider_configs", return_value={"ollama": provider}):
                with patch("codex_antigravity_auth.cli.load_provider_config", return_value={"providers": {}}):
                    with patch("builtins.print") as mock_print:
                        main()

        printed_text = "\n".join(call[0][0] for call in mock_print.call_args_list if call[0])
        self.assertIn("Ollama (local preset)", printed_text)
        self.assertNotIn("Ollama (configured)", printed_text)

    def test_provider_set_requires_base_url_for_new_custom_provider_without_traceback(self):
        argv = [
            "codex-antigravity",
            "provider",
            "set",
            "custom-one",
            "--model",
            "m",
        ]
        with patch.object(sys, "argv", argv):
            with patch("codex_antigravity_auth.byok.load_provider_config", return_value={"providers": {}}):
                with patch("codex_antigravity_auth.byok.update_secure_json_file") as mock_update:
                    with self.assertRaisesRegex(SystemExit, "base URL is required"):
                        main()

        mock_update.assert_not_called()

    def test_provider_set_reports_invalid_base_url_without_traceback(self):
        argv = [
            "codex-antigravity",
            "provider",
            "set",
            "custom-one",
            "--base-url",
            "localhost:8000/v1",
            "--model",
            "m",
        ]
        with patch.object(sys, "argv", argv):
            with patch("codex_antigravity_auth.byok.update_secure_json_file") as mock_update:
                with self.assertRaisesRegex(SystemExit, "absolute http\\(s\\) URL"):
                    main()

        mock_update.assert_not_called()

    def test_provider_set_reports_reserved_header_without_traceback(self):
        argv = [
            "codex-antigravity",
            "provider",
            "set",
            "deepseek",
            "--header",
            "Authorization:Bearer override",
        ]
        with patch.object(sys, "argv", argv):
            with patch("codex_antigravity_auth.byok.update_secure_json_file") as mock_update:
                with self.assertRaisesRegex(SystemExit, "must not override"):
                    main()

        mock_update.assert_not_called()

    def test_provider_set_reports_models_hidden_when_env_key_is_missing(self):
        argv = [
            "codex-antigravity",
            "provider",
            "set",
            "deepseek",
            "--api-key-env",
            "MISSING_DEEPSEEK_KEY",
            "--model",
            "deepseek-chat",
        ]
        provider = {
            "id": "deepseek",
            "displayName": "DeepSeek",
            "baseUrl": "https://api.deepseek.com",
            "apiKeyEnv": "MISSING_DEEPSEEK_KEY",
            "models": ["deepseek-chat"],
        }

        with patch.object(sys, "argv", argv):
            with patch.dict(os.environ, {}, clear=True):
                with patch("codex_antigravity_auth.cli.set_provider_config", return_value=provider):
                    with patch("builtins.print") as mock_print:
                        main()

        printed_text = "\n".join(call[0][0] for call in mock_print.call_args_list if call[0])
        self.assertIn("hidden until MISSING_DEEPSEEK_KEY", printed_text)
        self.assertNotIn("Exposed models", printed_text)

    def test_provider_set_reports_malformed_header_without_traceback(self):
        argv = [
            "codex-antigravity",
            "provider",
            "set",
            "deepseek",
            "--header",
            "X-Test:bad\x00value",
        ]
        with patch.object(sys, "argv", argv):
            with patch("codex_antigravity_auth.byok.update_secure_json_file") as mock_update:
                with self.assertRaisesRegex(SystemExit, "header values must be non-empty and must not contain control characters"):
                    main()

        mock_update.assert_not_called()

    def test_provider_set_reports_malformed_api_key_without_traceback(self):
        argv = [
            "codex-antigravity",
            "provider",
            "set",
            "deepseek",
            "--api-key",
            "secret\nbad",
        ]
        with patch.object(sys, "argv", argv):
            with patch("codex_antigravity_auth.byok.update_secure_json_file") as mock_update:
                with self.assertRaisesRegex(SystemExit, "API key must not contain control characters"):
                    main()

        mock_update.assert_not_called()

    def test_provider_set_reports_malformed_model_id_without_traceback(self):
        argv = [
            "codex-antigravity",
            "provider",
            "set",
            "deepseek",
            "--model",
            "bad\nmodel",
        ]
        with patch.object(sys, "argv", argv):
            with patch("codex_antigravity_auth.byok.update_secure_json_file") as mock_update:
                with self.assertRaisesRegex(SystemExit, "model ids must not contain whitespace or control characters"):
                    main()

        mock_update.assert_not_called()

    def test_provider_set_reports_malformed_display_name_without_traceback(self):
        argv = [
            "codex-antigravity",
            "provider",
            "set",
            "deepseek",
            "--display-name",
            "Deep\nSeek",
        ]
        with patch.object(sys, "argv", argv):
            with patch("codex_antigravity_auth.byok.update_secure_json_file") as mock_update:
                with self.assertRaisesRegex(SystemExit, "display name must not contain control characters"):
                    main()

        mock_update.assert_not_called()

    def test_provider_set_reports_malformed_api_key_env_without_traceback(self):
        argv = [
            "codex-antigravity",
            "provider",
            "set",
            "deepseek",
            "--api-key-env",
            "BAD-ENV",
        ]
        with patch.object(sys, "argv", argv):
            with patch("codex_antigravity_auth.byok.update_secure_json_file") as mock_update:
                with self.assertRaisesRegex(SystemExit, "env var name"):
                    main()

        mock_update.assert_not_called()

    def test_provider_set_reports_storage_failure_without_traceback(self):
        argv = [
            "codex-antigravity",
            "provider",
            "set",
            "deepseek",
            "--model",
            "deepseek-chat",
        ]
        with patch.object(sys, "argv", argv):
            with patch("codex_antigravity_auth.cli.set_provider_config", side_effect=RuntimeError("provider store locked")):
                with self.assertRaisesRegex(SystemExit, "provider store locked"):
                    main()

    def test_provider_set_redacts_secret_from_storage_failure(self):
        argv = [
            "codex-antigravity",
            "provider",
            "set",
            "deepseek",
            "--api-key",
            "sk-testsecret1234567890",
            "--model",
            "deepseek-chat",
        ]
        with patch.object(sys, "argv", argv):
            with patch(
                "codex_antigravity_auth.cli.set_provider_config",
                side_effect=RuntimeError("failed to write api_key=sk-testsecret1234567890"),
            ):
                with self.assertRaises(SystemExit) as raised:
                    main()

        self.assertNotIn("sk-testsecret1234567890", str(raised.exception))
        self.assertIn("[REDACTED]", str(raised.exception))

    def test_provider_remove_reports_storage_failure_without_traceback(self):
        argv = [
            "codex-antigravity",
            "provider",
            "remove",
            "deepseek",
        ]
        with patch.object(sys, "argv", argv):
            with patch("codex_antigravity_auth.cli.remove_provider_config", side_effect=RuntimeError("provider store locked")):
                with self.assertRaisesRegex(SystemExit, "provider store locked"):
                    main()


class TestVNextPolishCli(unittest.TestCase):
    def test_new_command_parsers_dispatch(self):
        command_cases = [
            (["codex-antigravity", "service", "status", "--json"], "run_service_command"),
            (["codex-antigravity", "logs", "--tail", "1", "--json"], "run_logs_command"),
            (["codex-antigravity", "logs", "summary", "--json"], "run_logs_command"),
            (["codex-antigravity", "accounts", "list"], "run_accounts_command"),
            (["codex-antigravity", "accounts", "remove", "a@example.com", "--yes"], "run_accounts_command"),
            (["codex-antigravity", "accounts", "reset", "a@example.com"], "run_accounts_command"),
            (["codex-antigravity", "models", "list", "--json"], "run_models_command"),
        ]
        for argv, handler_name in command_cases:
            with self.subTest(argv=argv):
                with patch.object(sys, "argv", argv):
                    with patch(f"codex_antigravity_auth.cli.{handler_name}") as mock_handler:
                        main()
                mock_handler.assert_called_once()

    def test_setup_repair_writes_only_codex_config(self):
        with TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            args = Namespace(
                check=False,
                json=False,
                write=False,
                repair=True,
                accounts=1,
                model="sonnet",
                provider="antigravity",
                provider_name="Google Antigravity",
                base_url=None,
                config=str(config_path),
                install_skill=True,
                skill_dir=str(Path(tmp) / "skills"),
                force=False,
                verify_skill=False,
                start=True,
                port=51122,
                host="127.0.0.1",
                allow_remote=False,
                gateway_timeout=0.01,
                gateway_token_env="ANTIGRAVITY_GATEWAY_TOKEN",
            )
            with patch("codex_antigravity_auth.cli.run_configure_codex") as mock_configure:
                with patch("codex_antigravity_auth.cli.codex_ready_report", return_value={"ok": True, "checks": [], "next_command": "codex"}):
                    with patch("codex_antigravity_auth.cli.run_login") as mock_login:
                        with patch("codex_antigravity_auth.cli.run_install_skill") as mock_install:
                            with patch("codex_antigravity_auth.cli.start_gateway_background") as mock_start:
                                with patch("builtins.print"):
                                    report = run_setup(args)

        self.assertTrue(report["ok"])
        self.assertEqual(report["mode"], "repair")
        mock_configure.assert_called_once()
        mock_login.assert_not_called()
        mock_install.assert_not_called()
        mock_start.assert_not_called()

    def test_codex_ready_report_suggests_repair_for_existing_config_drift(self):
        with TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.toml"
            config_path.write_text('model = "claude-3.5-sonnet"\n', encoding="utf-8")
            with patch("codex_antigravity_auth.cli.gateway_model_ids", return_value={"claude-3.5-sonnet"}):
                with patch("codex_antigravity_auth.cli.load_accounts", return_value={"accounts": [{"email": "a@example.com"}]}):
                    report = codex_ready_report(
                        config=str(config_path),
                        provider_id="antigravity",
                        expected_base_url="http://localhost:51122/v1",
                    )

        self.assertFalse(report["ok"])
        self.assertEqual(report["checks"][0]["name"], "codex_config")
        self.assertEqual(report["next_command"], "codex-antigravity setup --repair")

    def test_codex_ready_report_suggests_write_when_config_missing(self):
        with TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "missing.toml"
            with patch("codex_antigravity_auth.cli.gateway_model_ids", return_value={"claude-3.5-sonnet"}):
                report = codex_ready_report(
                    config=str(config_path),
                    provider_id="antigravity",
                    expected_base_url="http://localhost:51122/v1",
                    include_version_check=False,
                )

        self.assertFalse(report["ok"])
        self.assertIn("setup --write", report["next_command"])

    def test_setup_repair_and_write_are_mutually_exclusive(self):
        args = Namespace(
            check=False,
            json=False,
            write=True,
            repair=True,
            accounts=1,
            model="sonnet",
            provider="antigravity",
            provider_name="Google Antigravity",
            base_url=None,
            config="config.toml",
            install_skill=True,
            skill_dir="skills",
            force=False,
            verify_skill=False,
            start=True,
            port=51122,
            host="127.0.0.1",
            allow_remote=False,
            gateway_timeout=0.01,
            gateway_token_env="ANTIGRAVITY_GATEWAY_TOKEN",
        )

        with self.assertRaisesRegex(SystemExit, "repair or --write"):
            run_setup(args)

    def test_service_manifests_render_user_gateway_commands(self):
        macos_plist = render_macos_launch_agent(51122, "127.0.0.1")
        linux_unit = render_linux_systemd_unit(51122, "127.0.0.1")

        self.assertIn("com.codex-antigravity.gateway.51122", macos_plist)
        self.assertIn("<key>KeepAlive</key>", macos_plist)
        self.assertIn("<key>SuccessfulExit</key>", macos_plist)
        self.assertIn("<false/>", macos_plist)
        self.assertIn("codex_antigravity_auth.cli", macos_plist)
        self.assertIn("Restart=on-failure", linux_unit)
        self.assertIn("codex_antigravity_auth.cli", linux_unit)

    def test_service_command_can_wrap_gateway_with_onepassword_env_file(self):
        with TemporaryDirectory() as tmp:
            env_file = Path(tmp) / "antigravity.env"
            env_file.write_text("OPENROUTER_API_KEY=op://Private/OpenRouter/sk\n", encoding="utf-8")
            with patch("codex_antigravity_auth.onepassword.shutil.which", return_value="/usr/local/bin/op"):
                command = service_command(51122, "127.0.0.1", op_env_file=str(env_file))

        self.assertEqual(command[:4], ["/usr/local/bin/op", "run", "--env-file", str(env_file)])
        self.assertIn("--", command)
        self.assertIn("codex_antigravity_auth.cli", command)
        self.assertIn("start", command)

    def test_service_command_rejects_onepassword_when_op_missing(self):
        with TemporaryDirectory() as tmp:
            env_file = Path(tmp) / "antigravity.env"
            env_file.write_text("OPENROUTER_API_KEY=op://Private/OpenRouter/sk\n", encoding="utf-8")
            with patch("codex_antigravity_auth.onepassword.shutil.which", return_value=None):
                with self.assertRaisesRegex(ValueError, "1Password CLI"):
                    service_command(51122, "127.0.0.1", op_env_file=str(env_file))

    def test_service_install_rejects_onepassword_without_manifest_when_op_missing(self):
        with TemporaryDirectory() as tmp:
            env_file = Path(tmp) / "antigravity.env"
            env_file.write_text("OPENROUTER_API_KEY=op://Private/OpenRouter/sk\n", encoding="utf-8")
            manifest = Path(tmp) / "gateway.plist"
            with patch("codex_antigravity_auth.service.macos_launch_agent_path", return_value=manifest):
                with patch("codex_antigravity_auth.onepassword.shutil.which", return_value=None):
                    with self.assertRaisesRegex(ValueError, "1Password CLI"):
                        install_service(
                            51122,
                            "127.0.0.1",
                            platform_name="macos",
                            op_env_file=str(env_file),
                        )

            self.assertFalse(manifest.exists())

    def test_version_check_reports_update_available_and_writes_cache(self):
        response = MagicMock()
        response.read.return_value = b'{"info":{"version":"9.9.9"}}'
        response.__enter__ = lambda self_: response
        response.__exit__ = lambda self_, *exc: False

        with TemporaryDirectory() as tmp:
            with patch("codex_antigravity_auth.cli.get_codex_home", return_value=Path(tmp)):
                with patch("codex_antigravity_auth.cli._source_checkout_version", return_value=None):
                    with patch("codex_antigravity_auth.cli.importlib_metadata.version", return_value="1.4.0"):
                        with patch("codex_antigravity_auth.cli.urllib.request.urlopen", return_value=response):
                            result = version_check_result(timeout=0.01)

            cache_path = Path(tmp) / "antigravity-version-check.json"
            assert_mode_if_posix(self, cache_path, 0o600)

        self.assertEqual(result["status"], "warn")
        self.assertEqual(result["installed"], "1.4.0")
        self.assertEqual(result["latest"], "9.9.9")

    def test_version_check_uses_fresh_cache_without_network(self):
        with TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / "antigravity-version-check.json"
            cache_path.write_text(
                json.dumps({"checked_at": time.time(), "latest": "1.4.0"}),
                encoding="utf-8",
            )
            with patch("codex_antigravity_auth.cli.get_codex_home", return_value=Path(tmp)):
                with patch("codex_antigravity_auth.cli._source_checkout_version", return_value=None):
                    with patch("codex_antigravity_auth.cli.importlib_metadata.version", return_value="1.4.0"):
                        with patch("codex_antigravity_auth.cli.urllib.request.urlopen") as urlopen:
                            result = version_check_result(timeout=0.01)

        self.assertEqual(result["status"], "pass")
        urlopen.assert_not_called()

    def test_version_check_can_be_disabled_by_env(self):
        with patch.dict(os.environ, {"CODEX_ANTIGRAVITY_NO_UPDATE_CHECK": "1"}):
            with patch("codex_antigravity_auth.cli.urllib.request.urlopen") as urlopen:
                result = version_check_result(timeout=0.01)

        self.assertEqual(result["status"], "skip")
        urlopen.assert_not_called()

    def test_version_check_skips_non_numeric_latest_version(self):
        response = MagicMock()
        response.read.return_value = b'{"info":{"version":"not-a-version"}}'
        response.__enter__ = lambda self_: response
        response.__exit__ = lambda self_, *exc: False

        with TemporaryDirectory() as tmp:
            with patch("codex_antigravity_auth.cli.get_codex_home", return_value=Path(tmp)):
                with patch("codex_antigravity_auth.cli._source_checkout_version", return_value=None):
                    with patch("codex_antigravity_auth.cli.importlib_metadata.version", return_value="1.4.0"):
                        with patch("codex_antigravity_auth.cli.urllib.request.urlopen", return_value=response):
                            result = version_check_result(timeout=0.01)

        self.assertEqual(result["status"], "skip")
        self.assertEqual(result["detail"], "version check unavailable")

    def test_version_check_prefers_source_checkout_version_over_stale_installed_dist(self):
        with TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / "antigravity-version-check.json"
            cache_path.write_text(
                json.dumps({"checked_at": time.time(), "latest": "1.5.0"}),
                encoding="utf-8",
            )
            with patch("codex_antigravity_auth.cli.get_codex_home", return_value=Path(tmp)):
                with patch("codex_antigravity_auth.cli._source_checkout_version", return_value="1.5.0"):
                    with patch("codex_antigravity_auth.cli.importlib_metadata.version", return_value="1.0.0"):
                        result = version_check_result(timeout=0.01)

        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["installed"], "1.5.0")

    def test_process_is_running_uses_tasklist_on_windows(self):
        proc = MagicMock()
        proc.returncode = 0
        proc.stdout = '"Image Name","PID","Session Name"\n"python.exe","1234","Console"\n'

        with patch("codex_antigravity_auth.cli.sys.platform", "win32"):
            with patch("codex_antigravity_auth.cli.subprocess.run", return_value=proc) as mock_run:
                with patch("codex_antigravity_auth.cli.os.kill") as mock_kill:
                    self.assertTrue(process_is_running(1234))

        mock_run.assert_called_once()
        self.assertEqual(mock_run.call_args.args[0][0], "tasklist")
        mock_kill.assert_not_called()

    def test_gateway_process_command_uses_windows_process_probe(self):
        proc = MagicMock()
        proc.returncode = 0
        proc.stdout = "CommandLine=python -m uvicorn codex_antigravity_auth.server:app\n"

        with patch("codex_antigravity_auth.cli.sys.platform", "win32"):
            with patch("codex_antigravity_auth.cli.subprocess.run", return_value=proc) as mock_run:
                command = gateway_process_command(1234)

        self.assertIn("codex_antigravity_auth.server:app", command)
        self.assertEqual(mock_run.call_args.args[0][0], "wmic")

    def test_stop_gateway_uses_taskkill_on_windows(self):
        with TemporaryDirectory() as tmp:
            pid_file = Path(tmp) / "antigravity-gateway-51122.pid"
            pid_file.write_text("12345\n", encoding="utf-8")
            running_info = {
                "port": 51122,
                "status": "running",
                "running": True,
                "pid": 12345,
                "pid_file": str(pid_file),
                "log_file": str(Path(tmp) / "antigravity-gateway-51122.log"),
                "process_running": True,
                "process_matches": True,
            }
            stopped_info = {**running_info, "status": "stopped", "running": False, "pid": None}
            proc = MagicMock()
            proc.returncode = 0
            with patch("codex_antigravity_auth.cli.sys.platform", "win32"):
                with patch("codex_antigravity_auth.cli.gateway_status_info", side_effect=[running_info, stopped_info]):
                    with patch("codex_antigravity_auth.cli.subprocess.run", return_value=proc) as mock_run:
                        with patch("codex_antigravity_auth.cli.process_is_running", side_effect=[False, False]):
                            with patch("builtins.print"):
                                info = stop_gateway(Namespace(port=51122))

        self.assertEqual(info["status"], "stopped")
        self.assertFalse(pid_file.exists())
        self.assertEqual(mock_run.call_args.args[0][:3], ["taskkill", "/PID", "12345"])

    def test_service_install_uses_windows_scheduled_task_command(self):
        proc = MagicMock()
        proc.returncode = 0
        proc.stdout = ""
        with patch("codex_antigravity_auth.service._run", return_value=proc) as mock_run:
            status = install_service(51122, "127.0.0.1", platform_name="windows")

        self.assertEqual(status["platform"], "windows")
        create_call = mock_run.call_args_list[0].args[0]
        self.assertEqual(create_call[:2], ["schtasks", "/Create"])
        self.assertIn("/TR", create_call)
        task_command = create_call[create_call.index("/TR") + 1]
        self.assertIn('"start"', task_command)
        self.assertIn('"--port"', task_command)

    def test_stop_hints_when_durable_service_is_installed(self):
        with TemporaryDirectory() as tmp:
            pid_file = Path(tmp) / "gateway.pid"
            info = {
                "port": 51122,
                "status": "stopped",
                "running": False,
                "pid": None,
                "pid_file": str(pid_file),
                "log_file": str(Path(tmp) / "gateway.log"),
                "process_running": False,
                "process_matches": None,
            }
            with patch("codex_antigravity_auth.cli.gateway_status_info", return_value=info):
                with patch("codex_antigravity_auth.cli.service_status", return_value={"installed": True}):
                    with patch("builtins.print") as mock_print:
                        stop_gateway(Namespace(port=51122))

        printed = "\n".join(call.args[0] for call in mock_print.call_args_list if call.args)
        self.assertIn("service uninstall --port 51122", printed)

    def test_service_status_uses_platform_specific_probe(self):
        with patch("codex_antigravity_auth.service._run") as mock_run:
            mock_run.return_value.returncode = 1
            status = service_status(51122, platform_name="windows")

        self.assertEqual(status["platform"], "windows")
        self.assertFalse(status["installed"])
        self.assertEqual(status["task_name"], "CodexAntigravityGateway51122")
        mock_run.assert_called_once()
        self.assertEqual(mock_run.call_args.args[0][0], "schtasks")

    def test_request_log_redacts_and_omits_prompt_body(self):
        with TemporaryDirectory() as tmp:
            with patch("codex_antigravity_auth.observability.get_codex_home", return_value=Path(tmp)):
                write_request_record(
                    {
                        "request_id": "req_test",
                        "run_id": "anti-run_123",
                        "model": "claude-3.5-sonnet",
                        "route": "google",
                        "status": "failed",
                        "prompt": "do not persist",
                        "body": {"input": "also secret"},
                        "error": "provider key sk-testsecret1234567890 failed",
                    },
                    max_bytes=1024,
                )
                records = list(iter_request_records(tail=10))

        serialized = json.dumps(records)
        self.assertIn("req_test", serialized)
        self.assertIn("anti-run_123", serialized)
        self.assertNotIn("do not persist", serialized)
        self.assertNotIn("also secret", serialized)
        self.assertNotIn("sk-testsecret1234567890", serialized)
        self.assertIn("[REDACTED]", serialized)

    def test_request_log_summary_aggregates_latency_errors_and_malformed_lines(self):
        now = 1_700_000_000.0

        def timestamp(age_seconds: int) -> str:
            return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - age_seconds))

        with TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "antigravity-requests.jsonl"
            log_path.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "timestamp": timestamp(60),
                                "request_id": "req_1",
                                "route": "google",
                                "family": "claude",
                                "status": "success",
                                "latency_ms": 100,
                                "http_status": 200,
                            }
                        ),
                        json.dumps(
                            {
                                "timestamp": timestamp(120),
                                "request_id": "req_2",
                                "route": "google",
                                "family": "claude",
                                "status": "failed",
                                "latency_ms": 900,
                                "http_status": 429,
                                "rotation_attempted": True,
                                "error_class": "rate_limited",
                            }
                        ),
                        json.dumps(
                            {
                                "timestamp": timestamp(30),
                                "request_id": "req_3",
                                "route": "byok",
                                "provider": "openrouter",
                                "status": "failed",
                                "latency_ms": 300,
                                "http_status": 500,
                                "error_class": "provider_error",
                            }
                        ),
                        json.dumps(
                            {
                                "timestamp": timestamp(200000),
                                "request_id": "old",
                                "route": "google",
                                "family": "claude",
                                "status": "success",
                                "latency_ms": 1,
                                "http_status": 200,
                            }
                        ),
                        "{not-json",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            with patch("codex_antigravity_auth.observability.get_codex_home", return_value=Path(tmp)):
                summary = request_log_summary(since="24h", now=now)

        google = summary["groups"]["google/claude"]
        self.assertEqual(summary["included_records"], 3)
        self.assertEqual(summary["excluded_by_time"], 1)
        self.assertEqual(summary["malformed_records"], 1)
        self.assertEqual(google["request_count"], 2)
        self.assertEqual(google["success_count"], 1)
        self.assertEqual(google["rate_limit_count"], 1)
        self.assertEqual(google["rotation_attempted_count"], 1)
        self.assertEqual(google["p50_latency_ms"], 100)
        self.assertEqual(google["p95_latency_ms"], 900)
        self.assertEqual(google["top_error_classes"], [{"error_class": "rate_limited", "count": 1}])

    def test_logs_summary_human_output_handles_empty_log(self):
        with TemporaryDirectory() as tmp:
            with patch("codex_antigravity_auth.observability.get_codex_home", return_value=Path(tmp)):
                with patch("builtins.print") as mock_print:
                    main_argv = ["codex-antigravity", "logs", "summary", "--since", "all"]
                    with patch.object(sys, "argv", main_argv):
                        main()

        printed = "\n".join(call.args[0] for call in mock_print.call_args_list if call.args)
        self.assertIn("No request log entries matched", printed)

    def test_remove_google_account_prunes_state_and_repairs_active_indices(self):
        data = {
            "accounts": [
                {"email": "a@example.com"},
                {"email": "b@example.com"},
                {"email": "c@example.com"},
            ],
            "activeIndex": 2,
            "activeIndexByFamily": {"claude": 1, "gemini": 2},
            "accountState": {
                "failures": {"b@example.com": 2, "c@example.com": 1},
                "cooldowns": {"b@example.com": 999, "c@example.com": 888},
                "counters": {"b@example.com": {"claude": {"total_requests": 2}}},
            },
        }

        with patch("codex_antigravity_auth.cli.update_accounts", side_effect=lambda mutator: mutator(data)):
            result = remove_google_account("b@example.com")

        self.assertEqual(result["account_count"], 2)
        self.assertEqual([account["email"] for account in data["accounts"]], ["a@example.com", "c@example.com"])
        self.assertEqual(data["activeIndex"], 1)
        self.assertEqual(data["activeIndexByFamily"], {"claude": 1, "gemini": 1})
        self.assertNotIn("b@example.com", data["accountState"]["failures"])
        self.assertNotIn("b@example.com", data["accountState"]["cooldowns"])
        self.assertNotIn("b@example.com", data["accountState"]["counters"])

    def test_reset_google_account_state_clears_persisted_cooldown_and_failures(self):
        data = {
            "accounts": [{"email": "a@example.com"}],
            "accountState": {
                "failures": {"a@example.com": 2},
                "cooldowns": {"a@example.com": 999},
                "counters": {"a@example.com": {"claude": {"total_requests": 2}}},
            },
        }

        with patch("codex_antigravity_auth.cli.update_accounts", side_effect=lambda mutator: mutator(data)):
            result = reset_google_account_state("a@example.com")

        self.assertEqual(result["cleared"], {"failures": 1, "cooldowns": 1})
        self.assertEqual(data["accountState"]["failures"], {})
        self.assertEqual(data["accountState"]["cooldowns"], {})
        self.assertIn("a@example.com", data["accountState"]["counters"])

    def test_accounts_remove_requires_yes_when_non_interactive(self):
        argv = ["codex-antigravity", "accounts", "remove", "a@example.com"]
        with patch.object(sys, "argv", argv):
            with patch("sys.stdin.isatty", return_value=False):
                with self.assertRaisesRegex(SystemExit, "--yes"):
                    main()

    def test_model_overlay_catalog_and_collision_rules(self):
        with TemporaryDirectory() as tmp:
            overlay_path = Path(tmp) / "antigravity-models.toml"
            with patch("codex_antigravity_auth.models.MODEL_OVERLAY_FILE", str(overlay_path)):
                argv = [
                    "codex-antigravity",
                    "models",
                    "add",
                    "claude-extra",
                    "--backend-id",
                    "claude-extra-backend",
                    "--display-name",
                    "Claude Extra",
                    "--family",
                    "claude",
                    "--context-window",
                    "200000",
                    "--alias",
                    "cextra",
                ]
                with patch.object(sys, "argv", argv):
                    with patch("builtins.print"):
                        main()

                by_id = {model["id"]: model for model in native_model_catalog()}
                self.assertEqual(by_id["claude-extra"]["backend_id"], "claude-extra-backend")
                self.assertIn("cextra", by_id["claude-extra"]["aliases"])

                collision_argv = [
                    "codex-antigravity",
                    "models",
                    "add",
                    "claude-3.5-sonnet",
                    "--backend-id",
                    "claude-sonnet-4-6",
                    "--family",
                    "claude",
                    "--context-window",
                    "200000",
                ]
                with patch.object(sys, "argv", collision_argv):
                    with self.assertRaisesRegex(SystemExit, "built-in model"):
                        main()

                force_argv = collision_argv + ["--display-name", "Forced Sonnet", "--force"]
                with patch.object(sys, "argv", force_argv):
                    with patch("builtins.print"):
                        main()
                forced = {model["id"]: model for model in native_model_catalog()}["claude-3.5-sonnet"]
                self.assertEqual(forced["display_name"], "Forced Sonnet")

    def test_model_overlay_rejects_identifier_shadowing(self):
        cases = [
            (
                "id shadows built-in alias",
                [
                    "sonnet",
                    "--backend-id",
                    "claude-custom-backend",
                    "--family",
                    "claude",
                    "--context-window",
                    "200000",
                ],
            ),
            (
                "backend id shadows built-in backend",
                [
                    "claude-extra",
                    "--backend-id",
                    "claude-sonnet-4-6",
                    "--family",
                    "claude",
                    "--context-window",
                    "200000",
                ],
            ),
            (
                "alias shadows built-in alias",
                [
                    "claude-extra",
                    "--backend-id",
                    "claude-custom-backend",
                    "--family",
                    "claude",
                    "--context-window",
                    "200000",
                    "--alias",
                    "sonnet",
                ],
            ),
        ]
        for _label, args_tail in cases:
            with self.subTest(args_tail=args_tail):
                with TemporaryDirectory() as tmp:
                    overlay_path = Path(tmp) / "antigravity-models.toml"
                    with patch("codex_antigravity_auth.models.MODEL_OVERLAY_FILE", str(overlay_path)):
                        argv = ["codex-antigravity", "models", "add", *args_tail]
                        with patch.object(sys, "argv", argv):
                            with self.assertRaisesRegex(SystemExit, "model identifiers shadow"):
                                main()

    def test_models_list_reports_malformed_overlay_cleanly(self):
        with TemporaryDirectory() as tmp:
            overlay_path = Path(tmp) / "antigravity-models.toml"
            overlay_path.write_text("not valid before table\n", encoding="utf-8")
            with patch("codex_antigravity_auth.models.MODEL_OVERLAY_FILE", str(overlay_path)):
                with patch.object(sys, "argv", ["codex-antigravity", "models", "list"]):
                    with self.assertRaisesRegex(SystemExit, "Invalid model overlay TOML|Unexpected content"):
                        main()

    def test_models_doctor_flags_manual_identifier_shadowing(self):
        with TemporaryDirectory() as tmp:
            overlay_path = Path(tmp) / "antigravity-models.toml"
            overlay_path.write_text(
                "\n".join(
                    [
                        "[[models]]",
                        'id = "claude-extra"',
                        'backend_id = "claude-extra-backend"',
                        'display_name = "Claude Extra"',
                        'family = "claude"',
                        "context_window = 200000",
                        'aliases = ["sonnet"]',
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            with patch("codex_antigravity_auth.models.MODEL_OVERLAY_FILE", str(overlay_path)):
                with patch.object(sys, "argv", ["codex-antigravity", "models", "doctor"]):
                    with patch("builtins.print") as mock_print:
                        with self.assertRaises(SystemExit) as raised:
                            main()

        self.assertEqual(raised.exception.code, 1)
        printed = "\n".join(call.args[0] for call in mock_print.call_args_list if call.args)
        self.assertIn("identifier shadowing detected", printed)

    def test_model_overlay_refuses_symlinked_file(self):
        with TemporaryDirectory() as tmp:
            target_path = Path(tmp) / "target.toml"
            target_path.write_text("", encoding="utf-8")
            overlay_path = Path(tmp) / "antigravity-models.toml"
            try:
                overlay_path.symlink_to(target_path)
            except (OSError, NotImplementedError):
                self.skipTest("symlink creation is not available")
            with patch("codex_antigravity_auth.models.MODEL_OVERLAY_FILE", str(overlay_path)):
                argv = [
                    "codex-antigravity",
                    "models",
                    "add",
                    "claude-extra",
                    "--backend-id",
                    "claude-sonnet-4-6",
                    "--family",
                    "claude",
                    "--context-window",
                    "200000",
                ]
                with patch.object(sys, "argv", argv):
                    with self.assertRaisesRegex(SystemExit, "symlinked model overlay"):
                        main()

if __name__ == "__main__":
    unittest.main()
