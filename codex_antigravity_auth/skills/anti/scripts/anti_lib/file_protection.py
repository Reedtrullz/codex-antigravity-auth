"""Descriptor-validated private files and process locks for package and standalone use."""
from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import stat
import threading

try:
    import fcntl
except ImportError:
    fcntl = None
try:
    import msvcrt
except ImportError:
    msvcrt = None

WINDOWS = os.name == "nt"
_guard = threading.Lock()
_locks = {}
_held = threading.local()


def _windows_security():
    from .windows_file_security import WindowsFileSecurity
    try:
        return WindowsFileSecurity()
    except (AttributeError, OSError) as exc:
        raise OSError("Owner-only Windows storage protection is unavailable; refusing access") from exc


def _owned(info) -> None:
    if not WINDOWS and info.st_uid != os.geteuid():
        raise ValueError("Refusing a storage path owned by another user")


def _directory(path: Path, *, protect: bool) -> None:
    if path.is_symlink():
        raise ValueError("Refusing a symlinked private directory")
    if WINDOWS:
        security = _windows_security()
        if protect:
            try:
                security.verify_directory(path)
            except OSError:
                security.protect_directory(path)
        else:
            security.verify_directory_owner(path)
        return
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError("Private storage parent must be a directory")
        _owned(info)
        if protect:
            os.fchmod(descriptor, 0o700)
        elif info.st_mode & 0o022:
            raise ValueError("Refusing a private storage parent writable by other users")
    finally:
        os.close(descriptor)


def ensure_private_directory(path: Path, *, enforce_existing: bool = True) -> None:
    """Protect the managed leaf and new parents; never chmod an unrelated ancestor."""
    path = Path(path).absolute()
    missing = []
    current = path
    while not current.exists() and not current.is_symlink():
        missing.append(current)
        current = current.parent
    if not current.is_dir() or current.is_symlink():
        raise ValueError("Private storage parent must be a non-symlink directory")
    # The nearest existing ancestor is a trust boundary. /tmp may be the
    # ancestor of a newly-created private directory; the private leaf itself
    # is verified below before any secret or lock file is opened.
    for directory in reversed(missing):
        try:
            directory.mkdir(mode=0o700)
        except FileExistsError:
            pass
        _directory(directory, protect=True)
    _directory(path, protect=enforce_existing or bool(missing))


def verify_regular_descriptor(descriptor: int, path: Path | None = None):
    info = os.fstat(descriptor)
    if WINDOWS:
        # Native volume + 128-bit file identity avoids CRT stat identifier
        # differences across supported Python/Windows versions and filesystems.
        _windows_security().verify_descriptor_path(descriptor, path)
        return info
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError("Refusing nonregular or multiply-linked private file")
    _owned(info)
    if getattr(info, "st_file_attributes", 0) & 0x400:
        raise ValueError("Refusing a private file reparse point")
    if path is not None:
        entry = path.lstat()
        if (not stat.S_ISREG(entry.st_mode) or getattr(entry, "st_file_attributes", 0) & 0x400
                or (entry.st_dev, entry.st_ino) != (info.st_dev, info.st_ino)):
            raise ValueError("Private file path changed or is a symlink")
    return info


def protect_descriptor(descriptor: int, *, mode: int = 0o600, path: Path | None = None) -> None:
    if mode & 0o077:
        raise ValueError("Private files may not grant group or other permissions")
    verify_regular_descriptor(descriptor, path)
    if WINDOWS:
        _windows_security().protect_descriptor(descriptor)
    else:
        os.fchmod(descriptor, mode)


def protect_existing_file(path: Path) -> None:
    if path.is_symlink():
        raise ValueError("Refusing a symlinked private file")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    try:
        protect_descriptor(descriptor, path=path)
    finally:
        os.close(descriptor)


@contextmanager
def file_lock(path: Path, *, posix_backend=fcntl, windows_backend=msvcrt):
    if posix_backend is None and windows_backend is None:
        raise RuntimeError("No supported process lock; refusing unprotected storage access")
    path = Path(os.path.abspath(os.path.expanduser(str(path))))
    key = str(path)
    with _guard:
        thread_lock = _locks.setdefault(key, threading.RLock())
    with thread_lock:
        held = getattr(_held, "locks", None)
        if held is None:
            held = _held.locks = {}
        if key in held:
            verify_regular_descriptor(held[key], path.with_name(f".{path.name}.lock"))
            yield
            return
        ensure_private_directory(path.parent)
        lock_path = path.with_name(f".{path.name}.lock")
        if lock_path.is_symlink():
            raise ValueError("Refusing a symlinked storage lock")
        try:
            entry = lock_path.lstat()
        except FileNotFoundError:
            pass
        else:
            if not stat.S_ISREG(entry.st_mode) or (not WINDOWS and entry.st_nlink != 1) or getattr(entry, "st_file_attributes", 0) & 0x400:
                raise ValueError("Refusing a nonregular, linked or reparse storage lock")
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        descriptor = (_windows_security().open_lock_file(lock_path) if WINDOWS
                      else os.open(lock_path, flags, 0o600))
        acquired = False
        try:
            # All validation precedes permission mutation and Windows sentinel writes.
            protect_descriptor(descriptor, path=lock_path)
            if posix_backend is not None:
                posix_backend.flock(descriptor, posix_backend.LOCK_EX)
            else:
                if os.fstat(descriptor).st_size == 0:
                    os.write(descriptor, b"\0")
                os.lseek(descriptor, 0, os.SEEK_SET)
                windows_backend.locking(descriptor, windows_backend.LK_LOCK, 1)
            acquired = True
            verify_regular_descriptor(descriptor, lock_path)
            held[key] = descriptor
            yield
        finally:
            held.pop(key, None)
            try:
                if acquired and posix_backend is not None:
                    posix_backend.flock(descriptor, posix_backend.LOCK_UN)
                elif acquired:
                    os.lseek(descriptor, 0, os.SEEK_SET)
                    windows_backend.locking(descriptor, windows_backend.LK_UNLCK, 1)
            finally:
                os.close(descriptor)
