import json
import os
import threading
import keyring
import base64
import hashlib
import tempfile
from pathlib import Path
from typing import Any
from cryptography.fernet import Fernet
from .constants import ANTIGRAVITY_ACCOUNTS_FILE, get_codex_home

_accounts_lock = threading.RLock()

# Stable service name for OS Keyring integration
KEYRING_SERVICE_NAME = "codex-antigravity-auth"
KEYRING_KEY_NAME = "storage-encryption-key"
FALLBACK_KEY_FILE = "antigravity-storage.key"

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
        return path.read_text(encoding="utf-8").strip()

    key = Fernet.generate_key().decode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
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

def load_accounts() -> dict[str, Any]:
    with _accounts_lock:
        path = get_accounts_json_path()
        if not path.is_file():
            return {"accounts": [], "activeIndex": 0, "activeIndexByFamily": {"claude": 0, "gemini": 0}}
        try:
            with open(path, "rb") as f:
                encrypted_data = f.read()
            # Try decrypting first, fallback to reading plaintext for backward compatibility
            try:
                decrypted_str = decrypt_payload(encrypted_data)
                data = json.loads(decrypted_str)
            except Exception:
                # File might be stored as plaintext JSON initially
                data = json.loads(encrypted_data.decode("utf-8"))
            
            if not isinstance(data, dict):
                data = {}
            data.setdefault("accounts", [])
            data.setdefault("activeIndex", 0)
            data.setdefault("activeIndexByFamily", {"claude": 0, "gemini": 0})
            return data
        except Exception as e:
            raise RuntimeError(f"Failed to load accounts file {path}: {e}") from e

def save_accounts(data: dict[str, Any]) -> None:
    with _accounts_lock:
        path = get_accounts_json_path()
        temp_path = None
        try:
            json_str = json.dumps(data, indent=2)
            encrypted_data = encrypt_payload(json_str)
            with tempfile.NamedTemporaryFile("wb", delete=False, dir=path.parent, prefix=f".{path.name}.", suffix=".tmp") as f:
                temp_path = Path(f.name)
                f.write(encrypted_data)
            os.chmod(temp_path, 0o600)
            os.replace(temp_path, path)
        except Exception as e:
            if temp_path and temp_path.exists():
                try:
                    temp_path.unlink()
                except Exception:
                    pass
            raise RuntimeError(f"Failed to save accounts file securely: {e}")
