import os
import json
import sys
import stat
import ipaddress
from pathlib import Path

# Defaults
DEFAULT_CLIENT_ID = None
DEFAULT_CLIENT_SECRET = None
REDIRECT_URI = "http://localhost:51121/oauth-callback"

SCOPES = [
    "https://www.googleapis.com/auth/cloud-platform",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
    "https://www.googleapis.com/auth/cclog",
    "https://www.googleapis.com/auth/experimentsandconfigs",
]

ANTIGRAVITY_ENDPOINT_PROD = "https://cloudcode-pa.googleapis.com"
ANTIGRAVITY_ACCOUNTS_FILE = "~/.codex/antigravity-accounts.json"
CREDENTIALS_FILE = "~/.codex/antigravity-credentials.json"


def get_platform() -> str:
    return "WINDOWS" if sys.platform == "win32" else "MACOS"


def is_loopback_host(host: str | None) -> bool:
    if not host:
        return False
    normalized = str(host).strip().strip("[]").lower()
    if normalized in ("localhost", "testclient"):
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def get_codex_home() -> Path:
    p = Path(os.path.expanduser("~/.codex"))
    p.mkdir(parents=True, exist_ok=True)
    return p


def _strip(value) -> str:
    return value.strip() if isinstance(value, str) else ""


def _load_file_credentials() -> tuple[str | None, str | None]:
    cred_path = Path(os.path.expanduser(CREDENTIALS_FILE))
    if not cred_path.is_file():
        return None, None
    try:
        mode = stat.S_IMODE(cred_path.stat().st_mode)
        if mode & 0o077:
            os.chmod(cred_path, 0o600)
        with open(cred_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return None, None

    if not isinstance(data, dict):
        return None, None

    client_id = (
        _strip(data.get("client_id"))
        or _strip(data.get("clientId"))
        or _strip(data.get("ANTIGRAVITY_CLIENT_ID"))
    )
    client_secret = (
        _strip(data.get("client_secret"))
        or _strip(data.get("clientSecret"))
        or _strip(data.get("ANTIGRAVITY_CLIENT_SECRET"))
    )
    return client_id or None, client_secret or None


def resolve_oauth_credentials() -> tuple[str | None, str | None]:
    env_client_id = _strip(os.environ.get("ANTIGRAVITY_CLIENT_ID"))
    env_client_secret = _strip(os.environ.get("ANTIGRAVITY_CLIENT_SECRET"))
    file_client_id, file_client_secret = _load_file_credentials()

    return (
        env_client_id or file_client_id or DEFAULT_CLIENT_ID,
        env_client_secret or file_client_secret or DEFAULT_CLIENT_SECRET,
    )


def require_credentials() -> tuple[str, str]:
    cid, csec = resolve_oauth_credentials()
    if not cid or not csec:
        raise RuntimeError(
            "Antigravity OAuth credentials not configured.\n\n"
            "Options:\n"
            "  1. Set ANTIGRAVITY_CLIENT_ID and ANTIGRAVITY_CLIENT_SECRET env vars\n"
            "  2. Create ~/.codex/antigravity-credentials.json with client_id/client_secret\n"
        )
    return cid, csec
