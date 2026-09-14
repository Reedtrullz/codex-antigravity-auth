from __future__ import annotations

import re
from typing import Any


_PART_SUFFIX = re.compile(r" part \d+/\d+$")


def _is_part_label(label: str) -> bool:
    """Labels for split diff/file parts are not standalone file paths."""
    return label.startswith("diff part ") or bool(_PART_SUFFIX.search(label))


def _source_label(label: str) -> str:
    return _PART_SUFFIX.sub("", label)


def _source_bytes(chunk: dict[str, Any], path: str) -> int:
    value = chunk.get("metadata", {}).get("source_bytes", {}).get(path, 0)
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def chunk_manifest(
    chunks: list[dict[str, Any]],
    omitted_items: list[str],
    *,
    max_chunks: int,
    planned_chunk_count: int | None = None,
    planned_chunks: list[dict[str, Any]] | None = None,
    file_records: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Describe only chunks that survived budgeting and cap enforcement.

    ``planned_chunk_count`` is how many chunks the scope needed with no cap;
    ``omitted_chunk_count`` is how many of those were dropped by the cap.
    """
    included_items = [str(chunk["label"]) for chunk in chunks]
    included_files: list[str] = []
    for chunk in chunks:
        for path in chunk.get("metadata", {}).get("included_files", []):
            source_path = _source_label(str(path))
            if source_path not in included_files:
                included_files.append(source_path)
    planned = len(chunks) if planned_chunk_count is None else planned_chunk_count
    planned_items = planned_chunks or chunks
    coverage: list[dict[str, Any]] = []
    for original in file_records or []:
        record = dict(original)
        path = str(record.get("path") or "")
        expected = [
            str(chunk.get("label"))
            for chunk in planned_items
            if path and any(
                _source_label(str(item)) == path
                for item in chunk.get("metadata", {}).get("included_files", [])
            )
        ]
        sent = [
            str(chunk.get("label"))
            for chunk in chunks
            if path and any(
                _source_label(str(item)) == path
                for item in chunk.get("metadata", {}).get("included_files", [])
            )
        ]
        record["chunksExpected"] = len(expected)
        record["chunksSent"] = len(sent)
        expected_chunks = [
            chunk for chunk in planned_items
            if path and any(
                _source_label(str(item)) == path
                for item in chunk.get("metadata", {}).get("included_files", [])
            )
        ]
        sent_chunks = [
            chunk for chunk in chunks
            if path and any(
                _source_label(str(item)) == path
                for item in chunk.get("metadata", {}).get("included_files", [])
            )
        ]
        record["firstChunkId"] = expected_chunks[0].get("id") if expected_chunks else None
        record["lastChunkId"] = expected_chunks[-1].get("id") if expected_chunks else None
        record["sentFirstChunkId"] = sent_chunks[0].get("id") if sent_chunks else None
        record["sentLastChunkId"] = sent_chunks[-1].get("id") if sent_chunks else None
        planned_bytes = sum(_source_bytes(chunk, path) for chunk in expected_chunks)
        if planned_bytes or expected_chunks:
            record["bytesSent"] = sum(_source_bytes(chunk, path) for chunk in sent_chunks)
        if record.get("contentStatus") == "complete" and len(expected) != len(sent):
            record["contentStatus"] = "partial"
            record["reason"] = (
                f"{len(expected) - len(sent)} planned chunk(s) omitted by the review chunk cap"
            )
        coverage.append(record)
    coverage_incomplete = any(
        record.get("contentStatus") in {"truncated", "omitted", "partial", "failed", "error"}
        or int(record.get("chunksExpected") or 0) != int(record.get("chunksSent") or 0)
        for record in coverage
    )
    omitted_files = {
        _source_label(str(item))
        for item in omitted_items
        if not _is_part_label(str(item))
    }
    incomplete_files = {
        str(record.get("path"))
        for record in coverage
        if record.get("contentStatus") in {"omitted", "partial", "truncated", "failed", "error"}
        or int(record.get("chunksExpected") or 0) != int(record.get("chunksSent") or 0)
    }
    return {
        "chunk_count": len(chunks),
        "planned_chunk_count": max(planned, len(chunks)),
        "omitted_chunk_count": max(0, planned - len(chunks)),
        "max_chunks": max_chunks,
        "included_items": included_items,
        "included_files": included_files,
        "omitted_items": list(omitted_items),
        "omitted_file_count": len(omitted_files | incomplete_files),
        "coverage": coverage,
        "status": "incomplete" if omitted_items or coverage_incomplete else "complete",
    }
