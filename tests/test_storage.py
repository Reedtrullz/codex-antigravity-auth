import unittest
import tempfile
import os
import json
import threading
from pathlib import Path
from unittest.mock import patch
from codex_antigravity_auth.storage import FALLBACK_KEY_FILE, _get_file_fallback_key, load_accounts, save_accounts, update_accounts
from codex_antigravity_auth.secure_store import file_lock


def assert_mode_if_posix(testcase: unittest.TestCase, path: Path, expected: int) -> None:
    if os.name != "nt":
        testcase.assertEqual(oct(path.stat().st_mode & 0o777), oct(expected))


class TestStorage(unittest.TestCase):
    def test_file_lock_does_not_unlock_when_acquisition_fails(self):
        from codex_antigravity_auth import secure_store

        if secure_store.fcntl is None:
            self.skipTest("fcntl is unavailable")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "store.json"
            with patch.object(secure_store.fcntl, "flock", side_effect=OSError("lock failed")) as flock:
                with self.assertRaisesRegex(OSError, "lock failed"):
                    with file_lock(path):
                        pass

        self.assertEqual(flock.call_count, 1)
        self.assertEqual(flock.call_args.args[1], secure_store.fcntl.LOCK_EX)

    def test_storage_uses_the_shared_secure_store_lock(self):
        from codex_antigravity_auth import storage

        self.assertIs(storage._exclusive_file_lock, file_lock)

    def test_file_locks_for_different_paths_do_not_block_each_other(self):
        with tempfile.TemporaryDirectory() as tmp:
            first_path = Path(tmp) / "first.json"
            second_path = Path(tmp) / "second.json"
            first_entered = threading.Event()
            release_first = threading.Event()
            second_entered = threading.Event()

            def hold_first() -> None:
                with file_lock(first_path):
                    first_entered.set()
                    release_first.wait(2)

            def enter_second() -> None:
                with file_lock(second_path):
                    second_entered.set()

            first = threading.Thread(target=hold_first)
            second = threading.Thread(target=enter_second)
            first.start()
            self.assertTrue(first_entered.wait(1))
            second.start()
            try:
                self.assertTrue(second_entered.wait(1))
            finally:
                release_first.set()
                first.join(2)
                second.join(2)

    def test_key_initialization_lock_failure_does_not_trigger_fallback_key_creation(self):
        from codex_antigravity_auth import storage

        with patch.dict(os.environ, {}, clear=True):
            with patch.object(storage, "_exclusive_file_lock", side_effect=OSError("lock failed")):
                with patch.object(storage, "_get_file_fallback_key") as fallback:
                    with self.assertRaisesRegex(OSError, "lock failed"):
                        storage._get_encryption_key()
        fallback.assert_not_called()

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
                assert_mode_if_posix(self, tmp_path, 0o600)
                with open(tmp_path, "rb") as f:
                    raw_content = f.read()
                with self.assertRaises(json.JSONDecodeError):
                    json.loads(raw_content.decode("utf-8"))

    def test_load_accounts_normalizes_malformed_nested_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp) / "accounts.json"
            with patch("codex_antigravity_auth.storage.get_accounts_json_path", return_value=tmp_path):
                tmp_path.write_text(
                    json.dumps({
                        "accounts": "not-a-list",
                        "activeIndex": "first",
                        "activeIndexByFamily": [],
                        "accountState": "bad-state",
                    }),
                    encoding="utf-8",
                )

                loaded = load_accounts()

                self.assertEqual(loaded["accounts"], [])
                self.assertEqual(loaded["activeIndex"], 0)
                self.assertEqual(loaded["activeIndexByFamily"], {"claude": 0, "gemini": 0})
                self.assertEqual(
                    loaded["accountState"],
                    {"schemaVersion": 2, "failures": {}, "cooldowns": {}, "counters": {}},
                )

    def test_fallback_key_permissions_are_repaired(self):
        with tempfile.TemporaryDirectory() as tmp:
            key_path = Path(tmp) / FALLBACK_KEY_FILE
            key_path.write_text("legacy-fallback-key", encoding="utf-8")
            os.chmod(key_path, 0o644)

            with patch("codex_antigravity_auth.storage.get_codex_home", return_value=Path(tmp)):
                self.assertEqual(_get_file_fallback_key(), "legacy-fallback-key")

            assert_mode_if_posix(self, key_path, 0o600)

    def test_save_accounts_refuses_to_overwrite_malformed_existing_store(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp) / "accounts.json"
            tmp_path.write_text("[]", encoding="utf-8")

            with patch("codex_antigravity_auth.storage.get_accounts_json_path", return_value=tmp_path):
                with self.assertRaisesRegex(RuntimeError, "top-level JSON value is not an object"):
                    save_accounts({"accounts": [{"email": "new@gmail.com"}]})

            self.assertEqual(tmp_path.read_text(encoding="utf-8"), "[]")

    def test_update_accounts_refuses_to_overwrite_malformed_existing_store(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp) / "accounts.json"
            tmp_path.write_text("[]", encoding="utf-8")

            with patch("codex_antigravity_auth.storage.get_accounts_json_path", return_value=tmp_path):
                with self.assertRaisesRegex(RuntimeError, "top-level JSON value is not an object"):
                    update_accounts(lambda data: data.setdefault("accounts", []).append({"email": "new@gmail.com"}))

            self.assertEqual(tmp_path.read_text(encoding="utf-8"), "[]")

if __name__ == "__main__":
    unittest.main()
