from __future__ import annotations

import re
from typing import Any

from .secret_redaction import (
    _is_secret_key,
    redact_secret_text as shared_redact_secret_text,
    redact_secrets as shared_redact_secrets,
)


REDACTION_MARKER = "<redacted>"
HEADER_SECRET_RE = re.compile(r"(?im)(^|[ \t])((?:x-(?:request|user)[-_]?id)\s*:\s*)[^\r\n]+")
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
    return _is_secret_key(key) or any(
        fragment in normalized or fragment in compact for fragment in ("user_id", "userid", "request_id", "requestid")
    )


def redact_sensitive_text(text: str) -> str:
    redacted = shared_redact_secret_text(str(text))
    redacted = str(normalize_redaction_markers(redacted))
    redacted = HEADER_SECRET_RE.sub(lambda match: match.group(1) + match.group(2) + REDACTION_MARKER, redacted)
    redacted = PROVIDER_ID_JSON_RE.sub(lambda match: match.group(1) + REDACTION_MARKER + match.group(2), redacted)
    redacted = PROVIDER_ID_JSON_NUMBER_RE.sub(lambda match: match.group(1) + REDACTION_MARKER, redacted)
    redacted = PROVIDER_ID_PYTHON_REPR_RE.sub(lambda match: match.group(1) + REDACTION_MARKER + match.group(2), redacted)
    redacted = PROVIDER_ID_PYTHON_REPR_NUMBER_RE.sub(lambda match: match.group(1) + REDACTION_MARKER, redacted)
    redacted = PROVIDER_ID_VALUE_RE.sub(REDACTION_MARKER, redacted)
    redacted = PROVIDER_ID_FORM_RE.sub(lambda match: match.group(1) + REDACTION_MARKER, redacted)
    return redacted


def _secret_value_should_redact(key: Any, item: Any) -> bool:
    if item is None or item == "" or isinstance(item, bool):
        return False
    normalized = str(key).replace("-", "_").lower()
    return not (normalized == "code" and isinstance(item, int) and 100 <= item <= 599)


def _sanitize_json(value: Any) -> Any:
    value = normalize_redaction_markers(value)
    if isinstance(value, dict):
        return {
            str(key): REDACTION_MARKER
            if key_looks_secret(key) and _secret_value_should_redact(key, item)
            else _sanitize_json(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_sanitize_json(item) for item in value]
    if isinstance(value, str):
        return redact_sensitive_text(value)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return redact_sensitive_text(str(value))


def sanitize_json(value: Any) -> Any:
    # Bound structured inputs before privacy-marker normalization recurses.
    return _sanitize_json(shared_redact_secrets(value))
