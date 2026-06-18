import unittest
import tempfile
import os
import json
from pathlib import Path
from unittest.mock import patch
from codex_antigravity_auth.storage import FALLBACK_KEY_FILE, _get_file_fallback_key, save_accounts, load_accounts

class TestStorage(unittest.TestCase):
    def test_encrypted_accounts_storage_and_decryption(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp) / "accounts.json"
            with patch("codex_antigravity_auth.storage.get_accounts_json_path", return_value=tmp_path):
                test_data = {
                    "accounts": [{"email": "test@gmail.com", "accessToken": "secret_token"}],
                    "activeIndex": 0,
                    "activeIndexByFamily": {"claude": 0, "gemini": 0}
                }
                save_accounts(test_data)
                
                # Verify that it is not plaintext JSON on disk
                with open(tmp_path, "rb") as f:
                    raw_content = f.read()
                try:
                    json.loads(raw_content.decode("utf-8"))
                    self.fail("File was written in plaintext JSON!")
                except json.JSONDecodeError:
                    pass # Success: Not plaintext JSON
                
                # Verify we can decrypt and load the accurate dictionary back
                loaded = load_accounts()
                self.assertEqual(loaded["accounts"][0]["email"], "test@gmail.com")
                self.assertEqual(loaded["accounts"][0]["accessToken"], "secret_token")

    def test_backward_compatibility_fallback_reads_plaintext_and_migrates(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp) / "accounts.json"
            with patch("codex_antigravity_auth.storage.get_accounts_json_path", return_value=tmp_path):
                # Write a raw plaintext JSON file
                test_data = {
                    "accounts": [{"email": "legacy@gmail.com", "accessToken": "legacy_token"}],
                    "activeIndex": 0,
                    "activeIndexByFamily": {"claude": 0, "gemini": 0}
                }
                with open(tmp_path, "w") as f:
                    json.dump(test_data, f)
                
                # Should fallback to plaintext reading
                loaded = load_accounts()
                self.assertEqual(loaded["accounts"][0]["email"], "legacy@gmail.com")
                self.assertEqual(loaded["accounts"][0]["accessToken"], "legacy_token")

                # And immediately rewrite the legacy file as private encrypted storage
                self.assertEqual(oct(tmp_path.stat().st_mode & 0o777), "0o600")
                with open(tmp_path, "rb") as f:
                    raw_content = f.read()
                with self.assertRaises(json.JSONDecodeError):
                    json.loads(raw_content.decode("utf-8"))

    def test_fallback_key_permissions_are_repaired(self):
        with tempfile.TemporaryDirectory() as tmp:
            key_path = Path(tmp) / FALLBACK_KEY_FILE
            key_path.write_text("legacy-fallback-key", encoding="utf-8")
            os.chmod(key_path, 0o644)

            with patch("codex_antigravity_auth.storage.get_codex_home", return_value=Path(tmp)):
                self.assertEqual(_get_file_fallback_key(), "legacy-fallback-key")

            self.assertEqual(oct(key_path.stat().st_mode & 0o777), "0o600")

if __name__ == "__main__":
    unittest.main()
