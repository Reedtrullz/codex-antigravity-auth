"""Versioned publication validation. Consistency is not finding verification."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import stat
from typing import Any

from .cleanup import RUN_ID_RE
from .persistence import PersistenceError

RECORD_SCHEMA_VERSION = 1
SAVED_RESULT_SCHEMA_VERSION = 2
LANE_SCHEMA_VERSION = 1
MAX_LANES = 10000
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
    try:
        _require(not path.is_symlink() and stat.S_ISREG(path.stat().st_mode), "Artifact must be a regular file", "invalid_reference")
        return path.read_bytes()
    except FileNotFoundError as exc:
        raise ArtifactError("incomplete_publication", "A referenced artifact is missing") from exc
    except OSError as exc:
        raise ArtifactError("unreadable_artifact", "A referenced artifact cannot be read") from exc


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


def _checked_reference(root: Path, reference: Any, run_id: str, expected: str) -> tuple[Path, dict[str, Any]]:
    _require(isinstance(reference, dict), "Publication reference must be an object")
    _require(reference.get("path") == expected, "Publication reference does not match its revision", "invalid_reference")
    digest = reference.get("sha256")
    _require(isinstance(digest, str) and bool(re.fullmatch(r"[0-9a-f]{64}", digest)), "Publication checksum is missing or invalid")
    path = _owned_path(root, reference["path"], run_id, relative=True)
    raw = _bytes(path)
    _require(hashlib.sha256(raw).hexdigest() == digest, "Artifact checksum differs from the committed index", "checksum_mismatch")
    return path, _object(raw)


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
    _require(not current or "lanes" in result, "Result lane collection is missing")
    lanes = result.get("lanes", [])
    _require(isinstance(lanes, list) and all(isinstance(lane, dict) for lane in lanes), "Invalid result lanes")
    for lane in lanes:
        if "status" in lane:
            _require(isinstance(lane["status"], str), "Invalid lane status")
    for key in ("findings", "disagreements", "unverifiable", "recommendedNextActions"):
        if key in result:
            _require(isinstance(result[key], list), f"Invalid {key} collection")


def validate_record(record: dict[str, Any], path: Path) -> dict[str, Any]:
    """Return a read adapter with publicationStatus; never change saved bytes."""
    root = path.parent
    _require(not root.is_symlink(), "Run directory is a symlink", "invalid_reference")
    run_id = record.get("id", path.stem)
    _require(isinstance(run_id, str) and bool(RUN_ID_RE.fullmatch(run_id)) and run_id == path.stem,
             "Index identity does not match its filename", "identity_mismatch")
    models = record.get("models", [])
    _require(models is None or (isinstance(models, list) and all(isinstance(model, str) for model in models)), "Invalid index model list")
    if "recordSchemaVersion" not in record:
        # Legacy records have no publication checksum. Keep their scope/status,
        # never infer completeness from existence or silently upgrade trust.
        if record.get("resultPath"):
            result_path = _owned_path(root, record["resultPath"], run_id, relative=False)
            result = _object(_bytes(result_path))
            _result_shape(result, run_id, record, current=False)
            artifacts = result.get("artifacts", {})
            _require(isinstance(artifacts, dict), "Invalid legacy artifact references")
            lanes = artifacts.get("rawLanePaths", [])
            _require(isinstance(lanes, list), "Invalid legacy lane references")
            for reference in lanes:
                _object(_bytes(_owned_path(root, reference, run_id, relative=False)))
        return {**record, "publicationStatus": "legacy_unverified"}
    _require(type(record["recordSchemaVersion"]) is int and record["recordSchemaVersion"] == RECORD_SCHEMA_VERSION,
             "Unsupported run index schema version", "unsupported_version")
    state = record.get("status")
    _require(isinstance(state, str) and state in STATES, "Invalid index lifecycle")
    _require(record.get("runStatus") == ("failed" if state == "error" else state), "Index lifecycle fields conflict", "conflicting_status")
    owner = record.get("writerId")
    _require(isinstance(owner, str) and bool(re.fullmatch(r"[0-9a-f]{32}", owner)), "Invalid writer identity")
    mode = record.get("save_output")
    _require(mode in ("never", "summary", "full"), "Invalid retention mode")
    if mode == "never":
        _require("publication" not in record and "resultPath" not in record, "Never-mode index must not reference content")
        return {**record, "publicationStatus": "lifecycle_only"}
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
    result_path, result = _checked_reference(root, publication.get("result"), run_id, f"{prefix}/result.json")
    _result_shape(result, run_id, record, current=True)
    _require(result.get("revisionId") == revision and result.get("writerId") == owner, "Result revision/writer differs from its index", "identity_mismatch")
    _require(record.get("resultPath") == str(result_path) and result.get("resultPath") == str(result_path), "Result path aliases disagree", "invalid_reference")
    references = publication.get("lanes")
    _require(isinstance(references, list) and len(references) <= MAX_LANES, "Invalid lane manifest")
    _require(mode == "full" or not references, "Summary mode must not publish raw lanes")
    expected_paths = []
    for index, reference in enumerate(references, start=1):
        lane_path, lane = _checked_reference(root, reference, run_id, f"{prefix}/lane-{index:04d}.json")
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
