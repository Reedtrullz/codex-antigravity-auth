import os
import json
import secrets
import sys
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


def get_codex_home() -> Path:
    p = Path(os.path.expanduser("~/.codex"))
    p.mkdir(parents=True, exist_ok=True)
    return p


def resolve_oauth_credentials() -> tuple[str | None, str | None]:
    client_id = os.environ.get("ANTIGRAVITY_CLIENT_ID")
    client_secret = os.environ.get("ANTIGRAVITY_CLIENT_SECRET")
    if client_id and client_secret:
        return client_id, client_secret

    cred_path = Path(os.path.expanduser(CREDENTIALS_FILE))
    if cred_path.is_file():
        try:
            with open(cred_path, "r") as f:
                data = json.load(f)
                return data.get("client_id") or data.get("clientId"), data.get("client_secret") or data.get("clientSecret")
        except Exception:
            pass

    return DEFAULT_CLIENT_ID, DEFAULT_CLIENT_SECRET


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
