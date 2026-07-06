import os
import json
import sys
import stat
import ipaddress
import tempfile
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
GATEWAY_TOKEN_MIN_LENGTH = 32


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


def validate_gateway_token_strength(token: str | None) -> str:
    token = _strip(token)
    if not token:
        raise ValueError("ANTIGRAVITY_GATEWAY_TOKEN must be set when remote access is enabled.")
    if len(token) < GATEWAY_TOKEN_MIN_LENGTH:
        raise ValueError(
            f"ANTIGRAVITY_GATEWAY_TOKEN must be at least {GATEWAY_TOKEN_MIN_LENGTH} visible characters when remote access is enabled."
        )
    if any(ord(ch) < 0x21 or ord(ch) > 0x7E for ch in token):
        raise ValueError("ANTIGRAVITY_GATEWAY_TOKEN must contain only visible ASCII characters without whitespace.")
    return token


def get_codex_home() -> Path:
    p = Path(os.path.expanduser("~/.codex"))
    p.mkdir(parents=True, exist_ok=True)
    return p


def _strip(value) -> str:
    return value.strip() if isinstance(value, str) else ""


def _validate_oauth_credential_value(value: str, *, label: str) -> str:
    value = _strip(value)
    if not value:
        raise ValueError(f"{label} must not be empty")
    if any(ord(ch) < 0x21 or ord(ch) > 0x7E for ch in value):
        raise ValueError(f"{label} must contain only visible ASCII characters without whitespace")
    return value


def save_oauth_credentials(client_id: str, client_secret: str) -> Path:
    """Persist Google OAuth desktop-client credentials with private file mode."""
    client_id = _validate_oauth_credential_value(client_id, label="OAuth client id")
    client_secret = _validate_oauth_credential_value(client_secret, label="OAuth client secret")
    cred_path = Path(os.path.expanduser(CREDENTIALS_FILE))
    if cred_path.is_symlink():
        raise RuntimeError(f"Refusing to write OAuth credentials through symlink: {cred_path}")
    cred_path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        {"client_id": client_id, "client_secret": client_secret},
        indent=2,
        sort_keys=True,
    ) + "\n"
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            delete=False,
            dir=cred_path.parent,
            prefix=f".{cred_path.name}.",
            suffix=".tmp",
        ) as handle:
            temp_path = Path(handle.name)
            os.chmod(temp_path, 0o600)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, cred_path)
        os.chmod(cred_path, 0o600)
    except Exception:
        if temp_path and temp_path.exists():
            try:
                temp_path.unlink()
            except Exception:
                pass
        raise
    return cred_path


def _load_file_credentials() -> tuple[str | None, str | None]:
    cred_path = Path(os.path.expanduser(CREDENTIALS_FILE))
    fd = None
    try:
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(cred_path, flags)
        stat_result = os.fstat(fd)
        if not stat.S_ISREG(stat_result.st_mode):
            return None, None
        mode = stat.S_IMODE(stat_result.st_mode)
        if mode & 0o077:
            if hasattr(os, "fchmod"):
                os.fchmod(fd, 0o600)
            else:
                os.chmod(cred_path, 0o600)
        with os.fdopen(fd, "r", encoding="utf-8") as f:
            fd = None
            data = json.load(f)
    except Exception:
        return None, None
    finally:
        if fd is not None:
            os.close(fd)

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
