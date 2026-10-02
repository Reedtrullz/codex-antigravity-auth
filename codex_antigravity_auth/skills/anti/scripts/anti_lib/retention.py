"""Persistence-only projections; never change the result printed to a caller."""
from __future__ import annotations

import math
from itertools import islice
from typing import Any

from .data_policy import audit_projection
from .redaction import REDACTION_MARKER, key_looks_secret, redact_sensitive_text

SUMMARY_STRING_CHARS = 1600
SUMMARY_TOTAL_CHARS = 16000
SUMMARY_ITEMS = 40
SUMMARY_DEPTH = 8
SUMMARY_NODES = 800


def summary_projection(value: Any) -> Any:
    """Bound all strings, keys, collections and nesting in one saved payload.

    Redact before clipping so a preview cannot retain a cut credential prefix.
    This is a lossy preview, never a complete result or a verification claim.
    """
    remaining_chars = SUMMARY_TOTAL_CHARS
    remaining_nodes = SUMMARY_NODES

    def preview(text: str, limit: int = SUMMARY_STRING_CHARS) -> str:
        nonlocal remaining_chars
        text = redact_sensitive_text(text)
        result = text[:min(limit, remaining_chars)]
        remaining_chars -= len(result)
        return result

    def visit(item: Any, depth: int) -> Any:
        nonlocal remaining_nodes
        remaining_nodes -= 1
        if remaining_nodes < 0 or depth > SUMMARY_DEPTH:
            return None
        if item is None or isinstance(item, bool):
            return item
        if isinstance(item, str):
            return preview(item)
        if isinstance(item, int):
            return item if abs(item) <= 2**63 - 1 else None
        if isinstance(item, float):
            return item if math.isfinite(item) else None
        if isinstance(item, dict):
            result = {}
            for key, child in islice(item.items(), SUMMARY_ITEMS):
                if remaining_nodes <= 0 or remaining_chars <= 0:
                    break
                key_preview = preview(str(key), 160)
                if key_preview in result:
                    continue  # Never let colliding clipped keys overwrite evidence.
                result[key_preview] = visit(REDACTION_MARKER if key_looks_secret(key) else child, depth + 1)
            return result
        if isinstance(item, (list, tuple)):
            result = []
            for child in islice(item, SUMMARY_ITEMS):
                if remaining_nodes <= 0 or remaining_chars <= 0:
                    break
                result.append(visit(child, depth + 1))
            return result
        return None

    return visit(value, 0)


def summary_structure(value: dict[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    """Reserve caller-allowlisted scalars independently of content previews.

    The fixed field lists bound this envelope; arbitrary nested values cannot
    acquire structural status and bypass the content budget.
    """
    result = {}
    for key in fields:
        if key not in value:
            continue
        item = value[key]
        if isinstance(item, str):
            result[key] = redact_sensitive_text(item)[:160]
        elif item is None or isinstance(item, bool):
            result[key] = item
        elif isinstance(item, int) and abs(item) <= 2**63 - 1:
            result[key] = item
        elif isinstance(item, float) and math.isfinite(item):
            result[key] = item
        else:
            result[key] = None
    return result


def summary_retention() -> dict[str, Any]:
    return {
        "mode": "summary", "contentComplete": False, "budgetScope": "content_preview",
        "maxStringChars": SUMMARY_STRING_CHARS, "maxTotalStringChars": SUMMARY_TOTAL_CHARS,
        "maxItemsPerCollection": SUMMARY_ITEMS, "maxDepth": SUMMARY_DEPTH, "maxNodes": SUMMARY_NODES,
    }


def lifecycle_metadata(metadata: dict[str, Any] | None) -> dict[str, Any]:
    """Allow only known numeric counters and fixed enums in never mode."""
    source = metadata if isinstance(metadata, dict) else {}
    result = {}
    runtime = source.get("run_control")
    if isinstance(runtime, dict):
        projected = {"eventsRetained": False}
        for key in ("attempts_started", "permits_acquired", "permits_released", "deferred_calls", "events_omitted"):
            value = runtime.get(key)
            if type(value) is int and 0 <= value <= 2**63 - 1:
                projected[key] = value
        for key in ("limit_seconds", "elapsed_seconds", "remaining_seconds"):
            value = runtime.get(key)
            if type(value) in (int, float) and 0 <= value <= 2**63 - 1 and math.isfinite(value):
                projected[key] = value
        if runtime.get("scope") == "process_local":
            projected["scope"] = "process_local"
        if type(runtime.get("deadline_exceeded")) is bool:
            projected["deadline_exceeded"] = runtime["deadline_exceeded"]
        result["run_control"] = projected
    policy = audit_projection(source.get("dataPolicy"))
    if policy is not None:
        result["dataPolicy"] = policy
    for key in ("prompt_chars", "output_chars", "omitted_file_count", "omitted_chunk_count", "finding_count", "attempt_count", "panel_lane_count", "judge_attempt_count"):
        value = source.get(key)
        if type(value) is int and 0 <= value <= 2**63 - 1:
            result[key] = value
    for key, choices in {
        "synthesis_status": {"not_sent", "failed", "success", "truncated", "empty", "non_answer"},
        "runStatus": {"running", "success", "partial", "failed", "interrupted"},
        "scope_status": {"complete", "incomplete", "partial"},
        "scopeStatus": {"complete", "incomplete", "partial"},
        "panel_status": {"complete_multi_model", "partial_multi_model", "degraded_single_model", "failed"},
    }.items():
        value = source.get(key)
        if isinstance(value, str) and value in choices:
            result[key] = value
    return result
