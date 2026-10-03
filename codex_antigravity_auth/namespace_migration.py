"""Explicit same-machine copy of gateway configuration into an unused root."""
from __future__ import annotations

from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile

from .namespaces import root_path
from .secure_store import SecureStore, file_lock

# Runtime pid/log files and historical Anti outputs are intentionally not state
# configuration. Client auth/config are owned by Codex, never borrowed here.
STATE_FILES = (
    "antigravity-accounts.json", "antigravity-providers.json",
    "antigravity-credentials.json", "antigravity-storage.key",
    "antigravity-openai.json", "antigravity-models.toml",
)
MAX_STATE_FILE_BYTES = 16 * 1024 * 1024


def _snapshot(source: Path) -> dict[str, bytes]:
    if source.is_symlink() or not source.is_dir():
        raise ValueError("Source must be an existing non-symlink gateway directory")
    result = {}
    for name in STATE_FILES:
        path = source / name
        if path.is_symlink():
            raise ValueError("Refusing a symlinked gateway state file")
        if not path.exists():
            continue
        if not path.is_file() or path.stat().st_size > MAX_STATE_FILE_BYTES:
            raise ValueError("Gateway state file is not regular or exceeds the copy limit")
        result[name] = path.read_bytes()
        if len(result[name]) > MAX_STATE_FILE_BYTES:
            raise ValueError("Gateway state file exceeds the copy limit")
    return result


def copy_gateway_state(source: str, destination: str, *, write: bool = False) -> dict:
    source_path = root_path(source, label="source")
    destination_path = root_path(destination, label="destination")
    if source_path.resolve() == destination_path.resolve():
        raise ValueError("Source and destination must differ")
    if destination_path.exists() or destination_path.is_symlink():
        raise ValueError("Destination must not exist; existing state is never overwritten")
    snapshot = _snapshot(source_path)
    result = {
        "mode": "copied" if write else "dry_run", "files": sorted(snapshot),
        "bytes": sum(map(len, snapshot.values())), "source_retained": True,
        "key_requirement": "Retain the existing same-machine keyring key or storage-key environment; no keyring export is performed",
        "excluded": ["client auth/config", "process pid/log files", "Anti run history"],
    }
    if not write:
        return result
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    with ExitStack() as locks:
        # Match store writers and serialize cooperating destination publishers.
        # Stop the gateway/Anti before copying; external editors need not honor
        # these locks. Recheck every selected byte before publication as well.
        for path in sorted([source_path / name for name in STATE_FILES] + [destination_path], key=str):
            locks.enter_context(file_lock(path))
        snapshot = _snapshot(source_path)
        if destination_path.exists() or destination_path.is_symlink():
            raise ValueError("Destination appeared during copy; nothing was replaced")
        stage = Path(tempfile.mkdtemp(prefix=f".{destination_path.name}-", dir=destination_path.parent))
        try:
            for name, data in snapshot.items():
                SecureStore()._atomic_write_bytes_unlocked(stage / name, data)
            manifest = {
                "schemaVersion": 1, "kind": "gateway-namespace-copy", "sourceRetained": True,
                "files": {name: hashlib.sha256(data).hexdigest() for name, data in snapshot.items()},
                "keyRequirement": result["key_requirement"],
            }
            SecureStore()._atomic_write_bytes_unlocked(
                stage / "antigravity-namespace-migration.json",
                (json.dumps(manifest, indent=2) + "\n").encode(),
            )
            if _snapshot(source_path) != snapshot:
                raise RuntimeError("Source changed during copy; retry after stopping all state writers")
            if destination_path.exists() or destination_path.is_symlink():
                raise RuntimeError("Destination appeared during copy; nothing was replaced")
            os.rename(stage, destination_path)
            SecureStore._fsync_directory(destination_path.parent)
        finally:
            if stage.exists():
                shutil.rmtree(stage)
    result["files"] = sorted(snapshot)
    result["bytes"] = sum(map(len, snapshot.values()))
    return result
