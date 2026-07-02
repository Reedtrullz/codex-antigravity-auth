import secrets
import hashlib
import base64
import json
import math
import time
import urllib.request
import urllib.error
from urllib.parse import urlencode
from .constants import (
    require_credentials,
    SCOPES,
)
from .redaction import redact_secret_text

# In-memory PKCE verifier store
_pkce_verifier_store: dict[str, dict[str, str]] = {}
_PKCE_VERIFIER_TTL_SECONDS = 600
OAUTH_HTTP_TIMEOUT_SECONDS = 15.0


def token_expires_in_seconds(tokens: dict, default: int = 3600) -> int:
    try:
        expires_in = float(tokens.get("expires_in", default))
    except (AttributeError, TypeError, ValueError):
        return default
    if not math.isfinite(expires_in) or expires_in <= 0:
        return default
    return int(expires_in)


def generate_pkce() -> dict:
    verifier = secrets.token_urlsafe(64)
    sha256 = hashlib.sha256(verifier.encode("utf-8")).digest()
    challenge = base64.urlsafe_b64encode(sha256).decode("utf-8").rstrip("=")
    return {
        "challenge": challenge,
        "verifier": verifier
    }

def encode_state(payload: dict) -> str:
    json_bytes = json.dumps(payload, separators=(',', ':')).encode("utf-8")
    return base64.urlsafe_b64encode(json_bytes).decode("utf-8").rstrip("=")

def decode_state(state: str) -> dict:
    normalized = state.replace("-", "+").replace("_", "/")
    padded = normalized + "=" * ((4 - len(normalized) % 4) % 4)
    json_bytes = base64.b64decode(padded)
    parsed = json.loads(json_bytes.decode("utf-8", errors="ignore"))
    if not isinstance(parsed, dict):
        raise ValueError("Invalid state format")
    return parsed

def get_pkce_verifier(state_id: str) -> dict[str, str] | None:
    verifier_info = _pkce_verifier_store.pop(state_id, None)
    if not verifier_info:
        return None
    try:
        created_at = float(verifier_info.get("createdAt", "0"))
    except ValueError:
        return None
    if time.time() - created_at > _PKCE_VERIFIER_TTL_SECONDS:
        return None
    return verifier_info

def authorize_antigravity() -> dict:
    cid, csec = require_credentials()
    pkce = generate_pkce()
    
    state_id = secrets.token_urlsafe(32)
    state_payload = {"id": state_id}
    encoded_state = encode_state(state_payload)
    
    _pkce_verifier_store[state_id] = {
        "verifier": pkce["verifier"],
        "createdAt": str(time.time()),
    }
    
    params = {
        "client_id": cid,
        "redirect_uri": "http://localhost:51121/oauth-callback",
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "code_challenge": pkce["challenge"],
        "code_challenge_method": "S256",
        "state": encoded_state,
        "access_type": "offline",
        "prompt": "consent",
    }
    
    url = f"https://accounts.google.com/o/oauth2/v2/auth?{urlencode(params)}"
    return {"url": url, "state_id": state_id}

def exchange_antigravity(code: str, verifier: str) -> dict:
    cid, csec = require_credentials()
    payload = {
        "client_id": cid,
        "client_secret": csec,
        "code": code,
        "code_verifier": verifier,
        "grant_type": "authorization_code",
        "redirect_uri": "http://localhost:51121/oauth-callback",
    }
    
    req = urllib.request.Request(
        "https://oauth2.googleapis.com/token",
        data=urlencode(payload).encode("utf-8"),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    
    try:
        with urllib.request.urlopen(req, timeout=OAUTH_HTTP_TIMEOUT_SECONDS) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"OAuth exchange failed ({e.code}): {redact_secret_text(error_body)}")
    except Exception as e:
        raise RuntimeError(f"OAuth exchange failed: {redact_secret_text(str(e))}")

def refresh_access_token(refresh_token: str) -> dict:
    cid, csec = require_credentials()
    payload = {
        "client_id": cid,
        "client_secret": csec,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }
    req = urllib.request.Request(
        "https://oauth2.googleapis.com/token",
        data=urlencode(payload).encode("utf-8"),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=OAUTH_HTTP_TIMEOUT_SECONDS) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"Token refresh failed ({e.code}): {redact_secret_text(error_body)}")
    except Exception as e:
        raise RuntimeError(f"Token refresh failed: {redact_secret_text(str(e))}")
