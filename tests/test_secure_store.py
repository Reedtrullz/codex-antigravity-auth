import os
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from cryptography.fernet import Fernet

from codex_antigravity_auth.secure_store import SecureStore, StoreDecryptionError, file_lock
from codex_antigravity_auth.storage import _get_encryption_key


class TestKeyInitialization(unittest.TestCase):
    def test_concurrent_first_initialization_returns_persisted_winner(self):
        stored = {"key": None}
        lock = threading.Lock()

        def get_password(*args):
            with lock:
                value = stored["key"]
            if value is None:
                time.sleep(0.05)
            return value

        def set_password(_service, _name, value):
            with lock:
                stored["key"] = value

        with tempfile.TemporaryDirectory() as tmp:
            with patch("codex_antigravity_auth.storage.get_codex_home", return_value=Path(tmp)):
                with patch("codex_antigravity_auth.storage.fcntl", None):
                    with patch("codex_antigravity_auth.storage.keyring.get_password", side_effect=get_password):
                        with patch("codex_antigravity_auth.storage.keyring.set_password", side_effect=set_password):
                            with ThreadPoolExecutor(max_workers=2) as pool:
                                keys = list(pool.map(lambda _index: _get_encryption_key(), range(2)))

        self.assertEqual(keys[0], keys[1])
        self.assertEqual(keys[0], stored["key"])
        Fernet(keys[0].encode("utf-8"))


class TestSecureStore(unittest.TestCase):
    def test_file_lock_serializes_threads_without_fcntl(self):
        first_entered = threading.Event()
        release_first = threading.Event()
        second_entered = threading.Event()

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "state.json"

            def hold_first_lock():
                with file_lock(target):
                    first_entered.set()
                    release_first.wait(timeout=2)

            def enter_second_lock():
                with file_lock(target):
                    second_entered.set()

            with patch("codex_antigravity_auth.secure_store.fcntl", None):
                first = threading.Thread(target=hold_first_lock)
                second = threading.Thread(target=enter_second_lock)
                first.start()
                self.assertTrue(first_entered.wait(timeout=1))
                second.start()
                self.assertFalse(second_entered.wait(timeout=0.1))
                release_first.set()
                first.join(timeout=1)
                second.join(timeout=1)

        self.assertTrue(second_entered.is_set())

    def test_atomic_text_write_is_private_and_rejects_symlink(self):
        store = SecureStore()
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "models.toml"
            store.atomic_write_text(target, "first")
            self.assertEqual(target.read_text(encoding="utf-8"), "first")
            if os.name != "nt":
                self.assertEqual(target.stat().st_mode & 0o777, 0o600)

            real = Path(tmp) / "real.toml"
            real.write_text("original", encoding="utf-8")
            target.unlink()
            target.symlink_to(real)
            with self.assertRaisesRegex(ValueError, "symlink"):
                store.atomic_write_text(target, "unsafe")
            self.assertEqual(real.read_text(encoding="utf-8"), "original")

    def test_failed_atomic_replace_keeps_original_and_removes_temp(self):
        store = SecureStore()
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "state.txt"
            target.write_text("original", encoding="utf-8")
            with patch("codex_antigravity_auth.secure_store.os.replace", side_effect=OSError("replace failed")):
                with self.assertRaisesRegex(OSError, "replace failed"):
                    store.atomic_write_text(target, "new")

            self.assertEqual(target.read_text(encoding="utf-8"), "original")
            self.assertEqual(list(Path(tmp).glob(".*.tmp")), [])

    def test_wrong_key_ciphertext_is_not_treated_as_plaintext(self):
        store = SecureStore(key_provider=lambda: Fernet.generate_key().decode("utf-8"))
        other = Fernet(Fernet.generate_key()).encrypt(b'{"value": 1}')
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "secure.json"
            path.write_bytes(other)
            with self.assertRaises(StoreDecryptionError):
                store.load_json(path, default=lambda: {})

    def test_explicit_plaintext_json_is_migrated_immediately(self):
        key = Fernet.generate_key().decode("utf-8")
        store = SecureStore(key_provider=lambda: key)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "legacy.json"
            path.write_text('{"value": 1}', encoding="utf-8")

            loaded = store.load_json(path, default=lambda: {})

            self.assertEqual(loaded, {"value": 1})
            self.assertFalse(path.read_bytes().lstrip().startswith(b"{"))
            self.assertEqual(store.load_json(path, default=lambda: {}), {"value": 1})


if __name__ == "__main__":
    unittest.main()
