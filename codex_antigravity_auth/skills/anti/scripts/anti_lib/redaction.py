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
    "cookie", "set_cookie", "setcookie", "password", "user_id", "userid", "request_id", "requestid",
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
JSON_SECRET_RE = re.compile(rf'(?i)("(?:{SECRET_KEY_REGEX})"\s*:\s*")([^"]*)(")')
JSON_SECRET_NUMBER_RE = re.compile(rf'(?i)("(?:{SECRET_KEY_REGEX})"\s*:\s*)(-?\d+(?:\.\d+)?)')
PYTHON_REPR_SECRET_RE = re.compile(rf"(?i)('(?:{SECRET_KEY_REGEX})'\s*:\s*')([^']*)(')")
PYTHON_REPR_SECRET_NUMBER_RE = re.compile(rf"(?i)('(?:{SECRET_KEY_REGEX})'\s*:\s*)(-?\d+(?:\.\d+)?)")
FORM_SECRET_RE = re.compile(rf"(?i)\b({SECRET_KEY_REGEX})=([^&\s]+)")
UNQUOTED_SECRET_RE = re.compile(rf"(?i)\b({SECRET_KEY_REGEX})\s*[:=]\s*([^\s,;}}]+)")
HEADER_SECRET_RE = re.compile(
    r"(?im)(^|[ \t])((?:authorization|proxy-authorization|cookie|set-cookie|"
    r"x-(?:request|user)[-_]?id|"
    r"[\w-]*(?:api[-_]?key|api[-_]?token|token|secret|credential|password)[\w-]*)\s*:\s*)[^\r\n]+"
)
# Provider-level identifiers (e.g. OpenRouter user_id) are not credentials but
# are still private; redact them in passthrough error bodies before printing.
# The shape mirrors real OpenRouter ids (user_ + 8+ alnum containing a digit);
# shorter identifiers like user_models / user_abc123 are left alone.
PROVIDER_ID_VALUE_RE = re.compile(r"\buser_(?=[A-Za-z0-9]*[0-9])[A-Za-z0-9]{8,}")
PROVIDER_ID_JSON_RE = re.compile(r'(?i)("(?:user[-_]?id|request[-_]?id)"\s*:\s*")[^"]*(")')
PROVIDER_ID_JSON_NUMBER_RE = re.compile(r'(?i)("(?:user[-_]?id|request[-_]?id)"\s*:\s*)(-?\d+(?:\.\d+)?)')
PROVIDER_ID_PYTHON_REPR_RE = re.compile(r"(?i)('(?:user[-_]?id|request[-_]?id)'\s*:\s*')[^']*(')")
PROVIDER_ID_PYTHON_REPR_NUMBER_RE = re.compile(r"(?i)('(?:user[-_]?id|request[-_]?id)'\s*:\s*)(-?\d+(?:\.\d+)?)")
# Form/query context (`key=` with no spaces): covers request_id=req_999 and
# user_id=12345 that the JSON/repr patterns cannot see. Kept separate from the
# generic SECRET_KEY_REGEX so code like `user_id == 42` or
# `request_id: str = "x"` is never mistaken for a credential.
PROVIDER_ID_FORM_RE = re.compile(r"(?i)\b((?:user[-_]?id|request[-_]?id)=)[^&\s]+")


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


def _redact_secret_number(match: "re.Match[str]", quote: str) -> str:
    """Redact a numeric secret value, preserving HTTP-like status codes under ``code``."""
    key = match.group(1)[1:].split(quote, 1)[0].lower()
    try:
        number = float(match.group(2))
    except ValueError:
        number = None
    if key == "code" and number is not None and 100 <= number <= 599:
        return match.group(0)
    return match.group(1) + quote + REDACTION_MARKER + quote


def _redact_secret_string(match: "re.Match[str]", quote: str) -> str:
    """Redact a quoted secret value, preserving quoted HTTP-like status codes."""
    key = match.group(1)[1:].split(quote, 1)[0].lower()
    try:
        number = float(match.group(2))
    except ValueError:
        number = None
    if key == "code" and number is not None and 100 <= number <= 599:
        return match.group(0)
    return match.group(1) + REDACTION_MARKER + match.group(3)


def _redact_unquoted_secret(match: "re.Match[str]") -> str:
    """Redact an unquoted secret value, preserving HTTP-like status codes."""
    key = match.group(1).lower()
    try:
        number = float(match.group(2))
    except ValueError:
        number = None
    if key == "code" and number is not None and 100 <= number <= 599:
        return match.group(0)
    return f"{match.group(1)}={REDACTION_MARKER}"


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
    redacted = PROVIDER_ID_JSON_RE.sub(lambda match: match.group(1) + REDACTION_MARKER + match.group(2), redacted)
    redacted = PROVIDER_ID_JSON_NUMBER_RE.sub(lambda match: match.group(1) + REDACTION_MARKER, redacted)
    redacted = PROVIDER_ID_PYTHON_REPR_RE.sub(lambda match: match.group(1) + REDACTION_MARKER + match.group(2), redacted)
    redacted = PROVIDER_ID_PYTHON_REPR_NUMBER_RE.sub(lambda match: match.group(1) + REDACTION_MARKER, redacted)
    redacted = PROVIDER_ID_VALUE_RE.sub(REDACTION_MARKER, redacted)
    redacted = PROVIDER_ID_FORM_RE.sub(lambda match: match.group(1) + REDACTION_MARKER, redacted)
    redacted = JSON_SECRET_RE.sub(lambda match: _redact_secret_string(match, '"'), redacted)
    redacted = JSON_SECRET_NUMBER_RE.sub(lambda match: _redact_secret_number(match, '"'), redacted)
    redacted = PYTHON_REPR_SECRET_RE.sub(lambda match: _redact_secret_string(match, "'"), redacted)
    redacted = PYTHON_REPR_SECRET_NUMBER_RE.sub(lambda match: _redact_secret_number(match, "'"), redacted)
    redacted = FORM_SECRET_RE.sub(lambda match: f"{match.group(1)}={REDACTION_MARKER}", redacted)
    return UNQUOTED_SECRET_RE.sub(_redact_unquoted_secret, redacted)


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
