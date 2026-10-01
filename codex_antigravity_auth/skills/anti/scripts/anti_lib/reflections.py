"""Phase 8: Repo-level reflection memory.

Passively tracks review findings per repo so patterns can be surfaced
on subsequent reviews. Does NOT suppress or modify findings — only
records and reports.
"""
from __future__ import annotations

import hashlib
import json
import math
import time
from pathlib import Path
from typing import Any

from .redaction import sanitize_json
from .retention import summary_projection, summary_retention, summary_structure
from .persistence import PersistenceError, atomic_write_json, file_lock
from .file_protection import ensure_private_directory, protect_existing_file

REFLECTIONS_DIR = Path.home() / ".codex" / "anti-runs" / "reflections"
MAX_ENTRIES_PER_REPO = 500
TTL_DAYS = 90


def _repo_hash(repo_path: Path) -> str:
    """Stable short hash for a repo path."""
    resolved = str(repo_path.resolve())
    return hashlib.sha256(resolved.encode()).hexdigest()[:12]


def _reflection_path(repo_path: Path) -> Path:
    return REFLECTIONS_DIR / f"{_repo_hash(repo_path)}.json"


def _ensure_permissions(directory: Path | None = None) -> None:
    """Force owner-only access on reflection data, including legacy files."""
    directory = directory or REFLECTIONS_DIR
    if not directory.exists():
        return
    ensure_private_directory(directory, enforce_existing=True)
    for path in directory.rglob("*.json"):
        if not path.is_symlink() and path.is_file():
            protect_existing_file(path)


def _valid_record(row: Any) -> bool:
    """Validate reader inputs without normalizing or discarding extension fields."""
    if not isinstance(row, dict):
        return False
    timestamp = row.get("timestamp")
    if type(timestamp) not in (int, float) or not 0 <= timestamp <= 2**63 - 1 or not math.isfinite(timestamp):
        return False
    try:
        time.strftime("%Y-%m-%d %H:%M", time.localtime(timestamp))
    except (ValueError, OverflowError, OSError):
        return False
    count = row.get("findings_count", 0)
    if type(count) is not int or not 0 <= count <= 2**63 - 1:
        return False
    models = row.get("models", [])
    if not isinstance(models, list) or any(not isinstance(model, str) for model in models):
        return False
    for key in ("mode", "panel_status", "run_id", "verdict", "repo", "scope", "save_output"):
        if key in row and row[key] is not None and not isinstance(row[key], str):
            return False
    findings = row.get("findings", [])
    if not isinstance(findings, list):
        return False
    for finding in findings:
        if not isinstance(finding, dict):
            return False
        if not isinstance(finding.get("severity", "medium"), str):
            return False
        # Null file/fingerprint denotes an unmapped finding and is safely
        # skipped by readers. Containers and other scalars are malformed.
        for key in ("fingerprint", "file"):
            if key in finding and finding[key] is not None and not isinstance(finding[key], str):
                return False
    return True


def _validate_records(path: Path, records: Any) -> None:
    if not isinstance(records, list) or any(not _valid_record(row) for row in records):
        raise PersistenceError(
            f"Invalid reflection history shape at {path}. Preserve this file and make a backup "
            "before manual recovery; no history was replaced."
        )


def _load_records(path: Path) -> list[dict[str, Any]]:
    guidance = "Preserve this file and make a backup before manual recovery; no history was replaced."
    if path.is_symlink() or path.parent.is_symlink():
        raise PersistenceError(f"Refusing symlinked reflection history. {guidance}")
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except UnicodeError as exc:
        raise PersistenceError(f"Corrupt reflection history at {path}. {guidance}") from exc
    except OSError as exc:
        raise PersistenceError(f"Unreadable reflection history at {path}. {guidance}") from exc
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise PersistenceError(f"Corrupt reflection history at {path}. {guidance}") from exc
    _validate_records(path, data)
    return data


def _save_records(path: Path, records: list[dict[str, Any]]) -> None:
    _validate_records(path, records)
    ensure_private_directory(path.parent, enforce_existing=True)
    atomic_write_json(path, records)


def _prune_old(records: list[dict[str, Any]], ttl_days: int = TTL_DAYS) -> list[dict[str, Any]]:
    cutoff = time.time() - (ttl_days * 86400)
    return [r for r in records if r.get("timestamp", 0) > cutoff]


def record_review(
    *,
    repo_path: Path,
    findings: list[dict[str, Any]],
    models: list[str],
    panel_status: str,
    mode: str,
    scope: str = "",
    run_id: str | None = None,
    verdict: str = "pending",
    save_output: str = "summary",
) -> dict[str, Any] | None:
    """Record a review's findings for future pattern analysis.
    
    Returns the record that was saved.
    """
    if save_output not in {"never", "summary", "full"}:
        raise ValueError("unsupported reflection retention mode")
    if save_output == "never":
        return None
    record = {
        "save_output": save_output,
        "timestamp": int(time.time()),
        "repo": str(repo_path.resolve()),
        "mode": mode,
        "scope": scope,
        "models": models,
        "panel_status": panel_status,
        "run_id": run_id,
        "verdict": verdict,
        "findings": [
            {
                "id": f.get("id", ""),
                "fingerprint": f.get("fingerprint", ""),
                "severity": f.get("severity", "medium"),
                "file": f.get("file", ""),
                "line": f.get("line"),
                "claim": f.get("claim", ""),
                "evidence": f.get("evidence", "unverified"),
                "confidence": f.get("confidence", 0.5),
            }
            for f in findings if isinstance(f, dict)
        ],
        "findings_count": len(findings),
    }
    
    record = sanitize_json(record)
    if save_output == "summary":
        structure = summary_structure(record, (
            "save_output", "timestamp", "mode", "panel_status", "run_id", "verdict", "findings_count",
        ))
        record = summary_projection({key: value for key, value in record.items() if key not in structure})
        record.update(structure)
        record["retention"] = summary_retention()
    path = _reflection_path(repo_path)
    with file_lock(path):
        records = _load_records(path)
        _ensure_permissions()
        records.append(record)
        records = _prune_old(records)
        # Keep bounded
        if len(records) > MAX_ENTRIES_PER_REPO:
            records = records[-MAX_ENTRIES_PER_REPO:]
        _save_records(path, records)
    return record


def update_verdict(repo_path: Path, run_id: str, verdict: str) -> dict[str, Any] | None:
    """Update the verdict of the reflection record matching run_id."""
    path = _reflection_path(repo_path)
    updated: dict[str, Any] | None = None
    with file_lock(path):
        records = _load_records(path)
        for record in reversed(records):
            if record.get("run_id") == run_id:
                record["verdict"] = verdict
                updated = record
                break
        if updated is not None:
            _save_records(path, records)
    return updated


def list_records(repo_path: Path, limit: int | None = 20) -> list[dict[str, Any]]:
    """List recent reflection records for a repo."""
    records = _load_records(_reflection_path(repo_path))
    if limit is not None:
        records = records[-limit:]
    return list(reversed(records))


def get_summary(repo_path: Path) -> dict[str, Any]:
    """Summarize reflection history for a repo."""
    records = _load_records(_reflection_path(repo_path))
    if not records:
        return {"repo": str(repo_path), "records": 0}
    
    total_findings = sum(r.get("findings_count", 0) for r in records)
    all_fingerprints: dict[str, int] = {}
    all_severities: dict[str, int] = {}
    all_models: dict[str, int] = {}
    file_counts: dict[str, int] = {}
    
    for r in records:
        for f in r.get("findings", []):
            fp = f.get("fingerprint", "")
            if fp:
                all_fingerprints[fp] = all_fingerprints.get(fp, 0) + 1
            sev = f.get("severity", "medium")
            all_severities[sev] = all_severities.get(sev, 0) + 1
            file_path = f.get("file", "")
            if file_path:
                # Use just the filename for grouping
                fname = file_path.rsplit("/", 1)[-1] if "/" in file_path else file_path
                file_counts[fname] = file_counts.get(fname, 0) + 1
        for m in r.get("models", []):
            all_models[m] = all_models.get(m, 0) + 1
    
    # Find recurring fingerprints (same finding flagged in multiple reviews)
    recurring = {fp: count for fp, count in all_fingerprints.items() if count > 1}
    
    return {
        "repo": str(repo_path),
        "records": len(records),
        "total_findings": total_findings,
        "recurring_fingerprints": len(recurring),
        "top_recurring": sorted(recurring.items(), key=lambda x: -x[1])[:5],
        "severity_distribution": all_severities,
        "most_reviewed_files": sorted(file_counts.items(), key=lambda x: -x[1])[:10],
        "models_used": all_models,
        "date_range": (
            time.strftime("%Y-%m-%d", time.localtime(records[0]["timestamp"])),
            time.strftime("%Y-%m-%d", time.localtime(records[-1]["timestamp"])),
        ),
    }


def clear_records(repo_path: Path) -> int:
    """Delete all reflection records for a repo. Returns count deleted."""
    path = _reflection_path(repo_path)
    with file_lock(path):
        if path.is_symlink():
            raise RuntimeError(f"Refusing to delete symlinked reflection file: {path}")
        records = _load_records(path)
        count = len(records)
        if path.exists() and not path.is_symlink():
            path.unlink()
        return count


def prune_reflections_older_than(cutoff_epoch: float, *, dry_run: bool = False) -> int:
    """Remove reflection files whose newest record is older than the cutoff.

    A file is deleted entirely when every remaining record would be stale;
    partial pruning inside a file is handled by the per-record TTL during
    normal writes. Returns number of files removed (or that would be).
    """
    if not REFLECTIONS_DIR.exists():
        return 0
    removed = 0
    for path in REFLECTIONS_DIR.glob("*.json"):
        with file_lock(path):
            records = _load_records(path)
            if not records:
                continue
            newest = max(r.get("timestamp", 0) for r in records)
            if newest < cutoff_epoch:
                if not dry_run and path.exists() and not path.is_symlink():
                    path.unlink()
                removed += 1
    return removed
