"""Shared lock and durable JSON publication for packaged and standalone Anti."""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
from typing import Any


class PersistenceError(RuntimeError):
    """Stored bytes were preserved; callers should report rather than reconstruct."""


from .file_protection import ensure_private_directory, protect_descriptor

try:
    from codex_antigravity_auth.secure_store import file_lock
except ImportError:  # copied skill uses exactly the same stdlib primitive
    from .file_protection import file_lock


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
    ensure_private_directory(path.parent)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False,
                                         dir=path.parent, prefix=f".{path.name}.", suffix=".tmp") as handle:
            temporary = Path(handle.name)
            protect_descriptor(handle.fileno(), path=temporary)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        fsync_directory(path.parent)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
