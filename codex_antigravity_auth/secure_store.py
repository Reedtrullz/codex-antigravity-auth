"""Transactional local persistence primitives."""

from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import tempfile
import threading
from typing import Any, Callable, TypeVar

from cryptography.fernet import Fernet, InvalidToken

try:
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None

try:
    import msvcrt
except ImportError:  # pragma: no cover - only available on Windows.
    msvcrt = None

T = TypeVar("T")
_file_lock_thread_lock = threading.RLock()


class StoreError(RuntimeError):
    pass


class StoreNotFound(StoreError):
    pass


class StoreInvalidData(StoreError):
    pass


class StoreDecryptionError(StoreError):
    pass


class StorePermissionError(StoreError):
    pass


@contextmanager
def file_lock(path: Path):
    with _file_lock_thread_lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = path.with_name(f".{path.name}.lock")
        descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            os.chmod(lock_path, 0o600)
            if fcntl is not None:
                fcntl.flock(descriptor, fcntl.LOCK_EX)
            elif msvcrt is not None:
                if os.fstat(descriptor).st_size == 0:
                    os.write(descriptor, b"\0")
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_LOCK, 1)
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            elif msvcrt is not None:
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
            os.close(descriptor)


class SecureStore:
    def __init__(self, *, key_provider: Callable[[], str] | None = None) -> None:
        self._key_provider = key_provider
        self._thread_lock = threading.RLock()

    def _key(self) -> str:
        if self._key_provider is not None:
            return self._key_provider()
        from .storage import _get_encryption_key

        return _get_encryption_key()

    @staticmethod
    def _reject_symlink(path: Path) -> None:
        if path.is_symlink():
            raise ValueError(f"Refusing to use symlinked store path: {path}")

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        if os.name == "nt":
            return
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _atomic_write_bytes_unlocked(self, path: Path, content: bytes, *, mode: int = 0o600) -> None:
        self._reject_symlink(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "wb", delete=False, dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
            ) as handle:
                temp_path = Path(handle.name)
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temp_path, mode)
            os.replace(temp_path, path)
            self._fsync_directory(path.parent)
        except Exception:
            if temp_path is not None:
                try:
                    temp_path.unlink(missing_ok=True)
                except Exception:
                    pass
            raise

    def atomic_write_bytes(self, path: Path, content: bytes, *, mode: int = 0o600) -> None:
        with self._thread_lock, file_lock(path):
            self._atomic_write_bytes_unlocked(path, content, mode=mode)

    def atomic_write_text(self, path: Path, text: str, *, mode: int = 0o600) -> None:
        self.atomic_write_bytes(path, text.encode("utf-8"), mode=mode)

    def _load_json_unlocked(self, path: Path, default: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        self._reject_symlink(path)
        if not path.exists():
            return default()
        raw = path.read_bytes()
        plaintext = False
        try:
            decoded = Fernet(self._key().encode("utf-8")).decrypt(raw).decode("utf-8")
        except InvalidToken as exc:
            stripped = raw.lstrip()
            if not stripped.startswith(b"{"):
                raise StoreDecryptionError(f"Unable to decrypt secure store {path}") from exc
            try:
                decoded = raw.decode("utf-8")
                plaintext = True
            except UnicodeDecodeError as decode_exc:
                raise StoreDecryptionError(f"Unable to decrypt secure store {path}") from decode_exc
        try:
            data = json.loads(decoded)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise StoreInvalidData(f"Store {path} does not contain valid JSON") from exc
        if not isinstance(data, dict):
            raise StoreInvalidData(f"Store {path} top-level JSON value is not an object")
        if plaintext:
            encrypted = Fernet(self._key().encode("utf-8")).encrypt(
                json.dumps(data, indent=2).encode("utf-8")
            )
            self._atomic_write_bytes_unlocked(path, encrypted)
        return data

    def load_json(self, path: Path, *, default: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        with self._thread_lock, file_lock(path):
            return self._load_json_unlocked(path, default)

    def save_json(self, path: Path, data: dict[str, Any]) -> None:
        with self._thread_lock, file_lock(path):
            encrypted = Fernet(self._key().encode("utf-8")).encrypt(
                json.dumps(data, indent=2).encode("utf-8")
            )
            self._atomic_write_bytes_unlocked(path, encrypted)

    def update_json(
        self,
        path: Path,
        mutator: Callable[[dict[str, Any]], T],
        *,
        default: Callable[[], dict[str, Any]],
    ) -> T:
        with self._thread_lock, file_lock(path):
            data = self._load_json_unlocked(path, default)
            result = mutator(data)
            encrypted = Fernet(self._key().encode("utf-8")).encrypt(
                json.dumps(data, indent=2).encode("utf-8")
            )
            self._atomic_write_bytes_unlocked(path, encrypted)
            return result
