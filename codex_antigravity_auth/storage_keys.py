"""Stable key identity and read-only backend inspection for managed stores."""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import re
import stat

from cryptography.fernet import Fernet, InvalidToken

from .secure_store import SecureStore
from .skills.anti.scripts.anti_lib.file_protection import verify_regular_descriptor

SELECTION_FILE = "antigravity-storage-key.json"
TRANSITION_FILE = "antigravity-storage-transition.json"
MAX_STORE_BYTES = 16 * 1024 * 1024
KEY_ID = re.compile(r"^[0-9a-f]{64}$")
ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class StorageKeyError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def storage_module():
    from . import storage
    return storage


def home() -> Path:
    return storage_module()._codex_home_read_only()


def managed_paths() -> dict[str, Path]:
    from .byok import providers_json_path_read_only
    result = {"accounts": storage_module().accounts_json_path_read_only(), "providers": providers_json_path_read_only()}
    if len({path.resolve() for path in result.values()}) != len(result):
        raise StorageKeyError("invalid_store_paths", "Managed stores must use distinct paths")
    return result


def read_bytes(path: Path, *, limit: int | None = MAX_STORE_BYTES) -> bytes | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode) or path.is_symlink() or getattr(info, "st_file_attributes", 0) & 0x400:
        raise StorageKeyError("unsafe_path", "Refusing an unsafe storage/key path")
    if limit is not None and info.st_size > limit:
        raise StorageKeyError("size_limit", "Storage/key file exceeds the operation size limit")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    try:
        verify_regular_descriptor(descriptor, path)
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = None
            data = handle.read(limit + 1) if limit is not None else handle.read()
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if limit is not None and len(data) > limit:
        raise StorageKeyError("size_limit", "Storage/key file exceeds the operation size limit")
    return data


def key_id(key: str) -> str:
    Fernet(key.encode("ascii"))
    return hashlib.sha256(base64.urlsafe_b64decode(key.encode("ascii"))).hexdigest()


def valid_key(value) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    value = value.strip()
    try:
        key_id(value)
    except (ValueError, TypeError, UnicodeError):
        return None
    return base64.urlsafe_b64encode(base64.urlsafe_b64decode(value.encode("ascii"))).decode("ascii")


def named_key(name: str) -> str:
    if not ENV_NAME.fullmatch(name):
        raise StorageKeyError("invalid_key_env", "Use a valid environment variable name for the key")
    key = valid_key(os.environ.get(name))
    if key is None:
        raise StorageKeyError("key_unavailable", "The named environment variable must contain a valid Fernet key")
    return key


def transition_pending() -> bool:
    path = home() / TRANSITION_FILE
    return path.exists() or path.is_symlink()


def require_no_transition() -> None:
    if transition_pending():
        raise StorageKeyError("recovery_required", "An interrupted key transition requires storage restore from its encrypted backup")


def selection() -> dict | None:
    raw = read_bytes(home() / SELECTION_FILE, limit=4096)
    if raw is None:
        return None
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise StorageKeyError("invalid_key_selection", "Key-selection metadata is invalid; preserve it for recovery") from exc
    if (not isinstance(data, dict) or type(data.get("schemaVersion")) is not int or data["schemaVersion"] != 1
            or set(data) != {"schemaVersion", "backend", "keyId", "slot"}
            or not isinstance(data.get("backend"), str) or data["backend"] not in {"file", "keyring", "environment"}
            or not isinstance(data.get("keyId"), str) or not KEY_ID.fullmatch(data["keyId"])
            or (data["backend"] == "keyring" and (not isinstance(data.get("slot"), str) or data["slot"] not in {"legacy", "id"}))
            or (data["backend"] in {"file", "environment"} and data.get("slot") is not None)):
        raise StorageKeyError("unsupported_key_selection", "Key-selection metadata is unsupported; no replacement key was created")
    return data


def ring_name(identity: str | None = None) -> str:
    base = storage_module().KEYRING_KEY_NAME
    return f"{base}:{identity}" if identity else base


def ring_key(name: str) -> tuple[str | None, str]:
    storage = storage_module()
    try:
        value = storage.keyring.get_password(storage.KEYRING_SERVICE_NAME, name)
    except Exception:
        return None, "unavailable"
    if value is None or value == "":
        return None, "missing"
    key = valid_key(value)
    return key, "available" if key else "invalid"


def file_key() -> tuple[str | None, str]:
    try:
        raw = read_bytes(home() / storage_module().FALLBACK_KEY_FILE, limit=4096)
        if raw is None:
            return None, "missing"
        key = valid_key(raw.decode("utf-8"))
        return key, "available" if key else "invalid"
    except (ValueError, OSError, UnicodeError):
        return None, "unsafe_or_unreadable"


def environment_key() -> str | None:
    value = os.environ.get("ANTIGRAVITY_STORAGE_KEY")
    return valid_key(storage_module()._normalize_fernet_key(value)) if value else None


def existing_key() -> tuple[str | None, str | None, str | None]:
    require_no_transition()
    recorded = selection()
    environment = environment_key()
    if environment:
        if recorded and key_id(environment) != recorded["keyId"]:
            raise StorageKeyError("key_mismatch", "Environment key differs from the recorded store key; use explicit recovery/re-encryption")
        return environment, "environment", None
    if recorded:
        if recorded["backend"] == "environment":
            raise StorageKeyError("key_unavailable", "The recorded environment key must be supplied; no replacement was created")
        key, status = (file_key() if recorded["backend"] == "file" else
                       ring_key(ring_name(recorded["keyId"] if recorded["slot"] == "id" else None)))
        if key is None:
            raise StorageKeyError("key_unavailable", "The recorded encryption-key backend is unavailable; no replacement was created")
        if key_id(key) != recorded["keyId"]:
            raise StorageKeyError("key_mismatch", "Recorded encryption-key identity does not match the available key")
        return key, recorded["backend"], recorded["slot"]
    local, local_status = file_key()
    ring, ring_status = ring_key(ring_name())
    if local_status not in {"missing", "available"} or ring_status == "invalid":
        raise StorageKeyError("invalid_key_source", "An existing encryption-key source is invalid or unreadable")
    if local and ring and key_id(local) != key_id(ring):
        raise StorageKeyError("key_conflict", "Existing file and keyring keys conflict; use explicit backup-first re-encryption")
    if ring:
        return ring, "keyring", "legacy"
    if local:
        return local, "file", None
    return None, None, None


def write_selection(key: str, backend: str, slot: str | None = None) -> None:
    if backend not in {"file", "keyring", "environment"} or (backend == "keyring" and slot not in {"legacy", "id"}) or (backend != "keyring" and slot is not None):
        raise StorageKeyError("invalid_backend", "Invalid recorded encryption-key backend")
    payload = {"schemaVersion": 1, "backend": backend, "keyId": key_id(key), "slot": slot}
    SecureStore().atomic_write_text(home() / SELECTION_FILE, json.dumps(payload, sort_keys=True) + "\n")


def decoded_store(raw: bytes, keys: list[str]) -> tuple[bytes, str | None]:
    def object_bytes(data):
        try:
            value = json.loads(data)
        except (ValueError, UnicodeError, RecursionError) as exc:
            raise StorageKeyError("invalid_store", "Managed store payload is invalid; original bytes were preserved") from exc
        if not isinstance(value, dict):
            raise StorageKeyError("invalid_store", "Managed store payload must be a JSON object")
        return data
    if raw.lstrip().startswith(b"{"):
        return object_bytes(raw), None
    for key in keys:
        try:
            plain = Fernet(key.encode("ascii")).decrypt(raw)
        except InvalidToken:
            continue
        return object_bytes(plain), key
    raise StorageKeyError("key_unavailable", "No supplied or existing key can decrypt a managed store; original bytes were preserved")


def install_key(key: str, backend: str) -> str | None:
    if backend == "file":
        SecureStore().atomic_write_text(home() / storage_module().FALLBACK_KEY_FILE, key + "\n")
        return None
    if backend != "keyring":
        raise StorageKeyError("invalid_backend", "Target backend must be file or keyring")
    storage = storage_module()
    name = ring_name(key_id(key))
    current, status = ring_key(name)
    if status == "unavailable":
        raise StorageKeyError("keyring_unavailable", "Target keyring slot cannot be inspected safely")
    if status == "invalid":
        raise StorageKeyError("invalid_key_source", "Target keyring slot contains invalid existing material")
    if current and key_id(current) != key_id(key):
        raise StorageKeyError("key_conflict", "Keyring identity slot contains a conflicting key")
    if current is None:
        try:
            storage.keyring.set_password(storage.KEYRING_SERVICE_NAME, name, key)
        except Exception as exc:
            raise StorageKeyError("keyring_unavailable", "Target keyring key could not be persisted") from exc
    verified, _status = ring_key(name)
    if verified is None or key_id(verified) != key_id(key):
        raise StorageKeyError("keyring_unavailable", "Target keyring key could not be verified")
    return "id"


def get_key(*, create: bool) -> str | None:
    if not create:
        return existing_key()[0]
    # Recorded environment identities retain a no-keyring fast path. First use
    # is serialized and bound to metadata so a second process cannot create a
    # different-key store in the same namespace before the first write lands.
    require_no_transition()
    if environment_key() and selection() is not None:
        return existing_key()[0]
    storage = storage_module()
    with storage._exclusive_file_lock(storage.get_codex_home() / "antigravity-storage-key-init"):
        key, backend, slot = existing_key()
        if key is not None:
            if selection() is None:
                for path in managed_paths().values():
                    raw = read_bytes(path, limit=None)
                    if raw is not None:
                        decoded_store(raw, [key])
                write_selection(key, backend, slot)
            return key
        # Never invent another key while ciphertext is waiting for its original.
        for path in managed_paths().values():
            raw = read_bytes(path, limit=None)
            if raw is not None:
                decoded_store(raw, [])  # Only legacy plaintext is eligible for initialization.
        key = Fernet.generate_key().decode("ascii")
        try:
            slot = install_key(key, "keyring")
            backend = "keyring"
        except StorageKeyError as exc:
            if exc.code != "keyring_unavailable":
                raise
            # Retain the same candidate if a keyring write succeeded but its
            # read-back failed. No ciphertext has been written with another key.
            key = storage._get_file_fallback_key(key)
            if valid_key(key) is None:
                raise StorageKeyError("invalid_key_source", "Fallback key is invalid")
            slot, backend = None, "file"
        write_selection(key, backend, slot)
        return key


def candidates(extra_env: list[str] | None = None) -> tuple[list[str], dict]:
    if extra_env and len(extra_env) > 8:
        raise StorageKeyError("too_many_keys", "At most eight additional source key variables are supported")
    recorded = selection()
    environment = environment_key()
    values = {"environment": (environment, "available" if environment else "missing"),
              "file": file_key(), "keyring_legacy": ring_key(ring_name())}
    if recorded and recorded["backend"] == "keyring" and recorded["slot"] == "id":
        values["keyring_selected"] = ring_key(ring_name(recorded["keyId"]))
    for index, name in enumerate(extra_env or []):
        values[f"supplied_{index + 1}"] = named_key(name), "available"
    keys = {key_id(value): value for value, _status in values.values() if value}
    report = {label: {"status": status, "keyId": key_id(value) if value else None}
              for label, (value, status) in values.items()}
    return list(keys.values()), report


def key_diagnostics() -> dict:
    report = {"schemaVersion": 1, "readOnly": True, "ready": False}
    try:
        keys, sources = candidates()
        report.update(sources=sources, conflictingIdentities=len({key_id(key) for key in keys}) > 1,
                      recordedSelection=selection(), recoveryRequired=transition_pending())
        key, backend, _slot = existing_key()
        require_no_transition()
        stores = {}
        for name, path in managed_paths().items():
            raw = read_bytes(path)
            if raw is not None:
                decoded_store(raw, [key] if key else [])
                stores[name] = {"format": "plaintext" if raw.lstrip().startswith(b"{") else "encrypted", "keyAvailable": key is not None}
            else:
                stores[name] = {"format": "missing", "keyAvailable": key is not None}
        report["stores"] = stores
        if selection() != report["recordedSelection"] or (key and key_id(key) not in {key_id(item) for item in keys}):
            raise StorageKeyError("state_changed", "Key state changed during diagnosis; retry")
        report.update(ready=key is not None, selectedBackend=backend, selectedKeyId=key_id(key) if key else None)
    except StorageKeyError as exc:
        report["error_class"] = exc.code
    except (OSError, ValueError):
        report["error_class"] = "unreadable_key_configuration"
    return report
