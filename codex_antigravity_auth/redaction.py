"""Credential redaction shared with the dependency-free standalone Anti skill."""

from .skills.anti.scripts.anti_lib.secret_redaction import (
    REDACTED,
    redact_secret_text,
    redact_secrets,
)

__all__ = ["REDACTED", "redact_secret_text", "redact_secrets"]
