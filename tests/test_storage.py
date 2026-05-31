import unittest
import tempfile
import os
import json
from pathlib import Path
from unittest.mock import patch
from codex_antigravity_auth.storage import save_accounts, load_accounts, get_accounts_json_path

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

    def test_backward_compatibility_fallback_reads_plaintext(self):
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

if __name__ == "__main__":
    unittest.main()
