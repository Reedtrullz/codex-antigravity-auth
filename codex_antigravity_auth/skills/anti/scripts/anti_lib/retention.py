"""Persistence-only projections; never change the result printed to a caller."""
from __future__ import annotations

import math
import re
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


def control_metadata(source: dict[str, Any]) -> dict[str, Any]:
    """Fixed-size content-free counters shared by never and summary receipts."""
    def numbers(value, keys, *, nullable=False):
        result = {}
        if not isinstance(value, dict): return result
        for key in keys:
            item = value.get(key)
            if type(item) is int and 0 <= item <= 2**63 - 1:
                result[key] = item
            elif nullable and key in value and item is None:
                result[key] = None
        return result

    result = {}
    runtime = source.get('run_control')
    if isinstance(runtime, dict):
        projected = numbers(runtime, ('attempts_started','permits_acquired','permits_released','deferred_calls','events_omitted'))
        for key in ('limit_seconds','elapsed_seconds','remaining_seconds'):
            value = runtime.get(key)
            if type(value) in (int,float) and 0 <= value <= 2**63 - 1 and math.isfinite(value):
                projected[key] = value
        if runtime.get('scope') == 'process_local': projected['scope'] = 'process_local'
        if type(runtime.get('deadline_exceeded')) is bool: projected['deadline_exceeded'] = runtime['deadline_exceeded']
        projected['eventsRetained'] = False
        result['run_control'] = projected
    admission = source.get('admission_controls')
    if isinstance(admission, dict):
        projected = numbers(admission, ('refused_attempts','attempts_omitted'))
        for key in ('enabled','assumption_exceeded'):
            if type(admission.get(key)) is bool: projected[key] = admission[key]
        for key in ('token_limit_guarantee','billing_guarantee'):
            if admission.get(key) is False: projected[key] = False
        for key in ('limits','reserved','committed','observed_tokens','missing_usage_attempts'):
            if isinstance(admission.get(key), dict):
                projected[key] = numbers(admission[key], ('calls','input_tokens','output_tokens'), nullable=key=='limits')
        for key in ('currency_budget','currency_reserved','currency_committed_ceiling'):
            value = admission.get(key)
            if value is None and key in admission:
                projected[key] = None
            elif isinstance(value,str) and len(value) <= 32 and re.fullmatch(r'[0-9]{1,12}(?:\.[0-9]{1,9})?(?:E-[1-9])?',value):
                projected[key] = value
        currency = admission.get('currency')
        if currency is None and 'currency' in admission:
            projected['currency'] = None
        elif isinstance(currency,dict):
            quote = {}
            if isinstance(currency.get('currency'),str) and re.fullmatch(r'[A-Z]{3}',currency['currency']):
                quote['currency'] = currency['currency']
            if isinstance(currency.get('sha256'),str) and re.fullmatch(r'[0-9a-f]{64}',currency['sha256']):
                quote['sha256'] = currency['sha256']
            if currency.get('basis') == 'user_declared_complete_attempt_ceiling':
                quote['basis'] = currency['basis']
            if currency.get('provider_price_verified') is False:
                quote['provider_price_verified'] = False
            projected['currency'] = quote
        projected['attemptsRetained'] = False
        result['admission_controls'] = projected
    return result


def lifecycle_metadata(metadata: dict[str, Any] | None) -> dict[str, Any]:
    """Allow only known numeric counters and fixed enums in never mode."""
    source = metadata if isinstance(metadata, dict) else {}
    result = control_metadata(source)
    policy = audit_projection(source.get("dataPolicy"))
    if policy is not None:
        result["dataPolicy"] = policy
    for key in ("prompt_chars", "output_chars", "omitted_file_count", "omitted_chunk_count", "finding_count", "attempt_count", "panel_lane_count", "judge_attempt_count", "completed_chunk_count", "failed_chunk_count", "not_sent_chunk_count"):
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
