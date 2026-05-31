import json
import os
import threading
import keyring
import base64
from pathlib import Path
from typing import Any
from cryptography.fernet import Fernet
from .constants import ANTIGRAVITY_ACCOUNTS_FILE, get_codex_home

_accounts_lock = threading.RLock()

# Stable service name for OS Keyring integration
KEYRING_SERVICE_NAME = "codex-antigravity-auth"
KEYRING_KEY_NAME = "storage-encryption-key"

def _get_encryption_key() -> str:
    """Retrieve or generate a secure encryption key from the system keyring."""
    try:
        key = keyring.get_password(KEYRING_SERVICE_NAME, KEYRING_KEY_NAME)
        if not key:
            # Generate a new Fernet key and save it securely in the OS Keychain/Credential Manager
            key = Fernet.generate_key().decode("utf-8")
            keyring.set_password(KEYRING_SERVICE_NAME, KEYRING_KEY_NAME, key)
        return key
    except Exception:
        # Graceful fallback if no secure OS keyring service is available (e.g. headless CI)
        fallback_secret = "stable-fallback-salt-for-headless-systems-31415926535"
        key_hash = base64.urlsafe_b64encode(fallback_secret.encode("utf-8")[:32].ljust(32, b"="))
        return key_hash.decode("utf-8")

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
        except Exception:
            return {"accounts": [], "activeIndex": 0, "activeIndexByFamily": {"claude": 0, "gemini": 0}}

def save_accounts(data: dict[str, Any]) -> None:
    with _accounts_lock:
        path = get_accounts_json_path()
        try:
            json_str = json.dumps(data, indent=2)
            encrypted_data = encrypt_payload(json_str)
            temp_path = path.with_suffix(".tmp")
            with open(temp_path, "wb") as f:
                f.write(encrypted_data)
            os.replace(temp_path, path)
        except Exception as e:
            raise RuntimeError(f"Failed to save accounts file securely: {e}")
