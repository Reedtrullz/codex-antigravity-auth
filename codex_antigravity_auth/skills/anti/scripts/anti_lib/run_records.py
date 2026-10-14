"""Run-record retention checks and immutable publication; no CLI back-imports.

Callers own the cross-process lock and pass captured runtime metadata. Publication
writes immutable artifacts before atomically replacing the authoritative index.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import time
from typing import Any
import uuid

from .errors import AntiError
from .artifacts import (ArtifactError, RECORD_SCHEMA_VERSION, SAVED_RESULT_SCHEMA_VERSION,
                        LANE_SCHEMA_VERSION, file_reference, read_record, validate_record, coverage_has_loss)
from .cleanup import RUN_ID_RE, assert_not_deleted
from .context import coverage_summary
from .persistence import atomic_write_json, fsync_directory
from .redaction import redact_sensitive_text, sanitize_json
from .media import projection as media_projection
from .retention import lifecycle_metadata, summary_projection, summary_retention, summary_structure

def utc_timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

def new_run_id() -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + uuid.uuid4().hex[:8]

def check_record_retention(record_id: str, output_mode: str, *, runs_dir: Path) -> None:
    """Do not silently mix policies or delete older artifacts when reusing an ID."""
    if not RUN_ID_RE.fullmatch(record_id):
        raise AntiError("run id must contain only letters, numbers, '_' or '-'")
    if runs_dir.is_symlink():
        raise AntiError("refusing to write Anti run record through symlinked directory")
    assert_not_deleted(runs_dir, record_id)
    path = runs_dir / f"{record_id}.json"
    if path.is_symlink():
        raise AntiError("refusing to overwrite symlinked run record")
    if path.exists():
        try:
            previous = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError) as exc:
            raise AntiError("cannot inspect existing run retention; choose a new run id") from exc
        if not isinstance(previous, dict) or previous.get("save_output") != output_mode:
            raise AntiError("run id already exists with another retention policy; choose a new run id")
    artifact_dir = runs_dir / record_id
    if artifact_dir.is_symlink():
        raise AntiError("refusing to write result artifact through symlink")
    temporary = path.with_suffix(path.suffix + ".tmp")
    if not path.exists() and (artifact_dir.exists() or temporary.exists() or temporary.is_symlink()):
        raise AntiError("run id has orphan artifacts with unknown retention policy; choose a new run id")
    if (output_mode == "never" and artifact_dir.exists()) or (
        output_mode == "summary" and artifact_dir.exists() and any(artifact_dir.glob("lane-*.json"))
    ):
        raise AntiError("run id has artifacts incompatible with retention policy; choose a new run id")

def publish_unlocked(
    args: argparse.Namespace,
    *,
    runs_dir: Path,
    output_mode: str,
    timestamp: str,
    helper: dict | None,
    output_preview_chars: int,
    verification_required_checks: list,
    policy_audit: dict | None = None,
    write_json=None,
    mode: str,
    status: str,
    models: list[str] | None = None,
    base_url: str | None = None,
    prompt_text: str | None = None,
    output_text: str | None = None,
    caveats: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
    error: str | None = None,
    execution_ledger: list[dict[str, Any]] | None = None,
    force_full_output: bool = False,
) -> Path | None:
    write_json = write_json or atomic_write_json
    record_id = getattr(args, "run_id", None)
    if not record_id and output_mode != "never":
        record_id = new_run_id()
    if record_id:
        check_record_retention(str(record_id), output_mode, runs_dir=runs_dir)
    if output_mode == "never":
        # Minimal lifecycle record: correlation survives even when prompt and
        # output retention are disabled (bug report root cause 2).
        record_id = getattr(args, "run_id", None)
        if not record_id:
            return None
        if not RUN_ID_RE.fullmatch(str(record_id)):
            raise AntiError("run id must contain only letters, numbers, '_' or '-'")
        if runs_dir.is_symlink():
            raise AntiError(f"refusing to write Anti run record through symlinked directory: {runs_dir}")
        os.makedirs(runs_dir, mode=0o700, exist_ok=True)
        try:
            os.chmod(runs_dir, 0o700)
        except OSError:
            pass
        commands = {"consult", "review", "plan", "panel", "moa", "fusion", "workflow", "compare"}
        statuses = {"running", "success", "partial", "error", "interrupted", "failed"}
        command = getattr(args, "command", mode)
        record: dict[str, Any] = {
            "id": str(record_id),
            "created_at": timestamp,
            "command": command if command in commands else "unknown",
            "mode": mode if mode in commands else "unknown",
            "status": status if status in statuses else "unknown",
            "save_output": output_mode,
            "runStatus": "failed" if status == "error" else status if status in statuses else "unknown",
            "metadata": {
                **lifecycle_metadata(metadata),
                "request_log_correlation_id": str(record_id),
            },
        }
        if error:
            record["error"] = "interrupted" if status == "interrupted" else "run_failed"
        record = sanitize_json(record)
        record["id"] = str(record_id)
        record["metadata"]["request_log_correlation_id"] = str(record_id)
        record_path = runs_dir / f"{record['id']}.json"
        if record_path.exists() and record_path.is_symlink():
            raise AntiError(f"refusing to overwrite symlinked run record: {record_path}")
        record["writerId"] = args._anti_writer_id
        record["recordSchemaVersion"] = RECORD_SCHEMA_VERSION
        if policy_audit is not None:
            record["metadata"]["dataPolicy"] = policy_audit
        validate_record(record, record_path)
        write_json(record_path, record)
        args.run_record_written = status != "running"
        return record_path

    if runs_dir.is_symlink():
        raise AntiError(f"refusing to write Anti run record through symlinked directory: {runs_dir}")
    os.makedirs(runs_dir, mode=0o700, exist_ok=True)
    try:
        os.chmod(runs_dir, 0o700)
    except OSError:
        pass

    output_chars = len(output_text or "")
    prompt_chars = len(prompt_text or "")
    if not RUN_ID_RE.fullmatch(str(record_id)):
        raise AntiError("run id must contain only letters, numbers, '_' or '-'")

    record: dict[str, Any] = {
        "id": str(record_id),
        "created_at": timestamp,
        "command": getattr(args, "command", mode),
        "workflow": getattr(args, "workflow_name", None),
        "run_label": getattr(args, "run_label", None),
        "mode": mode,
        "status": status,
        "gateway": base_url,
        "models": models or [],
        "prompt_chars": prompt_chars,
        "output_chars": output_chars,
        "caveats": caveats or [],
        "metadata": metadata or {},
        "save_output": output_mode,
        "helper": helper,
    }
    # B7: split run lifecycle from scope coverage so consumers never confuse
    # "the command ran" with "the requested scope was fully reviewed".
    record["runStatus"] = "failed" if status == "error" else status
    scope_status: str | None = None
    if isinstance(metadata, dict):
        # Panel ``status`` is an integrity result (for example
        # ``degraded_single_model``), while review/plan ``scope_status`` keeps
        # the older complete/incomplete coverage contract for run records.
        metadata_status = (
            metadata.get("scope_status")
            or metadata.get("scopeStatus")
            or metadata.get("status")
        )
        if metadata_status in {"incomplete", "partial"}:
            scope_status = "partial"
        elif metadata_status == "complete":
            scope_status = "complete"
        omitted_items = metadata.get("omitted_files") or metadata.get("chunk_omitted_items") or []
        manifest_file_count = metadata.get("omitted_file_count")
        record["omittedFileCount"] = int(
            manifest_file_count if manifest_file_count is not None else len(omitted_items)
        )
        record["omittedChunkCount"] = int(metadata.get("omitted_chunk_count") or 0)
    record["scopeStatus"] = scope_status or ("complete" if status == "success" else "partial")
    if error:
        record["error"] = error
    if output_mode == "summary" and output_text:
        record["output_preview"] = redact_sensitive_text(output_text)[:output_preview_chars]
    elif output_mode == "full":
        if prompt_text is not None:
            record["prompt_sha256"] = hashlib.sha256(prompt_text.encode("utf-8")).hexdigest()
            record["prompt_chars"] = len(prompt_text)
        if output_text is not None:
            record["output_text"] = output_text
        if execution_ledger is not None:
            record["execution_ledger"] = execution_ledger

    if output_mode == "summary":
        # Tool check descriptors are typed records, not strings to clip midway.
        # Summary retains their counts; live JSON and full mode retain details.
        summary_metadata = dict(record["metadata"])
        verification = summary_metadata.get("verification")
        if isinstance(verification, dict) and "checks" in verification:
            summary_metadata["verification"] = {key: value for key, value in verification.items() if key != "checks"}
            summary_metadata["verification"]["checksRetained"] = False
        contract = summary_metadata.get("findings")
        if isinstance(contract, dict) and isinstance(contract.get("findings"), list):
            contract = dict(contract)
            contract["findings"] = [
                {key: value for key, value in finding.items() if key != "checks"}
                if isinstance(finding, dict) else finding for finding in contract["findings"]
            ]
            summary_metadata["findings"] = contract
        record["metadata"] = summary_metadata
    # Summary projection already bounds and redacts content. Apply it before
    # the shared redactor's whole-object budget, preserving structural counts.
    if output_mode != "summary":
        record = sanitize_json(record)
        if not isinstance(record, dict):
            raise AntiError("Full run record exceeds the structured redaction limit")
    # The record id is generated by us or validated by RUN_ID_RE; never let
    # value redaction mangle it (e.g. a run id shaped like user_12345678).
    record["id"] = str(record_id)
    if record.get("metadata", {}).get("request_log_correlation_id") is not None:
        record["metadata"]["request_log_correlation_id"] = str(record_id)
    run_record_path = runs_dir / f"{record['id']}.json"
    run_dir = runs_dir / str(record_id)
    revisions_dir = run_dir / "revisions"
    for directory in (run_dir, revisions_dir):
        if directory.is_symlink():
            raise AntiError("refusing to publish artifacts through a symlink")
        directory.mkdir(mode=0o700, exist_ok=True)
    revision_id = uuid.uuid4().hex
    artifact_dir = revisions_dir / revision_id
    artifact_dir.mkdir(mode=0o700)  # Never overwrite an existing revision.
    artifact_path = artifact_dir / "result.json"
    raw_lane_paths: list[str] = []
    if output_mode == "full" and execution_ledger:
        for index, entry in enumerate(execution_ledger, start=1):
            lane_path = artifact_dir / f"lane-{index:04d}.json"
            lane = sanitize_json(entry)
            if not isinstance(lane, dict):
                raise AntiError("Raw lane exceeds the structured redaction limit")
            lane.update({"laneSchemaVersion": LANE_SCHEMA_VERSION, "runId": str(record_id), "revisionId": revision_id})
            write_json(lane_path, lane)
            raw_lane_paths.append(str(lane_path))
    artifact_scope_status = record.get("scopeStatus") or ("complete" if status == "success" else "partial")
    artifact_metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
    artifact_run_status = "failed" if status == "error" else status
    finding_contract = artifact_metadata.get("findings")
    if not isinstance(finding_contract, dict):
        finding_contract = {}
    artifact = {
            "schemaVersion": SAVED_RESULT_SCHEMA_VERSION,
            "runId": str(record_id),
            "createdAt": record["created_at"],
            "sourceCommit": artifact_metadata.get("sourceCommit") or artifact_metadata.get("source_commit"),
            "helper": record.get("helper"),
            "mode": mode,
            "runStatus": artifact_run_status,
            "scopeStatus": artifact_scope_status,
            "panelStatus": artifact_metadata.get("panel_status") or artifact_metadata.get("panelStatus"),
            "coverage": coverage_summary(artifact_metadata),
            "requestedModels": artifact_metadata.get("requested_models") or record.get("models", []),
            "actualModels": artifact_metadata.get("actual_models", []),
            "actualProviders": artifact_metadata.get("actual_providers", []),
            "lanes": artifact_metadata.get("panel_results", []),
            "findings": finding_contract.get("findings", []),
            "disagreements": finding_contract.get("disagreements", []),
            "unverifiable": finding_contract.get("unverifiable", []),
            "recommendedNextActions": finding_contract.get("recommended_next_actions", []),
            "summary": finding_contract.get("summary"),
            "output_text": output_text,
            "failureDiagnostics": artifact_metadata.get("failure_diagnostics", []),
            "verification": artifact_metadata.get(
                "verification",
                {
                    "status": "not_run",
                    "requiredChecks": verification_required_checks,
                    "performedBy": None,
                    "evidence": [],
                },
            ),
            "caveats": record.get("caveats", []),
            "error": record.get("error"),
            "artifacts": {
                "runRecordPath": str(run_record_path),
                "resultPath": str(artifact_path),
                "rawLanePaths": raw_lane_paths,
            },
            "resultPath": str(artifact_path),
    }
    media_receipt = media_projection(artifact_metadata.get('media_coverage'), hashes=True)
    if media_receipt is not None:
        artifact['media_coverage'] = media_receipt
    if output_mode != "summary":
        artifact = sanitize_json(artifact)
        if not isinstance(artifact, dict):
            raise AntiError("Full result exceeds the structured redaction limit")

    if coverage_has_loss(artifact["coverage"]):
        artifact["coverage"]["status"] = "partial"
    if artifact["coverage"]["status"] == "partial" or record.get("omittedFileCount", 0) or record.get("omittedChunkCount", 0):
        record["scopeStatus"] = artifact["scopeStatus"] = "partial"
    if output_mode == "summary":
        artifact.pop("output_text", None)
        artifact["output_preview"] = redact_sensitive_text(output_text or "")[:output_preview_chars]
        artifact["output_chars"] = output_chars
        # Fixed-size structural fields survive exhaustion of the content budget.
        structure = summary_structure(artifact, (
            "schemaVersion", "runId", "createdAt", "mode", "runStatus", "scopeStatus", "panelStatus", "output_chars",
        ))
        coverage = artifact["coverage"]
        coverage_structure = summary_structure(coverage, (
            "status", "chunksExpected", "chunksCompleted", "chunksFailed", "chunksOmitted", "chunksNotSent",
        ))
        verification = artifact.get("verification")
        verification = verification if isinstance(verification, dict) else {"status": "unknown"}
        verification_structure = summary_structure(verification, ("status", "performedBy", "evidenceCount"))
        pointers = artifact["artifacts"]
        # Give verification and the primary answer first access to content space.
        ordered_artifact = {key: artifact[key] for key in ("verification", "output_preview")}
        ordered_artifact.update({key: value for key, value in artifact.items() if key not in structure and key not in {"artifacts", "resultPath"}})
        artifact = summary_projection(ordered_artifact)
        artifact.update(structure)
        artifact["runId"] = str(record_id)
        artifact["coverage"] = {**artifact.get("coverage", {}), **coverage_structure}
        artifact["verification"] = {**artifact.get("verification", {}), **verification_structure}
        artifact["artifacts"] = pointers
        artifact["resultPath"] = str(artifact_path)
        if media_receipt is not None:
            artifact['media_coverage'] = media_receipt
        artifact["retention"] = summary_retention()
        artifact = sanitize_json(artifact)
        # Reserve lifecycle/count fields before metadata consumes the preview budget.
        content_keys = {"metadata", "caveats", "error", "output_preview"}
        ordered = {key: value for key, value in record.items() if key not in content_keys}
        ordered.update({key: record[key] for key in ("output_preview", "error", "caveats", "metadata") if key in record})
        if isinstance(ordered.get("metadata"), dict):
            metadata = ordered["metadata"]
            priority = ("request_log_correlation_id", "runStatus", "scopeStatus", "scope_status", "panel_status", "panel_results", "findings", "failure_diagnostics")
            ordered["metadata"] = {key: metadata[key] for key in priority if key in metadata}
            ordered["metadata"].update(metadata)
        structure = summary_structure(record, (
            "id", "created_at", "command", "mode", "status", "runStatus", "scopeStatus", "save_output",
            "prompt_chars", "output_chars", "omittedFileCount", "omittedChunkCount",
        ))
        essential_metadata = lifecycle_metadata(ordered.get('metadata'))
        if media_receipt is not None:
            essential_metadata['media_coverage'] = media_receipt
        record = summary_projection({key: value for key, value in ordered.items() if key not in structure})
        preview_metadata = record.get('metadata')
        record['metadata'] = {**(preview_metadata if isinstance(preview_metadata,dict) else {}), **essential_metadata}
        record.update(structure)
        record["retention"] = summary_retention()
        record = sanitize_json(record)
        # Preserve validated correlation after projection, as in full mode.
        record["id"] = str(record_id)
        if "metadata" in record:
            record["metadata"]["request_log_correlation_id"] = str(record_id)
    if output_mode == "full":
        artifact["retention"] = record["retention"] = {"mode": "full", "contentComplete": True}
    artifact["writerId"] = args._anti_writer_id
    artifact["runId"] = str(record_id)
    artifact["revisionId"] = revision_id
    artifact.setdefault("lanes", [])
    # Preview clipping must not redact trusted publication identity/path aliases.
    artifact["artifacts"] = {"runRecordPath": str(run_record_path), "resultPath": str(artifact_path), "rawLanePaths": raw_lane_paths}
    artifact["resultPath"] = str(artifact_path)
    write_json(artifact_path, artifact)
    record["resultPath"] = str(artifact_path)
    record["writerId"] = args._anti_writer_id
    record["recordSchemaVersion"] = RECORD_SCHEMA_VERSION
    record["publication"] = {
        "revision": revision_id,
        "result": file_reference(runs_dir, artifact_path),
        "lanes": [file_reference(runs_dir, Path(path)) for path in raw_lane_paths],
    }
    path = run_record_path
    if policy_audit is not None:
        record.setdefault("metadata", {})["dataPolicy"] = policy_audit
    validate_record(record, path)
    fsync_directory(revisions_dir)
    fsync_directory(run_dir)
    # This is the publication commit. Earlier immutable files alone grant no
    # terminal authority; an interrupted replacement leaves the old index valid.
    write_json(path, record)
    args.run_record_written = status != "running"
    return path

def iter_run_records(runs_dir: Path, *, warn) -> list[Path]:
    if runs_dir.is_symlink():
        raise AntiError(f"refusing to read Anti run records through symlinked directory: {runs_dir}")
    if not runs_dir.exists():
        return []
    records: list[Path] = []
    # Newest records first by actual write time; filenames are not
    # trustworthy for ordering because custom run ids are not timestamped.
    for path in sorted(runs_dir.glob("*.json"), key=lambda p: (p.stat().st_mtime, p.name), reverse=True):
        if path.is_symlink() or not path.is_file():
            warn(f"[anti] skipping non-regular run record: {path}")
            continue
        records.append(path)
    return records

def resolve_run_record_path(run_id: str, *, runs_dir: Path) -> Path:
    if not RUN_ID_RE.fullmatch(run_id):
        raise AntiError("run id must contain only letters, numbers, '_' or '-'")
    if not runs_dir.exists():
        raise AntiError(f"run record not found: {run_id}")
    if runs_dir.is_symlink():
        raise AntiError(f"refusing to read Anti run records through symlinked directory: {runs_dir}")

    root = runs_dir.resolve()
    path = runs_dir / f"{run_id}.json"
    if not path.exists():
        matches = list(runs_dir.glob(f"{run_id}*.json"))
        if len(matches) == 1:
            path = matches[0]
    if not path.exists():
        if (runs_dir / run_id).exists():
            raise ArtifactError("incomplete_publication", "Run artifacts exist without a committed index")
        raise AntiError(f"run record not found: {run_id}")
    if path.is_symlink():
        raise ArtifactError("invalid_reference", "Run index is a symlink")

    resolved = path.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise AntiError(f"run record path escaped Anti run directory: {run_id}") from exc
    return path
