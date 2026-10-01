import json
import os
import threading
import keyring
import base64
import hashlib
import time
from pathlib import Path
from .namespaces import gateway_home, gateway_file
from typing import Any, Callable
from cryptography.fernet import Fernet, InvalidToken
from .constants import ANTIGRAVITY_ACCOUNTS_FILE, get_codex_home
from .account_state import SCHEMA_VERSION, migrate_account_state
from .secure_store import SecureStore, file_lock as _exclusive_file_lock
from .skills.anti.scripts.anti_lib.file_protection import ensure_private_directory, protect_descriptor, protect_existing_file

_accounts_lock = threading.RLock()
_DEFAULT_GET_CODEX_HOME = get_codex_home


def _codex_home_read_only() -> Path:
    if get_codex_home is not _DEFAULT_GET_CODEX_HOME:
        return get_codex_home()
    return gateway_home()

# Stable service name for OS Keyring integration
KEYRING_SERVICE_NAME = "codex-antigravity-auth"
KEYRING_KEY_NAME = "storage-encryption-key"
FALLBACK_KEY_FILE = "antigravity-storage.key"


def default_accounts_data() -> dict[str, Any]:
    return {"accounts": [], "activeIndex": 0, "activeIndexByFamily": {"claude": 0, "gemini": 0}}


def normalize_accounts_data(data: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(data, dict):
        data = {}
    accounts = data.get("accounts")
    if not isinstance(accounts, list):
        accounts = []
    data["accounts"] = [account for account in accounts if isinstance(account, dict)]

    active_index = data.get("activeIndex")
    if not isinstance(active_index, int) or isinstance(active_index, bool):
        active_index = 0
    if active_index < 0 or active_index >= max(len(data["accounts"]), 1):
        active_index = 0
    data["activeIndex"] = active_index

    family_map = data.get("activeIndexByFamily")
    if not isinstance(family_map, dict):
        family_map = {}
    normalized_family_map: dict[str, int] = {}
    for family in ("claude", "gemini"):
        value = family_map.get(family, 0)
        if not isinstance(value, int) or isinstance(value, bool):
            value = 0
        if value < 0 or value >= max(len(data["accounts"]), 1):
            value = 0
        normalized_family_map[family] = value
    data["activeIndexByFamily"] = normalized_family_map

    account_state = data.get("accountState")
    if account_state is not None and not isinstance(account_state, dict):
        data["accountState"] = {}
    migrated, _changed = migrate_account_state(data, now=time.time())
    return migrated


def _ensure_private_file(path: Path) -> None:
    if path.is_symlink():
        raise RuntimeError(f"Refusing to use symlinked secret file: {path}")
    if path.exists():
        protect_existing_file(path)


def _normalize_fernet_key(secret: str) -> str:
    try:
        Fernet(secret.encode("utf-8"))
        return secret
    except Exception:
        digest = hashlib.sha256(secret.encode("utf-8")).digest()
        return base64.urlsafe_b64encode(digest).decode("utf-8")

def _get_file_fallback_key(candidate: str | None = None) -> str:
    path = get_codex_home() / FALLBACK_KEY_FILE
    if path.is_file():
        _ensure_private_file(path)
        return path.read_text(encoding="utf-8").strip()

    key = candidate or Fernet.generate_key().decode("utf-8")
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        _ensure_private_file(path)
        return path.read_text(encoding="utf-8").strip()
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            protect_descriptor(f.fileno(), path=path)
            f.write(key)
    except Exception:
        path.unlink(missing_ok=True)  # We exclusively created this empty key file.
        raise
    return key

def _get_encryption_key() -> str:
    from .storage_keys import get_key
    return get_key(create=True)


def _peek_encryption_key() -> str | None:
    from .storage_keys import get_key
    return get_key(create=False)


def _require_no_key_transition() -> None:
    from .storage_keys import require_no_transition
    require_no_transition()


def account_store_diagnostics() -> dict[str, Any]:
    """Inspect account-store format and schema without migrating or writing it."""
    path = gateway_file(ANTIGRAVITY_ACCOUNTS_FILE, "antigravity-accounts.json")
    report: dict[str, Any] = {
        "path": str(path),
        "exists": path.is_file(),
        "accessible": False,
        "format": "missing" if not path.exists() else "unknown",
        "migration": "none" if not path.exists() else "blocked",
        "account_state_schema_version": 0,
        "target_account_state_schema_version": SCHEMA_VERSION,
        "account_count": 0,
    }
    try:
        _require_no_key_transition()
    except ValueError as exc:
        report["error_class"] = getattr(exc, "code", "key_configuration_unavailable")
        return report
    if not path.exists():
        report["accessible"] = True
        return report
    if path.is_symlink() or not path.is_file():
        report["error_class"] = "unsafe_path"
        return report
    try:
        raw = path.read_bytes()
    except OSError:
        report["error_class"] = "read_error"
        return report
    stripped = raw.lstrip()
    try:
        if stripped.startswith(b"{"):
            decoded = raw.decode("utf-8")
            report["format"] = "plaintext"
        else:
            key = _peek_encryption_key()
            if not key:
                report["format"] = "encrypted"
                report["error_class"] = "key_unavailable"
                return report
            decoded = Fernet(key.encode("utf-8")).decrypt(raw).decode("utf-8")
            report["format"] = "encrypted"
        data = json.loads(decoded)
    except (InvalidToken, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        report["error_class"] = "invalid_or_undecryptable"
        return report
    if not isinstance(data, dict):
        report["error_class"] = "invalid_top_level"
        return report
    accounts = data.get("accounts")
    state = data.get("accountState")
    version = state.get("schemaVersion") if isinstance(state, dict) else None
    report.update(
        {
            "accessible": True,
            "account_count": len(accounts) if isinstance(accounts, list) else 0,
            "account_state_schema_version": version if isinstance(version, int) and not isinstance(version, bool) else 0,
        }
    )
    report["migration"] = (
        "completed"
        if report["format"] == "encrypted" and report["account_state_schema_version"] == SCHEMA_VERSION
        else "pending"
    )
    return report


def provider_store_diagnostics(path: Path) -> dict[str, Any]:
    """Inspect provider-store encryption/accessibility without loading or migrating it."""
    report: dict[str, Any] = {
        "path": str(path),
        "exists": path.is_file(),
        "accessible": False,
        "format": "missing" if not path.exists() else "unknown",
        "migration": "none" if not path.exists() else "blocked",
        "provider_count": 0,
    }
    try:
        _require_no_key_transition()
    except ValueError as exc:
        report["error_class"] = getattr(exc, "code", "key_configuration_unavailable")
        return report
    if not path.exists():
        report["accessible"] = True
        return report
    if path.is_symlink() or not path.is_file():
        report["error_class"] = "unsafe_path"
        return report
    try:
        raw = path.read_bytes()
        if raw.lstrip().startswith(b"{"):
            decoded = raw.decode("utf-8")
            report["format"] = "plaintext"
        else:
            key = _peek_encryption_key()
            report["format"] = "encrypted"
            if not key:
                report["error_class"] = "key_unavailable"
                return report
            decoded = Fernet(key.encode("utf-8")).decrypt(raw).decode("utf-8")
        data = json.loads(decoded)
    except (OSError, InvalidToken, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        report["error_class"] = "invalid_or_undecryptable"
        return report
    if not isinstance(data, dict):
        report["error_class"] = "invalid_top_level"
        return report
    providers = data.get("providers")
    report.update(
        {
            "accessible": True,
            "provider_count": len(providers) if isinstance(providers, dict) else 0,
            "migration": "completed" if report["format"] == "encrypted" else "pending",
        }
    )
    return report

def encrypt_payload(data_str: str) -> bytes:
    key = _get_encryption_key()
    fernet = Fernet(key.encode("utf-8"))
    return fernet.encrypt(data_str.encode("utf-8"))

def decrypt_payload(encrypted_bytes: bytes) -> str:
    key = _get_encryption_key()
    fernet = Fernet(key.encode("utf-8"))
    return fernet.decrypt(encrypted_bytes).decode("utf-8")

def get_accounts_json_path() -> Path:
    p = accounts_json_path_read_only()
    ensure_private_directory(p.parent, enforce_existing=True)
    return p


def accounts_json_path_read_only() -> Path:
    return gateway_file(ANTIGRAVITY_ACCOUNTS_FILE, "antigravity-accounts.json")


def _load_secure_json_unlocked(
    path: Path,
    default_factory: Callable[[], dict[str, Any]],
    *,
    strict: bool = False,
) -> tuple[dict[str, Any], bool]:
    _require_no_key_transition()
    if not path.is_file():
        return default_factory(), False
    _ensure_private_file(path)
    encrypted_data = path.read_bytes()
    try:
        decrypted_str = decrypt_payload(encrypted_data)
        data = json.loads(decrypted_str)
        plaintext = False
    except Exception:
        data = json.loads(encrypted_data.decode("utf-8"))
        plaintext = True
    if not isinstance(data, dict):
        if strict:
            raise ValueError(f"{path} top-level JSON value is not an object")
        data = default_factory()
    return data, plaintext


def _save_secure_json_unlocked(path: Path, data: dict[str, Any]) -> None:
    encrypted_data = encrypt_payload(json.dumps(data, indent=2))
    # The caller already owns the cross-process store lock.
    SecureStore(key_provider=_get_encryption_key)._atomic_write_bytes_unlocked(
        path,
        encrypted_data,
        mode=0o600,
    )


def load_secure_json_file(
    path: Path,
    default_factory: Callable[[], dict[str, Any]],
    *,
    normalize: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    error_label: str,
) -> dict[str, Any]:
    try:
        with _exclusive_file_lock(path):
            data, plaintext = _load_secure_json_unlocked(path, default_factory)
            if normalize:
                data = normalize(data)
            if plaintext:
                _save_secure_json_unlocked(path, data)
            return data
    except Exception as e:
        raise RuntimeError(f"Failed to load {error_label} file {path}: {e}") from e


def load_secure_json_file_read_only(
    path: Path,
    default_factory: Callable[[], dict[str, Any]],
    *,
    normalize: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    error_label: str,
) -> dict[str, Any]:
    """Read a secure store without chmod, migration, key creation, or writes."""
    _require_no_key_transition()
    if not path.exists():
        return default_factory()
    if path.is_symlink() or not path.is_file():
        raise RuntimeError(f"Failed to inspect {error_label} file {path}: unsafe store path")
    try:
        raw = path.read_bytes()
        if raw.lstrip().startswith(b"{"):
            decoded = raw.decode("utf-8")
        else:
            key = _peek_encryption_key()
            if not key:
                raise RuntimeError("configured encryption key is unavailable")
            decoded = Fernet(key.encode("utf-8")).decrypt(raw).decode("utf-8")
        data = json.loads(decoded)
        if not isinstance(data, dict):
            raise ValueError("top-level JSON value is not an object")
        return normalize(data) if normalize else data
    except Exception as exc:
        raise RuntimeError(f"Failed to inspect {error_label} file {path}: {exc}") from exc


def save_secure_json_file(
    path: Path,
    data: dict[str, Any],
    *,
    error_label: str,
    default_factory: Callable[[], dict[str, Any]] | None = None,
    normalize: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> None:
    try:
        with _exclusive_file_lock(path):
            if path.exists() and default_factory is not None:
                existing, _ = _load_secure_json_unlocked(path, default_factory, strict=True)
                if normalize:
                    normalize(existing)
            _save_secure_json_unlocked(path, data)
    except Exception as e:
        raise RuntimeError(f"Failed to save {error_label} file securely: {e}") from e


def update_secure_json_file(
    path: Path,
    default_factory: Callable[[], dict[str, Any]],
    mutator: Callable[[dict[str, Any]], Any],
    *,
    normalize: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    error_label: str,
) -> Any:
    try:
        with _exclusive_file_lock(path):
            data, plaintext = _load_secure_json_unlocked(path, default_factory, strict=True)
            if normalize:
                data = normalize(data)
            result = mutator(data)
            if plaintext or result is not False:
                if normalize:
                    data = normalize(data)
                _save_secure_json_unlocked(path, data)
            return result
    except Exception as e:
        raise RuntimeError(f"Failed to update {error_label} file securely: {e}") from e

def load_accounts() -> dict[str, Any]:
    with _accounts_lock:
        path = get_accounts_json_path()
        return load_secure_json_file(
            path,
            default_accounts_data,
            normalize=normalize_accounts_data,
            error_label="accounts",
        )


def load_accounts_read_only() -> dict[str, Any]:
    path = accounts_json_path_read_only()
    return load_secure_json_file_read_only(
        path,
        default_accounts_data,
        normalize=normalize_accounts_data,
        error_label="accounts",
    )

def save_accounts(data: dict[str, Any]) -> None:
    with _accounts_lock:
        path = get_accounts_json_path()
        save_secure_json_file(
            path,
            normalize_accounts_data(data),
            error_label="accounts",
            default_factory=default_accounts_data,
            normalize=normalize_accounts_data,
        )


def update_accounts(mutator: Callable[[dict[str, Any]], Any]) -> Any:
    with _accounts_lock:
        return update_secure_json_file(
            get_accounts_json_path(),
            default_accounts_data,
            mutator,
            normalize=normalize_accounts_data,
            error_label="accounts",
        )
