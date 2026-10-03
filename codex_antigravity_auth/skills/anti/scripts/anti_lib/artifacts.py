"""Versioned publication validation. Consistency is not finding verification."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
import re
from typing import Any

from .cleanup import RUN_ID_RE
from .persistence import PersistenceError
from .retention import lifecycle_metadata
from .data_policy import audit_projection
from .inventory import read_path

RECORD_SCHEMA_VERSION = 1
SAVED_RESULT_SCHEMA_VERSION = 2
LANE_SCHEMA_VERSION = 1
MAX_LANES = 10000
MAX_ARTIFACT_BYTES = 64 * 1024 * 1024
STATES = {"running", "success", "partial", "error", "failed", "interrupted"}
SCOPES = {"complete", "partial"}
VERIFICATION_STATES = {"not_run", "completed_no_evidence", "tool_checks", "unknown"}


class ArtifactError(PersistenceError):
    def __init__(self, code: str, detail: str):
        self.code = code
        super().__init__(f"{code}: {detail}; preserve the saved run for inspection")


def _require(condition: bool, detail: str, code: str = "invalid_artifact") -> None:
    if not condition:
        raise ArtifactError(code, detail)


def _object(raw: bytes) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise ArtifactError("invalid_json", "Saved artifact is not valid JSON") from exc
    _require(isinstance(value, dict), "Saved artifact must be an object")
    return value


def _bytes(path: Path) -> bytes:
    raw, _size, reason = read_path(path, max_file_bytes=MAX_ARTIFACT_BYTES)
    if reason == "missing":
        raise ArtifactError("incomplete_publication", "A referenced artifact is missing")
    if reason in {"symlink", "special_file"}:
        raise ArtifactError("invalid_reference", "Artifact must be a regular file without symlinks")
    if reason == "file_byte_limit":
        raise ArtifactError("artifact_too_large", "Artifact exceeds its bounded read limit")
    if reason is not None:
        raise ArtifactError("unreadable_artifact", "A referenced artifact cannot be read safely")
    return raw


def _owned_path(root: Path, value: Any, run_id: str, *, relative: bool) -> Path:
    _require(isinstance(value, str) and bool(value), "Artifact path must be a string", "invalid_reference")
    if relative:
        pure = PurePosixPath(value)
        _require(not pure.is_absolute() and "\\" not in value and all(p not in {"", ".", ".."} for p in value.split("/")),
                 "Artifact reference must be a canonical relative path", "invalid_reference")
        parts = pure.parts
    else:
        candidate = Path(value)
        try:
            parts = candidate.relative_to(root).parts if candidate.is_absolute() else candidate.parts
        except ValueError as exc:
            raise ArtifactError("invalid_reference", "Legacy artifact escapes the run store") from exc
    _require(bool(parts) and parts[0] == run_id and all(p not in {".", ".."} for p in parts),
             "Artifact reference escapes its owning run", "invalid_reference")
    path = root
    for part in parts:
        path = path / part
        _require(not path.is_symlink(), "Artifact path contains a symlink", "invalid_reference")
    return path


def file_reference(root: Path, path: Path) -> dict[str, str]:
    return {"path": path.relative_to(root).as_posix(), "sha256": hashlib.sha256(_bytes(path)).hexdigest()}


def _checked_reference(root: Path, reference: Any, run_id: str, expected: str, *, read_bytes=None) -> tuple[Path, dict[str, Any]]:
    _require(isinstance(reference, dict), "Publication reference must be an object")
    _require(reference.get("path") == expected, "Publication reference does not match its revision", "invalid_reference")
    digest = reference.get("sha256")
    _require(isinstance(digest, str) and bool(re.fullmatch(r"[0-9a-f]{64}", digest)), "Publication checksum is missing or invalid")
    path = _owned_path(root, reference["path"], run_id, relative=True)
    raw = (read_bytes or _bytes)(path)
    _require(hashlib.sha256(raw).hexdigest() == digest, "Artifact checksum differs from the committed index", "checksum_mismatch")
    return path, _object(raw)


COVERAGE_LOSS_COUNTS = ("chunksFailed", "chunksOmitted", "chunksNotSent")
COVERAGE_LOSS_LISTS = ("omittedFiles", "truncatedFiles", "partialFiles", "failedFiles")
NEVER_FIELDS = {"recordSchemaVersion", "id", "writerId", "created_at", "command", "mode", "status",
                "save_output", "runStatus", "metadata", "error"}
LIFECYCLE_COMMANDS = {"consult", "review", "plan", "panel", "moa", "fusion", "workflow", "compare", "unknown"}


def coverage_has_loss(coverage: dict[str, Any]) -> bool:
    if any(coverage.get(key, 0) for key in COVERAGE_LOSS_COUNTS):
        return True
    if any(coverage.get(key, []) for key in COVERAGE_LOSS_LISTS):
        return True
    if coverage.get("chunksExpected", 0) > coverage.get("chunksCompleted", 0):
        return True
    for row in coverage.get("files", []):
        if row.get("contentStatus") in ("partial", "truncated", "omitted", "failed", "error"):
            return True
        if row.get("bytesDeclared", 0) > row.get("bytesSent", 0):
            return True
    return any(row.get("status") in ("failed", "error", "not_sent", "partial", "truncated", "omitted", "pending", "running")
               for row in coverage.get("chunks", []))


def _retention_agrees(value: Any, mode: str) -> bool:
    return (isinstance(value, dict) and value.get("mode") == mode
            and value.get("contentComplete") is (mode == "full"))


def _result_shape(result: dict[str, Any], run_id: str, record: dict[str, Any], *, current: bool) -> None:
    version = result.get("schemaVersion")
    _require(type(version) is int and version == (SAVED_RESULT_SCHEMA_VERSION if current else 1),
             "Unsupported saved result schema version", "unsupported_version")
    _require(result.get("runId") == run_id, "Result belongs to another run", "identity_mismatch")
    _require(isinstance(result.get("runStatus"), str) and result["runStatus"] in STATES - {"error"}, "Invalid result lifecycle")
    _require(isinstance(result.get("scopeStatus"), str) and result["scopeStatus"] in SCOPES, "Invalid result scope")
    for key in ("runStatus", "scopeStatus"):
        if record.get(key) is not None:
            _require(result[key] == record[key], f"Index and result disagree on {key}", "conflicting_status")
    coverage = result.get("coverage")
    _require(isinstance(coverage, dict) and coverage.get("status") in ("complete", "partial"), "Invalid coverage record")
    _require(not (coverage["status"] == "partial" and result["scopeStatus"] == "complete"), "Partial coverage cannot have complete scope", "conflicting_status")
    for key in ("chunksExpected", "chunksCompleted", "chunksFailed", "chunksOmitted", "chunksNotSent"):
        if key in coverage:
            _require(type(coverage[key]) is int and coverage[key] >= 0, "Coverage counts must be nonnegative integers")
    for key in COVERAGE_LOSS_LISTS:
        if key in coverage:
            _require(isinstance(coverage[key], list) and all(isinstance(item, str) for item in coverage[key]), "Invalid coverage file list")
    for key in ("files", "chunks"):
        rows = coverage.get(key, [])
        _require(isinstance(rows, list) and all(isinstance(row, dict) for row in rows), "Invalid detailed coverage")
        for row in rows:
            for field in ("contentStatus", "status"):
                if field in row:
                    _require(isinstance(row[field], str), "Invalid detailed coverage status")
            for field in ("bytesDeclared", "bytesSent"):
                if field in row:
                    _require(type(row[field]) is int and row[field] >= 0, "Invalid coverage byte count")
    if coverage_has_loss(coverage):
        _require(coverage["status"] == "partial" and result["scopeStatus"] == "partial", "Coverage loss cannot be labelled complete", "conflicting_status")
    verification = result.get("verification")
    _require(isinstance(verification, dict) and isinstance(verification.get("status"), str)
             and verification["status"] in VERIFICATION_STATES, "Invalid verification state")
    if "performedBy" in verification:
        _require(verification["performedBy"] is None or isinstance(verification["performedBy"], str), "Invalid verification actor")
    if "evidenceCount" in verification:
        _require(type(verification["evidenceCount"]) is int and verification["evidenceCount"] >= 0, "Invalid verification evidence count")
    if "requiredChecks" in verification:
        _require(isinstance(verification["requiredChecks"], list) and all(isinstance(v, str) for v in verification["requiredChecks"]), "Invalid required checks")
    if "evidence" in verification:
        _require(isinstance(verification["evidence"], list), "Invalid verification evidence")
    if "checks" in verification:
        checks = verification["checks"]
        _require(isinstance(checks, list) and all(isinstance(check, dict) for check in checks), "Invalid file check records")
        for check in checks:
            _require(isinstance(check.get("status"), str) and check["status"] in {"passed", "failed", "skipped", "error"}, "Invalid file check status")
            _require(isinstance(check.get("checkId"), str) and bool(re.fullmatch(r"[0-9a-f]{24}", check["checkId"])), "Invalid file check identity")
            _require("file" in check and (check["file"] is None or isinstance(check["file"], str)), "Invalid checked file path")
            _require("fileHash" in check, "Missing checked file hash field")
            _require(check.get("fileHash") is None or (isinstance(check["fileHash"], str) and bool(re.fullmatch(r"[0-9a-f]{64}", check["fileHash"]))), "Invalid checked file hash")
            for field in ("check", "reason", "cwd", "output"):
                _require(isinstance(check.get(field), str), "Invalid file check scalar")
            _require(len(check["output"]) <= 2000, "File check output exceeds preview limit")
            _require(type(check.get("durationMs")) is int and check["durationMs"] >= 0, "Invalid file check duration")
            _require(isinstance(check.get("command"), list) and all(isinstance(arg, str) for arg in check["command"]), "Invalid file check command identity")
            if check["check"] == "eslint":
                identity = check.get("identityContext")
                _require(isinstance(identity, dict) and identity.get("scope") == "invocation"
                         and identity.get("effectiveTool") == "unknown" and identity.get("effectiveConfig") == "unknown"
                         and isinstance(identity.get("observationId"), str) and bool(re.fullmatch(r"[0-9a-f]{32}", identity["observationId"]))
                         and check.get("comparableAcrossRuns") is False, "ESLint identity must disclose its invocation-scoped uncertainty")
    _require(not current or "lanes" in result, "Result lane collection is missing")
    lanes = result.get("lanes", [])
    _require(isinstance(lanes, list) and all(isinstance(lane, dict) for lane in lanes), "Invalid result lanes")
    for lane in lanes:
        if "status" in lane:
            _require(isinstance(lane["status"], str), "Invalid lane status")
    for key in ("findings", "disagreements", "unverifiable", "recommendedNextActions"):
        if key in result:
            _require(isinstance(result[key], list), f"Invalid {key} collection")


def validate_record(record: dict[str, Any], path: Path, *, read_bytes=None) -> dict[str, Any]:
    """Return a read adapter with publicationStatus; never change saved bytes."""
    read_bytes = read_bytes or _bytes
    root = path.parent
    _require(not root.is_symlink(), "Run directory is a symlink", "invalid_reference")
    current = "recordSchemaVersion" in record
    if current:
        _require(type(record["recordSchemaVersion"]) is int and record["recordSchemaVersion"] == RECORD_SCHEMA_VERSION,
                 "Unsupported run index schema version", "unsupported_version")
    run_id = record.get("id") if current else record.get("id", path.stem)
    _require(isinstance(run_id, str) and bool(RUN_ID_RE.fullmatch(run_id)) and run_id == path.stem,
             "Index identity does not match its filename", "identity_mismatch")
    models = record.get("models", [])
    _require(models is None or (isinstance(models, list) and all(isinstance(model, str) for model in models)), "Invalid index model list")
    if not current:
        # Legacy records have no publication checksum. Keep their scope/status,
        # never infer completeness from existence or silently upgrade trust.
        if record.get("resultPath"):
            result_path = _owned_path(root, record["resultPath"], run_id, relative=False)
            result = _object(read_bytes(result_path))
            _result_shape(result, run_id, record, current=False)
            artifacts = result.get("artifacts", {})
            _require(isinstance(artifacts, dict), "Invalid legacy artifact references")
            lanes = artifacts.get("rawLanePaths", [])
            _require(isinstance(lanes, list), "Invalid legacy lane references")
            for reference in lanes:
                _object(read_bytes(_owned_path(root, reference, run_id, relative=False)))
        return {**record, "publicationStatus": "legacy_unverified"}
    state = record.get("status")
    _require(isinstance(state, str) and state in STATES, "Invalid index lifecycle")
    _require(record.get("runStatus") == ("failed" if state == "error" else state), "Index lifecycle fields conflict", "conflicting_status")
    owner = record.get("writerId")
    _require(isinstance(owner, str) and bool(re.fullmatch(r"[0-9a-f]{32}", owner)), "Invalid writer identity")
    mode = record.get("save_output")
    _require(mode in ("never", "summary", "full"), "Invalid retention mode")
    if isinstance(record.get("metadata"), dict) and "dataPolicy" in record["metadata"]:
        _require(audit_projection(record["metadata"]["dataPolicy"]) is not None, "Invalid content-free policy audit")
    if mode == "never":
        _require(set(record) <= NEVER_FIELDS, "Never-mode index contains non-lifecycle fields")
        for key in ("command", "mode"):
            _require(isinstance(record.get(key), str) and record[key] in LIFECYCLE_COMMANDS, "Invalid lifecycle command")
        _require(isinstance(record.get("created_at"), str) and bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", record["created_at"])), "Invalid lifecycle timestamp")
        _require(record.get("error") in (None, "run_failed", "interrupted"), "Never-mode index contains a content-bearing error")
        metadata = record.get("metadata")
        _require(isinstance(metadata, dict) and metadata.get("request_log_correlation_id") == run_id, "Invalid lifecycle correlation")
        permitted = {**lifecycle_metadata(metadata), "request_log_correlation_id": run_id}
        _require(metadata == permitted, "Never-mode metadata contains non-lifecycle fields")
        return {**record, "publicationStatus": "lifecycle_only"}
    _require(_retention_agrees(record.get("retention"), mode), "Index retention declaration disagrees with its mode", "retention_mismatch")
    _require(record.get("scopeStatus") in ("complete", "partial"), "Invalid index scope")
    for key in ("omittedFileCount", "omittedChunkCount"):
        if key in record:
            _require(type(record[key]) is int and record[key] >= 0, "Invalid omitted count")
            _require(not record[key] or record["scopeStatus"] == "partial", "Known omissions cannot have complete scope", "conflicting_status")
    publication = record.get("publication")
    _require(isinstance(publication, dict), "Publication manifest is missing", "incomplete_publication")
    revision = publication.get("revision")
    _require(isinstance(revision, str) and bool(re.fullmatch(r"[0-9a-f]{32}", revision)), "Invalid publication revision")
    prefix = f"{run_id}/revisions/{revision}"
    result_path, result = _checked_reference(root, publication.get("result"), run_id, f"{prefix}/result.json", read_bytes=read_bytes)
    _result_shape(result, run_id, record, current=True)
    _require(_retention_agrees(result.get("retention"), mode), "Index and result retention disagree", "retention_mismatch")
    _require(result.get("revisionId") == revision and result.get("writerId") == owner, "Result revision/writer differs from its index", "identity_mismatch")
    _require(record.get("resultPath") == str(result_path) and result.get("resultPath") == str(result_path), "Result path aliases disagree", "invalid_reference")
    references = publication.get("lanes")
    _require(isinstance(references, list) and len(references) <= MAX_LANES, "Invalid lane manifest")
    _require(mode == "full" or not references, "Summary mode must not publish raw lanes")
    expected_paths = []
    for index, reference in enumerate(references, start=1):
        lane_path, lane = _checked_reference(root, reference, run_id, f"{prefix}/lane-{index:04d}.json", read_bytes=read_bytes)
        _require(type(lane.get("laneSchemaVersion")) is int and lane["laneSchemaVersion"] == LANE_SCHEMA_VERSION,
                 "Unsupported lane schema version", "unsupported_version")
        _require(lane.get("runId") == run_id and lane.get("revisionId") == revision, "Lane identity differs from its publication", "identity_mismatch")
        expected_paths.append(str(lane_path))
    artifacts = result.get("artifacts")
    _require(isinstance(artifacts, dict) and artifacts.get("rawLanePaths") == expected_paths
             and artifacts.get("resultPath") == str(result_path) and artifacts.get("runRecordPath") == str(path),
             "Result references disagree with the committed manifest", "invalid_reference")
    if mode == "summary":
        _require(isinstance(result.get("retention"), dict) and result["retention"].get("contentComplete") is False
                 and "output_text" not in result, "Summary artifact must remain an explicit preview")
    return {**record, "publicationStatus": "validated"}


def read_record(path: Path) -> dict[str, Any]:
    _require(not path.parent.is_symlink(), "Run directory is a symlink", "invalid_reference")
    return validate_record(_object(_bytes(path)), path)


def read_publication(path: Path) -> dict[str, Any]:
    """Read a bounded consistent publication for local export; never repair it."""
    from .inventory import path_kind, read_file
    path = Path(path).absolute()
    captured = {}
    total = 0

    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            _require(key not in result, "Duplicate artifact JSON key")
            result[key] = value
        return result

    def read_bytes(selected):
        nonlocal total
        selected = Path(selected).absolute()
        if selected in captured:
            return captured[selected][0]
        _require(len(captured) < 258, "HTML export allows at most256 lane files; use the JSON CLI for larger publications", "export_limit")
        remaining = min(8 * 1024 * 1024, 16 * 1024 * 1024 - total)
        try:
            anchor = Path(selected.anchor)
            relative = selected.relative_to(anchor).as_posix()
            _require(path_kind(anchor, relative) == "file", "HTML input must be a regular file without symlink parents", "invalid_reference")
            raw, _size, reason = read_file(anchor, relative, remaining, max_file_bytes=8 * 1024 * 1024)
            if reason in {"file_byte_limit", "total_byte_limit", "source_byte_limit"}:
                raise ArtifactError("export_limit", "HTML publication exceeds8MiB/file or16MiB total")
            if reason == "changed_during_read":
                raise ArtifactError("changed_during_read", "Artifact changed during read")
            if reason is not None:
                raise ArtifactError("invalid_reference", "HTML input must be a regular file without symlink parents")
            value = json.loads(raw, object_pairs_hook=unique_object,
                               parse_constant=lambda _: _require(False, "Non-finite artifact number"))
            _require(isinstance(value, dict), "Saved artifact must be an object")
            captured[selected] = (raw, value)
            total += len(raw)
            return raw
        except ArtifactError:
            raise
        except (OSError, ValueError, UnicodeError, RecursionError) as exc:
            raise ArtifactError("invalid_artifact", "Cannot read bounded HTML input") from exc

    read_bytes(path)
    try:
        record = validate_record(captured[path][1], path, read_bytes=read_bytes)
    except (TypeError, ValueError, KeyError, RecursionError) as exc:
        raise ArtifactError("invalid_artifact", "Malformed publication fields") from exc
    result = None
    if record.get("resultPath"):
        selected = Path(record["resultPath"])
        selected = selected if selected.is_absolute() else path.parent / selected
        _require(selected in captured, "Result was not in the validated publication", "invalid_reference")
        result = captured[selected][1]
    lanes = []
    if result is not None:
        for reference in result.get("artifacts", {}).get("rawLanePaths", []):
            selected = Path(reference)
            selected = selected if selected.is_absolute() else path.parent / selected
            _require(selected in captured, "Lane was not in the validated publication", "invalid_reference")
            raw, value = captured[selected]
            lanes.append({"sha256":hashlib.sha256(raw).hexdigest(), "value":value})
    return {"record":record, "result":result, "lanes":lanes,
            "indexSha256":hashlib.sha256(captured[path][0]).hexdigest(),
            "filesRead":len(captured), "bytesRead":total}
