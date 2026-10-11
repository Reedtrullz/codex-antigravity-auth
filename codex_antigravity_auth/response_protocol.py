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


POLICY_FINISH_REASONS = frozenset({
    "content_filter", "safety", "recitation", "blocklist", "prohibited_content", "spii",
    "image_safety", "image_prohibited_content", "image_recitation", "escalation",
})
_ERROR_FINISH_REASONS = frozenset({
    "language", "malformed_function_call", "unexpected_tool_call", "too_many_tool_calls",
    "missing_thought_signature", "malformed_response", "no_image", "image_other", "pup_limited_disabled",
})


class PrimaryAlternativeSelector:
    """Select index zero without merging provider alternatives across frames.

    A sole unindexed item is the legacy single-answer format. Once a stream
    contains nonprimary indices, unindexed chunks are ambiguous and rejected.
    """

    def __init__(self) -> None:
        self._unindexed_seen = False
        self._nonprimary_seen = False

    def select(self, alternatives: object) -> list[dict[str, Any]]:
        if not isinstance(alternatives, list) or any(not isinstance(item, dict) for item in alternatives):
            raise ValueError("Provider alternatives must be a list of objects")
        if not alternatives:
            return []
        if any("index" not in item for item in alternatives):
            if len(alternatives) != 1 or self._nonprimary_seen:
                raise ValueError("Unindexed provider alternatives are ambiguous")
            self._unindexed_seen = True
            return alternatives
        indices = [item["index"] for item in alternatives]
        if any(type(index) is not int or index < 0 for index in indices) or indices.count(0) > 1:
            raise ValueError("Provider alternative indices must be nonnegative integers with one primary index")
        nonprimary = any(index != 0 for index in indices)
        if nonprimary and self._unindexed_seen:
            raise ValueError("Mixed indexed and unindexed provider alternatives are ambiguous")
        self._nonprimary_seen = self._nonprimary_seen or nonprimary
        return [item for item in alternatives if item["index"] == 0]


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
    usage: dict[str, Any]
    terminal: ProviderTerminal
    provider_response_id: str | None = None


_OUTCOME_SCOPES = frozenset({"none", "family", "account"})
_OUTCOME_CATEGORIES = frozenset(
    {"success", "rate_limit", "quota", "auth", "invalid_request", "transport", "cancelled"}
)

# Mirrors is_validation_required_error: curable auth blocks ride the normal
# cooldown path and must not escalate the terminal-ban strike counter.
# age_rejection is removed: age ineligibility is a permanent per-account state,
# not a curable block. It disables the account immediately via account_state.
CURABLE_AUTH_ERROR_CLASSES = frozenset({"validation_required"})


@dataclass(frozen=True)
class AttemptOutcome:
    scope: Literal["none", "family", "account"]
    category: Literal[
        "success", "rate_limit", "quota", "auth", "invalid_request", "transport", "cancelled"
    ]
    retry_after_seconds: float | None = None
    curable_auth: bool = False

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
    input_modalities: frozenset[str] = frozenset({"text"})
    image_forms: frozenset[str] = frozenset({"url", "data_url"})
    image_detail: bool = True
    pcm_wav_probe: bool = False
    reasoning_effort_parameter: str | None = None
    reasoning_effort_levels: tuple[str, ...] = ()
    reasoning_replay: bool = True
    opaque_reasoning_replay: bool = False
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
    *, reasoning_tokens: Any = None,
) -> dict[str, Any]:
    normalized_input = _token_count(input_tokens)
    normalized_output = _token_count(output_tokens)
    normalized_total = _token_count(total_tokens)
    if normalized_total <= 0 and (normalized_input or normalized_output):
        normalized_total = normalized_input + normalized_output
    result = {
        "input_tokens": normalized_input,
        "output_tokens": normalized_output,
        "total_tokens": normalized_total,
    }
    if type(reasoning_tokens) is int and reasoning_tokens >= 0:
        result['output_tokens_details'] = {'reasoning_tokens': reasoning_tokens}
    return result


def refusal_item(safety_block: dict[str, Any] | None = None, *, refusal_text: str | None = None) -> dict[str, Any]:
    """Preserve user-facing refusal text; do not expose policy metadata as text."""

    reason = "The provider declined to produce this response."
    if isinstance(safety_block, dict):
        block_reason = safety_block.get("blockReason") or safety_block.get("block_reason")
        if (
            isinstance(block_reason, str)
            and 1 <= len(block_reason) <= 64
            and all(character.isupper() or character.isdigit() or character == "_" for character in block_reason)
        ):
            reason = f"The provider declined this response ({block_reason})."
    if isinstance(refusal_text, str) and refusal_text:
        reason = refusal_text
    return {
        "type": "message",
        "id": f"msg_{uuid.uuid4().hex[:12]}",
        "status": "completed",
        "role": "assistant",
        "content": [{"type": "refusal", "refusal": reason}],
    }


def meaningful_output_items(output: Sequence[dict[str, Any]]) -> tuple[dict[str, Any], ...]:
    """Return supported output items that contain enough data to be actionable."""

    meaningful: list[dict[str, Any]] = []
    for item in output:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type == "function_call":
            if (
                isinstance(item.get("id"), str)
                and item["id"]
                and isinstance(item.get("call_id"), str)
                and item["call_id"]
                and isinstance(item.get("name"), str)
                and item["name"]
                and isinstance(item.get("arguments"), str)
            ):
                meaningful.append(item)
            continue
        if item_type == "reasoning":
            summary = item.get("step_by_step_summary") or item.get("summary")
            if (isinstance(summary, str) and summary) or (isinstance(summary, list) and summary):
                meaningful.append(item)
            continue
        if item_type != "message":
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        if any(
            isinstance(part, dict)
            and (
                (part.get("type") == "output_text" and isinstance(part.get("text"), str) and bool(part["text"]))
                or (part.get("type") == "refusal" and isinstance(part.get("refusal"), str) and bool(part["refusal"]))
            )
            for part in content
        ):
            meaningful.append(item)
    return tuple(meaningful)


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
    if isinstance(finish_reason, str) and not normalized_reason:
        return ProviderTerminal(
            TerminalKind.FAILED, "missing_terminal_signal",
            error_code="missing_terminal_signal",
            error_message="The provider ended without a nonblank finish reason.",
        )
    if normalized_reason in {"max_tokens", "max_output_tokens", "length"}:
        return ProviderTerminal(
            TerminalKind.INCOMPLETE,
            normalized_reason,
            incomplete_reason="max_output_tokens",
        )

    if normalized_reason in _ERROR_FINISH_REASONS:
        return ProviderTerminal(
            TerminalKind.FAILED, normalized_reason,
            error_code=f"provider_finish_{normalized_reason}",
            error_message=f"The provider stopped with {normalized_reason}.",
        )
    if normalized_reason not in {"", "stop", "tool_calls", "function_call"} | POLICY_FINISH_REASONS:
        return ProviderTerminal(
            TerminalKind.FAILED, "unknown_finish_reason",
            error_code="unknown_finish_reason",
            error_message="The provider returned an unsupported finish reason.",
        )

    meaningful = meaningful_output_items(output)
    if safety_block or normalized_reason in POLICY_FINISH_REASONS:
        ordinary_output = any(
            item.get("type") == "function_call" or (
                item.get("type") == "message" and any(
                    isinstance(part, dict) and part.get("type") == "output_text"
                    and isinstance(part.get("text"), str) and bool(part["text"])
                    for part in item.get("content", [])
                )
            ) for item in meaningful
        )
        if ordinary_output:
            return ProviderTerminal(
                TerminalKind.INCOMPLETE, normalized_reason or "content_filter", incomplete_reason="content_filter",
            )
        if any(
            item.get("type") == "message" and any(
                isinstance(part, dict) and part.get("type") == "refusal"
                and isinstance(part.get("refusal"), str) and bool(part["refusal"])
                for part in item.get("content", [])
            ) for item in meaningful
        ):
            return ProviderTerminal(TerminalKind.COMPLETED, normalized_reason or "refusal")
        return ProviderTerminal(
            TerminalKind.FAILED,
            "blocked_without_refusal",
            error_code="blocked_without_refusal",
            error_message="The provider blocked the response without a refusal item.",
        )

    if meaningful:
        return ProviderTerminal(TerminalKind.COMPLETED, normalized_reason or "completed")

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


def _advertised_function_names(request: dict[str, Any], *, native=False) -> set[str]:
    names: set[str] = set()
    from .tool_calls import declaration_sources
    tools = [tool for tool, _ in declaration_sources(request, native=native)]
    if len(tools) > 100000:
        raise CapabilityError("tools: declaration limit exceeded")
    pending = list(tools)
    visited = 0
    while pending:
        tool = pending.pop()
        visited += 1
        if visited > 100000:
            raise CapabilityError("tools: declaration limit exceeded")
        if native and isinstance(tool, dict) and tool.get("type") == "namespace":
            children = tool.get("tools", [])
            if isinstance(children, list):
                if len(children) + len(pending) + visited > 100000:
                    raise CapabilityError("tools: declaration limit exceeded")
                pending.extend(children)
            continue
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
    from .input_fidelity import validate_input
    try:
        validate_input(request, capabilities.input_modalities, capabilities.image_forms, image_detail=capabilities.image_detail,
                       native_passthrough=capabilities.native_responses, pcm_wav_probe=capabilities.pcm_wav_probe)
    except ValueError as exc:
        raise CapabilityError(str(exc)) from exc

    if "parallel_tool_calls" in request and not isinstance(request["parallel_tool_calls"], bool):
        raise CapabilityError("parallel_tool_calls must be a boolean")
    if "parallel_tool_calls" in request and not capabilities.parallel_tool_calls:
        raise CapabilityError("parallel_tool_calls is not supported by the selected route")

    tool_choice = request.get("tool_choice")
    if tool_choice is not None:
        native_choice = (capabilities.native_responses and isinstance(tool_choice, dict)
                         and tool_choice.get("type") != "function")
        mode = tool_choice if isinstance(tool_choice, str) else tool_choice.get("type", "function") if isinstance(tool_choice, dict) else "function"
        if not native_choice and mode not in capabilities.tool_choice_modes:
            raise CapabilityError(f"tool_choice mode '{mode}' is not supported by the selected route")
        if mode == "required" and not (_advertised_function_names(request, native=capabilities.native_responses) or (capabilities.native_responses and request.get("tools"))):
            raise CapabilityError("tool_choice 'required' needs at least one advertised function")
        if mode == "function":
            if not isinstance(tool_choice, dict):
                raise CapabilityError("function tool_choice must be an object")
            name = _function_choice_name(tool_choice)
            if not name:
                raise CapabilityError("function tool_choice requires a function name")
            if name not in _advertised_function_names(request, native=capabilities.native_responses):
                raise CapabilityError(f"tool_choice function '{name}' was not advertised")

    if "stop" in request and not capabilities.stop_sequences:
        raise CapabilityError("stop sequences are not supported by the selected route")
    if request.get("reasoning") is not None:
        if not capabilities.reasoning:
            raise CapabilityError("reasoning is not supported by the selected route")
        if isinstance(request["reasoning"], dict) and request["reasoning"].get("effort") is not None and capabilities.reasoning_effort_levels and request["reasoning"]["effort"] not in capabilities.reasoning_effort_levels:
            raise CapabilityError("reasoning.effort is not supported by the selected route")
        if capabilities.reasoning_effort_parameter is not None:
            reasoning = request["reasoning"]
            if not isinstance(reasoning, dict):
                raise CapabilityError("reasoning must be an object")
            if set(reasoning) != {"effort"}:
                raise CapabilityError("reasoning: the selected mapping requires exactly one effort setting")
            if capabilities.reasoning_effort_parameter == "thinking_budget" and isinstance(request.get("max_output_tokens"), int) and request["max_output_tokens"] <= 1024:
                raise CapabilityError("max_output_tokens must exceed 1024 when requesting a thinking budget")
            if "effort" in reasoning and reasoning["effort"] not in capabilities.reasoning_effort_levels:
                raise CapabilityError("reasoning.effort is not supported by the selected provider/model")
    items = request.get("input")
    if isinstance(items, list):
        for index, item in enumerate(items):
            if not isinstance(item, dict) or item.get("type") != "reasoning":
                continue
            if not capabilities.reasoning_replay:
                raise CapabilityError(f"input[{index}]: reasoning replay is not supported by the selected route")
            if not capabilities.opaque_reasoning_replay and any(key in item for key in ("encrypted_content", "reasoning_details")):
                raise CapabilityError(f"input[{index}]: opaque reasoning replay is not supported by the selected route")

    text = request.get("text")
    if (isinstance(text, dict) and isinstance(text.get("format"), dict)
            and text['format'].get('type') != 'text' and not capabilities.structured_output):
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
            reasoning_tokens=(result.usage.get('output_tokens_details') or {}).get('reasoning_tokens'),
        ),
        "status": result.terminal.kind.value,
    }
    if result.terminal.kind is TerminalKind.INCOMPLETE:
        response["incomplete_details"] = {
            "reason": result.terminal.incomplete_reason or result.terminal.reason
        }
    if result.terminal.kind is TerminalKind.FAILED or result.terminal.error_code:
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
        self._text_state: dict[str, Any] | None = None
        self._reasoning_state: dict[str, Any] | None = None
        self._completed_items: dict[int, dict[str, Any]] = {}

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
        self._completed_items[output_index] = dict(stable_item)
        return [
            self._event("response.output_item.added", output_index=output_index, item=dict(stable_item)),
            self._event("response.output_item.done", output_index=output_index, item=dict(stable_item)),
        ]

    def error(self, code: str, message: str) -> dict[str, Any]:
        self._require_output_open()
        return self._event("error", error={"code": code, "message": message})

    def _require_output_open(self) -> None:
        if not self._created:
            raise ProtocolStateError("response.created must be emitted before output")
        if self._terminal:
            raise ProtocolStateError("cannot add output after the terminal event")

    def add_text_delta(self, delta: str) -> list[dict[str, Any]]:
        self._require_output_open()
        if not isinstance(delta, str) or not delta:
            raise ProtocolStateError("text delta must be a non-empty string")
        events: list[dict[str, Any]] = []
        if self._text_state is None:
            self._text_state = {
                "id": f"msg_{uuid.uuid4().hex[:12]}",
                "output_index": self._next_output_index,
                "fragments": [],
                "finished": False,
            }
            self._next_output_index += 1
            item = {
                "type": "message",
                "id": self._text_state["id"],
                "status": "in_progress",
                "role": "assistant",
                "content": [],
            }
            events.extend(
                [
                    self._event(
                        "response.output_item.added",
                        output_index=self._text_state["output_index"],
                        item=item,
                    ),
                    self._event(
                        "response.content_part.added",
                        item_id=self._text_state["id"],
                        output_index=self._text_state["output_index"],
                        content_index=0,
                        part={"type": "output_text", "text": "", "annotations": []},
                    ),
                ]
            )
        if self._text_state["finished"]:
            raise ProtocolStateError("text output has already been finished")
        self._text_state["fragments"].append(delta)
        events.append(
            self._event(
                "response.output_text.delta",
                item_id=self._text_state["id"],
                output_index=self._text_state["output_index"],
                content_index=0,
                delta=delta,
            )
        )
        return events

    def finish_text(self) -> list[dict[str, Any]]:
        self._require_output_open()
        if self._text_state is None or self._text_state["finished"]:
            raise ProtocolStateError("text output is not active")
        self._text_state["finished"] = True
        text = "".join(self._text_state.pop("fragments"))
        part = {"type": "output_text", "text": text, "annotations": []}
        item = {
            "type": "message",
            "id": self._text_state["id"],
            "status": "completed",
            "role": "assistant",
            "content": [part],
        }
        self._completed_items[self._text_state["output_index"]] = dict(item)
        common = {
            "item_id": self._text_state["id"],
            "output_index": self._text_state["output_index"],
            "content_index": 0,
        }
        return [
            self._event("response.output_text.done", **common, text=text),
            self._event("response.content_part.done", **common, part=part),
            self._event(
                "response.output_item.done",
                output_index=self._text_state["output_index"],
                item=item,
            ),
        ]

    def add_reasoning_delta(self, delta: str) -> list[dict[str, Any]]:
        self._require_output_open()
        if not isinstance(delta, str) or not delta:
            raise ProtocolStateError("reasoning delta must be a non-empty string")
        events: list[dict[str, Any]] = []
        if self._reasoning_state is None:
            self._reasoning_state = {
                "id": f"rs_{uuid.uuid4().hex[:12]}",
                "output_index": self._next_output_index,
                "fragments": [],
                "finished": False,
            }
            self._next_output_index += 1
            events.append(
                self._event(
                    "response.output_item.added",
                    output_index=self._reasoning_state["output_index"],
                    item={
                        "type": "reasoning",
                        "id": self._reasoning_state["id"],
                        "step_by_step_summary": "",
                    },
                )
            )
        if self._reasoning_state["finished"]:
            raise ProtocolStateError("reasoning output has already been finished")
        self._reasoning_state["fragments"].append(delta)
        events.append(
            self._event(
                "response.reasoning_text.delta",
                item_id=self._reasoning_state["id"],
                output_index=self._reasoning_state["output_index"],
                delta=delta,
            )
        )
        return events

    def finish_reasoning(self) -> list[dict[str, Any]]:
        self._require_output_open()
        if self._reasoning_state is None or self._reasoning_state["finished"]:
            raise ProtocolStateError("reasoning output is not active")
        self._reasoning_state["finished"] = True
        text = "".join(self._reasoning_state.pop("fragments"))
        item = {
            "type": "reasoning",
            "id": self._reasoning_state["id"],
            "step_by_step_summary": text,
        }
        self._completed_items[self._reasoning_state["output_index"]] = dict(item)
        return [
            self._event(
                "response.reasoning_text.done",
                item_id=self._reasoning_state["id"],
                output_index=self._reasoning_state["output_index"],
                text=text,
            ),
            self._event(
                "response.output_item.done",
                output_index=self._reasoning_state["output_index"],
                item=item,
            ),
        ]

    def add_function_call(
        self,
        name: str,
        arguments: str,
        *,
        call_id: str | None = None,
    ) -> list[dict[str, Any]]:
        self._require_output_open()
        if not isinstance(name, str) or not name:
            raise ProtocolStateError("function call name must be a non-empty string")
        if not isinstance(arguments, str):
            raise ProtocolStateError("function call arguments must be a string")
        stable_call_id = call_id if isinstance(call_id, str) and call_id else f"call_{uuid.uuid4().hex[:12]}"
        item_id = f"fc_{stable_call_id}" if stable_call_id.startswith("call_") else f"fc_{uuid.uuid4().hex[:12]}"
        output_index = self._next_output_index
        self._next_output_index += 1
        added_item = {
            "type": "function_call",
            "id": item_id,
            "call_id": stable_call_id,
            "name": name,
            "arguments": "",
        }
        done_item = {**added_item, "arguments": arguments}
        self._completed_items[output_index] = dict(done_item)
        events = [
            self._event("response.output_item.added", output_index=output_index, item=added_item),
        ]
        if arguments:
            events.append(
                self._event(
                    "response.function_call_arguments.delta",
                    item_id=item_id,
                    output_index=output_index,
                    delta=arguments,
                )
            )
        events.extend(
            [
                self._event(
                    "response.function_call_arguments.done",
                    item_id=item_id,
                    output_index=output_index,
                    name=name,
                    arguments=arguments,
                ),
                self._event("response.output_item.done", output_index=output_index, item=done_item),
            ]
        )
        return events

    def terminal(self, result: ProviderResult) -> dict[str, Any]:
        if not self._created:
            raise ProtocolStateError("response.created must be emitted before the terminal event")
        if self._terminal:
            raise ProtocolStateError("a terminal event has already been emitted")
        self._terminal = True
        if self._completed_items:
            result = ProviderResult(
                output=tuple(
                    self._completed_items[index] for index in sorted(self._completed_items)
                ),
                usage=result.usage,
                terminal=result.terminal,
                provider_response_id=result.provider_response_id,
            )
        return self._event(f"response.{result.terminal.kind.value}", response=self._response(result))

    def done_marker(self) -> str:
        if not self._terminal:
            raise ProtocolStateError("[DONE] requires a terminal event")
        if self._done:
            raise ProtocolStateError("[DONE] has already been emitted")
        self._done = True
        return "[DONE]"
