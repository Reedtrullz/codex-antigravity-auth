"""OpenAI-compatible Chat Completions and Responses translation contracts."""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
import time
from typing import Any, AsyncIterator
import uuid

import httpx

from .byok import (
    resolve_api_key,
    validate_http_base_url,
    validate_provider_api_key,
    validate_provider_headers,
)

from .response_protocol import (
    ProviderCapabilities,
    ProviderResult,
    ProviderTerminal,
    ResponseEventBuilder,
    TerminalKind,
    classify_terminal,
    meaningful_output_items,
    normalize_usage,
    refusal_item,
)
from .transform import function_call_arguments_string, valid_function_name
from .transform import transform_request_to_chat


class TransportConfigError(ValueError):
    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class PreparedOpenAIRequest:
    payload: dict[str, Any]
    url: str
    headers: dict[str, str]
    timeout: float


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
    def __init__(
        self,
        *,
        timeout: float,
        capabilities: ProviderCapabilities | None = None,
        client_factory: Any = httpx.AsyncClient,
    ) -> None:
        self.timeout = timeout
        self.client_factory = client_factory
        self.capabilities = capabilities or ProviderCapabilities(
            native_responses=False,
            parallel_tool_calls=True,
            structured_output=True,
            stop_sequences=True,
            reasoning=True,
            streaming_usage=True,
        )

    def provider_timeout(self, provider: dict[str, Any]) -> float:
        timeout = provider.get("timeout", self.timeout)
        if (
            not isinstance(timeout, (int, float))
            or isinstance(timeout, bool)
            or not math.isfinite(float(timeout))
            or float(timeout) <= 0
        ):
            raise TransportConfigError(400, f"Provider '{provider['id']}' timeout must be a positive number")
        return float(timeout)

    def _base_url(self, provider: dict[str, Any]) -> str:
        base_url = provider.get("baseUrl", "")
        if not isinstance(base_url, str):
            raise TransportConfigError(400, f"Provider '{provider['id']}' baseUrl must be a string")
        if not base_url.strip():
            raise TransportConfigError(500, f"Provider '{provider['id']}' has no baseUrl configured")
        try:
            return validate_http_base_url(base_url, label=f"Provider '{provider['id']}' baseUrl")
        except ValueError as exc:
            raise TransportConfigError(400, str(exc)) from exc

    def chat_completions_url(self, provider: dict[str, Any]) -> str:
        base_url = self._base_url(provider)
        return base_url if base_url.endswith("/chat/completions") else f"{base_url}/chat/completions"

    def responses_url(self, provider: dict[str, Any]) -> str:
        base_url = self._base_url(provider)
        return base_url if base_url.endswith("/responses") else f"{base_url}/responses"

    def build_headers(self, provider: dict[str, Any]) -> dict[str, str]:
        try:
            api_key = validate_provider_api_key(resolve_api_key(provider))
        except ValueError as exc:
            raise TransportConfigError(400, f"Provider '{provider['id']}' {exc}") from exc
        if not api_key:
            raise TransportConfigError(
                401,
                f"No API key configured for provider '{provider['id']}'. "
                f"Set {provider.get('apiKeyEnv', 'provider API key')} or run provider set.",
            )
        try:
            provider_headers = validate_provider_headers(provider.get("headers", {}) or {})
        except ValueError as exc:
            raise TransportConfigError(400, str(exc)) from exc
        return {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            **(provider_headers or {}),
        }

    def prepare_chat_request(
        self,
        request: dict[str, Any],
        provider: dict[str, Any],
        provider_model: str,
        *,
        stream: bool,
    ) -> PreparedOpenAIRequest:
        try:
            payload = transform_request_to_chat({**request, "stream": stream}, provider_model)
        except ValueError as exc:
            raise TransportConfigError(400, str(exc)) from exc
        payload["stream"] = stream
        return PreparedOpenAIRequest(
            payload=payload,
            url=self.chat_completions_url(provider),
            headers=self.build_headers(provider),
            timeout=self.provider_timeout(provider),
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

    @staticmethod
    def _failed_result(code: str, message: str) -> ProviderResult:
        return ProviderResult(
            output=(),
            usage=normalize_usage(),
            terminal=ProviderTerminal(
                TerminalKind.FAILED,
                code,
                error_code=code,
                error_message=message,
            ),
        )

    async def stream_chat_events(
        self,
        prepared: PreparedOpenAIRequest,
        *,
        response_id: str,
        display_model: str,
    ) -> AsyncIterator[dict[str, Any] | str]:
        """Execute and normalize one Chat Completions SSE request."""

        builder = ResponseEventBuilder(
            response_id=response_id,
            model=display_model,
            created_at=int(time.time()),
        )
        accumulator = ChatResponseAccumulator()
        tool_calls: dict[int, dict[str, str]] = {}
        tool_seen_order: list[int] = []
        text_active = False
        reasoning_active = False
        terminal_emitted = False
        provider_done = False
        yield builder.created()

        async def fail(code: str, message: str) -> AsyncIterator[dict[str, Any] | str]:
            nonlocal terminal_emitted
            if reasoning_active:
                for event in builder.finish_reasoning():
                    yield event
            if text_active:
                for event in builder.finish_text():
                    yield event
            yield builder.error(code, message)
            yield builder.terminal(self._failed_result(code, message))
            terminal_emitted = True
            yield builder.done_marker()

        buffer = ""
        try:
            async with self.client_factory(timeout=prepared.timeout) as client:
                async with client.stream(
                    "POST",
                    prepared.url,
                    json=prepared.payload,
                    headers=prepared.headers,
                ) as response:
                    if response.status_code != 200:
                        async for event in fail(
                            "backend_error",
                            f"Provider returned HTTP {response.status_code}.",
                        ):
                            yield event
                        return
                    async for chunk in response.aiter_text():
                        buffer += chunk
                        while "\n" in buffer:
                            line, buffer = buffer.split("\n", 1)
                            stripped = line.strip()
                            if not stripped or not stripped.startswith("data:"):
                                continue
                            data = stripped[5:].strip()
                            if data == "[DONE]":
                                if provider_done:
                                    async for event in fail(
                                        "duplicate_done",
                                        "The provider emitted [DONE] more than once.",
                                    ):
                                        yield event
                                    return
                                provider_done = True
                                accumulator.mark_done()
                                continue
                            if provider_done:
                                async for event in fail(
                                    "output_after_done",
                                    "The provider emitted output after [DONE].",
                                ):
                                    yield event
                                return
                            try:
                                payload = json.loads(data)
                            except json.JSONDecodeError:
                                async for event in fail(
                                    "invalid_stream_chunk",
                                    "The provider returned malformed stream JSON.",
                                ):
                                    yield event
                                return
                            if not isinstance(payload, dict):
                                continue
                            provider_error = payload.get("error")
                            if isinstance(provider_error, dict):
                                code = provider_error.get("code")
                                async for event in fail(
                                    code if isinstance(code, str) and code else "provider_error",
                                    "The provider stream failed.",
                                ):
                                    yield event
                                return
                            accumulator.consume(payload)
                            choices = payload.get("choices", [])
                            if not isinstance(choices, list):
                                continue
                            for choice in choices:
                                if not isinstance(choice, dict):
                                    continue
                                delta = choice.get("delta")
                                if not isinstance(delta, dict):
                                    continue
                                reasoning = delta.get("reasoning_content")
                                if isinstance(reasoning, str) and reasoning:
                                    reasoning_active = True
                                    for event in builder.add_reasoning_delta(reasoning):
                                        yield event
                                content = delta.get("content")
                                if isinstance(content, str) and content:
                                    text_active = True
                                    for event in builder.add_text_delta(content):
                                        yield event
                                raw_calls = delta.get("tool_calls")
                                if not isinstance(raw_calls, list):
                                    continue
                                for position, raw_call in enumerate(raw_calls):
                                    if not isinstance(raw_call, dict):
                                        continue
                                    raw_index = raw_call.get("index", position)
                                    if isinstance(raw_index, bool):
                                        continue
                                    try:
                                        index = int(raw_index)
                                    except (TypeError, ValueError):
                                        continue
                                    if index < 0:
                                        continue
                                    if index not in tool_calls:
                                        tool_seen_order.append(index)
                                    state = tool_calls.setdefault(
                                        index,
                                        {"call_id": "", "name": "", "arguments": ""},
                                    )
                                    call_id = raw_call.get("id")
                                    if isinstance(call_id, str) and call_id:
                                        state["call_id"] = call_id
                                    function = raw_call.get("function")
                                    if not isinstance(function, dict):
                                        continue
                                    for field in ("name", "arguments"):
                                        fragment = function.get(field)
                                        if isinstance(fragment, str):
                                            state[field] += fragment
            if buffer.strip():
                async for event in fail(
                    "invalid_stream_chunk",
                    "The provider stream ended with an incomplete SSE frame.",
                ):
                    yield event
                return
        except Exception:
            if not terminal_emitted:
                async for event in fail("connection_error", "The provider connection failed."):
                    yield event
            return

        result = accumulator.finalize()
        if reasoning_active:
            for event in builder.finish_reasoning():
                yield event
        if text_active:
            for event in builder.finish_text():
                yield event
        refusal = next(
            (
                item
                for item in result.output
                if item.get("type") == "message"
                and isinstance(item.get("content"), list)
                and item["content"]
                and isinstance(item["content"][0], dict)
                and item["content"][0].get("type") == "refusal"
            ),
            None,
        )
        if refusal is not None:
            for event in builder.add_output_item(refusal):
                yield event
        for index in tool_seen_order:
            state = tool_calls[index]
            if valid_function_name(state["name"]):
                for event in builder.add_function_call(
                    state["name"],
                    function_call_arguments_string(state["arguments"]),
                    call_id=state["call_id"] or None,
                ):
                    yield event
        if result.terminal.kind is TerminalKind.FAILED:
            yield builder.error(
                result.terminal.error_code or "provider_error",
                result.terminal.error_message or "The provider stream failed.",
            )
        yield builder.terminal(result)
        yield builder.done_marker()

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
        meaningful_output = meaningful_output_items(output)
        if status in {"completed", "incomplete"}:
            response["output"] = list(meaningful_output)
        if status == "completed" and not meaningful_output:
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


class NativeResponsesStreamAdapter:
    """Validate a native Responses SSE stream before exposing terminal state."""

    _TERMINAL_TYPES = {"response.completed", "response.incomplete", "response.failed"}

    def __init__(self, *, display_model: str) -> None:
        self.display_model = display_model
        self._buffer = ""
        self._terminal_event: dict[str, Any] | None = None
        self._terminal_emitted = False
        self._provider_done = False
        self._visible_output_started = False
        self._response_id = f"resp_{uuid.uuid4().hex[:12]}"

    @property
    def visible_output_started(self) -> bool:
        return self._visible_output_started

    @property
    def terminal_seen(self) -> bool:
        return self._terminal_event is not None

    def _failure(self, code: str, message: str) -> dict[str, Any]:
        return {
            "type": "response.failed",
            "response": {
                "id": self._response_id,
                "object": "response",
                "status": "failed",
                "model": self.display_model,
                "output": [],
                "error": {"code": code, "message": message},
            },
        }

    def _set_failure(self, code: str, message: str) -> None:
        self._terminal_event = self._failure(code, message)

    def _release_terminal(self) -> list[dict[str, Any]]:
        if self._terminal_event is None or self._terminal_emitted:
            return []
        self._terminal_emitted = True
        return [self._terminal_event]

    def _consume_line(self, line: str) -> list[dict[str, Any]]:
        stripped = line.strip()
        if not stripped or stripped.startswith(":") or not stripped.startswith("data:"):
            return []
        data = stripped[5:].strip()
        if data == "[DONE]":
            if self._provider_done:
                self._set_failure("duplicate_done", "The provider emitted [DONE] more than once.")
            self._provider_done = True
            if self._terminal_event is None:
                self._set_failure(
                    "missing_terminal_signal",
                    "The provider stream ended without a terminal response event.",
                )
            return self._release_terminal()
        if self._provider_done:
            self._set_failure("output_after_done", "The provider emitted output after [DONE].")
            return self._release_terminal()
        try:
            event = json.loads(data)
        except json.JSONDecodeError:
            self._set_failure("invalid_stream_chunk", "The provider returned malformed stream JSON.")
            return []
        if not isinstance(event, dict) or not isinstance(event.get("type"), str):
            self._set_failure("invalid_stream_event", "The provider returned an invalid stream event.")
            return []
        event_type = event["type"]
        if event_type in self._TERMINAL_TYPES:
            if self._terminal_event is not None:
                self._set_failure("duplicate_terminal", "The provider emitted more than one terminal event.")
                return []
            response = event.get("response")
            if not isinstance(response, dict):
                self._set_failure("invalid_terminal_event", "The provider returned an invalid terminal event.")
                return []
            expected_status = event_type.removeprefix("response.")
            provider_status = response.get("status")
            if provider_status is not None and provider_status != expected_status:
                self._set_failure("invalid_terminal_event", "The provider terminal status did not match its event type.")
                return []
            normalized = {**response, "status": expected_status, "model": self.display_model}
            output = response.get("output")
            meaningful = meaningful_output_items(output) if isinstance(output, list) else ()
            if expected_status in {"completed", "incomplete"}:
                if meaningful:
                    normalized["output"] = list(meaningful)
                elif self._visible_output_started:
                    normalized.setdefault("output", [])
                else:
                    self._set_failure("empty_response", "The provider returned no meaningful output.")
                    return []
            if expected_status == "failed":
                error = response.get("error")
                code = error.get("code") if isinstance(error, dict) else None
                normalized["error"] = {
                    "code": code if isinstance(code, str) and code else "provider_error",
                    "message": "The provider request failed.",
                }
            normalized_type = f"response.{expected_status}"
            self._terminal_event = {"type": normalized_type, "response": normalized}
            return []
        if self._terminal_event is not None:
            self._set_failure("output_after_terminal", "The provider emitted output after its terminal event.")
            return []
        if event_type.startswith("response.output") or event_type.startswith("response.reasoning"):
            self._visible_output_started = True
        if isinstance(event.get("response"), dict):
            event = dict(event)
            event["response"] = {**event["response"], "model": self.display_model}
        return [event]

    def consume_bytes(self, chunk: bytes) -> list[dict[str, Any]]:
        if not isinstance(chunk, bytes):
            self._set_failure("invalid_stream_chunk", "The provider returned a non-byte stream chunk.")
            return []
        self._buffer += chunk.decode("utf-8", errors="replace")
        events: list[dict[str, Any]] = []
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            events.extend(self._consume_line(line))
        return events

    def finish(self) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        if self._buffer.strip():
            events.extend(self._consume_line(self._buffer))
        self._buffer = ""
        if self._terminal_event is None:
            self._set_failure(
                "missing_terminal_signal",
                "The provider stream ended without a terminal response event.",
            )
        events.extend(self._release_terminal())
        return events
