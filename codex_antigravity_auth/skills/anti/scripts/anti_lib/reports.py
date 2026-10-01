"""Local, redacted review interchange. Exporting never publishes or suppresses."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import html
import json
import os
import tempfile
from pathlib import Path
from urllib.parse import quote

from .finding_verdicts import cohort_summary, finding_key, source_hash, verdict
from .redaction import sanitize_json
from .persistence import fsync_directory

REPORT_SCHEMA = "urn:anti:review-report:1"
SARIF_SCHEMA = "https://docs.oasis-open.org/sarif/sarif/v2.1.0/os/schemas/sarif-schema-2.1.0.json"


def _strings(value):
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


def _location(value, repo: Path) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    path = Path(value)
    if path.is_absolute():
        try:
            path = path.relative_to(repo.resolve())
        except ValueError:
            return None
    if ".." in path.parts or str(path).startswith(("/", "\\")):
        return None
    return path.as_posix()


def _export_value(value, repo: Path):
    """Retain advisory content, redact credentials and remove local root labels."""
    if isinstance(value, dict):
        return {str(key): ("<workspace>" if key in {"cwd", "repo", "workspace_root"}
                           else _location(child, repo) if key in {"file", "sourceFile"}
                           else _export_value(child, repo)) for key, child in value.items()}
    if isinstance(value, list):
        return [_export_value(item, repo) for item in value]
    if isinstance(value, str):
        return value.replace(str(repo.resolve()), "<workspace>")
    return value


def build_report(records: list[dict], repo: Path) -> dict:
    runs = []
    for record in records:
        context = record.get("context") if isinstance(record.get("context"), dict) else {}
        findings = []
        for index, original in enumerate(record.get("findings", [])):
            item = _export_value(deepcopy(original), repo)
            key = original.get("findingKey") or finding_key(original, index)
            annotation = item.pop("adjudication", None)
            findings.append({"findingKey": key, "verdict": verdict(original),
                             "claimVerification": "unverified", "sourceHash": source_hash(original),
                             "advisory": item, "adjudication": annotation})
        scope = context.get("scopeStatus") or context.get("scope_status")
        scope = scope if isinstance(scope, str) and scope in {"complete", "partial"} else "unknown"
        if (context.get("omitted_files") or context.get("omitted_items")
                or any(type(context.get(key)) is int and context[key] > 0
                       for key in ("omitted_file_count", "omitted_chunk_count"))):
            scope = "partial"
        runs.append({
            "runId": record.get("run_id"), "timestamp": record["timestamp"],
            "sourceRecordHash": hashlib.sha256(json.dumps(record, sort_keys=True, allow_nan=False).encode()).hexdigest(),
            "panelStatus": record.get("panel_status") or "unknown",
            "runVerdict": record.get("verdict") or "pending",
            "actualModels": _strings(context.get("actualModels")),
            "actualProviders": _strings(context.get("actualProviders")),
            "requestedModels": _strings(context.get("requestedModels")) or _strings(record.get("models")),
            "judgeActualModel": next((context[key] for key in ("judge_actual_model", "judge_model_used") if isinstance(context.get(key), str)), None),
            "provenanceStatus": "recorded" if _strings(context.get("actualModels")) and _strings(context.get("actualProviders")) else "unknown",
            "scopeStatus": scope, "coverage": _export_value(context, repo),
            "contentComplete": record.get("save_output") == "full" and record.get("findings_count") == len(findings),
            "declaredFindingCount": record.get("findings_count", 0), "retainedFindingCount": len(findings),
            "retention": record.get("save_output") or "legacy_unknown", "findings": findings,
        })
    result = sanitize_json({"$schema": REPORT_SCHEMA, "schemaVersion": 1, "kind": "anti-review-report",
                            "claimVerification": "unverified", "runs": runs,
                            "summary": _export_value(cohort_summary(records), repo)})
    if not isinstance(result, dict):
        raise ValueError("Report exceeds the structured redaction limit; select one --run-id")
    validate_report(result)
    return result


def validate_report(report: dict) -> None:
    if report.get("schemaVersion") != 1 or report.get("kind") != "anti-review-report" or not isinstance(report.get("runs"), list):
        raise ValueError("Invalid review report envelope")
    for run in report["runs"]:
        if (run.get("scopeStatus") not in {"complete", "partial", "unknown"}
                or type(run.get("contentComplete")) is not bool
                or not isinstance(run.get("findings"), list)
                or run.get("retainedFindingCount") != len(run["findings"])):
            raise ValueError("Invalid review report run")
        for finding in run["findings"]:
            if (not isinstance(finding.get("findingKey"), str)
                    or finding.get("verdict") not in {"confirmed", "rejected", "unresolved"}
                    or finding.get("claimVerification") != "unverified"
                    or not isinstance(finding.get("advisory"), dict)):
                raise ValueError("Invalid review report finding")


def to_sarif(report: dict) -> dict:
    validate_report(report)
    runs = []
    for index, source in enumerate(report["runs"]):
        results = []
        for finding in source["findings"]:
            advisory = finding["advisory"]
            result = {
                "ruleId": "anti.advisory", "kind": "review",
                "level": {"critical": "error", "high": "error", "medium": "warning", "low": "note", "info": "note"}.get(advisory.get("severity"), "warning"),
                "message": {"text": str(advisory.get("claim") or "Unverified advisory finding")},
                "partialFingerprints": {"anti/finding/v1": str(advisory.get("fingerprint") or finding["findingKey"])},
                "properties": deepcopy(finding),
            }
            path = advisory.get("file")
            if isinstance(path, str) and path:
                location = {"artifactLocation": {"uri": quote(path, safe="/")}}
                line = advisory.get("line")
                if type(line) is int and line > 0:
                    location["region"] = {"startLine": line}
                result["locations"] = [{"physicalLocation": location}]
            results.append(result)
        runs.append({"tool": {"driver": {"name": "Anti", "rules": [{"id": "anti.advisory", "name": "UnverifiedAdvisory"}]}},
                     "automationDetails": {"id": str(source.get("runId") or f"legacy-{index}")},
                     "results": results, "properties": {key: deepcopy(value) for key, value in source.items() if key != "findings"}})
    return {"$schema": SARIF_SCHEMA, "version": "2.1.0", "runs": runs,
            "properties": {"antiSchemaVersion": 1, "summary": report["summary"], "claimVerification": "unverified"}}


def _markdown(value) -> str:
    text = html.escape(str(value), quote=True)
    for character in ("\\", "`", "*", "_", "[", "]", "#", "|"):
        text = text.replace(character, "\\" + character)
    return text.replace("\r", " ").replace("\n", " / ")


def to_markdown(report: dict) -> str:
    validate_report(report)
    lines = ["# Anti review report", "", "Model claims remain unverified. Local adjudication is recorded separately.", ""]
    for run in report["runs"]:
        lines += [f"## Run {_markdown(run['runId'] or 'legacy')}", "",
                  f"Actual models: {_markdown(', '.join(run['actualModels']) or 'unknown')}. Actual providers: {_markdown(', '.join(run['actualProviders']) or 'unknown')}.",
                  f"Scope: {_markdown(run['scopeStatus'])}. Retained findings: {run['retainedFindingCount']}/{run['declaredFindingCount']}. Complete content retained: {run['contentComplete']}.",
                  "Coverage/provenance: " + _markdown(json.dumps(run["coverage"], sort_keys=True)), ""]
        for finding in run["findings"]:
            advisory = finding["advisory"]
            lines += [f"### {_markdown(finding['findingKey'])}: {_markdown(finding['verdict'])}", "",
                      _markdown(advisory.get("claim") or "Unverified advisory finding"),
                      "Model evidence: " + _markdown(advisory.get("evidence") or "unavailable"),
                      "Advisory/checks: " + _markdown(json.dumps(advisory, sort_keys=True)),
                      "Local adjudication: " + _markdown(json.dumps(finding["adjudication"], sort_keys=True)), ""]
    return "\n".join(lines)


def write_export(destination: Path, text: str) -> None:
    """Publish a complete local file atomically without replacing any path."""
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", prefix=".anti-report-", dir=destination.parent, delete=False) as handle:
            temporary = Path(handle.name)
            os.chmod(temporary, 0o600)
            handle.write(text + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        # Same-directory link creation is atomic and fails if any destination
        # exists, including a symlink. Unsupported filesystems fail explicitly.
        os.link(temporary, destination)
        fsync_directory(destination.parent)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
