"""Central helpers for redacting secrets from diagnostics and API errors."""

from __future__ import annotations

import json
import re
from typing import Any

REDACTED = "[REDACTED]"
MAX_TEXT_CHARS = 512 * 1024
MAX_DEPTH = 32
MAX_ITEMS = 10_000
LIMIT_MARKER = REDACTED + " (diagnostic input limit exceeded)"

_SECRET_KEY_FRAGMENTS = (
    "access_token",
    "accesstoken",
    "refresh_token",
    "refreshtoken",
    "id_token",
    "idtoken",
    "authorization",
    "client_secret",
    "clientsecret",
    "code_verifier",
    "codeverifier",
    "oauth_code",
    "oauthcode",
    "session_token",
    "sessiontoken",
    "api_key",
    "apikey",
    "api_token",
    "apitoken",
    "cookie",
    "set_cookie",
    "setcookie",
    "auth_token",
    "authtoken",
    "password",
    "private_key",
    "privatekey",
    "credential",
    "secret_key",
    "client_key",
)

_EXACT_SECRET_KEYS = {
    "access",
    "refresh",
    "token",
    "secret",
    "code",
    "cookie",
    "set_cookie",
    "setcookie",
    "password",
    "credential",
    "credentials",
    "key",
}

_BEARER_RE = re.compile(r"Bearer\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE)
_QUERY_SECRET_RE = re.compile(
    r"(?i)([?&](?:access_token|accessToken|refresh_token|refreshToken|id_token|idToken|client_secret|clientSecret|code|code_verifier|codeVerifier|session_token|sessionToken|api_key|apiKey|apikey|x-api-key|x-goog-api-key|cookie|set-cookie|set_cookie|setCookie|key)=)[^&#\s]+"
)
_JSON_SECRET_RE = re.compile(
    r'(?i)("(?:access_token|refresh_token|id_token|accessToken|refreshToken|idToken|client_secret|clientSecret|code_verifier|codeVerifier|session_token|sessionToken|oauth_code|oauthCode|authorization|refresh|access|code|api_key|apiKey|apikey|x-api-key|x-goog-api-key|key|cookie|set-cookie|set_cookie|setCookie)"\s*:\s*")([^"]*)(")'
)
_PYTHON_REPR_SECRET_RE = re.compile(
    r"(?i)('(?:access_token|refresh_token|id_token|accessToken|refreshToken|idToken|client_secret|clientSecret|code_verifier|codeVerifier|session_token|sessionToken|oauth_code|oauthCode|authorization|refresh|access|code|api_key|apiKey|apikey|x-api-key|x-goog-api-key|key|cookie|set-cookie|set_cookie|setCookie)'\s*:\s*')([^']*)(')"
)
_FORM_SECRET_RE = re.compile(
    r"(?i)\b(access_token|accessToken|refresh_token|refreshToken|id_token|idToken|client_secret|clientSecret|code_verifier|codeVerifier|session_token|sessionToken|authorization|code|api_key|apiKey|apikey|x-api-key|x-goog-api-key|cookie|set-cookie|set_cookie|setCookie|key)=([^&\s]+)"
)
_HEADER_SECRET_RE = re.compile(
    r"(?im)(^|[ \t])((?:authorization|proxy-authorization|cookie|set-cookie|[\w-]*(?:api[-_]?key|api[-_]?token|token|secret|credential|password)[\w-]*)\s*:\s*)[^\r\n]+"
)
_PROVIDER_KEY_RE = re.compile(r"(?:sk-or-v1|sk)-[A-Za-z0-9][A-Za-z0-9._-]{12,}")
_GOOGLE_TOKEN_RE = re.compile(r"ya29\.[A-Za-z0-9._~-]+")
_URL_USERINFO_RE = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^/@\s?#]+@")
_QUOTED_FIELD_RE = re.compile(r'''(?P<key>"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')\s*:\s*''')
_SINGLE_QUOTED_VALUE_RE = re.compile(r"'(?:\\.|[^'\\])*'")
_RAW_SECRET_NAMES = "|".join(sorted(set(_SECRET_KEY_FRAGMENTS) | _EXACT_SECRET_KEYS | {"x-api-key", "x-goog-api-key", "set-cookie"}, key=len, reverse=True))
_RAW_SECRET_RE = re.compile(rf"(?i)\b(?P<key>{_RAW_SECRET_NAMES})\s*[=:]\s*(?P<value>[^\s,;}}&]+)")
_UNQUOTED_FIELD_RE = re.compile(r"(?<![\w\"'])\b(?P<key>[A-Za-z_][\w.-]*)\s*[=:]\s*(?![=:])")
_DOUBLE_QUOTED_VALUE_RE = re.compile(r'"(?:\\.|[^"\\])*"')
_BARE_VALUE_RE = re.compile(r"[^\s,;}&]+")
_GOOGLE_VALIDATION_URL_RE = re.compile(
    r'(?i)(https://accounts\.google\.com/[^\s"<>]+)[?][^\s"<>]*'
)


def _is_secret_key(key: Any) -> bool:
    normalized = str(key).replace("-", "_").lower()
    compact = normalized.replace("_", "")
    metadata_suffixes = (
        "_cached",
        "cached",
        "_expires",
        "expires",
        "_expires_at",
        "expiresat",
        "_last_refresh_at",
        "lastrefreshat",
        "_chars", "chars", "_count", "count", "_tokens", "tokens",
    )
    if normalized.endswith(metadata_suffixes) or compact.endswith(metadata_suffixes):
        return False
    if normalized in _EXACT_SECRET_KEYS:
        return True
    return any(fragment in normalized or fragment in compact for fragment in _SECRET_KEY_FRAGMENTS)


def _is_status_code_value(key: Any, value: Any) -> bool:
    return (
        str(key).replace("-", "_").lower() == "code"
        and isinstance(value, (int, float))
        and not isinstance(value, bool)
        and 100 <= value <= 599
    )


def _preserve_status_code_string(match: "re.Match[str]", quote: str) -> str:
    key = match.group(1)[1:].split(quote, 1)[0].lower()
    try:
        number = float(match.group(2))
    except ValueError:
        number = None
    if key == "code" and number is not None and 100 <= number <= 599:
        return match.group(0)
    return match.group(1) + REDACTED + match.group(3)


def _redact_quoted_fields(text: str, depth: int) -> str:
    """Interpret quoted field values, including escaped and compound secrets."""
    decoder = json.JSONDecoder()
    pieces = []
    cursor = 0
    search_at = 0
    fields = 0
    while match := _QUOTED_FIELD_RE.search(text, search_at):
        fields += 1
        if fields > MAX_ITEMS:
            return LIMIT_MARKER
        token = match.group("key")
        try:
            key = json.loads(token) if token.startswith('"') else token[1:-1]
        except ValueError:
            key = token[1:-1]
        start = match.end()
        search_at = start
        secret = _is_secret_key(key)
        try:
            value, end = decoder.raw_decode(text, start)
        except (ValueError, RecursionError):
            single = _SINGLE_QUOTED_VALUE_RE.match(text, start)
            if single:
                value, end = single.group()[1:-1], single.end()
            elif secret:
                # A malformed/unclosed secret has no trustworthy end boundary.
                pieces.extend((text[cursor:start], json.dumps(REDACTED)))
                return "".join(pieces)
            else:
                continue
        if secret:
            replacement = value if value in (None, "") or isinstance(value, bool) or _is_status_code_value(key, value) else REDACTED
            if str(key).lower() == "code" and isinstance(value, str):
                try:
                    if 100 <= float(value) <= 599:
                        replacement = value
                except ValueError:
                    pass
        elif isinstance(value, str):
            replacement = _redact_text(value, depth + 1)
        else:
            continue  # Keep scanning inside non-secret objects and arrays.
        if replacement != value:
            pieces.extend((text[cursor:start], json.dumps(replacement)))
            cursor = end
        search_at = end
    pieces.append(text[cursor:])
    return "".join(pieces)


def _redact_raw_value(match: re.Match[str]) -> str:
    key, value = match.group("key"), match.group("value")
    if key.lower() == "code":
        try:
            if 100 <= float(value) <= 599:
                return match.group()
        except ValueError:
            pass
    return f"{key}={REDACTED}"


def _redact_unquoted_fields(text: str) -> str:
    pieces = []
    cursor = search_at = 0
    while match := _UNQUOTED_FIELD_RE.search(text, search_at):
        key, start = match.group("key"), match.end()
        search_at = start
        if not _is_secret_key(key):
            continue
        if text[start:start + 1] in {'"', "'"}:
            pattern = _DOUBLE_QUOTED_VALUE_RE if text[start] == '"' else _SINGLE_QUOTED_VALUE_RE
            value = pattern.match(text, start)
            # No reliable end delimiter: discard the remaining diagnostic tail.
            end = value.end() if value else len(text)
        else:
            value = _BARE_VALUE_RE.match(text, start)
            end = value.end() if value else start
        raw = text[start:end].strip('"\'')
        if key.lower() == "code":
            try:
                if 100 <= float(raw) <= 599:
                    search_at = end
                    continue
            except ValueError:
                pass
        pieces.extend((text[cursor:start], REDACTED))
        cursor = search_at = end
    pieces.append(text[cursor:])
    return "".join(pieces)


def _redact_text(text: str, depth: int = 0) -> str:
    """Redact token-shaped values from free-form text."""
    if len(text) > MAX_TEXT_CHARS or depth > MAX_DEPTH:
        return LIMIT_MARKER
    if not text:
        return text
    # Canonical RIFF/WAV base64 can be echoed inside model/error text. Remove
    # the entire token (including partial echoes) before parsing or clipping.
    text = re.sub(r"(?<![A-Za-z0-9+/])(?:data:audio/wav;base64,)?UklGR[A-Za-z0-9+/=]{11,}",
                  REDACTED + " (WAV payload)", text)
    if text.lstrip().startswith(("{", "[", '"')):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            pass
        except (ValueError, RecursionError):
            return LIMIT_MARKER
        else:
            sanitized = _redact_tree(parsed, depth + 1, [MAX_ITEMS, MAX_TEXT_CHARS])
            return json.dumps(sanitized, ensure_ascii=False) if sanitized != parsed else text
    text = _redact_quoted_fields(text, depth)
    text = _redact_unquoted_fields(text)
    text = _PROVIDER_KEY_RE.sub(REDACTED, text)
    text = _GOOGLE_TOKEN_RE.sub(REDACTED, text)
    text = _URL_USERINFO_RE.sub(lambda m: m.group(1) + REDACTED + "@", text)
    # Validation URLs carry account-binding query params (e.g. plt=...); keep the bare flow URL.
    text = _GOOGLE_VALIDATION_URL_RE.sub(r"\1?REDACTED", text)
    redacted = _BEARER_RE.sub("Bearer " + REDACTED, text)
    redacted = _HEADER_SECRET_RE.sub(lambda m: m.group(1) + m.group(2) + REDACTED, redacted)
    redacted = _QUERY_SECRET_RE.sub(lambda m: m.group(1) + REDACTED, redacted)
    redacted = _JSON_SECRET_RE.sub(lambda m: _preserve_status_code_string(m, '"'), redacted)
    redacted = _PYTHON_REPR_SECRET_RE.sub(lambda m: _preserve_status_code_string(m, "'"), redacted)
    redacted = _FORM_SECRET_RE.sub(lambda m: f"{m.group(1)}={REDACTED}", redacted)
    return _RAW_SECRET_RE.sub(_redact_raw_value, redacted)


class _InputLimit(Exception):
    pass


def _redact_tree(obj: Any, depth: int, remaining: list[int]) -> Any:
    """Return a copy of an object with OAuth tokens, auth headers, and API keys redacted."""
    remaining[0] -= 1
    if depth > MAX_DEPTH or remaining[0] < 0:
        raise _InputLimit
    if isinstance(obj, dict):
        if len(obj) > remaining[0]:
            raise _InputLimit
        result: dict[Any, Any] = {}
        for key, value in obj.items():
            remaining[1] -= len(str(key))
            if remaining[1] < 0:
                raise _InputLimit
            if _is_secret_key(key):
                result[key] = (
                    value
                    if value in (None, "") or isinstance(value, bool) or _is_status_code_value(key, value) or (
                        str(key).lower() == "code" and isinstance(value, str) and len(value) == 3 and value.isdecimal() and 100 <= int(value) <= 599
                    )
                    else REDACTED
                )
            else:
                result[key] = _redact_tree(value, depth + 1, remaining)
        return result
    if isinstance(obj, list):
        if len(obj) > remaining[0]:
            raise _InputLimit
        return [_redact_tree(item, depth + 1, remaining) for item in obj]
    if isinstance(obj, tuple):
        if len(obj) > remaining[0]:
            raise _InputLimit
        return tuple(_redact_tree(item, depth + 1, remaining) for item in obj)
    if isinstance(obj, str):
        remaining[1] -= len(obj)
        if remaining[1] < 0:
            raise _InputLimit
        return _redact_text(obj, depth + 1)
    return obj


def redact_secret_text(text: str) -> str:
    try:
        return _redact_text(text)
    except (_InputLimit, RecursionError):
        return LIMIT_MARKER


def redact_secrets(obj: Any) -> Any:
    try:
        return _redact_tree(obj, 0, [MAX_ITEMS, MAX_TEXT_CHARS])
    except (_InputLimit, RecursionError):
        return LIMIT_MARKER
