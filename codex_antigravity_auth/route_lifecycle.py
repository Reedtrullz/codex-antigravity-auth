"""Shared route terminal normalization and deadline-aware diagnostic writes.

The gateway passes request-local context and the current sink. This module has no
server import; late writes retain the captured sink and authoritative stop state.
"""
from __future__ import annotations

import anyio
from .request_budget import RequestDeadlineExceeded

async def write_route_lifecycle(
    status: str,
    *,
    request_id: str,
    request_run_id: str | None,
    request_started: float,
    upstream_observation: dict,
    budget,
    writer,
    sanitize_error,
    clock,
    model: str = "",
    route: str = "unknown",
    provider: str | None = None,
    family: str | None = None,
    stream: bool = False,
    http_status: int | None = None,
    retry_after_source: str | None = None,
    rotation_attempted: bool = False,
    usage: dict | None = None,
    error_class: str | None = None,
    error: object | None = None,
    terminal_kind: str | None = None,
    terminal_reason: str | None = None,
    attempt_count: int | None = None,
    rotation_count: int | None = None,
    cooldown_scope: str | None = None,
    cooldown_category: str | None = None,
    outcome_category: str | None = None,
    cancelled: bool = False,
    terminal_cleanup: bool = False,
    response_payload: dict | None = None,
) -> None:
    if isinstance(response_payload, dict):
        terminal_kind = response_payload.get("status")
        if not isinstance(terminal_kind, str) or terminal_kind not in {"completed", "incomplete", "failed"}:
            terminal_kind = "failed"
        if usage is None and isinstance(response_payload.get("usage"), dict):
            usage = response_payload["usage"]
        incomplete = response_payload.get("incomplete_details")
        if terminal_kind == "incomplete" and isinstance(incomplete, dict):
            terminal_reason = incomplete.get("reason") or terminal_reason
        failure = response_payload.get("error")
        if terminal_kind == "failed" and isinstance(failure, dict):
            error_class = failure.get("code") or error_class
            error = failure.get("message") or error
    if status == "cancelled" and budget.failure_code and response_payload is None:
        status, terminal_kind, terminal_reason = "failed", "failed", budget.failure_code
        error_class, cancelled = budget.failure_code, False
    abort_record = status == "cancelled" or error_class == "request_deadline_exceeded"
    phase = "started" if status == "stream_started" else "terminal"
    if phase == "terminal":
        cancelled = cancelled or status == "cancelled"
        terminal_kind = terminal_kind or {"success": "completed", "incomplete": "incomplete"}.get(status, "failed")
        status = "cancelled" if cancelled else {"completed": "success", "incomplete": "incomplete"}.get(terminal_kind, "failed")
        terminal_reason = "cancelled" if cancelled else terminal_reason or error_class or terminal_kind
    record = {
        "request_id": request_id,
        "run_id": request_run_id,
        "model": model,
        "route": route,
        "provider": provider,
        "family": family,
        "stream": stream,
        "status": status,
        "lifecycle_phase": phase,
        "upstream_http_status": upstream_observation.get("http_status"),
        "provider_accepted": (200 <= upstream_observation["http_status"] < 300) if upstream_observation.get("http_status") is not None else None,
        "latency_ms": int((clock() - request_started) * 1000),
        "http_status": http_status,
        "retry_after_source": retry_after_source,
        "rotation_attempted": rotation_attempted,
        "usage": usage,
        "error_class": error_class,
        "error": sanitize_error(error) if error is not None else None,
        "terminal_kind": terminal_kind,
        "terminal_reason": terminal_reason,
        "attempt_count": attempt_count,
        "rotation_count": rotation_count,
        "cooldown_scope": cooldown_scope,
        "cooldown_category": cooldown_category,
        "outcome_category": outcome_category,
        "cancelled": cancelled,
    }
    def write_diagnostic():
        writer(record)
        stopped = budget.failure_code or ("cancelled" if budget.cancelled else None)
        if phase == "terminal" and stopped and record.get("terminal_reason") != stopped:
            # A blocked filesystem call cannot safely be killed. Once it
            # returns, append the authoritative stop after its stale row.
            # Capture the writer so a late completion keeps the same sink.
            corrected = {**record, "status": "cancelled" if stopped == "cancelled" else "failed",
                         "terminal_kind": "failed", "terminal_reason": stopped, "error_class": stopped,
                         "cancelled": stopped == "cancelled", "http_status": None if stopped == "cancelled" else 504,
                         "error": "Gateway request stopped before completion."}
            writer(corrected)

    remaining = 2.0 if terminal_cleanup else budget.deadline - clock()
    if remaining <= 0:
        raise RequestDeadlineExceeded()
    try:
        with anyio.fail_after(min(2.0, remaining)):
            await anyio.to_thread.run_sync(
                write_diagnostic,
                abandon_on_cancel=True,
            )
            if phase == "terminal":
                budget.terminal_observed = True
                budget.abort_reported = budget.abort_reported or abort_record
    except TimeoutError as exc:
        if terminal_cleanup:
            return
        if not budget.terminal_observed:
            budget.failure_code = "request_deadline_exceeded"
        raise RequestDeadlineExceeded() from exc
    except Exception:
        return
