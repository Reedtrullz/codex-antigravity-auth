import unittest
import os
import stat
import sys
from argparse import Namespace
from pathlib import Path
from tempfile import TemporaryDirectory
import urllib.request
from urllib.parse import parse_qs, urlparse
from unittest.mock import patch, MagicMock
from codex_antigravity_auth.oauth import authorize_antigravity
from codex_antigravity_auth.cli import (
    account_rotation_lines,
    configure_codex_write_command,
    main,
    merge_codex_config,
    normalize_epoch_seconds,
    provider_key_status,
    render_codex_config_snippet,
    require_safe_gateway_host,
    run_configure_codex,
    run_doctor,
    run_login,
    run_setup_google,
    upsert_google_account,
    validate_codex_model_id,
    validate_codex_provider_name,
    write_codex_config,
)

class TestCliDoctor(unittest.TestCase):
    @patch("codex_antigravity_auth.cli.resolve_oauth_credentials")
    @patch("codex_antigravity_auth.cli.load_accounts")
    @patch("urllib.request.urlopen")
    def test_run_doctor_displays_accurate_information(self, mock_urlopen, mock_load, mock_creds):
        mock_creds.return_value = ("client_id_val", "client_secret_val")
        mock_load.return_value = {"accounts": []}
        
        # Mock successful network check
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_urlopen.return_value.__enter__.return_value = mock_resp
        
        with patch("builtins.print") as mock_print:
            run_doctor()
            
            # Extract printed strings
            printed_args = [call[0][0] for call in mock_print.call_args_list if call[0]]
            printed_text = "\n".join(printed_args)
            
            self.assertIn("Configured", printed_text)
            self.assertIn("Token Storage Encryption", printed_text)

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
        with patch("codex_antigravity_auth.cli.all_provider_configs", return_value={"deepseek": provider}):
            with patch("builtins.print") as mock_print:
                run_doctor()

        printed_args = [call[0][0] for call in mock_print.call_args_list if call[0]]
        printed_text = "\n".join(printed_args)
        self.assertIn("malformed key", printed_text)
        self.assertNotIn("secret", printed_text)

    def test_normalize_epoch_seconds_treats_non_finite_values_as_expired(self):
        for value in (float("nan"), float("inf"), "-inf"):
            with self.subTest(value=repr(value)):
                self.assertEqual(normalize_epoch_seconds(value), 0)


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
        with patch.dict("os.environ", {"ANTIGRAVITY_GATEWAY_TOKEN": "gateway-secret"}, clear=True):
            require_safe_gateway_host("0.0.0.0", allow_remote=True)
            self.assertEqual(os.environ["ANTIGRAVITY_ALLOW_REMOTE"], "1")


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


class TestConfigureCodex(unittest.TestCase):
    def test_render_codex_config_snippet_contains_gateway_provider(self):
        snippet = render_codex_config_snippet()

        self.assertIn('model = "gemini-3.5-flash-high"', snippet)
        self.assertIn('model_provider = "antigravity"', snippet)
        self.assertIn("[model_providers.antigravity]", snippet)
        self.assertIn('base_url = "http://localhost:51122/v1"', snippet)
        self.assertIn('wire_api = "responses"', snippet)

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
        self.assertIn('model = "gemini-3.5-flash-high"', merged)
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
            self.assertEqual(stat.S_IMODE(backup_path.stat().st_mode), 0o600)
            self.assertIn('model_provider = "antigravity"', config_path.read_text(encoding="utf-8"))
            self.assertEqual(stat.S_IMODE(config_path.stat().st_mode), 0o600)

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
            self.assertEqual(stat.S_IMODE(config_path.stat().st_mode), 0o600)

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
            self.assertEqual(stat.S_IMODE(first_backup.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(second_backup.stat().st_mode), 0o600)

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
            self.assertEqual(stat.S_IMODE(config_path.stat().st_mode), 0o644)
            self.assertEqual(observed_config_temp_modes, [0o600])
            backups = list(Path(tmp).glob("config.toml.bak-*"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_text(encoding="utf-8"), 'model = "gpt-5"\n')
            self.assertEqual(stat.S_IMODE(backups[0].stat().st_mode), 0o600)
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
            self.assertEqual(stat.S_IMODE(target_path.stat().st_mode), 0o600)
            self.assertIsNotNone(backup_path)
            self.assertEqual(backup_path.parent.resolve(), target_path.parent.resolve())
            self.assertEqual(backup_path.read_text(encoding="utf-8"), 'model = "gpt-5"\n')
            self.assertEqual(stat.S_IMODE(backup_path.stat().st_mode), 0o600)


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

if __name__ == "__main__":
    unittest.main()
