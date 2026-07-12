from __future__ import annotations

from typing import Any


def chunk_manifest(chunks: list[dict[str, Any]], omitted_items: list[str], *, max_chunks: int) -> dict[str, Any]:
    """Describe only chunks that survived budgeting and cap enforcement."""
    included_items = [str(chunk["label"]) for chunk in chunks]
    included_files: list[str] = []
    for chunk in chunks:
        for path in chunk.get("metadata", {}).get("included_files", []):
            if path not in included_files:
                included_files.append(str(path))
    return {
        "chunk_count": len(chunks),
        "max_chunks": max_chunks,
        "included_items": included_items,
        "included_files": included_files,
        "omitted_items": list(omitted_items),
        "status": "incomplete" if omitted_items else "complete",
    }
