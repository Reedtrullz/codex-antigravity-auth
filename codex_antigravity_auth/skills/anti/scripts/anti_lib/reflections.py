"""Phase 8: Repo-level reflection memory.

Passively tracks review findings per repo so patterns can be surfaced
on subsequent reviews. Does NOT suppress or modify findings — only
records and reports.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

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
    os.chmod(directory, 0o700)
    for path in directory.rglob("*.json"):
        if path.is_file():
            current_mode = path.stat().st_mode & 0o777
            if current_mode != 0o600:
                os.chmod(path, 0o600)


def _load_records(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def _save_records(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        # Existing files keep their old mode through O_TRUNC, so enforce 600 here.
        os.fchmod(handle.fileno(), 0o600)
        handle.write(json.dumps(records, indent=2, sort_keys=True))


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
    verdict: str = "pending",
) -> dict[str, Any]:
    """Record a review's findings for future pattern analysis.
    
    Returns the record that was saved.
    """
    record = {
        "timestamp": int(time.time()),
        "repo": str(repo_path.resolve()),
        "mode": mode,
        "scope": scope,
        "models": models,
        "panel_status": panel_status,
        "verdict": verdict,
        "findings": [
            {
                "id": f.get("id", ""),
                "fingerprint": f.get("fingerprint", ""),
                "severity": f.get("severity", "medium"),
                "file": f.get("file", ""),
                "line": f.get("line"),
                "claim": f.get("claim", "")[:200],
                "evidence": f.get("evidence", "unverified")[:200],
                "confidence": f.get("confidence", 0.5),
            }
            for f in findings if isinstance(f, dict)
        ],
        "findings_count": len(findings),
    }
    
    path = _reflection_path(repo_path)
    _ensure_permissions()
    records = _load_records(path)
    records.append(record)
    records = _prune_old(records)
    # Keep bounded
    if len(records) > MAX_ENTRIES_PER_REPO:
        records = records[-MAX_ENTRIES_PER_REPO:]
    _save_records(path, records)
    return record


def list_records(repo_path: Path, limit: int = 20) -> list[dict[str, Any]]:
    """List recent reflection records for a repo."""
    records = _load_records(_reflection_path(repo_path))
    return list(reversed(records[-limit:]))


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
    records = _load_records(path)
    count = len(records)
    if path.exists():
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
        records = _load_records(path)
        if not records:
            continue
        newest = max(r.get("timestamp", 0) for r in records)
        if newest < cutoff_epoch:
            if not dry_run:
                path.unlink()
            removed += 1
    return removed
