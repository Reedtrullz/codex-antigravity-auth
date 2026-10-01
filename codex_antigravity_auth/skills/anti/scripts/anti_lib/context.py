"""Captured-source rendering and shared coverage aggregation, without CLI imports."""
from __future__ import annotations

import hashlib
from pathlib import Path
import re
from typing import Any, Iterable


def ordered_prompt(pieces: Iterable[str | None]) -> str:
    """Join prompt sources in their declared precedence order."""
    return "\n\n".join(piece.strip() for piece in pieces if piece and piece.strip()).strip()


MAX_FILE_BYTES = 180_000

CHUNK_PART_SUFFIX_RE = re.compile(r" part \d+/\d+$")

GIT_DIFF_TRUNCATION_CAVEAT = "Git diff truncated to fit max prompt budget"

def truncate_at_line_boundary(text: str, max_chars: int) -> str:
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    truncated = text[:max_chars]
    newline = truncated.rfind("\n")
    if newline > max_chars // 2:
        return truncated[:newline]
    return truncated

def read_text_file(
    root: Path,
    rel_path: str,
    *,
    truncate: bool = True,
) -> tuple[str, str | None]:
    path = root / rel_path
    if not path.is_file():
        return "", f"{rel_path}: not a regular file"
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return "", f"{rel_path}: {exc}"
    return decode_source_bytes(rel_path, raw, truncate=truncate)

def decode_source_bytes(
    rel_path: str,
    raw: bytes,
    *,
    truncate: bool = True,
) -> tuple[str, str | None]:
    if b"\0" in raw:
        return "", f"{rel_path}: binary file skipped"
    note = None
    if truncate and len(raw) > MAX_FILE_BYTES:
        original_len = len(raw)
        raw = raw[:MAX_FILE_BYTES]
        note = f"{rel_path}: truncated to {MAX_FILE_BYTES} bytes ({original_len} original bytes)"
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        if note and exc.start >= max(0, len(raw) - 4):
            raw = raw[: exc.start]
            text = raw.decode("utf-8")
            note += "; trimmed partial UTF-8 character at truncation boundary"
        else:
            return "", f"{rel_path}: non-UTF-8 file skipped"
    return text, note

def file_coverage_record(
    root: Path,
    rel_path: str,
    text: str,
    note: str | None,
    *,
    source_kind: str = "file",
    raw: bytes | None = None,
) -> dict[str, Any]:
    """Describe the exact source bytes available to the review planner."""
    if raw is None:
        path = root / rel_path
        try:
            raw = path.read_bytes()
        except OSError as exc:
            return {
                "path": rel_path,
                "sha256": None,
                "bytesDeclared": 0,
                "bytesSent": 0,
                "chunksExpected": 0,
                "chunksSent": 0,
                "contentStatus": "omitted",
                "reason": str(exc),
                "sourceKind": source_kind,
            }
    status = "complete"
    if note:
        status = "omitted" if any(word in note.lower() for word in ("binary", "non-utf", "not a regular")) else "truncated"
    return {
        "path": rel_path,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "bytesDeclared": len(raw),
        "bytesSent": len(text.encode("utf-8")),
        "chunksExpected": 0,
        "chunksSent": 0,
        "contentStatus": status,
        "reason": note,
        "sourceKind": source_kind,
    }

def coverage_is_incomplete(records: list[dict[str, Any]] | None) -> bool:
    return any(
        record.get("contentStatus") in {"truncated", "omitted", "partial", "failed", "error"}
        or int(record.get("bytesSent") or 0) < int(record.get("bytesDeclared") or 0)
        for record in (records or [])
    )

def coverage_summary(metadata: dict[str, Any] | None) -> dict[str, Any]:
    """Return one stable coverage shape for stdout and saved result artifacts."""
    metadata = metadata or {}
    records = [
        dict(record)
        for record in (metadata.get("coverage") or [])
        if isinstance(record, dict) and record.get("path")
    ]

    def unique(values: list[Any]) -> list[str]:
        result: list[str] = []
        for value in values:
            text = str(value)
            if text and text not in result:
                result.append(text)
        return result

    def source_path(value: Any) -> str:
        text = str(value)
        text = CHUNK_PART_SUFFIX_RE.sub("", text)
        if " (" in text:
            text = text.split(" (", 1)[0]
        return text

    declared = unique(
        list(metadata.get("declared_files") or [])
        + [record.get("path") for record in records]
    )
    included = unique(
        [source_path(path) for path in (metadata.get("included_files") or [])]
        + [
            record["path"]
            for record in records
            if record.get("contentStatus") == "complete"
            and (
                int(record.get("chunksSent") or 0) > 0
                or int(record.get("bytesDeclared") or 0) == 0
            )
        ]
    )
    omitted: list[str] = []
    truncated: list[str] = []
    partial: list[str] = []
    failed: list[str] = []
    for record in records:
        path = str(record["path"])
        status = str(record.get("contentStatus") or "")
        try:
            byte_incomplete = int(record.get("bytesSent") or 0) < int(record.get("bytesDeclared") or 0)
        except (TypeError, ValueError):
            byte_incomplete = True
        try:
            chunks_completed = int(
                record.get("chunksCompleted", record.get("chunksSent")) or 0
            )
            chunk_incomplete = chunks_completed < int(record.get("chunksExpected") or 0)
        except (TypeError, ValueError):
            chunk_incomplete = True
        if status == "truncated" or (byte_incomplete and status not in {"omitted", "failed", "error"}):
            truncated.append(path)
        if status in {"omitted", "error", "failed"}:
            omitted.append(path)
        if status == "partial" or chunk_incomplete:
            partial.append(path)
        if chunk_incomplete and status not in {"omitted", "failed", "error", "partial"}:
            partial.append(path)
        if status in {"error", "failed"}:
            failed.append(path)
    declared_set = set(declared)
    for item in metadata.get("omitted_files") or metadata.get("chunk_omitted_items") or []:
        path = source_path(item)
        if path in declared_set:
            record = next((item for item in records if item.get("path") == path), None)
            if record and record.get("contentStatus") == "partial":
                partial.append(path)
            else:
                omitted.append(path)

    planned_chunks = metadata.get("planned_chunk_count")
    sent_chunks = metadata.get("completed_chunk_count", metadata.get("chunk_count"))
    if planned_chunks is None:
        planned_chunks = sum(int(record.get("chunksExpected") or 0) for record in records)
    if sent_chunks is None:
        sent_chunks = sum(int(record.get("chunksSent") or 0) for record in records)
    try:
        planned_chunks = max(0, int(planned_chunks or 0))
    except (TypeError, ValueError):
        planned_chunks = 0
    try:
        sent_chunks = max(0, int(sent_chunks or 0))
    except (TypeError, ValueError):
        sent_chunks = 0
    try:
        omitted_chunks = max(0, int(metadata.get("omitted_chunk_count") or planned_chunks - sent_chunks))
    except (TypeError, ValueError):
        omitted_chunks = max(0, planned_chunks - sent_chunks)
    try:
        failed_chunks = max(0, int(metadata.get("failed_chunk_count") or 0))
    except (TypeError, ValueError):
        failed_chunks = 0
    try:
        not_sent_chunks = max(0, int(metadata.get("not_sent_chunk_count") or 0))
    except (TypeError, ValueError):
        not_sent_chunks = 0
    if failed_chunks or not_sent_chunks:
        omitted_chunks = max(0, planned_chunks - sent_chunks - failed_chunks)
    metadata_status = str(
        metadata.get("scope_status")
        or metadata.get("scopeStatus")
        or metadata.get("status")
        or ""
    )
    incomplete = bool(
        omitted
        or truncated
        or partial
        or failed
        or omitted_chunks
        or metadata_status in {"incomplete", "partial"}
        or metadata.get("diff_truncated")
        or metadata.get("assembly_over_budget")
        or bool((metadata.get("run_control") or {}).get("deferred_calls"))
    )
    result = {
        "status": "partial" if incomplete else "complete",
        "declaredFiles": declared,
        "includedFiles": unique(included),
        "omittedFiles": unique(omitted),
        "truncatedFiles": unique(truncated),
        "partialFiles": unique(partial),
        "failedFiles": unique(failed),
        "chunksExpected": planned_chunks,
        "chunksCompleted": min(sent_chunks, planned_chunks) if planned_chunks else sent_chunks,
        "chunksFailed": max(int(metadata.get("failed_chunk_count") or 0), len(failed)),
        "chunksOmitted": omitted_chunks,
        "chunksNotSent": not_sent_chunks or omitted_chunks,
        "chunks": [
            {
                key: item.get(key)
                for key in ("id", "index", "kind", "label", "prompt_chars", "status", "model_used")
                if item.get(key) is not None
            }
            for item in (metadata.get("chunk_prompts") or [])
            if isinstance(item, dict)
        ],
        "files": records,
    }
    return result

def review_prompt_parts(
    *,
    scope_line: str,
    diff: str,
    included_files: list[tuple[str, str]],
    omitted_files: list[str],
    excluded: list[str],
    caveats: list[str],
    omission_reasons: dict[str, str] | None = None,
) -> list[str]:
    incomplete = bool(omitted_files) or any("truncated" in caveat.lower() for caveat in caveats)
    manifest_lines = [
        "## Review Manifest",
        f"- status: {'incomplete' if incomplete else 'complete'}",
        f"- scope: {scope_line}",
        f"- included_files: {', '.join(path for path, _text in included_files) if included_files else 'none'}",
        f"- omitted_files: {', '.join(omitted_files) if omitted_files else 'none'}",
        f"- excluded_paths: {', '.join(excluded[:20]) if excluded else 'none'}",
    ]
    if omission_reasons:
        manifest_lines.append("- omission_reasons:")
        manifest_lines.extend(f"  - {path}: {reason}" for path, reason in omission_reasons.items())
    if caveats:
        manifest_lines.append("- helper_warnings:")
        manifest_lines.extend(f"  - {caveat}" for caveat in caveats)
    else:
        manifest_lines.append("- helper_warnings: none")

    parts = [
        "You are an Antigravity sidecar reviewer for a Codex coding session.",
        "Review independently. Lead with concrete defects, regressions, security risks, install/usability problems, or missing tests. Be concise; no code or patches.",
        "Use file paths and precise behavior references. If no issues, say so and list caveats; group duplicates; do not restate source.",
        "Treat the Review Manifest as authoritative. Helper warnings, omitted files, and partial diffs are scope caveats, not source-code defects.",
        "\n".join(manifest_lines),
    ]
    if diff.strip():
        parts.append("## Git Diff\n```diff\n" + diff + "\n```")
    if included_files:
        blocks = [f"### {rel}\n```text\n{text}\n```" for rel, text in included_files]
        parts.append("## File Contents\n" + "\n\n".join(blocks))
    if not diff.strip() and not included_files:
        parts.append("No diff or file content was available in the requested scope. Explain that limitation.")
    return parts

def review_read_omissions(records):
    return {str(record['path']): str(record.get('reason') or record.get('contentStatus') or 'incomplete capture')
            for record in records or [] if record.get('path') and coverage_is_incomplete([record])}

def build_review_prompt(
    *,
    scope_line: str,
    diff: str,
    file_texts: list[tuple[str, str]],
    excluded: list[str],
    initial_caveats: list[str],
    max_prompt_chars: int,
    file_records: list[dict[str, Any]] | None = None,
) -> tuple[str, list[str], dict[str, Any]]:
    caveats = list(initial_caveats)
    diff_for_prompt = diff
    records_by_path = {
        str(record.get("path")): record
        for record in (file_records or [])
        if record.get("path")
    }

    def is_includable(rel: str, text: str) -> bool:
        # An empty regular file is still covered; without its manifest entry,
        # the historical truthiness check misreported it as omitted.
        record = records_by_path.get(rel)
        return bool(text) or bool(record and record.get("contentStatus") == "complete")

    omission_reasons = review_read_omissions(file_records)
    omitted_files = list(dict.fromkeys([*omission_reasons,
        *(rel for rel, text in file_texts if not is_includable(rel, text))]))
    candidates = [(rel, text) for rel, text in file_texts if is_includable(rel, text)]
    included: list[tuple[str, str]] = []

    if max_prompt_chars > 0 and diff_for_prompt:
        prompt_without_files = "\n\n".join(
            review_prompt_parts(
                scope_line=scope_line,
                diff=diff_for_prompt,
                included_files=[],
                omitted_files=list(dict.fromkeys([*omitted_files, *(rel for rel, _text in candidates)])),
                excluded=excluded,
                caveats=caveats,
                omission_reasons=omission_reasons,
            )
        )
        if len(prompt_without_files) > max_prompt_chars:
            base_parts = review_prompt_parts(
                scope_line=scope_line,
                diff="",
                included_files=[],
                omitted_files=list(dict.fromkeys([*omitted_files, *(rel for rel, _text in candidates)])),
                excluded=excluded,
                caveats=caveats,
                omission_reasons=omission_reasons,
            )
            base_len = len("\n\n".join(base_parts))
            available = max(0, max_prompt_chars - base_len - len("\n\n## Git Diff\n```diff\n\n```"))
            diff_for_prompt = truncate_at_line_boundary(diff_for_prompt, available)
            caveats.append(
                f"{GIT_DIFF_TRUNCATION_CAVEAT} ({len(diff)} original chars, {len(diff_for_prompt)} included)"
            )

    for index, (rel, text) in enumerate(candidates):
        trial_included = [*included, (rel, text)]
        # Remaining source parts are future chunks, not omitted scope. Listing
        # them in every trial manifest can consume the entire prompt budget
        # before any source content is admitted.
        trial_omitted = list(omitted_files)
        trial_prompt = "\n\n".join(
            review_prompt_parts(
                scope_line=scope_line,
                diff=diff_for_prompt,
                included_files=trial_included,
                omitted_files=trial_omitted,
                excluded=excluded,
                caveats=caveats,
                omission_reasons=omission_reasons,
            )
        )
        if max_prompt_chars <= 0 or len(trial_prompt) <= max_prompt_chars:
            included = trial_included
        else:
            omitted_files.append(f"{rel} (omitted to keep whole-file prompt under {max_prompt_chars} chars)")

    prompt = "\n\n".join(
        review_prompt_parts(
            scope_line=scope_line,
            diff=diff_for_prompt,
            included_files=included,
            omitted_files=omitted_files,
            excluded=excluded,
            caveats=caveats,
            omission_reasons=omission_reasons,
        )
    )
    metadata = {
        "status": "incomplete"
        if omitted_files
        or any("truncated" in item.lower() for item in caveats)
        or coverage_is_incomplete(file_records)
        else "complete",
        "prompt_chars": len(prompt),
        "diff_chars": len(diff_for_prompt),
        "diff_original_chars": len(diff),
        "diff_truncated": diff_for_prompt != diff,
        "included_files": [rel for rel, _text in included],
        "omitted_files": omitted_files,
        "omission_reasons": omission_reasons,
        "excluded_paths": excluded,
        "helper_warnings": caveats,
        "coverage": [dict(record) for record in (file_records or [])],
    }
    for record in metadata["coverage"]:
        if record.get("path") in metadata["included_files"]:
            record["chunksExpected"] = 1
            record["chunksSent"] = 1
    return prompt, caveats, metadata
