"""OpenAI-compatible Chat Completions and Responses translation contracts."""

from __future__ import annotations

from typing import Any
import uuid

from .response_protocol import (
    ProviderCapabilities,
    ProviderResult,
    ProviderTerminal,
    TerminalKind,
    classify_terminal,
    normalize_usage,
    refusal_item,
)
from .transform import function_call_arguments_string, valid_function_name


def _message_output(message: object) -> list[dict[str, Any]]:
    if not isinstance(message, dict):
        return []
    output: list[dict[str, Any]] = []
    reasoning = message.get("reasoning_content")
    if isinstance(reasoning, str) and reasoning:
        output.append(
            {
                "type": "reasoning",
                "id": f"rs_{uuid.uuid4().hex[:8]}",
                "encrypted_content": "",
                "step_by_step_summary": reasoning,
            }
        )
    content = message.get("content")
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        text = "".join(
            part.get("text", "")
            for part in content
            if isinstance(part, dict) and isinstance(part.get("text"), str)
        )
    else:
        text = ""
    if text:
        output.append(
            {
                "type": "message",
                "id": f"msg_{uuid.uuid4().hex[:8]}",
                "status": "completed",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text, "annotations": []}],
            }
        )
    tool_calls = message.get("tool_calls")
    if isinstance(tool_calls, list):
        for tool_call in tool_calls:
            if not isinstance(tool_call, dict):
                continue
            function = tool_call.get("function")
            if not isinstance(function, dict) or not valid_function_name(function.get("name")):
                continue
            provider_id = tool_call.get("id")
            call_id = provider_id if isinstance(provider_id, str) and provider_id else f"call_{uuid.uuid4().hex[:8]}"
            output.append(
                {
                    "type": "function_call",
                    "id": call_id if call_id.startswith("fc_") else f"fc_{uuid.uuid4().hex[:8]}",
                    "call_id": call_id,
                    "name": function["name"],
                    "arguments": function_call_arguments_string(function.get("arguments", "{}")),
                }
            )
    return output


class ChatResponseAccumulator:
    def __init__(self) -> None:
        self._text = ""
        self._reasoning = ""
        self._finish_reason: str | None = None
        self._usage = normalize_usage()
        self._done = False
        self._refusal = False
        self._tool_names: dict[int, str] = {}

    def mark_done(self) -> None:
        self._done = True

    def consume(self, payload: object) -> None:
        if not isinstance(payload, dict):
            return
        usage = payload.get("usage")
        if isinstance(usage, dict):
            self._usage = normalize_usage(
                usage.get("prompt_tokens", usage.get("input_tokens")),
                usage.get("completion_tokens", usage.get("output_tokens")),
                usage.get("total_tokens"),
            )
        choices = payload.get("choices", [])
        if not isinstance(choices, list):
            return
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            finish_reason = choice.get("finish_reason")
            if isinstance(finish_reason, str) and finish_reason:
                self._finish_reason = finish_reason
                if finish_reason == "content_filter":
                    self._refusal = True
            delta = choice.get("delta")
            if not isinstance(delta, dict):
                continue
            content = delta.get("content")
            if isinstance(content, str):
                self._text += content
            reasoning = delta.get("reasoning_content")
            if isinstance(reasoning, str):
                self._reasoning += reasoning
            if isinstance(delta.get("refusal"), str) and delta["refusal"]:
                self._refusal = True
            tool_calls = delta.get("tool_calls")
            if isinstance(tool_calls, list):
                for position, tool_call in enumerate(tool_calls):
                    if not isinstance(tool_call, dict):
                        continue
                    index = tool_call.get("index", position)
                    if isinstance(index, bool) or not isinstance(index, int) or index < 0:
                        continue
                    function = tool_call.get("function")
                    if not isinstance(function, dict):
                        continue
                    name = function.get("name")
                    if isinstance(name, str):
                        self._tool_names[index] = self._tool_names.get(index, "") + name

    def finalize(self) -> ProviderResult:
        output: list[dict[str, Any]] = []
        if self._reasoning:
            output.append(
                {
                    "type": "reasoning",
                    "id": f"rs_{uuid.uuid4().hex[:8]}",
                    "encrypted_content": "",
                    "step_by_step_summary": self._reasoning,
                }
            )
        if self._text:
            output.append(
                {
                    "type": "message",
                    "id": f"msg_{uuid.uuid4().hex[:8]}",
                    "status": "completed",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": self._text, "annotations": []}],
                }
            )
        if self._refusal and not output:
            output.append(refusal_item({"blockReason": "CONTENT_FILTER"}))
        for index in sorted(self._tool_names):
            name = self._tool_names[index]
            if valid_function_name(name):
                output.append(
                    {
                        "type": "function_call",
                        "id": f"fc_{uuid.uuid4().hex[:8]}",
                        "call_id": f"call_{uuid.uuid4().hex[:8]}",
                        "name": name,
                        "arguments": "{}",
                    }
                )
        terminal = classify_terminal(
            output=output,
            finish_reason=self._finish_reason,
            safety_block={"blockReason": "CONTENT_FILTER"} if self._refusal else None,
        )
        if terminal.kind is TerminalKind.COMPLETED and self._finish_reason is None and not self._done:
            terminal = ProviderTerminal(
                TerminalKind.FAILED,
                "missing_terminal_signal",
                error_code="missing_terminal_signal",
                error_message="The provider stream ended without a terminal signal.",
            )
        return ProviderResult(output=tuple(output), usage=self._usage, terminal=terminal)


class OpenAICompatibleTransport:
    def __init__(self, *, timeout: float, capabilities: ProviderCapabilities | None = None) -> None:
        self.timeout = timeout
        self.capabilities = capabilities or ProviderCapabilities(
            native_responses=False,
            parallel_tool_calls=True,
            structured_output=True,
            stop_sequences=True,
            reasoning=True,
            streaming_usage=True,
        )

    def parse_chat_response(self, payload: object) -> ProviderResult:
        if not isinstance(payload, dict):
            payload = {}
        choices = payload.get("choices", [])
        if not isinstance(choices, list):
            choices = []
        output: list[dict[str, Any]] = []
        finish_reason: str | None = None
        refusal = False
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            reason = choice.get("finish_reason")
            if isinstance(reason, str) and reason:
                finish_reason = reason
                refusal = refusal or reason == "content_filter"
            message = choice.get("message")
            if isinstance(message, dict):
                refusal = refusal or bool(message.get("refusal"))
            output.extend(_message_output(message))
        if refusal and not output:
            output.append(refusal_item({"blockReason": "CONTENT_FILTER"}))
        usage = payload.get("usage")
        usage = usage if isinstance(usage, dict) else {}
        normalized_usage = normalize_usage(
            usage.get("prompt_tokens", usage.get("input_tokens")),
            usage.get("completion_tokens", usage.get("output_tokens")),
            usage.get("total_tokens"),
        )
        terminal = classify_terminal(
            output=output,
            finish_reason=finish_reason,
            safety_block={"blockReason": "CONTENT_FILTER"} if refusal else None,
        )
        return ProviderResult(output=tuple(output), usage=normalized_usage, terminal=terminal)

    def validate_native_response(
        self,
        payload: object,
        *,
        display_model: str,
    ) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ValueError("native Responses payload must be an object")
        status = payload.get("status")
        if status is not None and status not in {"completed", "incomplete", "failed"}:
            raise ValueError("native Responses payload has an invalid status")
        output = payload.get("output")
        if not isinstance(output, list):
            raise ValueError("native Responses payload output must be a list")
        response = dict(payload)
        response["model"] = display_model
        if status is None:
            status = "completed" if output else "failed"
            response["status"] = status
        if status == "completed" and not output:
            response["status"] = "failed"
            response["error"] = {
                "code": "empty_response",
                "message": "The provider returned no meaningful output.",
            }
        elif status == "failed":
            error = payload.get("error")
            code = error.get("code") if isinstance(error, dict) else None
            response["error"] = {
                "code": code if isinstance(code, str) and code else "provider_error",
                "message": "The provider request failed.",
            }
        return response
