"""Terminal-only cleanup, coordinated with the existing per-run writer lock."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import time
from typing import Any

from .persistence import PersistenceError, atomic_write_json, file_lock, fsync_directory

RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
TERMINAL_STATES = {"success", "partial", "error", "failed", "interrupted"}


def assert_not_deleted(root: Path, run_id: str) -> None:
    directory = root / ".deleted"
    if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
        raise PersistenceError("Unsafe run deletion-marker directory; refusing a run write")
    marker = directory / f"{run_id}.json"
    if marker.exists() or marker.is_symlink():
        raise PersistenceError("Run id was reserved by cleanup; choose a new run id")


def _artifact_problem(path: Path) -> str | None:
    if path.is_symlink():
        return "artifact_symlink"
    if path.exists():
        if not path.is_dir():
            return "artifact_not_directory"
        if any(child.is_symlink() for child in path.rglob("*")):
            return "artifact_contains_symlink"
    return None


def _candidate(root: Path, run_id: str, cutoff: float) -> dict[str, Any]:
    row: dict[str, Any] = {"id": run_id, "action": "skip", "reason": "unknown"}
    if not RUN_ID_RE.fullmatch(run_id):
        return {**row, "reason": "invalid_id"}
    path = root / f"{run_id}.json"
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode):
            return {**row, "reason": "record_not_regular"}
        raw = path.read_bytes()
        value = json.loads(raw)
        if not isinstance(value, dict) or value.get("id") != run_id:
            return {**row, "reason": "unknown_identity"}
        state = value.get("status")
        if state == "running":
            return {**row, "reason": "running_or_uncertain"}
        if not isinstance(state, str) or state not in TERMINAL_STATES:
            return {**row, "reason": "unknown_state"}
        if info.st_mtime >= cutoff:
            return {**row, "reason": "too_recent"}
        problem = _artifact_problem(root / run_id)
        if problem:
            return {**row, "reason": problem}
        return {**row, "action": "remove", "reason": "old_terminal", "status": state,
                "sha256": hashlib.sha256(raw).hexdigest(), "mtime_ns": info.st_mtime_ns}
    except FileNotFoundError:
        return {**row, "reason": "missing_record"}
    except (OSError, ValueError):
        return {**row, "reason": "unreadable_or_corrupt"}


def _marker(root: Path, run_id: str) -> dict[str, Any] | None:
    path = root / ".deleted" / f"{run_id}.json"
    if path.is_symlink():
        raise PersistenceError("Unsafe cleanup marker; preserve it for manual recovery")
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PersistenceError("Unreadable cleanup marker; preserve it for manual recovery") from exc
    if (not isinstance(value, dict) or value.get("schemaVersion") != 1 or value.get("runId") != run_id
            or value.get("state") not in ("deleting", "deleted")
            or not isinstance(value.get("sourceSha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", value["sourceSha256"])
            or type(value.get("sourceMtimeNs")) is not int):
        raise PersistenceError("Invalid cleanup marker; preserve it for manual recovery")
    return value


def _pending_problem(root: Path, run_id: str, marker: dict[str, Any], cutoff: float) -> str | None:
    path = root / f"{run_id}.json"
    artifact = root / run_id
    if path.exists() or path.is_symlink():
        current = _candidate(root, run_id, cutoff)
        if (current["action"] != "remove" or current["sha256"] != marker["sourceSha256"]
                or current["mtime_ns"] != marker["sourceMtimeNs"]):
            return "pending_record_changed"
    elif artifact.exists() or artifact.is_symlink():
        return "orphan_artifact_after_cleanup"
    return None


def _incomplete(root: Path, run_id: str, reason: str) -> dict[str, Any]:
    return {"id": run_id, "action": "error", "reason": reason,
            "recordPath": str(root / f"{run_id}.json"), "artifactPath": str(root / run_id),
            "markerPath": str(root / ".deleted" / f"{run_id}.json"),
            "recovery": "Preserve and inspect the retained paths for manual recovery; resume only after the inconsistency is resolved."}


def plan_cleanup(root: Path, cutoff: float, *, resume: bool = False) -> list[dict[str, Any]]:
    """A read-only snapshot. Execution always repeats the decision under lock."""
    if root.is_symlink():
        raise PersistenceError("Refusing cleanup through a symlinked run directory")
    directory = root / ".deleted"
    if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
        raise PersistenceError("Unsafe cleanup marker directory")
    if not root.exists():
        return []
    ids = {path.stem for path in root.glob("*.json")}
    ids.update(path.stem for path in directory.glob("*.json"))
    rows = []
    for run_id in sorted(ids):
        if not RUN_ID_RE.fullmatch(run_id):
            rows.append({"id": run_id, "action": "skip", "reason": "invalid_id"})
            continue
        try:
            marker = _marker(root, run_id)
        except PersistenceError:
            rows.append({"id": run_id, "action": "skip", "reason": "invalid_cleanup_marker"})
            continue
        if marker:
            if marker["state"] == "deleted":
                # A reused path is retained for inspection, never silently deleted.
                if (root / f"{run_id}.json").exists() or (root / run_id).exists():
                    rows.append({"id": run_id, "action": "skip", "reason": "reserved_id_reappeared"})
                continue
            problem = _pending_problem(root, run_id, marker, cutoff) if resume else None
            row = {"id": run_id, "action": "resume" if resume and not problem else "skip", "reason": problem or "pending_cleanup"}
        else:
            row = _candidate(root, run_id, cutoff)
        rows.append(row)
    # No age-based deletion of files whose writer/state cannot be established.
    for path in sorted(root.glob("*.tmp")):
        rows.append({"id": path.name, "action": "skip", "reason": "temporary_ownership_unknown"})
    return rows


def _remove_locked(root: Path, row: dict[str, Any], cutoff: float, *, resume: bool) -> dict[str, Any]:
    run_id = row["id"]
    path = root / f"{run_id}.json"
    artifact = root / run_id
    directory = root / ".deleted"
    marker_path = directory / f"{run_id}.json"
    if root.is_symlink() or directory.is_symlink() or (directory.exists() and not directory.is_dir()):
        raise PersistenceError("Unsafe cleanup directory")
    marker = _marker(root, run_id)
    if marker:
        if marker["state"] == "deleted":
            if path.exists() or path.is_symlink() or artifact.exists() or artifact.is_symlink():
                return {"id": run_id, "action": "skip", "reason": "reserved_id_reappeared"}
            return {"id": run_id, "action": "skip", "reason": "cleanup_already_complete"}
        if not resume:
            return {"id": run_id, "action": "skip", "reason": "cleanup_state_changed"}
        problem = _pending_problem(root, run_id, marker, cutoff)
        if problem:
            return {"id": run_id, "action": "skip", "reason": problem}
    else:
        if row["action"] == "resume":
            return {"id": run_id, "action": "skip", "reason": "pending_marker_missing"}
        current = _candidate(root, run_id, cutoff)
        if current["action"] != "remove":
            return current
        if row.get("sha256") != current["sha256"] or row.get("mtime_ns") != current["mtime_ns"]:
            return {"id": run_id, "action": "skip", "reason": "record_changed_since_plan"}
        directory.mkdir(mode=0o700, exist_ok=True)
        os.chmod(directory, 0o700)
        marker = {"schemaVersion": 1, "runId": run_id, "state": "deleting",
                  "sourceSha256": current["sha256"], "sourceMtimeNs": current["mtime_ns"],
                  "createdAt": int(time.time())}
        # Writers check this reservation under the same run-record lock.
        atomic_write_json(marker_path, marker)
    problem = _artifact_problem(artifact)
    if problem:
        raise PersistenceError(problem)
    if artifact.exists():
        shutil.rmtree(artifact)
    path.unlink(missing_ok=True)
    fsync_directory(root)
    atomic_write_json(marker_path, {**marker, "state": "deleted"})
    return {"id": run_id, "action": "removed", "reason": "terminal_cleanup_complete"}


def clean_runs(root: Path, cutoff: float, *, dry_run: bool = False, resume: bool = False) -> dict[str, Any]:
    def report(rows):
        failures = {"pending_record_changed", "orphan_artifact_after_cleanup", "invalid_cleanup_marker",
                    "reserved_id_reappeared", "pending_marker_missing"}
        rows = [_incomplete(root, row["id"], row["reason"]) if resume and row["reason"] in failures else row
                for row in rows]
        public = [{key: value for key, value in row.items() if key not in {"sha256", "mtime_ns"}} for row in rows]
        return {"dryRun": dry_run, "rows": public, "reflections": "retained",
                "errors": sum(row["action"] == "error" for row in rows)}
    rows = plan_cleanup(root, cutoff, resume=resume)
    if dry_run:
        return report(rows)
    results = []
    for row in rows:
        if row["action"] not in {"remove", "resume"}:
            results.append(row)
            continue
        try:
            with file_lock(root / f"{row['id']}.json"):
                results.append(_remove_locked(root, row, cutoff, resume=resume))
        except (OSError, PersistenceError):
            # Do not embed raw error/record text in a shareable cleanup report.
            results.append(_incomplete(root, row["id"], "cleanup_incomplete"))
    return report(results)
