import json
import os
import stat
import threading
import keyring
import base64
import hashlib
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable
from cryptography.fernet import Fernet
from .constants import ANTIGRAVITY_ACCOUNTS_FILE, get_codex_home

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
    return data


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
        key = keyring.get_password(KEYRING_SERVICE_NAME, KEYRING_KEY_NAME)
        if not key:
            # Generate a new Fernet key and save it securely in the OS Keychain/Credential Manager
            key = Fernet.generate_key().decode("utf-8")
            keyring.set_password(KEYRING_SERVICE_NAME, KEYRING_KEY_NAME, key)
        return key
    except Exception:
        # Headless systems may not have a usable keyring. Use a generated,
        # machine-local key instead of a source-known static key.
        return _get_file_fallback_key()

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
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = None
    try:
        encrypted_data = encrypt_payload(json.dumps(data, indent=2))
        with tempfile.NamedTemporaryFile("wb", delete=False, dir=path.parent, prefix=f".{path.name}.", suffix=".tmp") as f:
            temp_path = Path(f.name)
            f.write(encrypted_data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(temp_path, 0o600)
        os.replace(temp_path, path)
    except Exception:
        if temp_path and temp_path.exists():
            try:
                temp_path.unlink()
            except Exception:
                pass
        raise


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
