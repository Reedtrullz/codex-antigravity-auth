"""Provider-neutral Responses API contracts.

This module is intentionally pure.  It owns response state and capability
semantics, but performs no network, account, credential, or filesystem work.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import math
from typing import Any, Literal, Sequence
import uuid


class TerminalKind(str, Enum):
    COMPLETED = "completed"
    INCOMPLETE = "incomplete"
    FAILED = "failed"


@dataclass(frozen=True)
class ProviderTerminal:
    kind: TerminalKind
    reason: str
    incomplete_reason: str | None = None
    error_code: str | None = None
    error_message: str | None = None


@dataclass(frozen=True)
class ProviderResult:
    output: tuple[dict[str, Any], ...]
    usage: dict[str, int]
    terminal: ProviderTerminal
    provider_response_id: str | None = None


_OUTCOME_SCOPES = frozenset({"none", "family", "account"})
_OUTCOME_CATEGORIES = frozenset(
    {"success", "rate_limit", "quota", "auth", "invalid_request", "transport", "cancelled"}
)


@dataclass(frozen=True)
class AttemptOutcome:
    scope: Literal["none", "family", "account"]
    category: Literal[
        "success", "rate_limit", "quota", "auth", "invalid_request", "transport", "cancelled"
    ]
    retry_after_seconds: float | None = None

    def __post_init__(self) -> None:
        if self.scope not in _OUTCOME_SCOPES:
            raise ValueError(f"unsupported attempt scope: {self.scope}")
        if self.category not in _OUTCOME_CATEGORIES:
            raise ValueError(f"unsupported attempt category: {self.category}")
        if self.retry_after_seconds is not None:
            delay = float(self.retry_after_seconds)
            if not math.isfinite(delay) or delay < 0:
                raise ValueError("retry_after_seconds must be a finite non-negative number")


@dataclass(frozen=True)
class ProviderCapabilities:
    native_responses: bool
    parallel_tool_calls: bool
    structured_output: bool
    stop_sequences: bool
    reasoning: bool
    streaming_usage: bool
    tool_choice_modes: frozenset[str] = field(
        default_factory=lambda: frozenset({"auto", "none", "required", "function"})
    )


class CapabilityError(ValueError):
    """The selected route cannot faithfully honor a requested capability."""


class ProtocolStateError(RuntimeError):
    """Response events were requested in an invalid lifecycle order."""


def _token_count(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        count = float(value)
    except (TypeError, ValueError):
        return 0
    if not math.isfinite(count) or count < 0:
        return 0
    return int(count)


def normalize_usage(
    input_tokens: Any = 0,
    output_tokens: Any = 0,
    total_tokens: Any = 0,
) -> dict[str, int]:
    normalized_input = _token_count(input_tokens)
    normalized_output = _token_count(output_tokens)
    normalized_total = _token_count(total_tokens)
    if normalized_total <= 0 and (normalized_input or normalized_output):
        normalized_total = normalized_input + normalized_output
    return {
        "input_tokens": normalized_input,
        "output_tokens": normalized_output,
        "total_tokens": normalized_total,
    }


def refusal_item(safety_block: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build a refusal without exposing provider policy internals."""

    reason = "The provider declined to produce this response."
    if isinstance(safety_block, dict):
        block_reason = safety_block.get("blockReason") or safety_block.get("block_reason")
        if (
            isinstance(block_reason, str)
            and 1 <= len(block_reason) <= 64
            and all(character.isupper() or character.isdigit() or character == "_" for character in block_reason)
        ):
            reason = f"The provider declined this response ({block_reason})."
    return {
        "type": "message",
        "id": f"msg_{uuid.uuid4().hex[:12]}",
        "status": "completed",
        "role": "assistant",
        "content": [{"type": "refusal", "refusal": reason}],
    }


def _has_meaningful_output(output: Sequence[dict[str, Any]]) -> bool:
    for item in output:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type == "function_call" and isinstance(item.get("name"), str) and item["name"]:
            return True
        if item_type == "reasoning":
            summary = item.get("step_by_step_summary") or item.get("summary")
            if isinstance(summary, str) and summary:
                return True
            if isinstance(summary, list) and summary:
                return True
        if item_type != "message":
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "output_text" and isinstance(part.get("text"), str) and part["text"]:
                return True
            if part.get("type") == "refusal" and isinstance(part.get("refusal"), str) and part["refusal"]:
                return True
    return False


def classify_terminal(
    *,
    output: Sequence[dict[str, Any]],
    finish_reason: str | None,
    safety_block: dict[str, Any] | None,
    malformed: bool = False,
) -> ProviderTerminal:
    if malformed:
        return ProviderTerminal(
            TerminalKind.FAILED,
            "malformed_provider_response",
            error_code="malformed_provider_response",
            error_message="The provider returned a malformed response.",
        )

    normalized_reason = finish_reason.strip().lower() if isinstance(finish_reason, str) else ""
    if normalized_reason in {"max_tokens", "max_output_tokens", "length"}:
        return ProviderTerminal(
            TerminalKind.INCOMPLETE,
            normalized_reason,
            incomplete_reason="max_output_tokens",
        )

    if _has_meaningful_output(output):
        return ProviderTerminal(TerminalKind.COMPLETED, normalized_reason or "completed")

    if safety_block:
        return ProviderTerminal(
            TerminalKind.FAILED,
            "blocked_without_refusal",
            error_code="blocked_without_refusal",
            error_message="The provider blocked the response without a refusal item.",
        )

    return ProviderTerminal(
        TerminalKind.FAILED,
        "empty_response",
        error_code="empty_response",
        error_message="The provider returned no meaningful output.",
    )


def _function_choice_name(tool_choice: dict[str, Any]) -> str | None:
    direct = tool_choice.get("name")
    if isinstance(direct, str) and direct:
        return direct
    nested = tool_choice.get("function")
    if isinstance(nested, dict) and isinstance(nested.get("name"), str) and nested["name"]:
        return nested["name"]
    return None


def _advertised_function_names(request: dict[str, Any]) -> set[str]:
    names: set[str] = set()
    tools = request.get("tools")
    if not isinstance(tools, list):
        return names
    for tool in tools:
        if not isinstance(tool, dict) or tool.get("type") != "function":
            continue
        name = tool.get("name")
        if not isinstance(name, str):
            nested = tool.get("function")
            name = nested.get("name") if isinstance(nested, dict) else None
        if isinstance(name, str) and name:
            names.add(name)
    return names


def validate_capabilities(request: dict[str, Any], capabilities: ProviderCapabilities) -> None:
    if "parallel_tool_calls" in request and not capabilities.parallel_tool_calls:
        raise CapabilityError("parallel_tool_calls is not supported by the selected route")

    tool_choice = request.get("tool_choice")
    if tool_choice is not None:
        mode = tool_choice if isinstance(tool_choice, str) else "function"
        if mode not in capabilities.tool_choice_modes:
            raise CapabilityError(f"tool_choice mode '{mode}' is not supported by the selected route")
        if mode == "required" and not _advertised_function_names(request):
            raise CapabilityError("tool_choice 'required' needs at least one advertised function")
        if mode == "function":
            if not isinstance(tool_choice, dict):
                raise CapabilityError("function tool_choice must be an object")
            name = _function_choice_name(tool_choice)
            if not name:
                raise CapabilityError("function tool_choice requires a function name")
            if name not in _advertised_function_names(request):
                raise CapabilityError(f"tool_choice function '{name}' was not advertised")

    if "stop" in request and not capabilities.stop_sequences:
        raise CapabilityError("stop sequences are not supported by the selected route")
    if "reasoning" in request and not capabilities.reasoning:
        raise CapabilityError("reasoning is not supported by the selected route")

    text = request.get("text")
    if isinstance(text, dict) and text.get("format") is not None and not capabilities.structured_output:
        raise CapabilityError("structured output is not supported by the selected route")


def response_from_result(
    result: ProviderResult,
    *,
    response_id: str,
    model: str,
    created_at: int,
) -> dict[str, Any]:
    response: dict[str, Any] = {
        "id": response_id,
        "object": "response",
        "created_at": created_at,
        "model": model,
        "output": list(result.output),
        "usage": normalize_usage(
            result.usage.get("input_tokens"),
            result.usage.get("output_tokens"),
            result.usage.get("total_tokens"),
        ),
        "status": result.terminal.kind.value,
    }
    if result.terminal.kind is TerminalKind.INCOMPLETE:
        response["incomplete_details"] = {
            "reason": result.terminal.incomplete_reason or result.terminal.reason
        }
    if result.terminal.kind is TerminalKind.FAILED:
        response["error"] = {
            "code": result.terminal.error_code or "provider_error",
            "message": result.terminal.error_message or "The provider request failed.",
        }
    return response


class ResponseEventBuilder:
    def __init__(self, *, response_id: str, model: str, created_at: int) -> None:
        self.response_id = response_id
        self.model = model
        self.created_at = created_at
        self._sequence_number = 0
        self._next_output_index = 0
        self._created = False
        self._terminal = False
        self._done = False

    def _event(self, event_type: str, **fields: Any) -> dict[str, Any]:
        event = {"type": event_type, "sequence_number": self._sequence_number, **fields}
        self._sequence_number += 1
        return event

    def _response(self, result: ProviderResult | None = None) -> dict[str, Any]:
        if result is None:
            return {
                "id": self.response_id,
                "object": "response",
                "created_at": self.created_at,
                "model": self.model,
                "output": [],
                "usage": normalize_usage(),
                "status": "in_progress",
            }
        return response_from_result(
            result,
            response_id=self.response_id,
            model=self.model,
            created_at=self.created_at,
        )

    def created(self) -> dict[str, Any]:
        if self._created:
            raise ProtocolStateError("response.created has already been emitted")
        self._created = True
        return self._event("response.created", response=self._response())

    def add_output_item(self, item: dict[str, Any]) -> list[dict[str, Any]]:
        if not self._created:
            raise ProtocolStateError("response.created must be emitted before output")
        if self._terminal:
            raise ProtocolStateError("cannot add output after the terminal event")
        stable_item = dict(item)
        if not isinstance(stable_item.get("id"), str) or not stable_item["id"]:
            stable_item["id"] = f"item_{uuid.uuid4().hex[:12]}"
        output_index = self._next_output_index
        self._next_output_index += 1
        return [
            self._event("response.output_item.added", output_index=output_index, item=dict(stable_item)),
            self._event("response.output_item.done", output_index=output_index, item=dict(stable_item)),
        ]

    def terminal(self, result: ProviderResult) -> dict[str, Any]:
        if not self._created:
            raise ProtocolStateError("response.created must be emitted before the terminal event")
        if self._terminal:
            raise ProtocolStateError("a terminal event has already been emitted")
        self._terminal = True
        return self._event(f"response.{result.terminal.kind.value}", response=self._response(result))

    def done_marker(self) -> str:
        if not self._terminal:
            raise ProtocolStateError("[DONE] requires a terminal event")
        if self._done:
            raise ProtocolStateError("[DONE] has already been emitted")
        self._done = True
        return "[DONE]"
