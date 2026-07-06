from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone
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


def _parse_since_seconds(value: str | None) -> float | None:
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in {"", "all"}:
        return None
    match = re.fullmatch(r"(\d+(?:\.\d+)?)([smhd]?)", text)
    if not match:
        raise ValueError("since must be a duration like 24h, 30m, 7d, or 'all'")
    amount = float(match.group(1))
    unit = match.group(2) or "s"
    multiplier = {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]
    return amount * multiplier


def _timestamp_epoch(value: Any) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def _percentile(values: list[int], percentile: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int((percentile / 100.0) * len(ordered) + 0.999999) - 1))
    return ordered[index]


def request_log_summary(*, since: str | None = "24h", now: float | None = None) -> dict[str, Any]:
    window_seconds = _parse_since_seconds(since)
    now_value = time.time() if now is None else float(now)
    cutoff = None if window_seconds is None else now_value - window_seconds
    records = list(iter_request_records())
    groups: dict[str, dict[str, Any]] = {}
    malformed_records = 0
    excluded_by_time = 0

    for record in records:
        if record.get("status") == "malformed":
            malformed_records += 1
            continue
        timestamp = _timestamp_epoch(record.get("timestamp"))
        if cutoff is not None and timestamp is not None and timestamp < cutoff:
            excluded_by_time += 1
            continue
        route = str(record.get("route") or "unknown")
        family = str(record.get("family") or record.get("provider") or "unknown")
        key = f"{route}/{family}"
        group = groups.setdefault(
            key,
            {
                "route": route,
                "family": family,
                "request_count": 0,
                "success_count": 0,
                "failure_count": 0,
                "rate_limit_count": 0,
                "rotation_attempted_count": 0,
                "_latencies": [],
                "_errors": {},
            },
        )
        group["request_count"] += 1
        if record.get("status") == "success":
            group["success_count"] += 1
        else:
            group["failure_count"] += 1
        try:
            latency_ms = int(float(record.get("latency_ms")))
            if latency_ms >= 0:
                group["_latencies"].append(latency_ms)
        except (TypeError, ValueError):
            pass
        try:
            if int(record.get("http_status")) == 429:
                group["rate_limit_count"] += 1
        except (TypeError, ValueError):
            pass
        if bool(record.get("rotation_attempted")):
            group["rotation_attempted_count"] += 1
        error_class = record.get("error_class")
        if isinstance(error_class, str) and error_class:
            errors = group["_errors"]
            errors[error_class] = int(errors.get(error_class, 0)) + 1

    rendered_groups: dict[str, dict[str, Any]] = {}
    for key, group in sorted(groups.items()):
        request_count = int(group["request_count"])
        success_count = int(group["success_count"])
        latencies = group.pop("_latencies")
        errors = group.pop("_errors")
        rendered = {
            **group,
            "success_rate": round(success_count / request_count, 4) if request_count else 0.0,
            "p50_latency_ms": _percentile(latencies, 50),
            "p95_latency_ms": _percentile(latencies, 95),
            "top_error_classes": [
                {"error_class": error_class, "count": count}
                for error_class, count in sorted(errors.items(), key=lambda item: (-item[1], item[0]))[:3]
            ],
        }
        rendered_groups[key] = rendered

    return {
        "path": str(request_log_path()),
        "since": since if since is not None else "all",
        "window_seconds": window_seconds,
        "total_records": len(records),
        "included_records": sum(group["request_count"] for group in rendered_groups.values()),
        "excluded_by_time": excluded_by_time,
        "malformed_records": malformed_records,
        "groups": rendered_groups,
    }


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
