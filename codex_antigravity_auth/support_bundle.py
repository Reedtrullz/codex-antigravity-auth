"""Allowlisted local support evidence. Never export raw diagnostic dictionaries."""
from __future__ import annotations

from datetime import datetime, timezone
from importlib import metadata
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import platform
import re
import secrets
import sys

from . import observability
from .codex_config import parse_provider_config
from .namespaces import client_config_path, gateway_home
from .skills.anti.scripts.anti_lib.file_protection import protect_descriptor, verify_regular_descriptor

MAX_CONFIG_BYTES = 1024 * 1024
MAX_HISTORY_BYTES = 2 * 1024 * 1024
MAX_HISTORY_RECORDS = 2000
MAX_SELECTED = 20
MAX_BUNDLE_BYTES = 256 * 1024
ROUTES = {"google", "openai", "byok", "antigravity", "unknown"}
OUTCOMES = {"completed", "incomplete", "failed", "cancelled", "started", "success", "error", "unknown"}


def number(value):
    if type(value) is int:
        return value if 0 <= value <= 2**53 - 1 else None
    return value if type(value) is float and math.isfinite(value) and 0 <= value <= 2**53 - 1 else None


def timestamp(value):
    if type(value) is not str or len(value) > 40:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return None
        return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    except ValueError:
        return None


def _open_read(path):
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    try:
        verify_regular_descriptor(fd, path)
    except BaseException:
        os.close(fd)
        raise
    return fd


def store_access(path):
    try:
        fd = _open_read(path)
    except FileNotFoundError:
        return {"state": "missing", "decryption": "not_attempted"}
    except (OSError, ValueError):
        return {"state": "unavailable", "decryption": "not_attempted"}
    os.close(fd)
    return {"state": "readable", "decryption": "not_attempted"}


def config_structure(path):
    empty = {"state": "missing", "modelSelected": False, "providerSelected": False,
             "providerTableCount": 0, "selectedProviderPresent": False, "wireApi": "unknown",
             "endpoint": "unknown", "liveProbe": "not_run"}
    try:
        with os.fdopen(_open_read(path), "rb") as handle:
            raw = handle.read(MAX_CONFIG_BYTES + 1)
        if len(raw) > MAX_CONFIG_BYTES:
            return {**empty, "state": "too_large"}
        parsed = parse_provider_config(raw.decode("utf-8"))
    except FileNotFoundError:
        return empty
    except (OSError, ValueError, RecursionError):
        return {**empty, "state": "unavailable"}
    tables = parsed["provider_tables"]
    active = parsed["active_provider"]
    selected = tables.get(active, {})
    wire = selected.get("wire_api")
    url = selected.get("base_url")
    # Structural classification only: no URL, host, model or provider label is exported.
    from urllib.parse import urlsplit
    endpoint = "unknown"
    if type(url) is str:
        try:
            parts = urlsplit(url)
            if parts.scheme == "https" and parts.hostname:
                endpoint = "https"
            elif parts.scheme == "http" and parts.hostname in {"localhost", "127.0.0.1", "::1"}:
                endpoint = "loopback_http"
            else:
                endpoint = "other"
        except ValueError:
            endpoint = "invalid"
    return {**empty, "state": "parsed", "modelSelected": bool(parsed["active_model"]),
            "providerSelected": bool(active), "providerTableCount": len(tables),
            "selectedProviderPresent": active in tables,
            "wireApi": "responses" if wire == "responses" else "missing" if wire is None else "other",
            "endpoint": endpoint}


def _public_version():
    try:
        value = metadata.version("codex-antigravity-auth")
    except metadata.PackageNotFoundError:
        return "unknown"
    return value if re.fullmatch(r"[0-9]+(?:\.[0-9]+){1,3}(?:(?:a|b|rc|post|dev)[0-9]+)?", value) else "unknown"


def _enum(value, options):
    return value if type(value) is str and value in options else "unknown"


def _project_record(row, *, key, selected_index):
    raw_id = row.get("request_id")
    reference = hmac.new(key, raw_id.encode("utf-8"), hashlib.sha256).hexdigest()[:24]
    return {"requestRef": reference, "selectedInputIndex": selected_index,
            "timestamp": timestamp(row.get("timestamp")),
            "route": _enum(row.get("route"), ROUTES),
            "phase": _enum(row.get("phase"), {"started", "terminal"}),
            "status": _enum(row.get("status"), OUTCOMES),
            "httpStatus": number(row.get("http_status")), "latencyMs": number(row.get("latency_ms")),
            "providerAccepted": row.get("provider_accepted") if type(row.get("provider_accepted")) is bool else None}


def collect_bundle(*, config="~/.codex/config.toml", since="24h", request_ids=()):
    if len(request_ids) > MAX_SELECTED or any(type(value) is not str or not 1 <= len(value) <= 256 for value in request_ids):
        raise ValueError("invalid request selection")
    # Validate the window before opening any diagnostic files.
    window = observability._parse_since_seconds(since)
    root = gateway_home()
    records = list(observability.iter_request_records(max_bytes=MAX_HISTORY_BYTES, max_records=MAX_HISTORY_RECORDS))
    summary = observability.request_log_summary(since=since, records=records)
    key = secrets.token_bytes(32)  # not exported; references cannot correlate bundles
    rows, matched = [], set()
    selected = {value: index for index, value in enumerate(request_ids)}
    cutoff = None if window is None else datetime.now(timezone.utc).timestamp() - window
    for row in records:
        raw_id = row.get("request_id")
        if type(raw_id) is not str or raw_id not in selected:
            continue
        normalized_time = timestamp(row.get("timestamp"))
        if cutoff is not None and (normalized_time is None or datetime.fromisoformat(normalized_time.replace("Z", "+00:00")).timestamp() < cutoff):
            continue
        if len(rows) >= 100:
            break
        rows.append(_project_record(row, key=key, selected_index=selected[raw_id]))
        matched.add(selected[raw_id])
    count_fields = ("request_count", "closed_request_count", "open_count", "success_count", "failure_count",
                    "incomplete_count", "cancellation_count", "rate_limit_count", "attempt_count", "rotation_count")
    counts = {field: sum(number(group.get(field)) or 0 for group in summary["groups"].values()) for field in count_fields}
    counts_limited = any(value > 2**53 - 1 for value in counts.values())
    counts = {field: min(value, 2**53 - 1) for field, value in counts.items()}
    config_info = config_structure(client_config_path(config))
    stores = {name: store_access(root / filename) for name, filename in (
        ("accounts", "antigravity-accounts.json"), ("providers", "antigravity-providers.json"))}
    warnings = ["aggregate_counts_limited"] if counts_limited else []
    if config_info["state"] != "parsed":
        warnings.append("config_unavailable")
    if any(store["state"] == "unavailable" for store in stores.values()):
        warnings.append("store_unavailable")
    if summary["requested_window_incomplete"] is not False:
        warnings.append("history_coverage_unknown" if summary["requested_window_incomplete"] is None else "history_incomplete")
    if len(matched) < len(set(request_ids)):
        warnings.append("request_selection_not_fully_observed")
    if len(rows) >= 100:
        warnings.append("selected_records_capped")
    bundle = {"schemaVersion": 1, "kind": "support_bundle", "createdAt": datetime.now(timezone.utc).isoformat(),
              "versions": {"package": _public_version(), "python": platform.python_version(),
                           "platform": sys.platform if sys.platform in {"darwin", "linux", "win32"} else "other"},
              "config": config_info, "stores": stores,
              "history": {"windowSeconds": window, "requestedWindowIncomplete": summary["requested_window_incomplete"],
                          "earliestRetainedTimestamp": timestamp(summary["earliest_retained_timestamp"]),
                          "latestRetainedTimestamp": timestamp(summary["latest_retained_timestamp"]),
                          "malformedRecords": summary["malformed_records"], "omittedRecords": summary["omitted_records"],
                          "counts": counts, "selectedRequestCount": len(set(request_ids)), "matchedRequestCount": len(matched),
                          "records": rows, "limits": {"bytes": MAX_HISTORY_BYTES, "records": MAX_HISTORY_RECORDS,
                                                       "selectedRecords": 100}},
              "warnings": warnings}
    encoded = json.dumps(bundle, ensure_ascii=True, allow_nan=False).encode("utf-8")
    if len(encoded) > MAX_BUNDLE_BYTES:
        raise ValueError("support bundle exceeded its limit")
    return bundle


def export_bundle(bundle, output):
    """Explicit create-only publication; no overwrite, upload or parent creation."""
    path = Path(output).expanduser().absolute()
    content = (json.dumps(bundle, indent=2, ensure_ascii=True, allow_nan=False) + "\n").encode()
    if len(content) > MAX_BUNDLE_BYTES:
        raise ValueError("support bundle exceeded its limit")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        protect_descriptor(fd, path=path)
        with os.fdopen(fd, "wb", closefd=False) as handle:
            handle.write(content)
            handle.flush()
            os.fsync(fd)
        verify_regular_descriptor(fd, path)
    finally:
        os.close(fd)
    # Failed writes intentionally retain the new partial file for inspection;
    # subsequent retries cannot overwrite it, and CLI reports failure.
