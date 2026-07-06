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
