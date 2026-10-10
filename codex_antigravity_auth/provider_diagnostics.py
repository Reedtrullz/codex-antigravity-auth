"""Bounded, redacted provider error diagnostics.

Parses structured Google rejection bodies into a fixed field set without
persisting raw provider payloads. Reason, domain, and error number are the
only structured fields extracted; everything else is a bounded text message.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

MAX_DIAGNOSTIC_MESSAGE_CHARS = 300

_DOMAIN_RE = re.compile(r"(?i)([a-z0-9-]+\.[a-z0-9.-]+)\.?\s*(?:error|\.\s|$|\n)")
_REASON_ORDER = (
    "VALIDATION_REQUIRED",
    "RESTRICTED_AGE",
    "PERMISSION_DENIED",
    "FORBIDDEN",
    "RESOURCE_EXHAUSTED",
    "RATE_LIMIT_EXCEEDED",
    "QUOTA_EXCEEDED",
    "UNAUTHENTICATED",
)
_ERROR_NUMBER_RE = re.compile(r"(?i)(?:^|[\s(])(\d{4})\s*[.:)]")
_QUERY_RE = re.compile(r"\?[\w=&%+;.-]+")


@dataclass(frozen=True)
class ProviderErrorDiagnostics:
    http_status: int
    reason: str | None
    domain: str | None
    error_number: int | None
    message: str


def _truncate(text: str, limit: int = MAX_DIAGNOSTIC_MESSAGE_CHARS) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "..."


def _strip_query(text: str) -> str:
    # Strip query params from any URL-like token (broader than the secret-redaction
    # module, which covers only accounts.google.com validation URLs).
    return _QUERY_RE.sub("", text)


def _extract_domain(text: str) -> str | None:
    match = _DOMAIN_RE.search(text)
    if not match:
        return None
    domain = match.group(1)
    # Keep only a plausible hostname: at most 3 labels, no path characters.
    parts = domain.split(".")
    if len(parts) < 2 or len(parts) > 4:
        return None
    return domain


def parse_provider_error(
    http_status: int,
    body: str | None,
) -> ProviderErrorDiagnostics:
    """Build bounded diagnostics from a provider error response.

    Never returns raw body text longer than MAX_DIAGNOSTIC_MESSAGE_CHARS.
    Query parameters are stripped before any parsing so validation URLs in
    provider messages cannot leak into logs or run records.
    """
    if not isinstance(body, str):
        body = ""
    safe_body = _strip_query(body)
    reason = None
    for candidate in _REASON_ORDER:
        if re.search(rf"(?i)\b{candidate}\b", safe_body):
            reason = candidate
            break
    domain = _extract_domain(safe_body)
    number_match = _ERROR_NUMBER_RE.search(safe_body)
    error_number = int(number_match.group(1)) if number_match else None
    message = _truncate(safe_body)
    return ProviderErrorDiagnostics(
        http_status=int(http_status),
        reason=reason,
        domain=domain,
        error_number=error_number,
        message=message,
    )


def provider_error_class(diagnostics: ProviderErrorDiagnostics) -> str:
    """Return a distinct error class for each provider rejection family.

    Classes: validation_required, age_ineligible, permission_denied,
    project_license, rate_limit, auth_failure.
    """
    reason = (diagnostics.reason or "").upper()
    if reason == "VALIDATION_REQUIRED":
        return "validation_required"
    if reason == "RESTRICTED_AGE":
        return "age_ineligible"
    if reason in {"PERMISSION_DENIED", "FORBIDDEN"}:
        return "permission_denied"
    if reason in {"RESOURCE_EXHAUSTED", "QUOTA_EXCEEDED", "RATE_LIMIT_EXCEEDED"}:
        return "rate_limit"
    if diagnostics.http_status == 401:
        return "auth_failure"
    return "auth_failure"
