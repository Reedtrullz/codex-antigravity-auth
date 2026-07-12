"""Google Antigravity request construction and response translation."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
import json
import math
from typing import Any
import uuid

import httpx

from .constants import ANTIGRAVITY_ENDPOINT_PROD, get_platform
from .response_protocol import (
    AttemptOutcome,
    ProviderResult,
    ProviderTerminal,
    TerminalKind,
    classify_terminal,
    normalize_usage,
    refusal_item,
)
from .transform import (
    function_call_arguments_json,
    safe_project_id,
    transform_gemini_candidate,
    transform_request,
    valid_function_name,
)


@dataclass(frozen=True)
class AccountLease:
    email: str
    project_id: str | None
    access_token: str
    fingerprint: dict[str, Any] | None = None


class GoogleHTTPError(RuntimeError):
    def __init__(self, status_code: int, outcome: AttemptOutcome) -> None:
        super().__init__(f"Google Antigravity returned HTTP {status_code}")
        self.status_code = status_code
        self.outcome = outcome


def outcome_for_http_status(status_code: int) -> AttemptOutcome:
    if status_code == 429:
        return AttemptOutcome(scope="family", category="rate_limit")
    if status_code in {401, 403}:
        return AttemptOutcome(scope="account", category="auth")
    if 400 <= status_code < 500:
        return AttemptOutcome(scope="none", category="invalid_request")
    return AttemptOutcome(scope="none", category="transport")


def outcome_for_backend_error(code: str, message: str) -> AttemptOutcome:
    combined = f"{code} {message}".lower()
    if "resource_exhausted" in combined or "quota" in combined:
        return AttemptOutcome(scope="family", category="quota")
    if code == "429" or "rate limit" in combined or "rate_limit" in combined:
        return AttemptOutcome(scope="family", category="rate_limit")
    if any(term in combined for term in ("401", "403", "unauthenticated", "permission_denied", "auth")):
        return AttemptOutcome(scope="account", category="auth")
    if "invalid_argument" in combined or code == "400":
        return AttemptOutcome(scope="none", category="invalid_request")
    return AttemptOutcome(scope="none", category="transport")


def _safe_header_string(value: object) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    if any(ord(character) < 0x20 or ord(character) > 0x7E for character in value):
        return None
    return value


def _safe_client_metadata(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    metadata: dict[str, Any] = {}
    for key, raw_value in value.items():
        safe_key = _safe_header_string(key)
        if safe_key is None:
            continue
        if isinstance(raw_value, str):
            safe_value = _safe_header_string(raw_value)
            if safe_value is not None:
                metadata[safe_key] = safe_value
        elif raw_value is None or isinstance(raw_value, bool):
            metadata[safe_key] = raw_value
        elif isinstance(raw_value, (int, float)) and math.isfinite(float(raw_value)):
            metadata[safe_key] = raw_value
    return metadata


class GoogleResponseAccumulator:
    def __init__(self) -> None:
        self._text = ""
        self._reasoning = ""
        self._function_calls: list[dict[str, Any]] = []
        self._finish_reason: str | None = None
        self._safety_block: dict[str, Any] | None = None
        self._usage = normalize_usage()
        self._malformed = False
        self._done = False

    def mark_malformed(self) -> None:
        self._malformed = True

    def mark_done(self) -> None:
        self._done = True

    def consume(self, payload: object) -> None:
        if not isinstance(payload, dict):
            self._malformed = True
            return
        if "response" in payload:
            nested = payload.get("response")
            if not isinstance(nested, dict):
                self._malformed = True
                return
            payload = nested

        prompt_feedback = payload.get("promptFeedback")
        if isinstance(prompt_feedback, dict) and prompt_feedback.get("blockReason"):
            self._safety_block = prompt_feedback

        usage = payload.get("usageMetadata")
        if isinstance(usage, dict):
            self._usage = normalize_usage(
                usage.get("promptTokenCount"),
                usage.get("candidatesTokenCount"),
                usage.get("totalTokenCount"),
            )

        candidates = payload.get("candidates", [])
        if not isinstance(candidates, list):
            return
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            finish_reason = candidate.get("finishReason")
            if isinstance(finish_reason, str) and finish_reason:
                self._finish_reason = finish_reason
            content = candidate.get("content")
            if content is None:
                continue
            if not isinstance(content, dict):
                continue
            parts = content.get("parts", [])
            if not isinstance(parts, list):
                continue
            for part in parts:
                if not isinstance(part, dict):
                    continue
                if part.get("thought") is True or part.get("type") == "thinking":
                    thought = part.get("text") or part.get("thinking")
                    if isinstance(thought, str):
                        self._reasoning += thought
                    continue
                if "text" in part:
                    text = part.get("text")
                    if isinstance(text, str):
                        self._text += text
                    continue
                if "functionCall" in part:
                    function_call = part.get("functionCall")
                    if not isinstance(function_call, dict):
                        continue
                    name = function_call.get("name")
                    if not valid_function_name(name):
                        continue
                    call_id = function_call.get("id")
                    if not isinstance(call_id, str) or not call_id:
                        call_id = f"call_{uuid.uuid4().hex[:8]}"
                    self._function_calls.append(
                        {
                            "type": "function_call",
                            "id": f"fc_{uuid.uuid4().hex[:8]}",
                            "call_id": call_id,
                            "name": name,
                            "arguments": function_call_arguments_json(function_call.get("args", {})),
                        }
                    )

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
        output.extend(self._function_calls)
        if self._safety_block and not output:
            output.append(refusal_item(self._safety_block))
        terminal = classify_terminal(
            output=output,
            finish_reason=self._finish_reason,
            safety_block=self._safety_block,
            malformed=self._malformed,
        )
        if (
            terminal.kind is TerminalKind.COMPLETED
            and self._finish_reason is None
            and not self._done
        ):
            terminal = ProviderTerminal(
                TerminalKind.FAILED,
                "missing_terminal_signal",
                error_code="missing_terminal_signal",
                error_message="The provider stream ended without a terminal signal.",
            )
        return ProviderResult(output=tuple(output), usage=self._usage, terminal=terminal)


class GoogleTransport:
    def __init__(
        self,
        *,
        timeout: float,
        platform_name: str | None = None,
        endpoint: str = ANTIGRAVITY_ENDPOINT_PROD,
        client_factory: Any = httpx.AsyncClient,
    ) -> None:
        self.timeout = timeout
        self.platform_name = platform_name or get_platform()
        self.endpoint = endpoint.rstrip("/")
        self.client_factory = client_factory

    def build_request(self, request: dict[str, Any], lease: AccountLease) -> dict[str, Any]:
        return transform_request(request, project_id=safe_project_id(lease.project_id))

    def build_headers(self, lease: AccountLease) -> dict[str, str]:
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/537.36 (KHTML, like Gecko) Antigravity/2.0.0 Chrome/138.0.7204.235 Electron/37.3.1 Safari/537.36",
            "X-Goog-Api-Client": "google-cloud-sdk vscode_cloudshelleditor/0.1",
            "Client-Metadata": json.dumps(
                {"ideType": "ANTIGRAVITY", "platform": self.platform_name, "pluginType": "GEMINI"},
                separators=(",", ":"),
            ),
            "Content-Type": "application/json",
            "Authorization": f"Bearer {lease.access_token}",
        }
        fingerprint = lease.fingerprint
        if isinstance(fingerprint, dict):
            user_agent = _safe_header_string(fingerprint.get("userAgent"))
            api_client = _safe_header_string(fingerprint.get("apiClient"))
            if user_agent is not None:
                headers["User-Agent"] = user_agent
            if api_client is not None:
                headers["X-Goog-Api-Client"] = api_client
            client_metadata = _safe_client_metadata(fingerprint.get("clientMetadata"))
            device_id = _safe_header_string(fingerprint.get("deviceId"))
            session_token = _safe_header_string(fingerprint.get("sessionToken"))
            if device_id is not None:
                client_metadata["deviceId"] = device_id
            if session_token is not None:
                client_metadata["sessionToken"] = session_token
            if client_metadata:
                headers["Client-Metadata"] = json.dumps(client_metadata)
        return headers

    async def post(self, request: dict[str, Any], lease: AccountLease) -> httpx.Response:
        url = f"{self.endpoint}/v1internal:generateContent"
        async with self.client_factory(timeout=self.timeout) as client:
            return await client.post(
                url,
                json=self.build_request(request, lease),
                headers=self.build_headers(lease),
            )

    async def execute(
        self,
        request: dict[str, Any],
        lease: AccountLease,
        *,
        stream: bool,
    ):
        if stream:
            return self.stream(request, lease)
        response = await self.post(request, lease)
        if response.status_code != 200:
            raise GoogleHTTPError(response.status_code, outcome_for_http_status(response.status_code))
        try:
            payload = response.json()
        except Exception:
            accumulator = GoogleResponseAccumulator()
            accumulator.mark_malformed()
            return accumulator.finalize()
        return self.parse_response(payload)

    @asynccontextmanager
    async def stream(self, request: dict[str, Any], lease: AccountLease):
        url = f"{self.endpoint}/v1internal:streamGenerateContent?alt=sse"
        async with self.client_factory(timeout=self.timeout) as client:
            async with client.stream(
                "POST",
                url,
                json=self.build_request(request, lease),
                headers=self.build_headers(lease),
            ) as response:
                yield response

    def parse_response(self, payload: object) -> ProviderResult:
        if isinstance(payload, list):
            payload = payload[0] if payload else {}
        if not isinstance(payload, dict):
            accumulator = GoogleResponseAccumulator()
            accumulator.mark_malformed()
            return accumulator.finalize()

        unwrapped = payload.get("response", payload)
        if not isinstance(unwrapped, dict):
            accumulator = GoogleResponseAccumulator()
            accumulator.mark_malformed()
            return accumulator.finalize()

        candidates = unwrapped.get("candidates", [])
        if not isinstance(candidates, list):
            accumulator = GoogleResponseAccumulator()
            accumulator.mark_malformed()
            return accumulator.finalize()

        output: list[dict[str, Any]] = []
        finish_reason: str | None = None
        malformed = False
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            candidate_reason = candidate.get("finishReason")
            if isinstance(candidate_reason, str) and candidate_reason:
                finish_reason = candidate_reason
            transformed = transform_gemini_candidate(candidate)
            reasoning = transformed.get("reasoning")
            if isinstance(reasoning, dict):
                output.append(reasoning)
            message = transformed.get("message")
            if isinstance(message, dict) and message.get("content"):
                output.append(message)
            function_calls = transformed.get("function_calls")
            if isinstance(function_calls, list):
                output.extend(item for item in function_calls if isinstance(item, dict))

        safety_block = unwrapped.get("promptFeedback")
        if not isinstance(safety_block, dict):
            safety_block = None
        if safety_block and not output:
            output.append(refusal_item(safety_block))

        usage = unwrapped.get("usageMetadata")
        usage = usage if isinstance(usage, dict) else {}
        normalized_usage = normalize_usage(
            usage.get("promptTokenCount"),
            usage.get("candidatesTokenCount"),
            usage.get("totalTokenCount"),
        )
        terminal = classify_terminal(
            output=output,
            finish_reason=finish_reason,
            safety_block=safety_block,
            malformed=malformed,
        )
        return ProviderResult(output=tuple(output), usage=normalized_usage, terminal=terminal)
