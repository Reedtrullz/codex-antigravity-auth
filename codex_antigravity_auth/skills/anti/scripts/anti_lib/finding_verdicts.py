"""Explicit local adjudication, separate from model claims and file checks."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
import time
from typing import Any

from .redaction import sanitize_json

VERDICTS = {"confirmed", "rejected", "unresolved"}
MAX_SOURCE_BYTES = 512 * 1024
MAX_EVIDENCE_CHARS = 4000
HEX_HASH = re.compile(r"^[0-9a-f]{64}$")


def finding_key(finding: dict, index: int) -> str:
    material = json.dumps([index, finding.get("id"), finding.get("fingerprint"),
                           finding.get("file"), finding.get("claim")], sort_keys=True)
    return "finding-" + hashlib.sha256(material.encode()).hexdigest()[:24]


def attach_keys(findings: list[dict]) -> list[dict]:
    result = []
    for index, original in enumerate(findings):
        if not isinstance(original, dict):
            continue
        item = deepcopy(original)
        # Neither models nor upstream payloads may manufacture local verdicts.
        item.pop("adjudication", None)
        item["findingKey"] = finding_key(item, index)
        result.append(item)
    return result


def valid_adjudication(value: Any) -> bool:
    if not isinstance(value, dict) or set(value) != {"status", "author", "evidence", "timestamp", "sourceHash", "sourceFile"}:
        return False
    return (
        isinstance(value["status"], str) and value["status"] in VERDICTS
        and isinstance(value["author"], str) and 0 < len(value["author"]) <= 120
        and isinstance(value["evidence"], str) and 0 < len(value["evidence"]) <= MAX_EVIDENCE_CHARS
        and type(value["timestamp"]) is int and 0 <= value["timestamp"] <= 2**63 - 1
        and isinstance(value["sourceHash"], str) and HEX_HASH.fullmatch(value["sourceHash"]) is not None
        and isinstance(value["sourceFile"], str) and bool(value["sourceFile"])
    )


def local_adjudication(repo: Path, source_file: str, *, status: str, author: str, evidence: str) -> dict:
    if status not in VERDICTS:
        raise ValueError("Finding verdict must be confirmed, rejected or unresolved")
    if not isinstance(author, str) or not author.strip() or len(author) > 120:
        raise ValueError("A local author label of at most 120 characters is required")
    if not isinstance(evidence, str) or not evidence.strip() or len(evidence) > MAX_EVIDENCE_CHARS:
        raise ValueError(f"Local evidence of at most {MAX_EVIDENCE_CHARS} characters is required")
    root = repo.resolve()
    path = (root / source_file).resolve()
    try:
        relative = path.relative_to(root).as_posix()
    except ValueError as exc:
        raise ValueError("Inspected source must be within the selected repository") from exc
    if not path.is_file() or path.stat().st_size > MAX_SOURCE_BYTES:
        raise ValueError("Inspected source must be a regular file no larger than 512 KiB")
    with path.open("rb") as handle:
        data = handle.read(MAX_SOURCE_BYTES + 1)
    if len(data) > MAX_SOURCE_BYTES:
        raise ValueError("Inspected source exceeds the hash limit")
    return sanitize_json({"status": status, "author": author.strip(), "evidence": evidence.strip(),
                          "timestamp": int(time.time()), "sourceHash": hashlib.sha256(data).hexdigest(),
                          "sourceFile": relative})


def verdict(finding: dict) -> str:
    annotation = finding.get("adjudication")
    return annotation["status"] if valid_adjudication(annotation) else "unresolved"


def source_hash(finding: dict) -> str | None:
    annotation = finding.get("adjudication")
    if valid_adjudication(annotation):
        return annotation["sourceHash"]
    checks = finding.get("checks")
    if not isinstance(checks, list):
        return None
    hashes = {row.get("fileHash") for row in checks
              if isinstance(row, dict) and isinstance(row.get("fileHash"), str)
              and HEX_HASH.fullmatch(row["fileHash"])}
    return next(iter(hashes)) if len(hashes) == 1 else None


def cohort_summary(records: list[dict]) -> dict:
    counts = {status: 0 for status in sorted(VERDICTS)}
    groups = {}
    for record in records:
        for index, finding in enumerate(record.get("findings", [])):
            status = verdict(finding)
            counts[status] += 1
            digest = source_hash(finding)
            # Content identity is path-independent, but it is not proof of a
            # rename: identical copies are explicitly in the same content group.
            claim = str(finding.get("claim", "")).strip().casefold()
            identity = ("content:" + hashlib.sha256((digest + "\0" + claim).encode()).hexdigest()
                        if digest else "fingerprint:" + str(finding.get("fingerprint") or finding_key(finding, index)))
            group = groups.setdefault(identity, {"identity": identity, "sourceHash": digest,
                                                 "cohorts": {key: 0 for key in counts}, "occurrences": []})
            group["cohorts"][status] += 1
            group["occurrences"].append({"runId": record.get("run_id"),
                                         "findingKey": finding.get("findingKey") or finding_key(finding, index),
                                         "file": finding.get("file"), "verdict": status,
                                         "modelEvidence": finding.get("evidence"),
                                         "localEvidence": (finding.get("adjudication") or {}).get("evidence")})
    recurring = [group for group in groups.values() if len(group["occurrences"]) > 1]
    return {"verdictCohorts": counts, "recurringFindings": recurring,
            "fileIdentityBasis": "source_content_hash_when_available; equal content does not prove a rename"}
