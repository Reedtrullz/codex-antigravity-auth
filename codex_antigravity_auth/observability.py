from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any, Iterable

from .constants import get_codex_home
from .redaction import redact_secret_text

REQUEST_LOG_FILE = "antigravity-requests.jsonl"
REQUEST_LOG_MAX_BYTES = 10 * 1024 * 1024
REQUEST_LOG_SECRET_KEYS = {
    "authorization",
    "api_key",
    "apikey",
    "apiKey",
    "access_token",
    "accessToken",
    "refresh_token",
    "refreshToken",
    "client_secret",
    "clientSecret",
    "token",
    "password",
    "secret",
    "prompt",
    "input",
    "request",
    "body",
    "headers",
}
REQUEST_LOG_PROVIDER_KEY_RE = re.compile(r"\b(?:sk-or-v1|sk)-[A-Za-z0-9][A-Za-z0-9._-]{12,}\b")


def request_log_path() -> Path:
    return get_codex_home() / REQUEST_LOG_FILE


def request_log_info() -> dict[str, Any]:
    path = request_log_path()
    rotated = path.with_suffix(path.suffix + ".1")
    return {
        "path": str(path),
        "exists": path.is_file(),
        "size_bytes": path.stat().st_size if path.is_file() else 0,
        "rotated_path": str(rotated),
        "max_bytes": REQUEST_LOG_MAX_BYTES,
    }


def _redact_json(value: Any) -> Any:
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if key_text in REQUEST_LOG_SECRET_KEYS or key_text.lower() in REQUEST_LOG_SECRET_KEYS:
                redacted[key_text] = "[redacted]"
            else:
                redacted[key_text] = _redact_json(item)
        return redacted
    if isinstance(value, list):
        return [_redact_json(item) for item in value]
    if isinstance(value, str):
        return REQUEST_LOG_PROVIDER_KEY_RE.sub("[REDACTED]", redact_secret_text(value))
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value
    return redact_secret_text(str(value))


def sanitize_request_record(record: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "timestamp",
        "request_id",
        "model",
        "route",
        "provider",
        "family",
        "stream",
        "status",
        "latency_ms",
        "http_status",
        "retry_after_source",
        "rotation_attempted",
        "usage",
        "error_class",
        "error",
    }
    sanitized = {key: _redact_json(value) for key, value in record.items() if key in allowed}
    sanitized.setdefault("timestamp", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    return sanitized


def _rotate_log_if_needed(path: Path, max_bytes: int) -> None:
    if max_bytes <= 0 or not path.exists():
        return
    try:
        if path.stat().st_size < max_bytes:
            return
    except OSError:
        return
    rotated = path.with_suffix(path.suffix + ".1")
    try:
        if rotated.exists():
            rotated.unlink()
        path.replace(rotated)
    except OSError:
        # Logging must never break gateway responses.
        return


def write_request_record(record: dict[str, Any], *, max_bytes: int = REQUEST_LOG_MAX_BYTES) -> None:
    path = request_log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        _rotate_log_if_needed(path, max_bytes)
        payload = json.dumps(sanitize_request_record(record), sort_keys=True, separators=(",", ":")) + "\n"
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(path, flags, 0o600)
        try:
            os.write(fd, payload.encode("utf-8"))
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)
    except Exception:
        return


def iter_request_records(*, tail: int | None = None) -> Iterable[dict[str, Any]]:
    path = request_log_path()
    if not path.is_file() or path.is_symlink():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return []
    if tail is not None and tail >= 0:
        lines = lines[-tail:]
    records = []
    for line in lines:
        if not line.strip():
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            records.append({"status": "malformed", "error": "malformed JSONL request-log entry"})
            continue
        records.append(_redact_json(parsed if isinstance(parsed, dict) else {"value": parsed}))
    return records


def clean_request_logs() -> list[str]:
    removed = []
    for path in (request_log_path(), request_log_path().with_suffix(request_log_path().suffix + ".1")):
        try:
            if path.exists() and not path.is_symlink():
                path.unlink()
                removed.append(str(path))
        except OSError:
            pass
    return removed
