import json
import os
import stat
import threading
import keyring
import base64
import hashlib
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable
from cryptography.fernet import Fernet, InvalidToken
from .constants import ANTIGRAVITY_ACCOUNTS_FILE, get_codex_home
from .account_state import SCHEMA_VERSION, migrate_account_state
from .secure_store import SecureStore

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback keeps imports working.
    fcntl = None

_accounts_lock = threading.RLock()

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
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & 0o077:
            os.chmod(path, 0o600)


@contextmanager
def _exclusive_file_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(f".{path.name}.lock")
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        os.chmod(lock_path, 0o600)
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)

def _normalize_fernet_key(secret: str) -> str:
    try:
        Fernet(secret.encode("utf-8"))
        return secret
    except Exception:
        digest = hashlib.sha256(secret.encode("utf-8")).digest()
        return base64.urlsafe_b64encode(digest).decode("utf-8")

def _get_file_fallback_key() -> str:
    path = get_codex_home() / FALLBACK_KEY_FILE
    if path.is_file():
        _ensure_private_file(path)
        return path.read_text(encoding="utf-8").strip()

    key = Fernet.generate_key().decode("utf-8")
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        _ensure_private_file(path)
        return path.read_text(encoding="utf-8").strip()
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(key)
    return key

def _get_encryption_key() -> str:
    """Retrieve or generate a secure encryption key from the system keyring."""
    env_key = os.environ.get("ANTIGRAVITY_STORAGE_KEY")
    if env_key:
        return _normalize_fernet_key(env_key)

    try:
        initialization_lock = get_codex_home() / "antigravity-storage-key-init"
        with _exclusive_file_lock(initialization_lock):
            key = keyring.get_password(KEYRING_SERVICE_NAME, KEYRING_KEY_NAME)
            if not key:
                candidate = Fernet.generate_key().decode("utf-8")
                keyring.set_password(KEYRING_SERVICE_NAME, KEYRING_KEY_NAME, candidate)
                key = keyring.get_password(KEYRING_SERVICE_NAME, KEYRING_KEY_NAME) or candidate
            return key
    except Exception:
        # Headless systems may not have a usable keyring. Use a generated,
        # machine-local key instead of a source-known static key.
        return _get_file_fallback_key()


def _peek_encryption_key() -> str | None:
    """Return an already-configured key without creating keyring or fallback state."""
    env_key = os.environ.get("ANTIGRAVITY_STORAGE_KEY")
    if env_key:
        return _normalize_fernet_key(env_key)
    try:
        key = keyring.get_password(KEYRING_SERVICE_NAME, KEYRING_KEY_NAME)
    except Exception:
        key = None
    if key:
        return key
    fallback = get_codex_home() / FALLBACK_KEY_FILE
    if not fallback.is_file() or fallback.is_symlink():
        return None
    try:
        return fallback.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def account_store_diagnostics() -> dict[str, Any]:
    """Inspect account-store format and schema without migrating or writing it."""
    path = Path(os.path.expanduser(ANTIGRAVITY_ACCOUNTS_FILE))
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
    p = Path(os.path.expanduser(ANTIGRAVITY_ACCOUNTS_FILE))
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _load_secure_json_unlocked(
    path: Path,
    default_factory: Callable[[], dict[str, Any]],
    *,
    strict: bool = False,
) -> tuple[dict[str, Any], bool]:
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
    path = Path(os.path.expanduser(ANTIGRAVITY_ACCOUNTS_FILE))
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
