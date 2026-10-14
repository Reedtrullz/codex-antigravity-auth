"""Explicit backup-first key transitions. Backups and recovery material stay encrypted."""
from __future__ import annotations

import base64
import binascii
from contextlib import contextmanager, ExitStack
import hashlib
import json
import os
from pathlib import Path
import tempfile
import uuid

from cryptography.fernet import Fernet, InvalidToken

from . import storage_keys as keys
from .secure_store import SecureStore, file_lock
from .skills.anti.scripts.anti_lib.file_protection import ensure_private_directory, protect_descriptor

FORMAT = "antigravity-storage-backup"
MAX_ARCHIVE_BYTES = 96 * 1024 * 1024
FILE_NAMES = {"accounts", "providers", "key_file", "selection"}


def _paths() -> dict[str, Path]:
    return {**keys.managed_paths(), "key_file": keys.home() / keys.storage_module().FALLBACK_KEY_FILE,
            "selection": keys.home() / keys.SELECTION_FILE}


def _new_backup_path(path: Path) -> Path:
    path = path.expanduser().absolute()
    protected = {*_paths().values(), keys.home() / keys.TRANSITION_FILE,
                 keys.home() / "antigravity-storage-key-init"}
    protected |= {item.with_name(f".{item.name}.lock") for item in protected}
    if path.resolve() in {item.resolve() for item in protected}:
        raise keys.StorageKeyError("invalid_backup_path", "Backup must not use a managed store/key/lock path")
    if path.exists() or path.is_symlink():
        raise keys.StorageKeyError("backup_exists", "Backup destination already exists; nothing will be overwritten")
    return path


def _validate_marker() -> None:
    raw = keys.read_bytes(keys.home() / keys.TRANSITION_FILE, limit=8192)
    if raw is None:
        return
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise keys.StorageKeyError("invalid_transition", "Recovery marker is invalid; preserve it and the encrypted backup") from exc
    if (not isinstance(data, dict) or type(data.get("schemaVersion")) is not int or data["schemaVersion"] != 1
            or not isinstance(data.get("backupSha256"), str) or not keys.KEY_ID.fullmatch(data["backupSha256"])
            or not isinstance(data.get("backupKeyId"), str) or not keys.KEY_ID.fullmatch(data["backupKeyId"])):
        raise keys.StorageKeyError("unsupported_transition", "Recovery marker is unsupported; no state was replaced")


def _snapshot(extra_env=None, extra_keys=None) -> tuple[dict, dict[str, bytes | None]]:
    available, _report = keys.candidates(extra_env)
    available = list({keys.key_id(key): key for key in [*available, *(extra_keys or [])]}.values())
    raw = {name: keys.read_bytes(path, limit=keys.MAX_STORE_BYTES if name in {"accounts", "providers"} else 4096)
           for name, path in _paths().items()}
    used = {}
    for name in ("accounts", "providers"):
        if raw[name] is not None:
            _plain, key = keys.decoded_store(raw[name], available)
            if key:
                used[keys.key_id(key)] = key
    payload = {"format": FORMAT, "schemaVersion": 1, "sourceTransitionPending": keys.transition_pending(),
               "files": {name: {"data": base64.b64encode(data).decode("ascii"), "sha256": hashlib.sha256(data).hexdigest()}
                         if data is not None else None for name, data in raw.items()},
               "keys": used, "keyRequirements": {"recovery": "matching backup Fernet key", "externalStoreKeysRequired": False}}
    return payload, raw


def _envelope(payload: dict, key: str) -> bytes:
    encrypted = Fernet(key.encode("ascii")).encrypt(json.dumps(payload, sort_keys=True).encode("utf-8")).decode("ascii")
    result = json.dumps({"format": FORMAT, "version": 1, "encryption": "fernet", "keyId": keys.key_id(key), "payload": encrypted}, sort_keys=True).encode("utf-8")
    if len(result) > MAX_ARCHIVE_BYTES:
        raise keys.StorageKeyError("size_limit", "Encrypted backup exceeds the archive size limit")
    return result


def _write_new_backup(path: Path, data: bytes) -> None:
    ensure_private_directory(path.parent)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("wb", dir=path.parent, prefix=".key-backup-", delete=False) as handle:
            temporary = Path(handle.name)
            protect_descriptor(handle.fileno(), path=temporary)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, path)  # Atomic no-replace publication in the same directory.
        temporary.unlink()
        temporary = None
        # Persist removal of the staging link too: recovery readers require a
        # singly-linked backup, including after a crash during the transition.
        SecureStore._fsync_directory(path.parent)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _read_backup(path: Path, key: str) -> tuple[dict, dict[str, bytes | None]]:
    raw = keys.read_bytes(path.expanduser(), limit=MAX_ARCHIVE_BYTES)
    try:
        envelope = json.loads(raw) if raw is not None else None
        if (not isinstance(envelope, dict) or set(envelope) != {"format", "version", "encryption", "keyId", "payload"}
                or envelope["format"] != FORMAT or type(envelope["version"]) is not int or envelope["version"] != 1
                or envelope["encryption"] != "fernet" or not isinstance(envelope["payload"], str)):
            raise keys.StorageKeyError("unsupported_backup", "Backup format/version is unsupported")
        if envelope["keyId"] != keys.key_id(key):
            raise keys.StorageKeyError("wrong_backup_key", "The supplied key does not match this encrypted backup")
        plaintext = Fernet(key.encode("ascii")).decrypt(envelope["payload"].encode("ascii"))
        payload = json.loads(plaintext)
        if (not isinstance(payload, dict) or payload.get("format") != FORMAT
                or type(payload.get("schemaVersion")) is not int or payload["schemaVersion"] != 1
                or type(payload.get("sourceTransitionPending")) is not bool
                or not isinstance(payload.get("files"), dict) or set(payload["files"]) != FILE_NAMES
                or not isinstance(payload.get("keys"), dict) or len(payload["keys"]) > 8
                or payload.get("keyRequirements") != {"recovery": "matching backup Fernet key", "externalStoreKeysRequired": False}):
            raise keys.StorageKeyError("invalid_backup", "Encrypted backup manifest is invalid")
        for identity, value in payload["keys"].items():
            if keys.valid_key(value) is None or identity != keys.key_id(value):
                raise keys.StorageKeyError("invalid_backup", "Encrypted backup key manifest is invalid")
        files = {}
        for name, entry in payload["files"].items():
            if entry is None:
                files[name] = None
                continue
            if (not isinstance(entry, dict) or set(entry) != {"data", "sha256"}
                    or not isinstance(entry["data"], str) or not isinstance(entry["sha256"], str)):
                raise keys.StorageKeyError("invalid_backup", "Encrypted backup file manifest is invalid")
            data = base64.b64decode(entry["data"], validate=True)
            limit = keys.MAX_STORE_BYTES if name in {"accounts", "providers"} else 4096
            if len(data) > limit or hashlib.sha256(data).hexdigest() != entry["sha256"]:
                raise keys.StorageKeyError("invalid_backup", "Encrypted backup file checksum/size is invalid")
            files[name] = data
        for name in ("accounts", "providers"):
            if files[name] is not None:
                keys.decoded_store(files[name], list(payload["keys"].values()))
        return payload, files
    except InvalidToken as exc:
        raise keys.StorageKeyError("wrong_backup_key", "Encrypted backup authentication failed") from exc
    except keys.StorageKeyError:
        raise
    except (ValueError, TypeError, UnicodeError, RecursionError, binascii.Error) as exc:
        raise keys.StorageKeyError("invalid_backup", "Encrypted backup cannot be decoded safely") from exc


@contextmanager
def _locked_stores():
    # Normal writers own one store lock, then the initialization lock. Taking
    # both store locks before initialization preserves that order without
    # acquiring AccountManager's thread locks or calling mutating loaders.
    with ExitStack() as locks:
        for path in sorted(keys.managed_paths().values(), key=lambda item: str(item.absolute())):
            locks.enter_context(file_lock(path))
        locks.enter_context(file_lock(keys.home() / "antigravity-storage-key-init"))
        yield


def _public_plan(raw: dict, key: str, backend: str | None = None) -> dict:
    return {"schemaVersion": 1, "write": False, "backupFormat": FORMAT, "backupVersion": 1,
            "keyId": keys.key_id(key), "targetBackend": backend,
            "stores": {name: "present" if raw[name] is not None else "absent" for name in ("accounts", "providers")},
            "cleartextTokensWritten": False}


def backup(output: Path, key_env: str, *, source_key_env=None, write=False) -> dict:
    keys.require_no_transition()
    key = keys.named_key(key_env)
    output = _new_backup_path(output)
    payload, raw = _snapshot(source_key_env)
    result = _public_plan(raw, key)
    if not write:
        return result
    with _locked_stores():
        keys.require_no_transition()
        output = _new_backup_path(output)
        payload, raw = _snapshot(source_key_env)
        _write_new_backup(output, _envelope(payload, key))
    result = _public_plan(raw, key)
    result.update(write=True, backupCreated=True)
    return result


def _publish(stored: dict[str, bytes | None], source_keys: list[str], key: str, backend: str,
             recovery_backup: Path, backup_bytes: bytes) -> dict:
    encrypted = {}
    for name in ("accounts", "providers"):
        encrypted[name] = (Fernet(key.encode("ascii")).encrypt(keys.decoded_store(stored[name], source_keys)[0])
                           if stored[name] is not None else None)
    marker_path = keys.home() / keys.TRANSITION_FILE
    marker = {"schemaVersion": 1, "operationId": uuid.uuid4().hex,
              "backup": str(recovery_backup.absolute()), "backupSha256": hashlib.sha256(backup_bytes).hexdigest(),
              "backupKeyId": keys.key_id(key), "targetBackend": backend}
    store = SecureStore()
    store.atomic_write_text(marker_path, json.dumps(marker, sort_keys=True) + "\n")
    try:
        slot = keys.install_key(key, backend)
        for name, path in keys.managed_paths().items():
            if encrypted[name] is None:
                path.unlink(missing_ok=True)
                SecureStore._fsync_directory(path.parent)
            else:
                store._atomic_write_bytes_unlocked(path, encrypted[name])
        keys.write_selection(key, backend, slot)
        for name, path in keys.managed_paths().items():
            if keys.read_bytes(path) != encrypted[name]:
                raise keys.StorageKeyError("verification_failed", "Re-encrypted store verification failed")
        available, _status = (keys.file_key() if backend == "file" else keys.ring_key(keys.ring_name(keys.key_id(key))))
        if available is None or keys.key_id(available) != keys.key_id(key):
            raise keys.StorageKeyError("verification_failed", "Selected key verification failed")
        marker_path.unlink()
    except Exception as exc:
        raise keys.StorageKeyError("recovery_required", "Key transition interrupted; restore from the encrypted backup with the same key and a new safety-backup path") from exc
    try:
        SecureStore._fsync_directory(marker_path.parent)
    except OSError as exc:
        raise keys.StorageKeyError("finalization_sync_failed", "Stores and key were verified, but final directory synchronization failed; the encrypted backup is retained") from exc
    current_env = keys.environment_key()
    return {"schemaVersion": 1, "write": True, "completed": True, "selectedBackend": backend,
            "keyId": keys.key_id(key), "backupPreserved": True, "cleartextTokensWritten": False,
            "environmentKeyMustBeUpdated": bool(current_env and keys.key_id(current_env) != keys.key_id(key))}


def reencrypt(key_env: str, backend: str, backup_path: Path, *, source_key_env=None, write=False) -> dict:
    keys.require_no_transition()
    if backend not in {"file", "keyring"}:
        raise keys.StorageKeyError("invalid_backend", "Target backend must be file or keyring")
    key = keys.named_key(key_env)
    backup_path = _new_backup_path(backup_path)
    payload, raw = _snapshot(source_key_env)
    if not write:
        return _public_plan(raw, key, backend)
    with _locked_stores():
        keys.require_no_transition()
        backup_path = _new_backup_path(backup_path)
        payload, raw = _snapshot(source_key_env)
        encoded = _envelope(payload, key)
        _write_new_backup(backup_path, encoded)
        return _publish(raw, list(payload["keys"].values()), key, backend, backup_path, encoded)


def restore(backup_path: Path, key_env: str, backend: str, safety_backup: Path | None,
            *, source_key_env=None, write=False) -> dict:
    if backend not in {"file", "keyring"}:
        raise keys.StorageKeyError("invalid_backend", "Target backend must be file or keyring")
    key = keys.named_key(key_env)
    payload, original = _read_backup(backup_path, key)  # Wrong keys cannot reach any mutation.
    _validate_marker()
    extra_keys = [key, *payload["keys"].values()]
    _current_payload, current = _snapshot(source_key_env, extra_keys)
    result = _public_plan(original, key, backend)
    result["replacesExistingState"] = any(value is not None for value in current.values())
    result["recoveryRequired"] = keys.transition_pending()
    result["backupCapturedDuringRecovery"] = payload["sourceTransitionPending"]
    if not write:
        return result
    if safety_backup is None:
        raise keys.StorageKeyError("safety_backup_required", "Restore --write requires a new encrypted safety-backup path")
    safety_backup = _new_backup_path(safety_backup)
    with _locked_stores():
        _validate_marker()
        payload, original = _read_backup(backup_path, key)
        safety_backup = _new_backup_path(safety_backup)
        current_payload, _current = _snapshot(source_key_env, [key, *payload["keys"].values()])
        encoded = _envelope(current_payload, key)
        _write_new_backup(safety_backup, encoded)
        return _publish(original, list(payload["keys"].values()), key, backend, safety_backup, encoded)
