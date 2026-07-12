from __future__ import annotations

import re
from typing import Any

try:
    from codex_antigravity_auth.redaction import redact_secret_text as package_redact_secret_text
except Exception:  # pragma: no cover - personal skill installs can run without the package.
    package_redact_secret_text = None


REDACTION_MARKER = "<redacted>"
SECRET_KEY_FRAGMENTS = (
    "access_token", "accesstoken", "refresh_token", "refreshtoken", "id_token", "idtoken",
    "authorization", "client_secret", "clientsecret", "code_verifier", "codeverifier", "oauth_code",
    "oauthcode", "session_token", "sessiontoken", "api_key", "apikey", "api_token", "apitoken",
    "cookie", "set_cookie", "setcookie", "password",
)
EXACT_SECRET_KEYS = {"access", "refresh", "token", "secret", "code", "cookie", "set_cookie", "setcookie", "key"}
SECRET_KEY_REGEX = (
    r"access_token|accessToken|refresh_token|refreshToken|id_token|idToken|client_secret|clientSecret|"
    r"code_verifier|codeVerifier|session_token|sessionToken|oauth_code|oauthCode|authorization|refresh|"
    r"access|token|secret|code|api_key|apiKey|apikey|api_token|apiToken|x-api-key|x-goog-api-key|cookie|set-cookie|"
    r"set_cookie|setCookie|password|key"
)
TOKEN_REDACTION_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9][A-Za-z0-9_-]{16,}"),
    re.compile(r"sk-or-v1-[A-Za-z0-9][A-Za-z0-9_-]{16,}"),
    re.compile(r"ya29\.[A-Za-z0-9_-]+"),
]
BEARER_RE = re.compile(r"Bearer\s+[A-Za-z0-9._~+/=-]{12,}", re.IGNORECASE)
URL_USERINFO_RE = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)([^/@\s]+)@")
JSON_SECRET_RE = re.compile(rf'(?i)("(?:{SECRET_KEY_REGEX})"\s*:\s*")[^"]*(")')
JSON_SECRET_NUMBER_RE = re.compile(rf'(?i)("(?:{SECRET_KEY_REGEX})"\s*:\s*)-?\d+(?:\.\d+)?')
PYTHON_REPR_SECRET_RE = re.compile(rf"(?i)('(?:{SECRET_KEY_REGEX})'\s*:\s*')[^']*(')")
PYTHON_REPR_SECRET_NUMBER_RE = re.compile(rf"(?i)('(?:{SECRET_KEY_REGEX})'\s*:\s*)-?\d+(?:\.\d+)?")
FORM_SECRET_RE = re.compile(rf"(?i)\b({SECRET_KEY_REGEX})=([^&\s]+)")
UNQUOTED_SECRET_RE = re.compile(rf"(?i)\b({SECRET_KEY_REGEX})\s*[:=]\s*([^\s,;}}]+)")
HEADER_SECRET_RE = re.compile(
    r"(?im)(^|[ \t])((?:authorization|proxy-authorization|cookie|set-cookie|"
    r"[\w-]*(?:api[-_]?key|api[-_]?token|token|secret|credential|password)[\w-]*)\s*:\s*)[^\r\n]+"
)


def normalize_redaction_markers(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): normalize_redaction_markers(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [normalize_redaction_markers(item) for item in value]
    if isinstance(value, str):
        return value.replace("[REDACTED]", REDACTION_MARKER)
    return value


def key_looks_secret(key: Any) -> bool:
    normalized = str(key).replace("-", "_").lower()
    compact = normalized.replace("_", "")
    metadata_suffixes = ("_chars", "chars", "_count", "count", "_tokens", "tokens")
    if normalized.endswith(metadata_suffixes) or compact.endswith(tuple(item.replace("_", "") for item in metadata_suffixes)):
        return False
    return normalized in EXACT_SECRET_KEYS or any(
        fragment in normalized or fragment in compact for fragment in SECRET_KEY_FRAGMENTS
    )


def redact_sensitive_text(text: str) -> str:
    redacted = str(text)
    if package_redact_secret_text is not None:
        try:
            redacted = str(package_redact_secret_text(redacted))
        except Exception:
            pass
    redacted = str(normalize_redaction_markers(redacted))
    for pattern in TOKEN_REDACTION_PATTERNS:
        redacted = pattern.sub(REDACTION_MARKER, redacted)
    redacted = URL_USERINFO_RE.sub(lambda match: match.group(1) + REDACTION_MARKER + "@", redacted)
    redacted = BEARER_RE.sub("Bearer " + REDACTION_MARKER, redacted)
    redacted = HEADER_SECRET_RE.sub(lambda match: match.group(1) + match.group(2) + REDACTION_MARKER, redacted)
    redacted = JSON_SECRET_RE.sub(lambda match: match.group(1) + REDACTION_MARKER + match.group(2), redacted)
    redacted = JSON_SECRET_NUMBER_RE.sub(lambda match: match.group(1) + '"' + REDACTION_MARKER + '"', redacted)
    redacted = PYTHON_REPR_SECRET_RE.sub(lambda match: match.group(1) + REDACTION_MARKER + match.group(2), redacted)
    redacted = PYTHON_REPR_SECRET_NUMBER_RE.sub(lambda match: match.group(1) + "'" + REDACTION_MARKER + "'", redacted)
    redacted = FORM_SECRET_RE.sub(lambda match: f"{match.group(1)}={REDACTION_MARKER}", redacted)
    return UNQUOTED_SECRET_RE.sub(lambda match: f"{match.group(1)}={REDACTION_MARKER}", redacted)


def _secret_value_should_redact(key: Any, item: Any) -> bool:
    if item is None or item == "" or isinstance(item, bool):
        return False
    normalized = str(key).replace("-", "_").lower()
    return not (normalized == "code" and isinstance(item, int) and 100 <= item <= 599)


def sanitize_json(value: Any) -> Any:
    value = normalize_redaction_markers(value)
    if isinstance(value, dict):
        return {
            str(key): REDACTION_MARKER
            if key_looks_secret(key) and _secret_value_should_redact(key, item)
            else sanitize_json(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [sanitize_json(item) for item in value]
    if isinstance(value, str):
        return redact_sensitive_text(value)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return redact_sensitive_text(str(value))
