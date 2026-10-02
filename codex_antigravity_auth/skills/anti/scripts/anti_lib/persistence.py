"""Shared lock and durable JSON publication for packaged and standalone Anti."""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import tempfile
import threading
from typing import Any


class PersistenceError(RuntimeError):
    """Stored bytes were preserved; callers should report rather than reconstruct."""


try:
    from codex_antigravity_auth.secure_store import file_lock
except ImportError:  # copied skill without the gateway package
    try:
        import fcntl
    except ImportError:  # Windows
        fcntl = None
    try:
        import msvcrt
    except ImportError:  # POSIX
        msvcrt = None
    _locks: dict[str, threading.RLock] = {}
    _locks_guard = threading.Lock()

    @contextmanager
    def file_lock(path: Path):
        key = os.path.abspath(str(path))
        with _locks_guard:
            lock = _locks.setdefault(key, threading.RLock())
        with lock:
            if fcntl is None and msvcrt is None:
                raise PersistenceError("No supported process lock; refusing to update saved history")
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            lock_path = path.with_name(f".{path.name}.lock")
            flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
            if lock_path.is_symlink():
                raise PersistenceError("Refusing symlinked history lock")
            descriptor = os.open(lock_path, flags, 0o600)
            acquired = False
            try:
                if fcntl is not None:
                    fcntl.flock(descriptor, fcntl.LOCK_EX)
                else:
                    if os.fstat(descriptor).st_size == 0:
                        os.write(descriptor, b"\0")
                    os.lseek(descriptor, 0, os.SEEK_SET)
                    msvcrt.locking(descriptor, msvcrt.LK_LOCK, 1)
                acquired = True
                yield
            finally:
                try:
                    if acquired and fcntl is not None:
                        fcntl.flock(descriptor, fcntl.LOCK_UN)
                    elif acquired:
                        os.lseek(descriptor, 0, os.SEEK_SET)
                        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
                finally:
                    os.close(descriptor)


def fsync_directory(path: Path) -> None:
    if os.name != "nt":
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def atomic_write_json(path: Path, value: Any) -> None:
    """Caller holds its ownership lock. Never remove another writer's temp file."""
    if path.is_symlink() or path.parent.is_symlink():
        raise PersistenceError("Refusing to publish saved history through a symlink")
    payload = json.dumps(value, indent=2, sort_keys=True) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False,
                                         dir=path.parent, prefix=f".{path.name}.", suffix=".tmp") as handle:
            temporary = Path(handle.name)
            os.chmod(temporary, 0o600)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        fsync_directory(path.parent)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
