from __future__ import annotations

from typing import Any


def _is_part_label(label: str) -> bool:
    """Labels for split diff/file parts are not standalone file paths."""
    return label.startswith("diff part ") or " part " in label


def chunk_manifest(
    chunks: list[dict[str, Any]],
    omitted_items: list[str],
    *,
    max_chunks: int,
    planned_chunk_count: int | None = None,
) -> dict[str, Any]:
    """Describe only chunks that survived budgeting and cap enforcement.

    ``planned_chunk_count`` is how many chunks the scope needed with no cap;
    ``omitted_chunk_count`` is how many of those were dropped by the cap.
    """
    included_items = [str(chunk["label"]) for chunk in chunks]
    included_files: list[str] = []
    for chunk in chunks:
        for path in chunk.get("metadata", {}).get("included_files", []):
            if path not in included_files:
                included_files.append(str(path))
    planned = len(chunks) if planned_chunk_count is None else planned_chunk_count
    return {
        "chunk_count": len(chunks),
        "planned_chunk_count": max(planned, len(chunks)),
        "omitted_chunk_count": max(0, planned - len(chunks)),
        "max_chunks": max_chunks,
        "included_items": included_items,
        "included_files": included_files,
        "omitted_items": list(omitted_items),
        "omitted_file_count": sum(1 for item in omitted_items if not _is_part_label(str(item))),
        "status": "incomplete" if omitted_items else "complete",
    }
