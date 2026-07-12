import unittest
import time
import tempfile
from pathlib import Path
from codex_antigravity_auth.accounts import AccountManager
from unittest.mock import patch

class TestAccounts(unittest.TestCase):
    def setUp(self):
        # Clear storage
        self.accounts_data = {
            "accounts": [
                {"email": "primary@gmail.com", "refreshToken": "ref_1", "accessToken": "acc_1", "expiresAt": time.time() + 1000},
                {"email": "secondary@gmail.com", "refreshToken": "ref_2", "accessToken": "acc_2", "expiresAt": time.time() + 1000}
            ],
            "activeIndex": 0,
            "activeIndexByFamily": {"claude": 0, "gemini": 0}
        }
        
    @patch("codex_antigravity_auth.accounts.update_accounts")
    def test_account_selection_happy_path(self, mock_update):
        mock_update.side_effect = lambda mutator: mutator(self.accounts_data)
        manager = AccountManager()
        
        # Select active account for Gemini
        selected = manager.select_active_account("gemini-3.5-flash-high")
        self.assertIsNotNone(selected)
        self.assertEqual(selected["email"], "primary@gmail.com")

    @patch("codex_antigravity_auth.accounts.update_accounts")
    def test_acquire_spreads_concurrent_requests_across_accounts(self, mock_update):
        mock_update.side_effect = lambda mutator: mutator(self.accounts_data)
        manager = AccountManager()

        first = manager.acquire_account("claude-3.5-sonnet")
        second = manager.acquire_account("claude-3.5-sonnet")

        self.assertEqual(first["email"], "primary@gmail.com")
        self.assertEqual(second["email"], "secondary@gmail.com")
        self.assertEqual(manager.in_flight_count("primary@gmail.com"), 1)
        self.assertEqual(manager.in_flight_count("secondary@gmail.com"), 1)

    @patch("codex_antigravity_auth.accounts.update_accounts")
    def test_acquire_preserves_sticky_selection_after_release(self, mock_update):
        mock_update.side_effect = lambda mutator: mutator(self.accounts_data)
        manager = AccountManager()

        first = manager.acquire_account("claude-3.5-sonnet")
        manager.release_account(first["email"])
        second = manager.acquire_account("claude-3.5-sonnet")

        self.assertEqual(first["email"], "primary@gmail.com")
        self.assertEqual(second["email"], "primary@gmail.com")

    @patch("codex_antigravity_auth.accounts.update_accounts")
    def test_acquire_avoids_cooling_down_accounts_even_when_less_busy(self, mock_update):
        mock_update.side_effect = lambda mutator: mutator(self.accounts_data)
        manager = AccountManager()
        manager._cooldowns["primary@gmail.com"] = time.time() + 300
        manager._in_flight["secondary@gmail.com"] = 3

        selected = manager.acquire_account("claude-3.5-sonnet")

        self.assertEqual(selected["email"], "secondary@gmail.com")

    def test_release_account_never_goes_negative(self):
        manager = AccountManager()

        manager.release_account("missing@gmail.com")
        manager.release_account("missing@gmail.com")

        self.assertEqual(manager.in_flight_count("missing@gmail.com"), 0)

    @patch("codex_antigravity_auth.accounts.update_accounts")
    def test_account_rotation_on_failure_cooldown(self, mock_update):
        mock_update.side_effect = lambda mutator: mutator(self.accounts_data)
        with tempfile.TemporaryDirectory() as tmp:
            missing_accounts_file = Path(tmp) / "antigravity-accounts.json"
            with patch("codex_antigravity_auth.accounts.get_accounts_json_path", return_value=missing_accounts_file):
                manager = AccountManager()

                # Mark primary as failed/cooling down
                manager.mark_failure("primary@gmail.com", "Too many requests")

                # Selecting an account should now rotate to secondary
                selected = manager.select_active_account("gemini-3.5-flash-high")
                self.assertIsNotNone(selected)
                self.assertEqual(selected["email"], "secondary@gmail.com")

    @patch("codex_antigravity_auth.accounts.update_accounts")
    def test_rate_limit_cooldown_is_family_scoped(self, mock_update):
        mock_update.side_effect = lambda mutator: mutator(self.accounts_data)
        with tempfile.TemporaryDirectory() as tmp:
            with patch("codex_antigravity_auth.accounts.get_accounts_json_path", return_value=Path(tmp) / "missing.json"):
                manager = AccountManager()
                manager.mark_failure(
                    "primary@gmail.com",
                    "rate limited",
                    model="claude-3.5-sonnet",
                    status_code=429,
                )

                claude = manager.select_active_account("claude-3.5-sonnet")
                gemini = manager.select_active_account("gemini-3.5-flash-high")

        self.assertEqual(claude["email"], "secondary@gmail.com")
        self.assertEqual(gemini["email"], "primary@gmail.com")

    @patch("codex_antigravity_auth.accounts.update_accounts")
    def test_backend_quota_outcome_without_http_status_is_family_scoped(self, mock_update):
        mock_update.side_effect = lambda mutator: mutator(self.accounts_data)
        with tempfile.TemporaryDirectory() as tmp:
            with patch("codex_antigravity_auth.accounts.get_accounts_json_path", return_value=Path(tmp) / "missing.json"):
                manager = AccountManager()
                manager.mark_failure(
                    "primary@gmail.com",
                    "Backend payload error RESOURCE_EXHAUSTED: quota exhausted",
                    model="claude-3.5-sonnet",
                )

                claude = manager.select_active_account("claude-3.5-sonnet")
                gemini = manager.select_active_account("gemini-3.5-flash-high")

        self.assertEqual(claude["email"], "secondary@gmail.com")
        self.assertEqual(gemini["email"], "primary@gmail.com")

    @patch("codex_antigravity_auth.accounts.update_accounts")
    def test_auth_cooldown_is_account_wide(self, mock_update):
        mock_update.side_effect = lambda mutator: mutator(self.accounts_data)
        with tempfile.TemporaryDirectory() as tmp:
            with patch("codex_antigravity_auth.accounts.get_accounts_json_path", return_value=Path(tmp) / "missing.json"):
                manager = AccountManager()
                manager.mark_failure(
                    "primary@gmail.com",
                    "auth failed",
                    model="claude-3.5-sonnet",
                    status_code=401,
                )

                gemini = manager.select_active_account("gemini-3.5-flash-high")

        self.assertEqual(gemini["email"], "secondary@gmail.com")

    @patch("codex_antigravity_auth.accounts.update_accounts")
    def test_legacy_state_migration_preserves_credentials_and_fingerprint(self, mock_update):
        fingerprint = {"deviceId": "device", "sessionToken": "session"}
        self.accounts_data["accounts"][0]["fingerprint"] = fingerprint
        self.accounts_data["accountState"] = {
            "failures": {"primary@gmail.com": 1},
            "cooldowns": {"primary@gmail.com": (time.time() + 120) * 1000},
        }
        before = dict(self.accounts_data["accounts"][0])
        mock_update.side_effect = lambda mutator: mutator(self.accounts_data)

        AccountManager().select_active_account("gemini-3.5-flash-high")

        self.assertEqual(self.accounts_data["accountState"]["schemaVersion"], 2)
        self.assertEqual(self.accounts_data["accounts"][0], before)

    @patch("codex_antigravity_auth.accounts.update_accounts")
    def test_empty_account_migration_persists_complete_schema(self, mock_update):
        data = {"accounts": []}
        mock_update.side_effect = lambda mutator: mutator(data)

        self.assertIsNone(AccountManager().select_active_account("gemini-3.5-flash-high"))

        self.assertEqual(
            data["accountState"],
            {"schemaVersion": 2, "failures": {}, "cooldowns": {}, "counters": {}},
        )

    @patch("codex_antigravity_auth.accounts.update_accounts")
    def test_clear_failures_can_clear_one_family_only(self, mock_update):
        self.accounts_data["accountState"] = {
            "schemaVersion": 2,
            "failures": {"primary@gmail.com": {"account": 1, "claude": 2}},
            "cooldowns": {"primary@gmail.com": {"account": time.time() + 300, "claude": time.time() + 300}},
            "counters": {},
        }
        mock_update.side_effect = lambda mutator: mutator(self.accounts_data)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "accounts.json"
            path.write_text("{}", encoding="utf-8")
            with patch("codex_antigravity_auth.accounts.get_accounts_json_path", return_value=path):
                manager = AccountManager()
                manager.select_active_account("gemini-3.5-flash-high")
                manager.clear_failures("primary@gmail.com", family="claude")

        scoped = self.accounts_data["accountState"]
        self.assertEqual(scoped["failures"]["primary@gmail.com"], {"account": 1})
        self.assertIn("account", scoped["cooldowns"]["primary@gmail.com"])

    @patch("codex_antigravity_auth.accounts.update_accounts")
    @patch("codex_antigravity_auth.accounts.refresh_access_token")
    def test_token_auto_refresh_trigger(self, mock_refresh, mock_update):
        # Primary token has expired
        self.accounts_data["accounts"][0]["expiresAt"] = time.time() - 10
        mock_update.side_effect = lambda mutator: mutator(self.accounts_data)
        mock_refresh.return_value = {
            "access_token": "refreshed_acc_1",
            "expires_in": 3600
        }
        
        manager = AccountManager()
        selected = manager.select_active_account("gemini-3.5-flash-high")
        
        self.assertEqual(selected["accessToken"], "refreshed_acc_1")
        mock_refresh.assert_called_once_with("ref_1")

    @patch("codex_antigravity_auth.accounts.update_accounts")
    @patch("codex_antigravity_auth.accounts.refresh_access_token")
    def test_acquire_refreshes_only_selected_candidate(self, mock_refresh, mock_update):
        for account in self.accounts_data["accounts"]:
            account["expiresAt"] = time.time() - 10
        mock_update.side_effect = lambda mutator: mutator(self.accounts_data)
        mock_refresh.return_value = {
            "access_token": "refreshed_acc_2",
            "expires_in": 3600,
        }

        manager = AccountManager()
        manager._in_flight["primary@gmail.com"] = 5
        selected = manager.acquire_account("claude-3.5-sonnet")

        self.assertEqual(selected["email"], "secondary@gmail.com")
        self.assertEqual(selected["accessToken"], "refreshed_acc_2")
        self.assertEqual(self.accounts_data["accounts"][0]["accessToken"], "acc_1")
        mock_refresh.assert_called_once_with("ref_2")

    @patch("codex_antigravity_auth.accounts.update_accounts")
    def test_request_counters_persist_with_cooldown_state(self, mock_update):
        mock_update.side_effect = lambda mutator: mutator(self.accounts_data)
        with tempfile.TemporaryDirectory() as tmp:
            accounts_file = Path(tmp) / "antigravity-accounts.json"
            accounts_file.write_text("{}", encoding="utf-8")
            with patch("codex_antigravity_auth.accounts.get_accounts_json_path", return_value=accounts_file):
                manager = AccountManager()
                manager.record_request(
                    "primary@gmail.com",
                    "claude-3.5-sonnet",
                    status="success",
                    status_code=200,
                    usage={"input_tokens": 4, "output_tokens": 5, "total_tokens": 9},
                )
                manager.mark_failure(
                    "primary@gmail.com",
                    "Rate limited / Quota exceeded",
                    retry_after_seconds=60,
                    model="claude-3.5-sonnet",
                    status_code=429,
                )
                manager.record_request(
                    "primary@gmail.com",
                    "claude-3.5-sonnet",
                    status="failure",
                    status_code=429,
                    error_class="rate_limited",
                )

        counter = self.accounts_data["accountState"]["counters"]["primary@gmail.com"]["claude"]
        self.assertEqual(counter["total_requests"], 2)
        self.assertEqual(counter["successes"], 1)
        self.assertEqual(counter["failures"], 1)
        self.assertEqual(counter["rate_limits"], 1)
        self.assertEqual(counter["total_tokens"], 9)

    @patch("codex_antigravity_auth.accounts.update_accounts")
    @patch("codex_antigravity_auth.accounts.refresh_access_token")
    def test_refresh_expiring_accounts_refreshes_ahead(self, mock_refresh, mock_update):
        self.accounts_data["accounts"][0]["expiresAt"] = time.time() + 120
        self.accounts_data["accounts"][1]["expiresAt"] = time.time() + 1000
        mock_update.side_effect = lambda mutator: mutator(self.accounts_data)
        mock_refresh.return_value = {"access_token": "fresh_access", "expires_in": 3600}

        with tempfile.TemporaryDirectory() as tmp:
            accounts_file = Path(tmp) / "antigravity-accounts.json"
            accounts_file.write_text("{}", encoding="utf-8")
            with patch("codex_antigravity_auth.accounts.get_accounts_json_path", return_value=accounts_file):
                manager = AccountManager()
                summary = manager.refresh_expiring_accounts(window_seconds=300)

        self.assertEqual(summary["checked"], 2)
        self.assertEqual(summary["refreshed"], 1)
        self.assertEqual(summary["failed"], 0)
        self.assertEqual(self.accounts_data["accounts"][0]["accessToken"], "fresh_access")
        mock_refresh.assert_called_once_with("ref_1")

if __name__ == "__main__":
    unittest.main()
