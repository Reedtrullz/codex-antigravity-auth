from __future__ import annotations

import json
import os
import re
import time
import stat
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from .namespaces import gateway_home
from typing import Any, Iterable

from .constants import get_codex_home
from .redaction import redact_secret_text, redact_secrets
from .secure_store import file_lock

_DEFAULT_GET_CODEX_HOME = get_codex_home


def _codex_home_read_only() -> Path:
    if get_codex_home is not _DEFAULT_GET_CODEX_HOME:
        return get_codex_home()
    return gateway_home()

REQUEST_LOG_FILE = "antigravity-requests.jsonl"
REQUEST_LOG_MAX_BYTES = 10 * 1024 * 1024
REQUEST_LOG_BACKUP_COUNT = 1
REQUEST_LOG_MAX_RECORD_BYTES = 64 * 1024
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
    return _codex_home_read_only() / REQUEST_LOG_FILE


def _retention_settings(max_bytes: int | None = None, backup_count: int | None = None) -> tuple[int, int, list[str]]:
    warnings = []
    values = []
    for explicit, name, default, lower, upper in (
        (max_bytes, "ANTIGRAVITY_REQUEST_LOG_MAX_BYTES", REQUEST_LOG_MAX_BYTES, 1024, REQUEST_LOG_MAX_BYTES),
        (backup_count, "ANTIGRAVITY_REQUEST_LOG_BACKUP_COUNT", REQUEST_LOG_BACKUP_COUNT, 0, 5),
    ):
        raw = explicit if explicit is not None else os.environ.get(name, default)
        try:
            value = int(raw)
            if type(raw) not in (str, int) or not lower <= value <= upper:
                raise ValueError
        except (ValueError, TypeError, OverflowError):
            value = default
            warnings.append(f"{name} must be an integer from {lower} to {upper}; using default {default}")
        values.append(value)
    return values[0], values[1], warnings


def _archive_paths(path: Path) -> list[tuple[int, Path]]:
    archives = []
    for candidate in path.parent.glob(path.name + ".*"):
        suffix = candidate.name[len(path.name) + 1:]
        if suffix.isascii() and suffix.isdecimal() and int(suffix) > 0 and str(int(suffix)) == suffix:
            archives.append((int(suffix), candidate))
    return sorted(archives, key=lambda item: item[0], reverse=True)


def _retained_paths(path: Path) -> list[Path]:
    return [candidate for candidate in [*(item[1] for item in _archive_paths(path)), path] if not candidate.is_symlink() and candidate.is_file()]


@contextmanager
def _log_lock(path: Path, *, existing_only: bool = False):
    lock_path = path.with_name(f".{path.name}.lock")
    # Writers create this persistent lock before any rotation. Check it LAST:
    # an absent segment alone may be the middle of a zero-backup rotation.
    # A completely unused namespace still needs no files for diagnostics.
    if existing_only and not _retained_paths(path) and not lock_path.exists() and not lock_path.is_symlink():
        yield False
        return
    if lock_path.is_symlink() or (lock_path.exists() and not lock_path.is_file()):
        raise OSError("unsafe request-log lock path")
    with file_lock(path):
        yield True


def request_log_info() -> dict[str, Any]:
    path = request_log_path()
    maximum, backups, warnings = _retention_settings()
    segments = []
    try:
        with _log_lock(path, existing_only=True) as available:
            if available:
                for segment in _retained_paths(path):
                    segments.append({"path": str(segment), "size_bytes": segment.stat().st_size})
    except OSError:
        segments = []
        warnings.append("Request-log metadata could not be read consistently")
    return {
        "path": str(path),
        "exists": any(item["path"] == str(path) for item in segments),
        "size_bytes": next((item["size_bytes"] for item in segments if item["path"] == str(path)), 0),
        "rotated_path": str(path.with_suffix(path.suffix + ".1")),
        "max_bytes": maximum,
        "backup_count": backups,
        "max_record_bytes": min(maximum, REQUEST_LOG_MAX_RECORD_BYTES),
        "retained_segments": segments,
        "configuration_warnings": warnings,
    }


def sanitize_request_record(record: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "timestamp",
        "request_id",
        "run_id",
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
        "terminal_kind",
        "terminal_reason",
        "attempt_count",
        "rotation_count",
        "cooldown_scope",
        "cooldown_category",
        "outcome_category",
        "cancelled",
        "lifecycle_phase",
        "provider_accepted",
        "upstream_http_status",
    }
    sanitized = {key: redact_secrets(value) for key, value in record.items() if key in allowed}
    sanitized.setdefault("timestamp", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    return sanitized


def _rotate_log_if_needed(path: Path, max_bytes: int, *, incoming_bytes: int = 0, backup_count: int = 1) -> None:
    # Caller holds the path-scoped cross-process lock across rotation and append.
    archives = _archive_paths(path)
    for candidate in [path, *(item[1] for item in archives)]:
        if candidate.is_symlink() or (candidate.exists() and not candidate.is_file()):
            raise OSError("unsafe request-log segment")
    for index, archive in archives:
        if index > backup_count:
            archive.unlink()
        else:
            os.chmod(archive, 0o600)
    if not path.exists():
        return
    os.chmod(path, 0o600)
    size = path.stat().st_size
    if not size or size + incoming_bytes <= max_bytes:
        return
    for index in range(backup_count, 0, -1):
        archive = path.with_name(f"{path.name}.{index}")
        if archive.exists():
            if index == backup_count:
                archive.unlink()
            else:
                archive.replace(path.with_name(f"{path.name}.{index + 1}"))
    if backup_count:
        path.replace(path.with_name(f"{path.name}.1"))
    else:
        path.unlink()


def write_request_record(record: dict[str, Any], *, max_bytes: int | None = None, backup_count: int | None = None) -> None:
    path = request_log_path()
    try:
        maximum, backups, _warnings = _retention_settings(max_bytes, backup_count)
        payload = (json.dumps(sanitize_request_record(record), sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        if len(payload) > min(maximum, REQUEST_LOG_MAX_RECORD_BYTES):
            # Preserve an explicit coverage gap, never fabricate a provider failure.
            payload = (json.dumps({
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "status": "log_gap", "error_class": "oversized_log_record",
                "error": "An oversized request-log record was omitted",
            }, sort_keys=True) + "\n").encode("utf-8")
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with _log_lock(path):
            _rotate_log_if_needed(path, maximum, incoming_bytes=len(payload), backup_count=backups)
            flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
            fd = os.open(path, flags, 0o600)
            previous_size = None
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode):
                    raise OSError("request log is not a regular file")
                previous_size = info.st_size
                remaining = memoryview(payload)
                while remaining:
                    written = os.write(fd, remaining)
                    if written <= 0:
                        raise OSError("short request-log write")
                    remaining = remaining[written:]
                if hasattr(os, "fchmod"):
                    os.fchmod(fd, 0o600)
                else:
                    os.chmod(path, 0o600)
            except BaseException:
                if previous_size is not None:
                    os.ftruncate(fd, previous_size)
                raise
            finally:
                os.close(fd)
    except Exception:
        # Diagnostic I/O must not break a gateway response.
        return


def iter_request_records(*, tail: int | None = None, max_bytes: int | None = None, max_records: int | None = None) -> Iterable[dict[str, Any]]:
    path = request_log_path()
    if tail == 0:
        return []
    lines: list[bytes] = []
    remaining = max_bytes
    bounded_gap = False
    try:
        with _log_lock(path, existing_only=True) as available:
            if not available:
                return []
            bounded = max_bytes is not None or max_records is not None
            segments = _retained_paths(path)
            # Spend diagnostic budgets on the newest evidence. Prepend each
            # older segment so terminal selection still sees chronological rows.
            for segment in reversed(segments) if bounded else segments:
                if (remaining is not None and remaining <= 0) or (max_records is not None and len(lines) >= max_records):
                    bounded_gap = True
                    break
                flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
                fd = os.open(segment, flags)
                with os.fdopen(fd, "rb") as handle:
                    info = os.fstat(handle.fileno())
                    if not stat.S_ISREG(info.st_mode):
                        raise OSError("request-log segment is not regular")
                    if remaining is None:
                        raw = handle.read()
                    else:
                        start = max(0, info.st_size - remaining)
                        if start:
                            bounded_gap = True
                            handle.seek(start - 1)
                            boundary = handle.read(1)
                        else:
                            boundary = b"\n"
                        raw = handle.read(remaining)
                        remaining -= len(raw)
                        if boundary not in {b"\n", b"\r"}:
                            # Discard only the partial leading record, never
                            # attempt to parse a clipped JSON or UTF-8 fragment.
                            end = raw.find(b"\n")
                            raw = raw[end + 1:] if end >= 0 else b""
                    segment_lines = raw.splitlines()
                    lines = segment_lines + lines if bounded else lines + segment_lines
                    if max_records is not None and len(lines) > max_records:
                        lines = lines[-max_records:]
                        bounded_gap = True
                        break
    except Exception:
        return [{"status": "malformed", "error": "request-log history could not be read consistently"}]
    records = ([{"status": "log_gap", "error": "bounded diagnostic history omitted data"}] if bounded_gap else [])
    seen = set()
    for line in lines:
        if not line.strip():
            continue
        try:
            parsed = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
            records.append({"status": "malformed", "error": "malformed JSONL request-log entry"})
            continue
        if not isinstance(parsed, dict):
            records.append({"status": "malformed", "error": "request-log entry is not an object"})
            continue
        # Only identified exact event duplicates are safe to remove. Distinct
        # terminal updates and legacy ID-less requests remain independent rows.
        if isinstance(parsed.get("request_id"), str) and parsed["request_id"]:
            try:
                identity = json.dumps(parsed, sort_keys=True, separators=(",", ":"))
            except (ValueError, RecursionError):
                records.append({"status": "malformed", "error": "request-log entry exceeds parsing limits"})
                continue
            if identity in seen:
                continue
            seen.add(identity)
        sanitized = redact_secrets(parsed)
        records.append(sanitized if isinstance(sanitized, dict) else
                       {"status": "malformed", "error": "request-log entry exceeds redaction limits"})
    if tail is not None and tail > 0:
        selected = records[-tail:]
        if bounded_gap and not any(row.get("status") == "log_gap" for row in selected):
            selected.insert(0, records[0])
        return selected
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


def _record_phase(record: dict[str, Any]) -> str:
    phase = record.get("lifecycle_phase")
    if isinstance(phase, str) and phase in {"started", "attempt", "terminal"}:
        return phase
    return "started" if record.get("status") == "stream_started" else "terminal"


def _record_outcome(record: dict[str, Any]) -> str:
    if record.get("cancelled") is True or record.get("status") == "cancelled":
        return "cancelled"
    terminal = record.get("terminal_kind")
    if isinstance(terminal, str) and terminal in {"completed", "incomplete", "failed"}:
        return terminal
    status = record.get("status")
    return {"success": "completed", "completed": "completed", "incomplete": "incomplete"}.get(status, "failed") if isinstance(status, str) else "failed"


def _nonnegative_int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    try:
        return max(0, int(value))
    except (ValueError, TypeError, OverflowError):
        return default


def request_log_summary(*, since: str | None = "24h", now: float | None = None,
                        records: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    window_seconds = _parse_since_seconds(since)
    now_value = time.time() if now is None else float(now)
    cutoff = None if window_seconds is None else now_value - window_seconds
    records = list(iter_request_records()) if records is None else records
    groups: dict[str, dict[str, Any]] = {}
    malformed_records = 0
    omitted_records = 0
    excluded_by_time = 0
    retained_times = [value for record in records if (value := _timestamp_epoch(record.get("timestamp"))) is not None]
    logical: dict[tuple[str, object], list[dict[str, Any]]] = {}
    for index, record in enumerate(records):
        if record.get("status") == "log_gap":
            omitted_records += 1
            continue
        if record.get("status") == "malformed":
            malformed_records += 1
            continue
        request_id = record.get("request_id")
        # Legacy rows without an ID cannot be safely combined with one another.
        key = ("id", request_id) if isinstance(request_id, str) and request_id else ("legacy", index)
        logical.setdefault(key, []).append(record)

    included_events = 0
    for events in logical.values():
        terminal_events = [event for event in events if _record_phase(event) == "terminal"]
        # The last terminal record is authoritative; later start/replayed
        # lifecycle records cannot reopen it. Duplicate terminals count once.
        chosen = terminal_events[-1] if terminal_events else events[-1]
        record = {**events[0], **chosen}
        for field in ("usage", "latency_ms", "attempt_count", "rotation_count", "http_status", "upstream_http_status", "provider_accepted", "family", "provider"):
            metric = field not in {"family", "provider"}
            if metric:
                record[field] = chosen.get(field)
            if record.get(field) is None:
                source = [] if metric else events
                previous = next((event[field] for event in reversed(source) if event.get(field) is not None), None)
                if previous is not None:
                    record[field] = previous
        timestamp = _timestamp_epoch(chosen.get("timestamp"))
        if cutoff is not None and timestamp is not None and timestamp < cutoff:
            excluded_by_time += len(events)
            continue
        included_events += len(events)
        route = str(record.get("route") or "unknown")
        family = str(record.get("family") or record.get("provider") or "unknown")
        key = f"{route}/{family}"
        group = groups.setdefault(key, {
            "route": route, "family": family,
            "request_count": 0, "closed_request_count": 0, "open_count": 0,
            "success_count": 0, "failure_count": 0, "incomplete_count": 0,
            "cancellation_count": 0, "provider_accepted_count": 0,
            "provider_acceptance_unknown_count": 0,
            "rate_limit_count": 0, "rotation_attempted_count": 0,
            "attempt_count": 0, "rotation_count": 0,
            "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
            "terminal_counts": {}, "terminal_reason_counts": {},
            "_latencies": [], "_errors": {},
        })
        group["request_count"] += 1
        acceptance = record.get("provider_accepted")
        if not isinstance(acceptance, bool):
            acceptance = None
        if acceptance is None and record.get("upstream_http_status") is not None:
            acceptance = 200 <= _nonnegative_int(record.get("upstream_http_status")) < 300
        if acceptance is True:
            group["provider_accepted_count"] += 1
        elif acceptance is None:
            group["provider_acceptance_unknown_count"] += 1
        if not terminal_events:
            group["open_count"] += 1
            continue
        group["closed_request_count"] += 1
        outcome = _record_outcome(record)
        count_field = {"completed": "success_count", "failed": "failure_count", "incomplete": "incomplete_count", "cancelled": "cancellation_count"}[outcome]
        group[count_field] += 1
        group["terminal_counts"][outcome] = group["terminal_counts"].get(outcome, 0) + 1
        reason = record.get("terminal_reason")
        reason = reason if isinstance(reason, str) and reason else outcome
        group["terminal_reason_counts"][reason] = group["terminal_reason_counts"].get(reason, 0) + 1
        latency = record.get("latency_ms")
        try:
            latency_ms = int(float(latency))
            if latency_ms >= 0:
                group["_latencies"].append(latency_ms)
        except (TypeError, ValueError, OverflowError):
            pass
        if 429 in {_nonnegative_int(record.get("http_status")), _nonnegative_int(record.get("upstream_http_status"))}:
            group["rate_limit_count"] += 1
        group["rotation_attempted_count"] += int(bool(record.get("rotation_attempted")))
        group["attempt_count"] += _nonnegative_int(record.get("attempt_count"), 1)
        group["rotation_count"] += _nonnegative_int(record.get("rotation_count"))
        usage = record.get("usage")
        if isinstance(usage, dict):
            for field in group["usage"]:
                group["usage"][field] += _nonnegative_int(usage.get(field))
        error_class = record.get("error_class")
        if isinstance(error_class, str) and error_class:
            group["_errors"][error_class] = group["_errors"].get(error_class, 0) + 1

    rendered_groups: dict[str, dict[str, Any]] = {}
    for key, group in sorted(groups.items()):
        closed = group["closed_request_count"]
        latencies, errors = group.pop("_latencies"), group.pop("_errors")
        rendered_groups[key] = {
            **group,
            "success_rate": round(group["success_count"] / closed, 4) if closed else None,
            "p50_latency_ms": _percentile(latencies, 50),
            "p95_latency_ms": _percentile(latencies, 95),
            "top_error_classes": [
                {"error_class": error_class, "count": count}
                for error_class, count in sorted(errors.items(), key=lambda item: (-item[1], item[0]))[:3]
            ],
        }
    return {
        "path": str(request_log_path()), "since": since if since is not None else "all",
        "window_seconds": window_seconds, "total_records": len(records),
        "included_records": sum(group["request_count"] for group in rendered_groups.values()),
        "included_event_records": included_events,
        "excluded_by_time": excluded_by_time, "malformed_records": malformed_records,
        "omitted_records": omitted_records,
        "earliest_retained_timestamp": datetime.fromtimestamp(min(retained_times), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if retained_times else None,
        "latest_retained_timestamp": datetime.fromtimestamp(max(retained_times), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if retained_times else None,
        "requested_window_incomplete": (
            (True if malformed_records or omitted_records else
             min(retained_times) > cutoff if retained_times else None)
            if cutoff is not None else None
        ),
        "groups": rendered_groups,
    }


def clean_request_logs() -> list[str]:
    path = request_log_path()
    removed = []
    try:
        with _log_lock(path, existing_only=True) as available:
            if not available:
                return []
            for segment in _retained_paths(path):
                try:
                    segment.unlink()
                    removed.append(str(segment))
                except OSError:
                    pass
    except OSError:
        pass
    return removed
