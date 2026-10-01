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

from .request_budget import owned_context
from .resource_limits import ResourceLimitError, json_loads_limited, read_response_bytes
from .redaction import redact_secret_text
from .native_output import (
    MAX_ITEMS, NativeOutputError, check_json, reconcile_output,
    validate_event, validate_output, validate_response,
)

from .response_protocol import (
    ProviderCapabilities,
    ProviderResult,
    ProviderTerminal,
    ResponseEventBuilder,
    TerminalKind,
    classify_terminal,
    normalize_usage,
    refusal_item,
)
from .transform import function_call_arguments_string, valid_function_name
from .transform import transform_request_to_chat


from .sse import SSEDecoder, SSELineError, SSELimitError, iter_sse_data


def parse_sse_payload(data: str, *, label: str = "provider") -> dict[str, Any]:
    try:
        payload = json_loads_limited(data)
    except ResourceLimitError as exc:
        raise (SSELimitError if exc.status == 413 else SSELineError)(str(exc)) from exc
    except (ValueError, RecursionError) as exc:
        raise SSELineError(f"The {label} stream returned malformed JSON: {exc}") from exc
    if isinstance(payload, list):
        payload = payload[0] if payload else {}
    if not isinstance(payload, dict):
        raise SSELineError(f"The {label} stream returned a non-object JSON value")
    return payload

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
        self._text: list[str] = []
        self._reasoning: list[str] = []
        self._finish_reason: str | None = None
        self._usage = normalize_usage()
        self._done = False
        self._refusal = False
        self._tool_names: dict[int, list[str]] = {}
        self._tool_arguments: dict[int, list[str]] = {}

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
            if isinstance(content, str) and content:
                self._text.append(content)
            reasoning = delta.get("reasoning_content")
            if isinstance(reasoning, str) and reasoning:
                self._reasoning.append(reasoning)
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
                    if isinstance(name, str) and name:
                        self._tool_names.setdefault(index, []).append(name)
                    arguments = function.get("arguments")
                    if isinstance(arguments, str) and arguments:
                        self._tool_arguments.setdefault(index, []).append(arguments)

    def finalize(self) -> ProviderResult:
        output: list[dict[str, Any]] = []
        if self._reasoning:
            output.append(
                {
                    "type": "reasoning",
                    "id": f"rs_{uuid.uuid4().hex[:8]}",
                    "encrypted_content": "",
                    "step_by_step_summary": "".join(self._reasoning),
                }
            )
        if self._text:
            output.append(
                {
                    "type": "message",
                    "id": f"msg_{uuid.uuid4().hex[:8]}",
                    "status": "completed",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "".join(self._text), "annotations": []}],
                }
            )
        if self._refusal and not output:
            output.append(refusal_item({"blockReason": "CONTENT_FILTER"}))
        for index in sorted(self._tool_names):
            name = "".join(self._tool_names[index])
            if valid_function_name(name):
                arguments = "".join(self._tool_arguments.get(index, []))
                output.append(
                    {
                        "type": "function_call",
                        "id": f"fc_{uuid.uuid4().hex[:8]}",
                        "call_id": f"call_{uuid.uuid4().hex[:8]}",
                        "name": name,
                        "arguments": arguments if arguments else "{}",
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
        telemetry: dict[str, Any] | None = None,
    ) -> AsyncIterator[dict[str, Any] | str]:
        """Execute and normalize one Chat Completions SSE request."""

        builder = ResponseEventBuilder(
            response_id=response_id,
            model=display_model,
            created_at=int(time.time()),
        )
        accumulator = ChatResponseAccumulator()
        tool_calls: dict[int, dict[str, Any]] = {}
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

        try:
            async with owned_context(self.client_factory(timeout=prepared.timeout)) as client:
                async with owned_context(client.stream(
                    "POST",
                    prepared.url,
                    json=prepared.payload,
                    headers=prepared.headers,
                )) as response:
                    if telemetry is not None:
                        telemetry["http_status"] = response.status_code
                    if response.status_code != 200:
                        detail = f"Provider returned HTTP {response.status_code}."
                        try:
                            body = (await read_response_bytes(response, limit=65536)).decode("utf-8", errors="replace")
                        except Exception:
                            body = ""
                        if body:
                            detail += f" {redact_secret_text(body)[:300]}"
                        async for event in fail(
                            "backend_error",
                            detail,
                        ):
                            yield event
                        return
                    try:
                        async for data in iter_sse_data(response, label="OpenAI", legacy_json_lines=True):
                            if data == "[DONE]":
                                if provider_done:
                                    async for event in fail("duplicate_done", "The provider emitted [DONE] more than once."):
                                        yield event
                                    return
                                provider_done = True
                                accumulator.mark_done()
                                continue
                            if provider_done:
                                async for event in fail("output_after_done", "The provider emitted output after [DONE]."):
                                    yield event
                                return
                            try:
                                payload = parse_sse_payload(data, label="OpenAI")
                            except SSELimitError as exc:
                                async for event in fail("provider_output_limit", str(exc)):
                                    yield event
                                return
                            except SSELineError:
                                async for event in fail("invalid_stream_chunk", "The provider returned malformed stream JSON."):
                                    yield event
                                return
                            provider_error = payload.get("error")
                            if isinstance(provider_error, dict):
                                code = provider_error.get("code")
                                async for event in fail(code if isinstance(code, str) and code else "provider_error", "The provider stream failed."):
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
                                content_str = delta.get("content")
                                if isinstance(content_str, str) and content_str:
                                    text_active = True
                                    for event in builder.add_text_delta(content_str):
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
                                    state = tool_calls.setdefault(index, {"call_id": "", "name": [], "arguments": []})
                                    call_id = raw_call.get("id")
                                    if isinstance(call_id, str) and call_id:
                                        state["call_id"] = call_id
                                    function = raw_call.get("function")
                                    if not isinstance(function, dict):
                                        continue
                                    for field in ("name", "arguments"):
                                        fragment = function.get(field)
                                        if isinstance(fragment, str) and fragment:
                                            state[field].append(fragment)
                    except SSELimitError as exc:
                        async for event in fail("provider_output_limit", str(exc)):
                            yield event
                        return
                    except SSELineError as exc:
                        async for event in fail("invalid_stream_chunk", str(exc)):
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
            name = "".join(state["name"])
            if valid_function_name(name):
                for event in builder.add_function_call(
                    name,
                    function_call_arguments_string("".join(state["arguments"])),
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
        return validate_response(payload, display_model=display_model)



class NativeResponsesStreamAdapter:
    """Validate a native Responses SSE stream before exposing terminal state."""

    _TERMINAL_TYPES = {"response.completed", "response.incomplete", "response.failed"}

    def __init__(self, *, display_model: str) -> None:
        self.display_model = display_model
        self._decoder = SSEDecoder()
        self._terminal_event: dict[str, Any] | None = None
        self._terminal_emitted = False
        self._provider_done = False
        self._visible_output_started = False
        self._response_id = f"resp_{uuid.uuid4().hex[:12]}"
        self._provider_response_id: str | None = None
        self._protocol_error = False
        self._last_sequence: int | None = None
        self._items: dict[int, str] = {}
        self._item_indices: dict[str, int] = {}
        self._identity_chars = 0
        self._native_types: dict[int, str] = {}
        self._completed_items: dict[int, dict] = {}
        self._completed_budget = [0, 0]

    @property
    def visible_output_started(self) -> bool:
        return self._visible_output_started

    @property
    def awaiting_eof(self) -> bool:
        return self._terminal_event is not None or self._provider_done

    @property
    def protocol_failed(self) -> bool:
        return self._protocol_error

    def _failure(self, code: str, message: str) -> dict[str, Any]:
        output = []
        if code == "provider_output_limit":
            if self._terminal_event is not None:
                output = self._terminal_event["response"].get("output", [])
            else:
                for index, item in sorted(self._completed_items.items()):
                    if index != len(output):
                        break
                    output.append(item)
            try:
                output = validate_output(output)
            except NativeOutputError:
                # Individually valid done items can still contradict one
                # another (for example duplicate call IDs). Error rendering
                # must neither publish that aggregate nor fail recursively.
                output = []
        return {
            "type": "response.failed",
            "response": {
                "id": self._response_id,
                "object": "response",
                "status": "failed",
                "model": self.display_model,
                "output": output,
                "error": {"code": code, "message": message},
            },
        }

    def _set_failure(self, code: str, message: str) -> None:
        if self._terminal_emitted or self._protocol_error:
            return
        self._protocol_error = True
        self._terminal_event = self._failure(code, message)

    def _valid_id(self, value: object) -> bool:
        if not isinstance(value, str) or not value:
            self._set_failure("invalid_stream_identity", "The provider returned an invalid stream identifier.")
            return False
        try:
            value.encode("utf-8")
        except UnicodeError:
            self._set_failure("invalid_stream_identity", "The provider returned an invalid stream identifier.")
            return False
        if len(value) > 65536:
            self._set_failure("stream_identity_limit", "The provider exceeded the stream identity limit.")
            return False
        return True

    def _bind_item(self, index: object, item_id: object) -> bool:
        if index is not None and (type(index) is not int or index < 0):
            self._set_failure("invalid_output_index", "The provider returned an invalid output index.")
            return False
        if item_id is None:
            return True
        if not self._valid_id(item_id):
            return False
        if index is None:
            return True
        if (index in self._items and self._items[index] != item_id) or (
            item_id in self._item_indices and self._item_indices[item_id] != index
        ):
            self._set_failure("mismatched_item_id", "The provider contradicted an output item's identity.")
            return False
        if index not in self._items:
            if len(self._items) >= 10000 or self._identity_chars + len(item_id) > 65536:
                self._set_failure("stream_identity_limit", "The provider exceeded the stream identity limit.")
                return False
            self._identity_chars += len(item_id)
            self._items[index] = item_id
            self._item_indices[item_id] = index
        return True

    def _validate_identity(self, event: dict[str, Any]) -> bool:
        # Compatible providers may omit these fields or lifecycle events. Check
        # supplied facts for contradictions without inventing missing state.
        if "sequence_number" in event:
            sequence = event["sequence_number"]
            if type(sequence) is not int or sequence < 0 or (
                self._last_sequence is not None and sequence <= self._last_sequence
            ):
                self._set_failure("invalid_stream_sequence", "The provider returned invalid or out-of-order sequence numbers.")
                return False
            self._last_sequence = sequence
        response = event.get("response")
        response = response if isinstance(response, dict) else {}
        for response_id in (event.get("response_id"), response.get("id")):
            if response_id is None:
                continue
            if not self._valid_id(response_id):
                return False
            if self._provider_response_id is not None and response_id != self._provider_response_id:
                self._set_failure("mismatched_response_id", "The provider contradicted the response identity.")
                return False
            self._provider_response_id = self._response_id = response_id
        item = event.get("item")
        item = item if isinstance(item, dict) else {}
        if not self._bind_item(event.get("output_index"), event.get("item_id")):
            return False
        if not self._bind_item(event.get("output_index"), item.get("id")):
            return False
        if isinstance(response.get("output"), list):
            for index, output in enumerate(response["output"]):
                if isinstance(output, dict) and not self._bind_item(index, output.get("id")):
                    return False
        return True

    def _release_terminal(self) -> list[dict[str, Any]]:
        if self._terminal_event is None or self._terminal_emitted:
            return []
        self._terminal_emitted = True
        return [self._terminal_event]

    def _consume_payload(self, data: str) -> list[dict[str, Any]]:
        if self._terminal_emitted or self._protocol_error:
            return []
        data = data.strip()
        if data == "[DONE]":
            if self._provider_done:
                self._set_failure("duplicate_done", "The provider emitted [DONE] more than once.")
            self._provider_done = True
            if self._terminal_event is None:
                self._set_failure(
                    "missing_terminal_signal",
                    "The provider stream ended without a terminal response event.",
                )
            return []
        if self._provider_done:
            self._set_failure("output_after_done", "The provider emitted output after [DONE].")
            return []
        try:
            event = json_loads_limited(data)
        except ResourceLimitError as exc:
            self._set_failure("provider_output_limit" if exc.status == 413 else "invalid_stream_chunk", str(exc))
            return []
        except (ValueError, RecursionError):
            self._set_failure("invalid_stream_chunk", "The provider returned malformed stream JSON.")
            return []
        if not isinstance(event, dict) or not isinstance(event.get("type"), str):
            self._set_failure("invalid_stream_event", "The provider returned an invalid stream event.")
            return []
        event_type = event["type"]
        if not self._validate_identity(event):
            return []
        if event_type == "error":
            self._set_failure("provider_error", "The provider reported a stream error.")
            return []
        if self._terminal_event is not None:
            if event_type in self._TERMINAL_TYPES:
                self._set_failure("duplicate_terminal", "The provider emitted more than one terminal event.")
            else:
                self._set_failure("output_after_terminal", "The provider emitted output after its terminal event.")
            return []
        try:
            expected_item = validate_event(event)
            index = event.get("output_index")
            supplied = []
            if expected_item is not None and index is not None:
                supplied.append((index, expected_item))
            snapshot = event.get("response")
            if isinstance(snapshot, dict) and isinstance(snapshot.get("output"), list):
                supplied.extend((offset, item["type"]) for offset, item in enumerate(snapshot["output"])
                                if isinstance(item, dict) and isinstance(item.get("type"), str))
            for offset, item_type in supplied:
                if offset in self._native_types and self._native_types[offset] != item_type:
                    raise NativeOutputError("conflicting_native_item")
                if len(self._native_types) >= MAX_ITEMS and offset not in self._native_types:
                    raise NativeOutputError("native_output_limit")
                self._native_types[offset] = item_type
            if event_type == "response.output_item.done":
                if index in self._completed_items:
                    raise NativeOutputError("duplicate_native_item")
                check_json(event["item"], budget=self._completed_budget)
                self._completed_items[index] = validate_output([event["item"]])[0]
        except NativeOutputError as exc:
            self._set_failure(exc.code, str(exc))
            return []
        if event_type in self._TERMINAL_TYPES:
            response = event.get("response")
            if not isinstance(response, dict):
                self._set_failure("invalid_terminal_event", "The provider returned an invalid terminal event.")
                return []
            expected_status = event_type.removeprefix("response.")
            provider_status = response.get("status")
            if provider_status is not None and provider_status != expected_status:
                self._set_failure("invalid_terminal_event", "The provider terminal status did not match its event type.")
                return []
            try:
                output = reconcile_output(response.get("output"), self._completed_items)
                if any(index >= len(output) for index in self._native_types):
                    raise NativeOutputError("incomplete_native_output")
                normalized = validate_response({**response, "status": expected_status, "output": output}, display_model=self.display_model)
            except (NativeOutputError, ValueError) as exc:
                self._set_failure(getattr(exc, "code", "invalid_native_output"), "The provider returned an invalid native terminal snapshot.")
                return []
            expected_status = normalized["status"]
            normalized_type = f"response.{expected_status}"
            self._terminal_event = {"type": normalized_type, "response": normalized}
            return []
        if event_type.startswith("response.output") or event_type.startswith("response.reasoning"):
            self._visible_output_started = True
        if isinstance(event.get("response"), dict):
            event = dict(event)
            event["response"] = {**event["response"], "model": self.display_model}
        return [event]

    def consume_bytes(self, chunk: bytes) -> list[dict[str, Any]]:
        if self._terminal_emitted or self._protocol_error:
            return []
        events: list[dict[str, Any]] = []
        try:
            for data in self._decoder.feed(chunk):
                events.extend(self._consume_payload(data))
        except SSELimitError as exc:
            self._set_failure("provider_output_limit", str(exc))
        except SSELineError as exc:
            self._set_failure("invalid_stream_chunk", str(exc))
        return events

    def finish(self) -> list[dict[str, Any]]:
        if self._terminal_emitted:
            return []
        events: list[dict[str, Any]] = []
        try:
            for data in self._decoder.finish():
                events.extend(self._consume_payload(data))
        except SSELimitError as exc:
            self._set_failure("provider_output_limit", str(exc))
        except SSELineError as exc:
            self._set_failure("invalid_stream_chunk", str(exc))
        if self._terminal_event is None:
            self._set_failure(
                "missing_terminal_signal",
                "The provider stream ended without a terminal response event.",
            )
        events.extend(self._release_terminal())
        return events

    def abort(self, code: str, message: str) -> list[dict[str, Any]]:
        self._set_failure(code, message)
        return self.finish()
