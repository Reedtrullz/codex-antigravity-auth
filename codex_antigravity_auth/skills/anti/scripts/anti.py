#!/usr/bin/env python3
from __future__ import annotations

import argparse
import concurrent.futures
import fnmatch
import json
import math
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from anti_lib.chunking import chunk_manifest
from anti_lib.context import ordered_prompt
from anti_lib.ledger import execution_entry, prompts_as_text
from anti_lib.redaction import REDACTION_MARKER, redact_sensitive_text, sanitize_json
from anti_lib.runner import presentable_result


DEFAULT_BASE_URL = "http://127.0.0.1:51122/v1"
DEFAULT_TOKEN_ENV = "ANTIGRAVITY_GATEWAY_TOKEN"
MODEL_ALIASES = {
    "opus": "claude-opus-4-6",
    "claude-opus": "claude-opus-4-6",
    "claude-opus-4-6": "claude-opus-4-6",
    "sonnet": "claude-3.5-sonnet",
    "claude-sonnet": "claude-3.5-sonnet",
    "claude-3.5-sonnet": "claude-3.5-sonnet",
    "claude-3-5-sonnet": "claude-3.5-sonnet",
    "grok": "xai-oauth:grok-build-0.1",
    "supergrok": "xai-oauth:grok-build-0.1",
    "xai-grok": "xai-oauth:grok-build-0.1",
    "grok-build": "xai-oauth:grok-build-0.1",
    "grok-build-0.1": "xai-oauth:grok-build-0.1",
    "grok-4.3": "xai-oauth:grok-4.3",
    "grok-4": "xai-oauth:grok-4.3",
}
DEFAULT_REVIEW_MODEL = "claude-opus-4-6"
DEFAULT_CONSULT_MODEL = "claude-3.5-sonnet"
DEFAULT_PLAN_MODEL = "claude-opus-4-6"
DEFAULT_PANEL_MODELS = ["claude-3.5-sonnet", "claude-opus-4-6"]
DEFAULT_PANEL_JUDGE_MODEL = "claude-opus-4-6"
COLLAB_PROFILES = {"none", "claude-grok"}
CLAUDE_GROK_PANEL_MODELS = ["sonnet", "opus", "grok"]
MAX_FILE_BYTES = 180_000
DEFAULT_MAX_PROMPT_CHARS = 120_000
DEFAULT_MAX_SYNTHESIS_CHARS = DEFAULT_MAX_PROMPT_CHARS
CLAUDE_SAFE_PROMPT_CHARS = 30_000
MAX_PROMPT_CHARS_HELP = (
    "Maximum prompt chars before truncation/chunking; use 0 for unlimited. "
    "Claude-family review/plan/panel calls still use the conservative safety budget with --chunked auto; "
    "add --chunked off when you intentionally want one large Claude request."
)
PID_FILE = Path.home() / ".codex" / "anti-gateway.pid"
LOG_FILE = Path.home() / ".codex" / "anti-gateway.log"
RUNS_DIR = Path.home() / ".codex" / "anti-runs"
RUN_OUTPUT_PREVIEW_CHARS = 1600
POST_FAILURE_MODEL_PROBE_TIMEOUT = 8.0
FALLBACK_POLICIES = {"never", "on-retryable", "on-timeout"}
SAVE_OUTPUT_MODES = {"never", "summary", "full"}
PANEL_OUTPUT_MODES = {"prose", "findings"}
BACKEND_TIMEOUT_METADATA_KEY = "antigravity_backend_timeout_seconds"
BACKEND_TIMEOUT_HINT_THRESHOLD_SECONDS = 120.0
BACKEND_TIMEOUT_HINT_BUFFER_SECONDS = 10.0
BACKEND_TIMEOUT_HINT_MAX_SECONDS = 600.0


EXCLUDED_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".aws",
    ".azure",
    ".venv",
    ".config",
    ".gcloud",
    ".gnupg",
    ".ssh",
    "venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".tox",
    ".cache",
    "dist",
    "build",
    ".build",
    ".deriveddata",
    "target",
    "credential",
    "credentials",
    "keychain",
    "keys",
    "private",
    "secret",
    "secrets",
    "tokens",
}
EXCLUDED_NAMES = {
    ".env",
    ".env.local",
    ".envrc",
    "accounts.json",
    "antigravity-accounts.json",
    "antigravity-providers.json",
    "antigravity-credentials.json",
    "antigravity-storage.key",
    "provider-keys.json",
    "provider_keys.json",
    "providers.json",
    "id_rsa",
    "id_ed25519",
    "known_hosts",
}
EXCLUDED_PATTERNS = [
    ".env.*",
    "antigravity-accounts.json.*",
    "antigravity-credentials.json.*",
    "antigravity-providers.json.*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*account*key*.json",
    "*provider*key*.json",
    "*credential*.json",
    "*credentials*.json",
    "*secret*.env",
    "*secret*.json",
    "*secret*.toml",
    "*secret*.txt",
    "*secret*.yaml",
    "*secret*.yml",
    "*token*.env",
    "*token*.json",
    "*token*.toml",
    "*token*.txt",
    "*token*.yaml",
    "*token*.yml",
    "*apikey*",
    "*api-key*",
]
RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


class AntiError(Exception):
    pass


def eprint(message: str) -> None:
    print(message, file=sys.stderr)


def utc_timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def new_run_id() -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + uuid.uuid4().hex[:8]


def ensure_run_id(args: argparse.Namespace) -> str | None:
    if save_output_mode(args) == "never":
        return None
    run_id = getattr(args, "run_id", None)
    if run_id:
        if not RUN_ID_RE.fullmatch(str(run_id)):
            raise AntiError("run id must contain only letters, numbers, '_' or '-'")
        return str(run_id)
    run_id = new_run_id()
    args.run_id = run_id
    return run_id


def progress(args: argparse.Namespace, message: str) -> None:
    if getattr(args, "progress", False):
        eprint(f"[anti] {redact_sensitive_text(message)}")


def save_output_mode(args: argparse.Namespace) -> str:
    value = getattr(args, "save_output", "never")
    if value not in SAVE_OUTPUT_MODES:
        raise AntiError(f"unsupported save output mode: {value}")
    return value


def write_run_record(
    args: argparse.Namespace,
    *,
    mode: str,
    status: str,
    models: list[str] | None = None,
    base_url: str | None = None,
    prompt_text: str | None = None,
    output_text: str | None = None,
    caveats: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
    error: str | None = None,
    execution_ledger: list[dict[str, Any]] | None = None,
) -> Path | None:
    output_mode = save_output_mode(args)
    if output_mode == "never":
        return None

    if RUNS_DIR.is_symlink():
        raise AntiError(f"refusing to write Anti run record through symlinked directory: {RUNS_DIR}")
    os.makedirs(RUNS_DIR, mode=0o700, exist_ok=True)
    try:
        os.chmod(RUNS_DIR, 0o700)
    except OSError:
        pass

    output_chars = len(output_text or "")
    prompt_chars = len(prompt_text or "")
    record_id = getattr(args, "run_id", None) or new_run_id()
    if not RUN_ID_RE.fullmatch(str(record_id)):
        raise AntiError("run id must contain only letters, numbers, '_' or '-'")

    record: dict[str, Any] = {
        "id": str(record_id),
        "created_at": utc_timestamp(),
        "command": getattr(args, "command", mode),
        "workflow": getattr(args, "workflow_name", None),
        "run_label": getattr(args, "run_label", None),
        "mode": mode,
        "status": status,
        "gateway": base_url,
        "models": models or [],
        "prompt_chars": prompt_chars,
        "output_chars": output_chars,
        "caveats": caveats or [],
        "metadata": metadata or {},
        "save_output": output_mode,
    }
    if error:
        record["error"] = error
    if output_mode == "summary" and output_text:
        record["output_preview"] = output_text[:RUN_OUTPUT_PREVIEW_CHARS]
    elif output_mode == "full":
        if prompt_text is not None:
            record["prompt_text"] = prompt_text
        if output_text is not None:
            record["output_text"] = output_text
        if execution_ledger is not None:
            record["execution_ledger"] = execution_ledger

    record = sanitize_json(record)
    path = RUNS_DIR / f"{record['id']}.json"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(record, indent=2, sort_keys=True) + "\n")
    progress(args, f"saved sanitized run record: {path}")
    return path


def error_is_retryable(error: str) -> bool:
    lowered = error.lower()
    if "retryable=true" in lowered:
        return True
    return any(f"http {status}" in lowered for status in ("408", "409", "425", "429", "500", "502", "503", "504"))


def error_is_timeout(error: str) -> bool:
    lowered = error.lower()
    return "timed out" in lowered or "timeouterror" in lowered or "timeout error" in lowered


def should_use_fallback(error: str, policy: str) -> bool:
    if policy == "never":
        return False
    if policy == "on-timeout":
        return error_is_timeout(error)
    if policy == "on-retryable":
        return error_is_retryable(error) or error_is_timeout(error)
    raise AntiError(f"unsupported fallback policy: {policy}")


def gateway_restart_hint(base_url: str) -> str:
    try:
        parsed = urllib.parse.urlparse(normalize_base_url(base_url))
        port = parsed.port
    except Exception:
        port = None
    port_arg = f" --port {port}" if port else ""
    return (
        "gateway appears wedged; restart recommended "
        f"(`python3 ~/.codex/skills/anti/scripts/anti.py start{port_arg}` after stopping the stale gateway, "
        f"or `codex-antigravity stop{port_arg}` then `codex-antigravity start --background{port_arg}`)"
    )


def gateway_post_failure_diagnostic(args: argparse.Namespace, error: str) -> str:
    if not (error_is_retryable(error) or error_is_timeout(error)):
        return ""
    base_url = getattr(args, "base_url", DEFAULT_BASE_URL)
    token_env = getattr(args, "gateway_token_env", DEFAULT_TOKEN_ENV)
    raw_timeout = getattr(args, "timeout", POST_FAILURE_MODEL_PROBE_TIMEOUT) or POST_FAILURE_MODEL_PROBE_TIMEOUT
    try:
        request_timeout = float(raw_timeout)
    except (TypeError, ValueError):
        request_timeout = POST_FAILURE_MODEL_PROBE_TIMEOUT
    timeout = min(POST_FAILURE_MODEL_PROBE_TIMEOUT, max(1.0, request_timeout))
    try:
        fetch_model_ids(base_url, timeout=timeout, token_env=token_env)
    except AntiError as exc:
        probe_error = redact_sensitive_text(str(exc))
        if error_is_timeout(probe_error):
            return (
                " Gateway health check after this retryable failure also timed out; "
                + gateway_restart_hint(base_url)
                + "."
            )
        return f" Gateway health check after this retryable failure also failed: {probe_error}."
    return (
        " Gateway /v1/models stayed responsive after this retryable failure; "
        "generation path appears unhealthy, not model-list readiness. "
        "For long Claude calls, retry with a narrower or chunked scope, use a fallback model, "
        "or inspect `codex-antigravity logs --tail 20`; restart the gateway only if /v1/models also fails."
    )


def enrich_generation_error(args: argparse.Namespace, error: str) -> str:
    diagnostic = gateway_post_failure_diagnostic(args, error)
    return error + diagnostic if diagnostic else error


def backend_timeout_hint(timeout: float) -> float | None:
    try:
        value = float(timeout)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value) or value <= BACKEND_TIMEOUT_HINT_THRESHOLD_SECONDS:
        return None
    return min(BACKEND_TIMEOUT_HINT_MAX_SECONDS, max(1.0, value - BACKEND_TIMEOUT_HINT_BUFFER_SECONDS))


def is_claude_model(model: str) -> bool:
    return str(model).startswith("claude-")


def prompt_budget_for_model(args: argparse.Namespace, model: str) -> int:
    raw_budget = int(getattr(args, "max_prompt_chars", DEFAULT_MAX_PROMPT_CHARS))
    if getattr(args, "chunked", "auto") == "off" or not is_claude_model(model):
        return raw_budget
    if raw_budget <= 0:
        return CLAUDE_SAFE_PROMPT_CHARS
    return min(raw_budget, CLAUDE_SAFE_PROMPT_CHARS)


def claude_guardrail_would_apply(args: argparse.Namespace, model: str, prompt_budget: int) -> bool:
    raw_budget = int(getattr(args, "max_prompt_chars", DEFAULT_MAX_PROMPT_CHARS))
    return getattr(args, "chunked", "auto") != "off" and is_claude_model(model) and (
        raw_budget <= 0 or prompt_budget < raw_budget
    )


def add_claude_guardrail_caveat(caveats: list[str], *, prompt_budget: int) -> None:
    caveat = (
        f"Claude safety budget: split broad Opus/Sonnet work into calls of about {prompt_budget} prompt chars "
        "to reduce timeout/auth-loss risk; use --chunked off only when you intentionally want one large call."
    )
    if caveat not in caveats:
        caveats.append(caveat)


def normalize_base_url(value: str) -> str:
    value = str(value).strip()
    if not value:
        raise AntiError("base URL must be non-empty")
    if any(ord(char) <= 0x20 for char in value):
        raise AntiError("base URL must not contain whitespace or control characters")
    parsed = urllib.parse.urlsplit(value)
    if parsed.username or parsed.password:
        raise AntiError("base URL must not contain username or password")
    if parsed.query or parsed.fragment:
        raise AntiError("base URL must not contain query strings or fragments")
    return value.rstrip("/")


def resolve_model(value: str | None, *, default: str) -> str:
    raw = (value or default).strip()
    return MODEL_ALIASES.get(raw.lower(), raw)


def positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{value!r} is not an integer") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError("value must be at least 1")
    return parsed


def non_negative_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{value!r} is not an integer") from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError("value must be at least 0")
    return parsed


def token_from_env(env_name: str) -> str | None:
    token = os.environ.get(env_name, "")
    return token if token else None


def request_json(
    method: str,
    url: str,
    *,
    payload: dict[str, Any] | None = None,
    timeout: float = 10.0,
    token_env: str = DEFAULT_TOKEN_ENV,
) -> tuple[int, dict[str, Any]]:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {"Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    token = token_from_env(token_env)
    if token:
        headers["Authorization"] = f"Bearer {token}"

    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            raw = res.read()
            status = int(res.status)
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        status = int(exc.code)
    except Exception as exc:
        raise AntiError(f"request to {url} failed: {exc}") from exc

    if not raw:
        return status, {}
    try:
        decoded = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise AntiError(f"request to {url} returned HTTP {status} non-JSON response") from exc
    if not isinstance(decoded, dict):
        raise AntiError(f"request to {url} returned JSON {type(decoded).__name__}, expected object")
    return status, decoded


def model_ids_from_catalog(payload: dict[str, Any]) -> set[str]:
    entries = payload.get("data")
    if not isinstance(entries, list):
        entries = payload.get("models")
    if not isinstance(entries, list):
        return set()
    ids: set[str] = set()
    for entry in entries:
        if isinstance(entry, dict) and isinstance(entry.get("id"), str):
            ids.add(entry["id"])
    return ids


def fetch_model_ids(base_url: str, *, timeout: float, token_env: str) -> set[str]:
    status, payload = request_json(
        "GET",
        f"{normalize_base_url(base_url)}/models",
        timeout=timeout,
        token_env=token_env,
    )
    if status != 200:
        detail = payload.get("detail") or payload.get("error") or payload
        raise AntiError(f"/v1/models returned HTTP {status}: {detail}")
    ids = model_ids_from_catalog(payload)
    if not ids:
        raise AntiError("/v1/models returned no usable model ids")
    return ids


def validate_git_rev_range(value: str, *, source: str) -> str:
    value = value.strip()
    if not value:
        raise AntiError(f"{source} must be non-empty")
    if value.startswith("-"):
        raise AntiError(f"{source} must not start with '-'")
    if "\0" in value or "\n" in value or "\r" in value:
        raise AntiError(f"{source} must be a single git revision/range argument")
    return value


def extract_response_text(payload: Any) -> str:
    texts: list[str] = []

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            if isinstance(value.get("output_text"), str):
                texts.append(value["output_text"])
            if isinstance(value.get("text"), str) and value.get("type") in {
                "output_text",
                "text",
                "message",
            }:
                texts.append(value["text"])
            for key in ("output", "content", "response"):
                if key in value:
                    visit(value[key])
        elif isinstance(value, list):
            for item in value:
                visit(item)

    visit(payload)
    merged = "\n".join(part.strip() for part in texts if part and part.strip()).strip()
    if merged:
        return merged
    return json.dumps(payload, indent=2, sort_keys=True)[:8000]


class ResponseText(str):
    def __new__(
        cls,
        text: str,
        *,
        usage: dict[str, int] | None = None,
        elapsed_ms: int | None = None,
        response_metadata: dict[str, Any] | None = None,
    ):
        obj = str.__new__(cls, text)
        obj.usage = usage
        obj.elapsed_ms = elapsed_ms
        obj.response_metadata = response_metadata or {}
        return obj


def int_usage(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value >= 0:
        return value
    if isinstance(value, float) and value >= 0 and value.is_integer():
        return int(value)
    return None


def normalize_usage(value: Any) -> dict[str, int] | None:
    if not isinstance(value, dict):
        return None
    input_tokens = int_usage(value.get("input_tokens"))
    if input_tokens is None:
        input_tokens = int_usage(value.get("prompt_tokens"))
    output_tokens = int_usage(value.get("output_tokens"))
    if output_tokens is None:
        output_tokens = int_usage(value.get("completion_tokens"))
    total_tokens = int_usage(value.get("total_tokens"))
    result: dict[str, int] = {}
    if input_tokens is not None:
        result["input_tokens"] = input_tokens
    if output_tokens is not None:
        result["output_tokens"] = output_tokens
    if total_tokens is not None:
        result["total_tokens"] = total_tokens
    elif input_tokens is not None and output_tokens is not None:
        result["total_tokens"] = input_tokens + output_tokens
    return result or None


def extract_usage(payload: Any) -> dict[str, int] | None:
    if isinstance(payload, dict):
        usage = normalize_usage(payload.get("usage"))
        if usage:
            return usage
        response = payload.get("response")
        if isinstance(response, dict):
            usage = normalize_usage(response.get("usage"))
            if usage:
                return usage
    return None


def response_call_metadata(value: Any) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    usage = normalize_usage(getattr(value, "usage", None))
    if usage:
        metadata["usage"] = usage
    elapsed_ms = getattr(value, "elapsed_ms", None)
    if isinstance(elapsed_ms, int) and elapsed_ms >= 0:
        metadata["elapsed_ms"] = elapsed_ms
    response_metadata = getattr(value, "response_metadata", None)
    if isinstance(response_metadata, dict):
        metadata.update(response_metadata)
    return metadata


def sum_usage(*values: Any) -> dict[str, int]:
    totals = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    any_usage = False

    def visit(value: Any) -> None:
        nonlocal any_usage
        if isinstance(value, dict):
            usage = normalize_usage(value)
            if usage:
                any_usage = True
                for key in totals:
                    totals[key] += int(usage.get(key, 0))
                return
            for item in value.values():
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    for value in values:
        visit(value)
    return totals if any_usage else {}


def post_response(
    *,
    base_url: str,
    model: str,
    prompt: str,
    max_output_tokens: int,
    timeout: float,
    token_env: str,
    retries: int = 0,
    model_ids: set[str] | None = None,
    run_id: str | None = None,
) -> ResponseText:
    available_model_ids = model_ids
    if available_model_ids is None:
        available_model_ids = fetch_model_ids(base_url, timeout=timeout, token_env=token_env)
    if model not in available_model_ids:
        sample = ", ".join(sorted(available_model_ids)[:12])
        raise AntiError(f"model {model!r} is not advertised by /v1/models. Available sample: {sample}")
    payload = {
        "model": model,
        "input": prompt,
        "max_output_tokens": max_output_tokens,
        "stream": False,
    }
    metadata: dict[str, Any] = {}
    if run_id:
        metadata["run_id"] = run_id
    backend_timeout = backend_timeout_hint(timeout)
    if backend_timeout is not None:
        metadata[BACKEND_TIMEOUT_METADATA_KEY] = backend_timeout
    if metadata:
        payload["metadata"] = metadata
    attempts = max(0, retries) + 1
    retryable_statuses = {408, 409, 425, 429, 500, 502, 503, 504}
    last_error: str | None = None
    response_url = f"{normalize_base_url(base_url)}/responses"
    started = time.monotonic()
    for attempt in range(1, attempts + 1):
        try:
            status, decoded = request_json(
                "POST",
                response_url,
                payload=payload,
                timeout=timeout,
                token_env=token_env,
            )
        except AntiError as exc:
            last_error = str(exc)
            if attempt < attempts:
                time.sleep(min(4.0, 0.75 * attempt))
                continue
            raise AntiError(
                "request failed after "
                f"{attempt} attempt(s): {last_error}. Diagnostics: "
                f"model={model}, prompt_chars={len(prompt)}, timeout={timeout}, gateway={base_url}"
            ) from exc

        if status == 200:
            return ResponseText(
                extract_response_text(decoded),
                usage=extract_usage(decoded),
                elapsed_ms=int((time.monotonic() - started) * 1000),
                response_metadata={"attempts": attempt},
            )

        detail = decoded.get("detail") or decoded.get("error") or decoded
        last_error = f"HTTP {status}: {detail}"
        if status in retryable_statuses and attempt < attempts:
            time.sleep(min(4.0, 0.75 * attempt))
            continue
        raise AntiError(
            f"/v1/responses returned {last_error} after {attempt} attempt(s). Diagnostics: "
            f"model={model}, prompt_chars={len(prompt)}, timeout={timeout}, gateway={base_url}, "
            f"retryable={str(status in retryable_statuses).lower()}"
        )

    raise AssertionError("post_response retry loop should have returned or raised")


def generate_with_fallback(
    args: argparse.Namespace,
    *,
    model: str,
    prompt: str,
    max_output_tokens: int,
    purpose: str,
    model_ids: set[str] | None = None,
) -> tuple[str, str, dict[str, Any]]:
    fallback_raw = getattr(args, "fallback_model", None)
    fallback_model = resolve_model(fallback_raw, default=fallback_raw) if fallback_raw else None
    fallback_policy = getattr(args, "fallback_policy", "never")
    if fallback_policy not in FALLBACK_POLICIES:
        raise AntiError(f"unsupported fallback policy: {fallback_policy}")

    failures: list[dict[str, str]] = []
    progress(args, f"{purpose}: calling {model} ({len(prompt)} prompt chars)")
    try:
        raw_text = post_response(
            base_url=args.base_url,
            model=model,
            prompt=prompt,
            max_output_tokens=max_output_tokens,
            timeout=args.timeout,
            token_env=args.gateway_token_env,
            retries=args.retry,
            model_ids=model_ids,
            run_id=getattr(args, "run_id", None),
        )
        text = str(raw_text)
        call_metadata = response_call_metadata(raw_text)
        progress(args, f"{purpose}: {model} completed ({len(text)} output chars)")
        return text, model, {
            "model_used": model,
            "primary_model": model,
            "fallback_model": fallback_model,
            "fallback_policy": fallback_policy,
            "fallback_used": False,
            "generation_failures": failures,
            **call_metadata,
        }
    except AntiError as exc:
        error = redact_sensitive_text(str(exc))
        failures.append({"model": model, "error": error})
        if not fallback_model or fallback_model == model or not should_use_fallback(error, fallback_policy):
            raise AntiError(enrich_generation_error(args, error)) from exc
        progress(args, f"{purpose}: {model} failed; trying fallback {fallback_model}")
        try:
            raw_text = post_response(
                base_url=args.base_url,
                model=fallback_model,
                prompt=prompt,
                max_output_tokens=max_output_tokens,
                timeout=args.timeout,
                token_env=args.gateway_token_env,
                retries=args.retry,
                model_ids=model_ids,
                run_id=getattr(args, "run_id", None),
            )
        except AntiError as fallback_exc:
            fallback_error = redact_sensitive_text(str(fallback_exc))
            failures.append({"model": fallback_model, "error": fallback_error})
            enriched_fallback_error = enrich_generation_error(args, fallback_error)
            raise AntiError(
                f"{purpose} failed on primary model {model} and fallback model {fallback_model}. "
                f"Primary error: {error}. Fallback error: {enriched_fallback_error}"
            ) from fallback_exc
        text = str(raw_text)
        call_metadata = response_call_metadata(raw_text)
        progress(args, f"{purpose}: fallback {fallback_model} completed ({len(text)} output chars)")
        return text, fallback_model, {
            "model_used": fallback_model,
            "primary_model": model,
            "fallback_model": fallback_model,
            "fallback_policy": fallback_policy,
            "fallback_used": True,
            "generation_failures": failures,
            **call_metadata,
        }


def find_repo_root(start: Path) -> Path | None:
    proc = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=start,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if proc.returncode != 0:
        return None
    return Path(proc.stdout.strip()).resolve()


def run_git(root: Path, args: list[str], *, check: bool = True) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=root,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if check and proc.returncode != 0:
        raise AntiError(proc.stderr.strip() or f"git {' '.join(args)} failed")
    return proc.stdout


def path_is_excluded(rel_path: str) -> bool:
    path = rel_path.replace("\\", "/")
    path_lower = path.lower()
    parts = [part.lower() for part in path.split("/") if part]
    if any(part in EXCLUDED_DIRS for part in parts):
        return True
    name = parts[-1] if parts else ""
    if name in EXCLUDED_NAMES:
        return True
    patterns = [pattern.lower() for pattern in EXCLUDED_PATTERNS]
    if any(fnmatch.fnmatch(name, pattern) for pattern in patterns):
        return True
    if any(fnmatch.fnmatch(part, pattern) for part in parts for pattern in patterns):
        return True
    return any(fnmatch.fnmatch(path_lower, pattern) for pattern in patterns)


def relative_safe_path(root: Path, raw_path: str) -> str:
    candidate = Path(raw_path)
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        resolved = candidate.resolve()
        rel = resolved.relative_to(root.resolve())
    except Exception as exc:
        raise AntiError(f"refusing path outside review root: {raw_path}") from exc
    return rel.as_posix()


def filter_paths(paths: list[str], *, root: Path) -> tuple[list[str], list[str]]:
    kept: list[str] = []
    excluded: list[str] = []
    seen: set[str] = set()
    for raw in paths:
        if not raw:
            continue
        validate_path_list_item(raw, source="path argument")
        rel = relative_safe_path(root, raw)
        if rel in seen:
            continue
        seen.add(rel)
        if path_is_excluded(rel):
            excluded.append(rel)
        else:
            kept.append(rel)
    return kept, excluded


def read_paths_file(spec: str) -> list[str]:
    if spec == "-":
        raw = sys.stdin.buffer.read()
    else:
        raw = Path(spec).expanduser().read_bytes()
    try:
        decoded = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise AntiError(f"path list {spec!r} is not valid UTF-8") from exc
    if "\0" in decoded:
        items = [item for item in decoded.split("\0") if item]
    else:
        items = [line for line in decoded.splitlines() if line]
    for item in items:
        validate_path_list_item(item, source=spec)
    return items


def validate_path_list_item(value: str, *, source: str) -> None:
    redacted = redact_sensitive_text(value)
    if redacted != value:
        raise AntiError(
            f"path list {source!r} contains secret-like content; refusing to use it "
            f"(offending entry, redacted: {redacted!r})"
        )


def selected_paths_from_args(args: argparse.Namespace) -> list[str]:
    paths = list(getattr(args, "file", None) or [])
    for spec in getattr(args, "files_from", None) or []:
        paths.extend(read_paths_file(spec))
    return paths


def review_rev_range(args: argparse.Namespace) -> str | None:
    explicit = getattr(args, "changed_files_range", None)
    if explicit:
        return validate_git_rev_range(explicit, source="--changed-files")
    base = getattr(args, "base", None)
    if base:
        return f"{validate_git_rev_range(base, source='--base')}...HEAD"
    return None


def changed_paths(
    root: Path,
    scope: str,
    selected: list[str],
    *,
    rev_range: str | None = None,
) -> tuple[list[str], list[str]]:
    if selected:
        return filter_paths(selected, root=root)
    if scope == "staged":
        raw = run_git(root, ["diff", "--cached", "--name-only", "--diff-filter=ACMRT"])
    elif scope == "working-tree":
        raw = run_git(root, ["diff", "HEAD", "--name-only", "--diff-filter=ACMRT"])
    elif scope == "diff":
        if not rev_range:
            raise AntiError("--scope diff requires --base or --changed-files")
        rev_range = validate_git_rev_range(rev_range, source="revision range")
        raw = run_git(root, ["diff", "--name-only", "--diff-filter=ACMRT", rev_range])
    elif scope == "files":
        raise AntiError("--scope files requires at least one --file")
    else:
        raise AntiError(f"unsupported review scope: {scope}")
    return filter_paths(raw.splitlines(), root=root)


def diff_for_paths(root: Path, scope: str, paths: list[str], *, rev_range: str | None = None) -> str:
    if not paths or scope == "files":
        return ""
    if scope == "staged":
        return run_git(root, ["diff", "--cached", "--no-ext-diff", "--", *paths], check=False)
    if scope == "diff":
        if not rev_range:
            raise AntiError("--scope diff requires --base or --changed-files")
        rev_range = validate_git_rev_range(rev_range, source="revision range")
        return run_git(root, ["diff", "--no-ext-diff", rev_range, "--", *paths], check=False)
    return run_git(root, ["diff", "HEAD", "--no-ext-diff", "--", *paths], check=False)


def file_is_tracked(root: Path, rel_path: str) -> bool:
    proc = subprocess.run(
        ["git", "ls-files", "--error-unmatch", "--", rel_path],
        cwd=root,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return proc.returncode == 0


def read_text_file(root: Path, rel_path: str) -> tuple[str, str | None]:
    path = root / rel_path
    if not path.is_file():
        return "", f"{rel_path}: not a regular file"
    raw = path.read_bytes()
    if b"\0" in raw:
        return "", f"{rel_path}: binary file skipped"
    note = None
    if len(raw) > MAX_FILE_BYTES:
        original_len = len(raw)
        raw = raw[:MAX_FILE_BYTES]
        note = f"{rel_path}: truncated to {MAX_FILE_BYTES} bytes ({original_len} original bytes)"
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        if note and exc.start >= max(0, len(raw) - 4):
            raw = raw[: exc.start]
            text = raw.decode("utf-8")
            note += "; trimmed partial UTF-8 character at truncation boundary"
        else:
            return "", f"{rel_path}: non-UTF-8 file skipped"
    return text, note


def apply_prompt_limit(prompt: str, max_prompt_chars: int, caveats: list[str]) -> str:
    if max_prompt_chars > 0 and len(prompt) > max_prompt_chars:
        caveats.append(f"Prompt truncated to {max_prompt_chars} characters")
        return prompt[:max_prompt_chars]
    return prompt


def truncate_at_line_boundary(text: str, max_chars: int) -> str:
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    truncated = text[:max_chars]
    newline = truncated.rfind("\n")
    if newline > max_chars // 2:
        return truncated[:newline]
    return truncated


def review_prompt_parts(
    *,
    scope_line: str,
    diff: str,
    included_files: list[tuple[str, str]],
    omitted_files: list[str],
    excluded: list[str],
    caveats: list[str],
) -> list[str]:
    incomplete = bool(omitted_files) or any("truncated" in caveat.lower() for caveat in caveats)
    manifest_lines = [
        "## Review Manifest",
        f"- status: {'incomplete' if incomplete else 'complete'}",
        f"- scope: {scope_line}",
        f"- included_files: {', '.join(path for path, _text in included_files) if included_files else 'none'}",
        f"- omitted_files: {', '.join(omitted_files) if omitted_files else 'none'}",
        f"- excluded_paths: {', '.join(excluded[:20]) if excluded else 'none'}",
    ]
    if caveats:
        manifest_lines.append("- helper_warnings:")
        manifest_lines.extend(f"  - {caveat}" for caveat in caveats)
    else:
        manifest_lines.append("- helper_warnings: none")

    parts = [
        "You are an Antigravity sidecar reviewer for a Codex coding session.",
        "Review independently. Lead with concrete defects, regressions, security risks, install/usability problems, or missing tests. Avoid speculative style comments.",
        "Use file paths and precise behavior references when possible. If you find no issues, say so and list residual verification caveats.",
        "Treat the Review Manifest as authoritative. Helper warnings, omitted files, and partial diffs are scope caveats, not source-code defects.",
        "\n".join(manifest_lines),
    ]
    if diff.strip():
        parts.append("## Git Diff\n```diff\n" + diff + "\n```")
    if included_files:
        blocks = [f"### {rel}\n```text\n{text}\n```" for rel, text in included_files]
        parts.append("## File Contents\n" + "\n\n".join(blocks))
    if not diff.strip() and not included_files:
        parts.append("No diff or file content was available in the requested scope. Explain that limitation.")
    return parts


def build_review_prompt(
    *,
    scope_line: str,
    diff: str,
    file_texts: list[tuple[str, str]],
    excluded: list[str],
    initial_caveats: list[str],
    max_prompt_chars: int,
) -> tuple[str, list[str], dict[str, Any]]:
    caveats = list(initial_caveats)
    diff_for_prompt = diff
    omitted_files = [rel for rel, text in file_texts if not text]
    candidates = [(rel, text) for rel, text in file_texts if text]
    included: list[tuple[str, str]] = []

    if max_prompt_chars > 0 and diff_for_prompt:
        prompt_without_files = "\n\n".join(
            review_prompt_parts(
                scope_line=scope_line,
                diff=diff_for_prompt,
                included_files=[],
                omitted_files=[rel for rel, _text in candidates],
                excluded=excluded,
                caveats=caveats,
            )
        )
        if len(prompt_without_files) > max_prompt_chars:
            base_parts = review_prompt_parts(
                scope_line=scope_line,
                diff="",
                included_files=[],
                omitted_files=[rel for rel, _text in candidates],
                excluded=excluded,
                caveats=caveats,
            )
            base_len = len("\n\n".join(base_parts))
            available = max(0, max_prompt_chars - base_len - len("\n\n## Git Diff\n```diff\n\n```"))
            diff_for_prompt = truncate_at_line_boundary(diff_for_prompt, available)
            caveats.append(
                f"Git diff truncated to fit max prompt budget ({len(diff)} original chars, {len(diff_for_prompt)} included)"
            )

    for index, (rel, text) in enumerate(candidates):
        trial_included = [*included, (rel, text)]
        trial_omitted = [item_rel for item_rel, _item_text in candidates[index + 1 :]]
        trial_omitted.extend(omitted_files)
        trial_prompt = "\n\n".join(
            review_prompt_parts(
                scope_line=scope_line,
                diff=diff_for_prompt,
                included_files=trial_included,
                omitted_files=trial_omitted,
                excluded=excluded,
                caveats=caveats,
            )
        )
        if max_prompt_chars <= 0 or len(trial_prompt) <= max_prompt_chars:
            included = trial_included
        else:
            omitted_files.append(f"{rel} (omitted to keep whole-file prompt under {max_prompt_chars} chars)")

    prompt = "\n\n".join(
        review_prompt_parts(
            scope_line=scope_line,
            diff=diff_for_prompt,
            included_files=included,
            omitted_files=omitted_files,
            excluded=excluded,
            caveats=caveats,
        )
    )
    metadata = {
        "status": "incomplete" if omitted_files or any("truncated" in item.lower() for item in caveats) else "complete",
        "prompt_chars": len(prompt),
        "diff_chars": len(diff_for_prompt),
        "diff_truncated": diff_for_prompt != diff,
        "included_files": [rel for rel, _text in included],
        "omitted_files": omitted_files,
        "excluded_paths": excluded,
        "helper_warnings": caveats,
    }
    return prompt, caveats, metadata


def collect_review_context(args: argparse.Namespace) -> dict[str, Any]:
    root = find_repo_root(Path.cwd())
    if root is None:
        if args.scope != "files":
            raise AntiError("review requires a git repository unless --scope files is used")
        root = Path.cwd().resolve()

    selected = selected_paths_from_args(args)
    rev_range = review_rev_range(args)
    paths, excluded = changed_paths(root, args.scope, selected, rev_range=rev_range)
    diff = diff_for_paths(root, args.scope, paths, rev_range=rev_range)
    notes: list[str] = []
    file_texts: list[tuple[str, str]] = []

    include_file_text = args.scope == "files"
    for rel in paths:
        if include_file_text or not file_is_tracked(root, rel):
            text, note = read_text_file(root, rel)
            if note:
                notes.append(note)
            if text:
                file_texts.append((rel, text))
            else:
                file_texts.append((rel, ""))

    scope_line = args.scope
    if rev_range:
        scope_line += f" ({rev_range})"
    if paths:
        scope_line += " over " + ", ".join(paths[:20])
        if len(paths) > 20:
            scope_line += f", ... ({len(paths)} files total)"

    caveats: list[str] = []
    if excluded:
        caveats.append("Excluded sensitive/cache/binary-looking paths: " + ", ".join(excluded[:20]))
    if notes:
        caveats.extend(notes)
    return {
        "root": root,
        "paths": paths,
        "excluded": excluded,
        "diff": diff,
        "file_texts": file_texts,
        "scope_line": scope_line,
        "caveats": caveats,
    }


def assemble_review_prompt_from_context(
    context: dict[str, Any],
    *,
    max_prompt_chars: int,
) -> tuple[str, list[str], list[str], dict[str, Any]]:
    prompt, caveats, metadata = build_review_prompt(
        scope_line=context["scope_line"],
        diff=context["diff"],
        file_texts=context["file_texts"],
        excluded=context["excluded"],
        initial_caveats=context["caveats"],
        max_prompt_chars=max_prompt_chars,
    )
    return prompt, context["paths"], caveats, metadata


def assemble_review_prompt(args: argparse.Namespace) -> tuple[str, list[str], list[str], dict[str, Any]]:
    context = collect_review_context(args)
    return assemble_review_prompt_from_context(context, max_prompt_chars=args.max_prompt_chars)


def split_text_by_budget(text: str, max_chars: int) -> list[str]:
    if max_chars <= 0 or len(text) <= max_chars:
        return [text]
    chunks: list[str] = []
    rest = text
    while rest:
        if len(rest) <= max_chars:
            chunks.append(rest)
            break
        piece = truncate_at_line_boundary(rest, max_chars)
        if not piece:
            piece = rest[:max_chars]
        chunks.append(piece)
        rest = rest[len(piece) :].lstrip("\n")
    return chunks


def prompt_fits(prompt: str, max_prompt_chars: int) -> bool:
    return max_prompt_chars <= 0 or len(prompt) <= max_prompt_chars


def build_review_chunk_prompts(
    context: dict[str, Any],
    *,
    max_prompt_chars: int,
    max_chunks: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    chunk_budget = max(1200, max_prompt_chars - 1800) if max_prompt_chars > 0 else 0
    chunks: list[dict[str, Any]] = []
    omitted_items: list[str] = []

    def add_chunk(kind: str, label: str, prompt: str, metadata: dict[str, Any]) -> None:
        if len(chunks) >= max_chunks:
            omitted_items.append(label)
            return
        chunks.append(
            {
                "kind": kind,
                "label": label,
                "prompt": prompt,
                "metadata": metadata,
                "prompt_chars": len(prompt),
            }
        )

    diff = str(context["diff"])
    if diff.strip():
        diff_parts = split_text_by_budget(diff, chunk_budget)
        for index, diff_part in enumerate(diff_parts, start=1):
            label = f"diff part {index}/{len(diff_parts)}"
            scope_line = f"{context['scope_line']} ({label})"
            prompt, caveats, metadata = build_review_prompt(
                scope_line=scope_line,
                diff=diff_part,
                file_texts=[],
                excluded=context["excluded"],
                initial_caveats=[
                    *context["caveats"],
                    f"Chunked review: {label}; synthesize with other chunks before final judgment.",
                ],
                max_prompt_chars=max_prompt_chars,
            )
            metadata["chunk_kind"] = "diff"
            metadata["chunk_label"] = label
            if not prompt_fits(prompt, max_prompt_chars):
                omitted_items.append(f"{label} (prompt still exceeds {max_prompt_chars} chars)")
                continue
            add_chunk("diff", label, prompt, metadata)

    file_items: list[tuple[str, str]] = []
    for rel, text in context["file_texts"]:
        if not text:
            omitted_items.append(rel)
            continue
        whole_prompt, _whole_caveats, whole_metadata = build_review_prompt(
            scope_line=f"{context['scope_line']} ({rel})",
            diff="",
            file_texts=[(rel, text)],
            excluded=context["excluded"],
            initial_caveats=context["caveats"],
            max_prompt_chars=max_prompt_chars,
        )
        if prompt_fits(whole_prompt, max_prompt_chars) and whole_metadata.get("included_files") == [rel]:
            file_items.append((rel, text))
            continue
        text_parts = split_text_by_budget(text, chunk_budget)
        for index, text_part in enumerate(text_parts, start=1):
            label = f"{rel} part {index}/{len(text_parts)}"
            file_items.append((label, text_part))

    current: list[tuple[str, str]] = []
    for rel, text in file_items:
        trial = [*current, (rel, text)]
        prompt, caveats, metadata = build_review_prompt(
            scope_line=f"{context['scope_line']} (file chunk)",
            diff="",
            file_texts=trial,
            excluded=context["excluded"],
            initial_caveats=[
                *context["caveats"],
                "Chunked review: file chunk; synthesize with other chunks before final judgment.",
            ],
            max_prompt_chars=max_prompt_chars,
        )
        if prompt_fits(prompt, max_prompt_chars) and not metadata["omitted_files"]:
            current = trial
            continue
        if current:
            current_prompt, _current_caveats, current_metadata = build_review_prompt(
                scope_line=f"{context['scope_line']} (file chunk)",
                diff="",
                file_texts=current,
                excluded=context["excluded"],
                initial_caveats=[
                    *context["caveats"],
                    "Chunked review: file chunk; synthesize with other chunks before final judgment.",
                ],
                max_prompt_chars=max_prompt_chars,
            )
            label = ", ".join(path for path, _item_text in current)
            current_metadata["chunk_kind"] = "files"
            current_metadata["chunk_label"] = label
            add_chunk("files", label, current_prompt, current_metadata)
        current = [(rel, text)]

    if current:
        current_prompt, _current_caveats, current_metadata = build_review_prompt(
            scope_line=f"{context['scope_line']} (file chunk)",
            diff="",
            file_texts=current,
            excluded=context["excluded"],
            initial_caveats=[
                *context["caveats"],
                "Chunked review: file chunk; synthesize with other chunks before final judgment.",
            ],
            max_prompt_chars=max_prompt_chars,
        )
        label = ", ".join(path for path, _item_text in current)
        current_metadata["chunk_kind"] = "files"
        current_metadata["chunk_label"] = label
        if prompt_fits(current_prompt, max_prompt_chars):
            add_chunk("files", label, current_prompt, current_metadata)
        else:
            omitted_items.append(f"{label} (prompt still exceeds {max_prompt_chars} chars)")

    metadata = chunk_manifest(chunks, omitted_items, max_chunks=max_chunks)
    return chunks, metadata


def build_chunk_synthesis_prompt(
    *,
    context: dict[str, Any],
    chunks: list[dict[str, Any]],
    chunk_outputs: list[str],
    chunk_metadata: dict[str, Any],
    max_chars: int,
) -> tuple[str, list[str], dict[str, Any]]:
    manifest = {
        "scope": context["scope_line"],
        "chunk_count": len(chunks),
        "included_files": chunk_metadata.get("included_files", []),
        "included_items": chunk_metadata.get("included_items", []),
        "omitted_items": chunk_metadata.get("omitted_items", []),
        "chunk_labels": [chunk["label"] for chunk in chunks],
        "status": chunk_metadata.get("status", "complete"),
    }

    def render(outputs: list[str]) -> str:
        chunk_sections = []
        for index, (chunk, output) in enumerate(zip(chunks, outputs), start=1):
            chunk_sections.append(
                "\n".join(
                    [
                        f"## Chunk {index}: {chunk['label']}",
                        f"- kind: {chunk['kind']}",
                        f"- prompt_chars: {chunk['prompt_chars']}",
                        output.strip(),
                    ]
                )
            )
        return "\n\n".join(
            [
                "You are synthesizing an Antigravity sidecar code review that was split into multiple bounded chunks.",
                "Use only the chunk findings below. Separate confirmed defects from risks and scope caveats. Do not invent findings for omitted items.",
                "If chunks disagree or a finding depends on omitted context, mark it as needing local verification.",
                "## Chunked Review Manifest\n```json\n" + json.dumps(manifest, indent=2, sort_keys=True) + "\n```",
                *chunk_sections,
            ]
        )

    outputs = [output.strip() for output in chunk_outputs]
    prompt = render(outputs)
    original_len = len(prompt)
    caveats: list[str] = []
    metadata: dict[str, Any] = {
        "synthesis_prompt_original_chars": original_len,
        "synthesis_truncated_outputs": [],
    }
    if max_chars <= 0 or len(prompt) <= max_chars or not outputs:
        metadata["synthesis_prompt_chars"] = len(prompt)
        return prompt, caveats, metadata

    marker = "\n[Chunk output truncated by helper to keep synthesis prompt bounded.]"
    empty_prompt_len = len(render([""] * len(outputs)))
    available_for_outputs = max_chars - empty_prompt_len - (len(marker) * len(outputs))
    truncated_labels: list[str] = []

    if available_for_outputs <= 0:
        limited_outputs = [marker.strip() for _output in outputs]
        truncated_labels = [chunk["label"] for chunk in chunks]
    else:
        per_output_budget = max(1, available_for_outputs // len(outputs))
        limited_outputs = []
        for chunk, output in zip(chunks, outputs):
            if len(output) <= per_output_budget:
                limited_outputs.append(output)
                continue
            cut = truncate_at_line_boundary(output, per_output_budget)
            if len(cut) > per_output_budget:
                cut = cut[:per_output_budget]
            limited_outputs.append((cut + marker).strip() if cut else marker.strip())
            truncated_labels.append(chunk["label"])

    prompt = render(limited_outputs)
    if len(prompt) > max_chars:
        prompt = truncate_at_line_boundary(prompt, max_chars)
        if len(prompt) > max_chars:
            prompt = prompt[:max_chars]
        if not truncated_labels:
            truncated_labels = [chunk["label"] for chunk in chunks]

    caveats.append(
        f"Synthesis chunk outputs truncated to keep prompt under {max_chars} characters "
        f"({original_len} original chars)"
    )
    metadata["synthesis_prompt_chars"] = len(prompt)
    metadata["synthesis_truncated_outputs"] = truncated_labels
    return prompt, caveats, metadata


def should_run_chunked_review(args: argparse.Namespace, metadata: dict[str, Any]) -> bool:
    mode = getattr(args, "chunked", "auto")
    if mode == "off":
        return False
    if mode == "always":
        return True
    return metadata.get("status") == "incomplete" or bool(metadata.get("omitted_files")) or bool(
        metadata.get("diff_truncated")
    )


def run_chunked_review(
    *,
    args: argparse.Namespace,
    context: dict[str, Any],
    model: str,
    base_metadata: dict[str, Any],
    max_prompt_chars: int,
) -> tuple[str, list[str], dict[str, Any]]:
    chunks, chunk_metadata = build_review_chunk_prompts(
        context,
        max_prompt_chars=max_prompt_chars,
        max_chunks=args.max_review_chunks,
    )
    if not chunks:
        raise AntiError("chunked review produced no reviewable chunks; narrow the file set or raise --max-prompt-chars")

    chunk_outputs: list[str] = []
    chunk_generation: list[dict[str, Any]] = []
    execution_ledger: list[dict[str, Any]] = []
    for index, chunk in enumerate(chunks, start=1):
        chunk_text, chunk_model, generation_metadata = generate_with_fallback(
            args,
            model=model,
            prompt=chunk["prompt"],
            max_output_tokens=args.chunk_output_tokens,
            purpose=f"review chunk {index}/{len(chunks)}",
        )
        chunk_outputs.append(chunk_text)
        chunk_generation.append({"index": index, "model_used": chunk_model, **generation_metadata})
        execution_ledger.append(
            execution_entry(
                stage=f"review_chunk_{index}",
                prompt=chunk["prompt"],
                output=chunk_text,
                model=chunk_model,
                generation=generation_metadata,
            )
        )

    synthesis_prompt, synthesis_caveats, synthesis_metadata = build_chunk_synthesis_prompt(
        context=context,
        chunks=chunks,
        chunk_outputs=chunk_outputs,
        chunk_metadata=chunk_metadata,
        max_chars=args.max_synthesis_chars,
    )
    caveats = list(context["caveats"])
    caveats.extend(synthesis_caveats)
    synthesis, synthesis_model, synthesis_generation = generate_with_fallback(
        args,
        model=model,
        prompt=synthesis_prompt,
        max_output_tokens=args.max_output_tokens,
        purpose="review synthesis",
    )
    execution_ledger.append(
        execution_entry(
            stage="review_synthesis",
            prompt=synthesis_prompt,
            output=synthesis,
            model=synthesis_model,
            generation=synthesis_generation,
        )
    )
    if chunk_metadata["omitted_items"]:
        caveats.append("Chunked review omitted items: " + ", ".join(chunk_metadata["omitted_items"][:20]))
    metadata = {
        **base_metadata,
        "status": "incomplete" if chunk_metadata["omitted_items"] else "complete",
        "chunked": True,
        "single_prompt_status": base_metadata.get("status"),
        "single_prompt_omitted_files": base_metadata.get("omitted_files", []),
        "omitted_files": chunk_metadata["omitted_items"],
        "chunk_count": len(chunks),
        "chunk_prompts": [
            {
                "index": index,
                "kind": chunk["kind"],
                "label": chunk["label"],
                "prompt_chars": chunk["prompt_chars"],
                "model_used": chunk_generation[index - 1]["model_used"],
            }
            for index, chunk in enumerate(chunks, start=1)
        ],
        "chunk_generation": chunk_generation,
        "synthesis_model_used": synthesis_model,
        "synthesis_generation": synthesis_generation,
        "chunk_omitted_items": chunk_metadata["omitted_items"],
        "included_files": chunk_metadata["included_files"],
        "included_items": chunk_metadata["included_items"],
        "prompt_budget_chars": max_prompt_chars,
        **synthesis_metadata,
        "_execution_ledger": execution_ledger,
    }
    return synthesis, caveats, metadata


def assemble_plan_prompt(args: argparse.Namespace, *, apply_limit: bool = True) -> tuple[str, list[str]]:
    user_goal = read_prompt(args)
    context = ""
    caveats: list[str] = []

    if args.scope != "none":
        root = find_repo_root(Path.cwd())
        if root is None:
            if args.scope != "files":
                raise AntiError("plan context requires a git repository unless --scope files is used")
            root = Path.cwd().resolve()

        paths, excluded = changed_paths(root, args.scope, args.file or [])
        diff = diff_for_paths(root, args.scope, paths)
        notes: list[str] = []
        file_blocks: list[str] = []
        include_file_text = args.scope == "files"

        for rel in paths:
            if include_file_text or not file_is_tracked(root, rel):
                text, note = read_text_file(root, rel)
                if note:
                    notes.append(note)
                if text:
                    file_blocks.append(f"### {rel}\n```text\n{text}\n```")

        scope_line = args.scope
        if paths:
            scope_line += " over " + ", ".join(paths[:20])
            if len(paths) > 20:
                scope_line += f", ... ({len(paths)} files total)"

        context_parts = [f"Planning context scope: {scope_line}."]
        if diff.strip():
            context_parts.append("## Git Diff\n```diff\n" + diff + "\n```")
        if file_blocks:
            context_parts.append("## File Contents\n" + "\n\n".join(file_blocks))
        if not diff.strip() and not file_blocks:
            context_parts.append("No diff or file content was available in the requested scope.")
        context = "\n\n".join(context_parts)

        if excluded:
            caveats.append("Excluded sensitive/cache/binary-looking paths: " + ", ".join(excluded[:20]))
        caveats.extend(notes)

    prompt = "\n".join(
        part
        for part in [
            "You are Claude Opus acting as an Antigravity deep-work planning lane for a Codex coding session.",
            "Produce a decision-complete plan for a long autonomous engineering session. Optimize for correctness, sequencing, verification, and keeping the main Codex agent unblocked.",
            "The plan must be executable by another senior agent without needing to make major decisions. Include: goal framing, phase order, task decomposition, critical path, parallelizable work, risks, checkpoints, validation commands, rollback/stop conditions, and explicit non-claims.",
            "Prefer concrete actions over generic advice. If repository context is incomplete, say exactly what is missing and how to gather it before execution.",
            f"User goal:\n{user_goal}",
            context,
        ]
        if part
    )

    if apply_limit:
        prompt = apply_prompt_limit(prompt, args.max_prompt_chars, caveats)
    elif args.max_prompt_chars > 0 and len(prompt) > args.max_prompt_chars:
        caveats.append(
            f"Plan prompt exceeds {args.max_prompt_chars} characters and will be split before generation "
            f"({len(prompt)} original chars)"
        )
    return prompt, caveats


def should_chunk_plan(args: argparse.Namespace, prompt: str, *, max_prompt_chars: int) -> bool:
    mode = getattr(args, "chunked", "auto")
    if mode == "off":
        return False
    if mode == "always":
        return True
    return max_prompt_chars > 0 and len(prompt) > max_prompt_chars


def run_chunked_plan(
    *,
    args: argparse.Namespace,
    model: str,
    prompt: str,
    caveats: list[str],
    max_prompt_chars: int,
) -> tuple[str, list[str], dict[str, Any], str]:
    chunk_wrapper_overhead = len(
        "\n\n".join(
            [
                "You are reviewing one bounded chunk of a larger Codex work-planning prompt.",
                "Extract concrete implementation tasks, risks, dependencies, validation ideas, and caveats from this chunk only.",
                f"Chunk {args.max_plan_chunks}/{args.max_plan_chunks}:",
                "",
            ]
        )
    )
    chunk_budget = max(1, max_prompt_chars - chunk_wrapper_overhead) if max_prompt_chars > 0 else len(prompt)
    prompt_chunks = split_text_by_budget(prompt, chunk_budget)
    if len(prompt_chunks) > args.max_plan_chunks:
        caveats.append(
            f"Plan prompt split into {len(prompt_chunks)} chunks but capped at {args.max_plan_chunks}; "
            "remaining chunks omitted"
        )
        prompt_chunks = prompt_chunks[: args.max_plan_chunks]
    if not prompt_chunks:
        raise AntiError("plan chunking produced no prompt chunks")

    chunk_outputs: list[str] = []
    chunk_generation: list[dict[str, Any]] = []
    sent_chunk_prompt_chars: list[int] = []
    execution_ledger: list[dict[str, Any]] = []
    for index, chunk in enumerate(prompt_chunks, start=1):
        chunk_prompt = "\n\n".join(
            [
                "You are reviewing one bounded chunk of a larger Codex work-planning prompt.",
                "Extract concrete implementation tasks, risks, dependencies, validation ideas, and caveats from this chunk only.",
                f"Chunk {index}/{len(prompt_chunks)}:",
                chunk,
            ]
        )
        chunk_caveats: list[str] = []
        chunk_prompt = apply_prompt_limit(chunk_prompt, max_prompt_chars, chunk_caveats)
        if chunk_caveats:
            caveats.extend(f"Plan chunk {index}: {caveat}" for caveat in chunk_caveats)
        sent_chunk_prompt_chars.append(len(chunk_prompt))
        text, model_used, generation_metadata = generate_with_fallback(
            args,
            model=model,
            prompt=chunk_prompt,
            max_output_tokens=args.chunk_output_tokens,
            purpose=f"plan chunk {index}/{len(prompt_chunks)}",
        )
        chunk_outputs.append(text)
        chunk_generation.append({"index": index, "model_used": model_used, **generation_metadata})
        execution_ledger.append(
            execution_entry(
                stage=f"plan_chunk_{index}",
                prompt=chunk_prompt,
                output=text,
                model=model_used,
                generation=generation_metadata,
            )
        )

    synthesis_prompt = "\n\n".join(
        [
            "You are synthesizing a decision-complete autonomous work plan from bounded planning chunks.",
            "Use only the chunk outputs below. Keep explicit caveats and do not claim local verification.",
            "Return a concise, executable plan with phases, critical path, validation commands, stop conditions, and non-claims.",
            "## Chunk Outputs",
            "\n\n".join(
                f"### Chunk {index}\n{output.strip()}" for index, output in enumerate(chunk_outputs, start=1)
            ),
        ]
    )
    synthesis_caveats: list[str] = []
    if args.max_synthesis_chars > 0 and len(synthesis_prompt) > args.max_synthesis_chars:
        synthesis_prompt = truncate_at_line_boundary(synthesis_prompt, args.max_synthesis_chars)
        if len(synthesis_prompt) > args.max_synthesis_chars:
            synthesis_prompt = synthesis_prompt[: args.max_synthesis_chars]
        synthesis_caveats.append(f"Plan synthesis prompt truncated to {args.max_synthesis_chars} characters")
    caveats = [*caveats, *synthesis_caveats]
    text, synthesis_model, synthesis_generation = generate_with_fallback(
        args,
        model=model,
        prompt=synthesis_prompt,
        max_output_tokens=args.max_output_tokens,
        purpose="plan synthesis",
    )
    execution_ledger.append(
        execution_entry(
            stage="plan_synthesis",
            prompt=synthesis_prompt,
            output=text,
            model=synthesis_model,
            generation=synthesis_generation,
        )
    )
    metadata = {
        "prompt_chars": len(prompt),
        "chunked": True,
        "chunk_count": len(prompt_chunks),
        "chunk_prompt_chars": [len(chunk) for chunk in prompt_chunks],
        "sent_chunk_prompt_chars": sent_chunk_prompt_chars,
        "chunk_generation": chunk_generation,
        "synthesis_prompt_chars": len(synthesis_prompt),
        "synthesis_model_used": synthesis_model,
        "synthesis_generation": synthesis_generation,
        "prompt_budget_chars": max_prompt_chars,
        "_execution_ledger": execution_ledger,
    }
    return text, caveats, metadata, synthesis_model


def read_prompt(args: argparse.Namespace) -> str:
    pieces: list[str] = []
    if args.prompt_file:
        path = Path(args.prompt_file).expanduser()
        raw = path.read_bytes()
        if b"\0" in raw:
            raise AntiError("prompt file looks binary")
        pieces.append(raw.decode("utf-8"))
    if args.prompt:
        pieces.append(args.prompt)
    if getattr(args, "prompt_parts", None):
        pieces.append(" ".join(args.prompt_parts))
    prompt = ordered_prompt(pieces)
    if not prompt:
        raise AntiError("provide --prompt, --prompt-file, or a positional prompt")
    return prompt


def read_optional_prompt(args: argparse.Namespace) -> str:
    if args.prompt_file or args.prompt or getattr(args, "prompt_parts", None):
        return read_prompt(args)
    return ""


def print_result(
    *,
    mode: str,
    model: str,
    base_url: str,
    text: str,
    caveats: list[str] | None = None,
    output_json: bool = False,
    metadata: dict[str, Any] | None = None,
) -> None:
    safe_gateway = redact_sensitive_text(base_url)
    model = redact_sensitive_text(model)
    text, caveats, metadata = presentable_result(
        text=text,
        caveats=caveats or [],
        metadata=metadata or {},
        sanitizer=sanitize_json,
    )
    if output_json:
        print(
            json.dumps(
                {
                    "mode": mode,
                    "model": model,
                    "gateway": safe_gateway,
                    "caveats": caveats,
                    "metadata": metadata,
                    "output_text": text.strip(),
                },
                indent=2,
                sort_keys=True,
            )
        )
        return
    print(f"## Antigravity {mode} ({model})")
    print(f"- Gateway: {safe_gateway}")
    if metadata.get("status"):
        print(f"- Status: {metadata['status']}")
    if caveats:
        for caveat in caveats:
            print(f"- Caveat: {caveat}")
    print()
    print(text.strip())


def find_cli() -> tuple[list[str], Path | None]:
    found = shutil.which("codex-antigravity")
    if found:
        return [found], None
    start = Path.cwd().resolve()
    for candidate in [start, *start.parents]:
        if (candidate / "codex_antigravity_auth" / "cli.py").exists():
            return [sys.executable, "-m", "codex_antigravity_auth.cli"], candidate
    raise AntiError("codex-antigravity CLI was not found on PATH and no source checkout was found above cwd")


def run_cli(args: list[str]) -> int:
    cmd, cwd = find_cli()
    proc = subprocess.run([*cmd, *args], cwd=cwd)
    return int(proc.returncode)


def run_cli_quiet(args: list[str]) -> int:
    cmd, cwd = find_cli()
    proc = subprocess.run(
        [*cmd, *args],
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return int(proc.returncode)


def check_gateway(base_url: str, *, timeout: float, token_env: str) -> bool:
    try:
        fetch_model_ids(base_url, timeout=timeout, token_env=token_env)
        return True
    except AntiError:
        return False


def normalize_collab_profile(value: str | None) -> str:
    profile = (value or "none").strip().lower()
    if profile not in COLLAB_PROFILES:
        raise AntiError(f"unsupported collaboration profile: {value}")
    return profile


def default_panel_models_for_collab(profile: str) -> list[str]:
    if normalize_collab_profile(profile) == "claude-grok":
        return list(CLAUDE_GROK_PANEL_MODELS)
    return list(DEFAULT_PANEL_MODELS)


def resolve_panel_models(values: list[str] | None, *, collab_profile: str | None = None) -> list[str]:
    profile = normalize_collab_profile(collab_profile)
    raw_values = values or default_panel_models_for_collab(profile)
    resolved: list[str] = []
    seen: set[str] = set()
    for value in raw_values:
        model = resolve_model(value, default=value)
        if model in seen:
            continue
        seen.add(model)
        resolved.append(model)
    if not resolved:
        raise AntiError("panel requires at least one model")
    return resolved


def ensure_models_available(
    *,
    base_url: str,
    models: list[str],
    timeout: float,
    token_env: str,
) -> set[str]:
    model_ids = fetch_model_ids(base_url, timeout=timeout, token_env=token_env)
    missing = [model for model in models if model not in model_ids]
    if missing:
        sample = ", ".join(sorted(model_ids)[:12])
        raise AntiError(
            "model(s) not advertised by /v1/models: "
            + ", ".join(missing)
            + f". Available sample: {sample}"
        )
    return model_ids


def panel_role_instruction(roles: list[str] | None) -> str:
    if not roles:
        return ""
    clean_roles = []
    for role in roles:
        role = role.strip()
        if role:
            clean_roles.append(role)
    if not clean_roles:
        return ""
    return (
        "Panel role lenses requested: "
        + ", ".join(clean_roles)
        + ". Apply these as review/planning perspectives, but do not invent findings just to fill a role."
    )


def model_is_byok(model: str) -> bool:
    provider, separator, provider_model = model.partition(":")
    return bool(separator and provider and provider_model)


def panel_receives_repo_context(args: argparse.Namespace) -> bool:
    if args.mode == "review":
        return args.scope != "none"
    if args.mode == "plan":
        return args.scope not in {"none", "prompt"}
    return False


def byok_repo_context_disclosure(panel_models: list[str], judge_model: str, args: argparse.Namespace) -> str | None:
    if not panel_receives_repo_context(args):
        return None
    provider_models = [model for model in [*panel_models, judge_model] if model_is_byok(model)]
    if not provider_models:
        return None
    return (
        "BYOK disclosure: repository/diff/file context will be sent to provider lane(s): "
        + ", ".join(provider_models)
        + ". Only use BYOK lanes you trust for this code."
    )


def gpt_complement_instruction() -> str:
    return (
        "GPT-complement lens: prioritize observations, failure modes, ambiguity, and verification hints "
        "that a GPT-family acting agent might plausibly miss. Do not speculate beyond the supplied context."
    )


def panel_collaboration_instruction(profile: str, panel_models: list[str]) -> str:
    if normalize_collab_profile(profile) != "claude-grok":
        return ""
    return "\n".join(
        [
            "Claude + Grok collaboration profile: use these lanes as complementary reviewers, not as a vote.",
            "Claude-family lanes should emphasize codebase reasoning, long-context consistency, API/protocol contracts, and implementation risk.",
            "Grok/xAI lanes should stress-test assumptions, challenge likely blind spots, look for runtime/user-workflow surprises, and propose discriminating checks.",
            "All lanes must cite concrete evidence from the supplied context and turn disagreements into local verification steps.",
            "Requested collaboration lanes: " + ", ".join(panel_models) + ".",
        ]
    )


def clean_string(value: Any, *, max_chars: int = 1200) -> str:
    if value is None:
        return ""
    text = redact_sensitive_text(str(value)).strip()
    if len(text) > max_chars:
        return text[:max_chars].rstrip() + "..."
    return text


def clean_string_list(value: Any, *, max_items: int = 20, max_chars: int = 500) -> list[str]:
    if isinstance(value, str):
        items: list[Any] = [value]
    elif isinstance(value, list):
        items = value
    else:
        return []
    result: list[str] = []
    for item in items[:max_items]:
        text = clean_string(item, max_chars=max_chars)
        if text:
            result.append(text)
    return result


def extract_json_object(text: str) -> Any:
    stripped = text.strip()
    if not stripped:
        raise ValueError("empty output")
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", stripped, flags=re.IGNORECASE | re.DOTALL)
    if fenced:
        return json.loads(fenced.group(1))
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start >= 0 and end > start:
            return json.loads(stripped[start : end + 1])
        raise


def normalize_finding_item(value: Any, index: int) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    claim = clean_string(value.get("claim"), max_chars=1600)
    verify = clean_string(value.get("verify"), max_chars=1200)
    if not claim or not verify:
        return None
    severity = clean_string(value.get("severity"), max_chars=40).lower()
    if severity not in {"critical", "high", "medium", "low", "info"}:
        severity = "medium"
    finding_id = clean_string(value.get("id"), max_chars=80) or f"F{index:03d}"
    finding_id = re.sub(r"[^A-Za-z0-9_.:-]+", "-", finding_id).strip("-._:") or f"F{index:03d}"
    lanes = clean_string_list(value.get("lanes"), max_items=12, max_chars=120)
    return {
        "id": finding_id,
        "claim": claim,
        "severity": severity,
        "lanes": lanes,
        "verify": verify,
    }


def parse_panel_findings(text: str) -> tuple[dict[str, Any] | None, str | None]:
    try:
        parsed = extract_json_object(text)
    except Exception as exc:
        return None, f"Judge did not return valid structured findings JSON; falling back to prose synthesis ({exc})"
    if not isinstance(parsed, dict):
        return None, "Judge structured findings output was not a JSON object; falling back to prose synthesis"

    raw_findings = parsed.get("findings")
    if not isinstance(raw_findings, list):
        return None, "Judge structured findings JSON did not contain a findings list; falling back to prose synthesis"
    findings = [
        normalized
        for index, item in enumerate(raw_findings, start=1)
        if (normalized := normalize_finding_item(item, index)) is not None
    ]
    contract = {
        "summary": clean_string(parsed.get("summary"), max_chars=1600),
        "disagreements": clean_string_list(parsed.get("disagreements"), max_items=20, max_chars=700),
        "findings": findings,
        "unverifiable": clean_string_list(
            parsed.get("unverifiable") or parsed.get("unverifiable_observations"),
            max_items=20,
            max_chars=700,
        ),
        "recommended_next_actions": clean_string_list(parsed.get("recommended_next_actions"), max_items=20, max_chars=700),
        "caveats": clean_string_list(parsed.get("caveats") or parsed.get("verification_caveats"), max_items=20, max_chars=700),
    }
    return sanitize_json(contract), None


def fallback_findings_contract(text: str, caveats: list[str]) -> dict[str, Any]:
    return sanitize_json(
        {
            "summary": clean_string(text, max_chars=4000),
            "disagreements": [],
            "findings": [],
            "unverifiable": [],
            "recommended_next_actions": [],
            "caveats": caveats,
        }
    )


def prompt_budget_for_panel_source(args: argparse.Namespace, panel_models: list[str]) -> int:
    for model in panel_models:
        if is_claude_model(model):
            return prompt_budget_for_model(args, model)
    return int(getattr(args, "max_prompt_chars", DEFAULT_MAX_PROMPT_CHARS))


def render_panel_findings(findings: dict[str, Any], caveats: list[str]) -> str:
    sections: list[str] = []
    if findings.get("summary"):
        sections.append(str(findings["summary"]).strip())

    disagreements = clean_string_list(findings.get("disagreements"), max_items=50, max_chars=1000)
    sections.append("## Disagreements")
    sections.append("\n".join(f"- {item}" for item in disagreements) if disagreements else "- None surfaced.")

    sections.append("## Findings")
    finding_lines: list[str] = []
    finding_items = findings.get("findings", []) if isinstance(findings.get("findings"), list) else []
    for item in finding_items:
        if not isinstance(item, dict):
            continue
        lanes = ", ".join(clean_string_list(item.get("lanes"), max_items=20, max_chars=160)) or "unspecified"
        finding_lines.extend(
            [
                f"- [{clean_string(item.get('severity'), max_chars=40) or 'medium'}] {clean_string(item.get('id'), max_chars=80)}: {clean_string(item.get('claim'), max_chars=1600)}",
                f"  Lanes: {lanes}",
                f"  Verify: {clean_string(item.get('verify'), max_chars=1200)}",
            ]
        )
    sections.append("\n".join(finding_lines) if finding_lines else "- No structured findings surfaced.")

    unverifiable = clean_string_list(findings.get("unverifiable"), max_items=50, max_chars=1000)
    sections.append("## Unverifiable Observations")
    sections.append("\n".join(f"- {item}" for item in unverifiable) if unverifiable else "- None surfaced.")

    actions = clean_string_list(findings.get("recommended_next_actions"), max_items=50, max_chars=1000)
    if actions:
        sections.append("## Recommended Next Actions")
        sections.append("\n".join(f"- {item}" for item in actions))

    rendered_caveats = clean_string_list([*(findings.get("caveats") or []), *caveats], max_items=80, max_chars=1000)
    sections.append("## Caveats")
    sections.append("\n".join(f"- {item}" for item in rendered_caveats) if rendered_caveats else "- None.")
    return "\n\n".join(sections).strip()


def assemble_panel_source_prompt(args: argparse.Namespace) -> tuple[str, list[str], dict[str, Any]]:
    caveats: list[str] = []
    collab_profile = normalize_collab_profile(getattr(args, "collab", "none"))
    resolved_panel_models = list(getattr(args, "resolved_panel_models", []) or [])
    metadata: dict[str, Any] = {"panel_mode": args.mode, "roles": args.role or []}
    if collab_profile != "none":
        metadata["collaboration_profile"] = collab_profile

    if args.mode == "review":
        if args.scope == "none":
            raise AntiError("panel review requires --scope working-tree, staged, files, or diff")
        prompt_budget = prompt_budget_for_panel_source(args, resolved_panel_models)
        claude_guardrail_available = any(
            claude_guardrail_would_apply(args, model, prompt_budget) for model in resolved_panel_models
        )
        context = collect_review_context(args)
        prompt, _paths, caveats, review_metadata = assemble_review_prompt_from_context(
            context,
            max_prompt_chars=prompt_budget,
        )
        extra_prompt = read_optional_prompt(args)
        if extra_prompt:
            prompt = "\n\n".join(["Additional review instructions:\n" + extra_prompt, prompt])
            prompt = apply_prompt_limit(prompt, prompt_budget, caveats)
            review_metadata["additional_prompt_chars"] = len(extra_prompt)
        claude_guardrail_used = claude_guardrail_available and should_run_chunked_review(args, review_metadata)
        if claude_guardrail_used:
            add_claude_guardrail_caveat(caveats, prompt_budget=prompt_budget)
            context["caveats"] = list(caveats)
        metadata.update(review_metadata)
        metadata["scope"] = context["scope_line"]
        metadata["prompt_chars"] = len(prompt)
        metadata["prompt_budget_chars"] = prompt_budget
        metadata["claude_prompt_guardrail"] = claude_guardrail_used
        metadata["_review_context"] = context
    elif args.mode == "plan":
        if args.scope == "diff":
            raise AntiError("panel plan does not support --scope diff; use working-tree, staged, files, or none")
        prompt, caveats = assemble_plan_prompt(args)
        metadata["scope"] = args.scope
        metadata["prompt_chars"] = len(prompt)
    else:
        prompt = read_prompt(args)
        prompt = apply_prompt_limit(prompt, args.max_prompt_chars, caveats)
        metadata["scope"] = "prompt"
        metadata["prompt_chars"] = len(prompt)

    role_instruction = panel_role_instruction(args.role)
    if role_instruction:
        prompt = "\n\n".join([role_instruction, prompt])
        prompt = apply_prompt_limit(prompt, args.max_prompt_chars, caveats)
        metadata["prompt_chars"] = len(prompt)
    collab_instruction = panel_collaboration_instruction(collab_profile, resolved_panel_models)
    if collab_instruction:
        prompt = "\n\n".join([collab_instruction, prompt])
        prompt = apply_prompt_limit(prompt, args.max_prompt_chars, caveats)
        metadata["prompt_chars"] = len(prompt)
    prompt = "\n\n".join([gpt_complement_instruction(), prompt])
    prompt = apply_prompt_limit(prompt, args.max_prompt_chars, caveats)
    metadata["prompt_chars"] = len(prompt)

    return prompt, caveats, metadata


def build_panel_synthesis_prompt(
    *,
    panel_mode: str,
    source_prompt: str,
    panel_results: list[dict[str, Any]],
    metadata: dict[str, Any],
    caveats: list[str],
    roles: list[str],
    max_chars: int,
    output_mode: str = "prose",
) -> tuple[str, list[str], dict[str, Any]]:
    manifest = {
        "panel_mode": panel_mode,
        "roles": roles,
        "panel_models": [result["model"] for result in panel_results],
        "successful_models": [result["model"] for result in panel_results if result["status"] == "success"],
        "failed_models": [result["model"] for result in panel_results if result["status"] != "success"],
        "source_metadata": metadata,
        "source_caveats": caveats,
        "requested_output": output_mode,
    }

    def render(source: str, outputs: list[str]) -> str:
        result_sections = []
        for result, output in zip(panel_results, outputs):
            lines = [
                f"## Panel Model: {result['model']}",
                f"- status: {result['status']}",
            ]
            if result.get("model_used") and result.get("model_used") != result["model"]:
                lines.append(f"- model_used: {result['model_used']}")
            if result["status"] == "success":
                lines.append(output.strip() or "(empty output)")
            else:
                lines.append("error: " + str(result.get("error", "unknown error")))
            result_sections.append("\n".join(lines))
        return "\n\n".join(
            [
                "You are synthesizing an Antigravity multi-model advisory panel for a Codex coding session.",
                "Use only the source prompt/context and panel outputs below. Do not claim local verification, tool execution, or proof that is not present.",
                "Prioritize disagreements, contradictions, and unique insights before consensus. Consensus is only a prioritization signal, not proof.",
                (
                    "Collaboration profile claude-grok: Compare Claude-backed lanes with Grok-backed lanes. "
                    "Name meaningful agreement, contradiction, and blind spots from each family, then give local checks that can adjudicate them."
                    if metadata.get("collaboration_profile") == "claude-grok"
                    else ""
                ),
                "Return one JSON object and no surrounding prose. The object must contain: summary (string), disagreements (array of strings), findings (array of objects), unverifiable (array of strings), recommended_next_actions (array of strings), and caveats (array of strings).",
                "Each findings item must contain: id (stable short string), claim (specific claim), severity (critical|high|medium|low|info), lanes (array of model ids that support it), and verify (a concrete local check Codex should run before acting).",
                "Put speculative or externally dependent observations in unverifiable, not findings. Do not include secrets, credentials, raw account identifiers, or provider keys.",
                "## Panel Manifest\n```json\n" + json.dumps(manifest, indent=2, sort_keys=True) + "\n```",
                "## Source Prompt / Context\n" + source.strip(),
                "## Panel Results\n" + "\n\n".join(result_sections),
            ]
        )

    outputs = [str(result.get("output_text", "")).strip() for result in panel_results]
    prompt = render(source_prompt, outputs)
    original_len = len(prompt)
    synthesis_caveats: list[str] = []
    synthesis_metadata: dict[str, Any] = {
        "synthesis_prompt_original_chars": original_len,
        "synthesis_truncated_source": False,
        "synthesis_truncated_models": [],
    }
    if max_chars <= 0 or len(prompt) <= max_chars:
        synthesis_metadata["synthesis_prompt_chars"] = len(prompt)
        return prompt, synthesis_caveats, synthesis_metadata

    marker = "\n[Panel content truncated by helper to keep synthesis prompt bounded.]"
    empty_len = len(render("", ["" for _ in outputs]))
    available = max_chars - empty_len - len(marker) * (len(outputs) + 1)
    truncated_source = False
    truncated_models: list[str] = []

    if available <= 0:
        limited_source = marker.strip()
        limited_outputs = [marker.strip() for _ in outputs]
        truncated_source = bool(source_prompt.strip())
        truncated_models = [result["model"] for result in panel_results if result["status"] == "success"]
    else:
        source_budget = max(1, available // 3)
        outputs_budget = max(1, available - source_budget)
        per_output_budget = max(1, outputs_budget // max(1, len(outputs)))
        if len(source_prompt) > source_budget:
            cut = truncate_at_line_boundary(source_prompt, source_budget)
            if len(cut) > source_budget:
                cut = cut[:source_budget]
            limited_source = (cut + marker).strip() if cut else marker.strip()
            truncated_source = True
        else:
            limited_source = source_prompt

        limited_outputs = []
        for result, output in zip(panel_results, outputs):
            if result["status"] != "success" or len(output) <= per_output_budget:
                limited_outputs.append(output)
                continue
            cut = truncate_at_line_boundary(output, per_output_budget)
            if len(cut) > per_output_budget:
                cut = cut[:per_output_budget]
            limited_outputs.append((cut + marker).strip() if cut else marker.strip())
            truncated_models.append(result["model"])

    prompt = render(limited_source, limited_outputs)
    if len(prompt) > max_chars:
        prompt = truncate_at_line_boundary(prompt, max_chars)
        if len(prompt) > max_chars:
            prompt = prompt[:max_chars]
        truncated_source = True
        if not truncated_models:
            truncated_models = [result["model"] for result in panel_results if result["status"] == "success"]

    synthesis_caveats.append(
        f"Panel synthesis prompt truncated to keep it under {max_chars} characters "
        f"({original_len} original chars)"
    )
    synthesis_metadata["synthesis_prompt_chars"] = len(prompt)
    synthesis_metadata["synthesis_truncated_source"] = truncated_source
    synthesis_metadata["synthesis_truncated_models"] = truncated_models
    return prompt, synthesis_caveats, synthesis_metadata


def run_panel_call(
    *,
    args: argparse.Namespace,
    model: str,
    prompt: str,
    max_output_tokens: int,
    model_ids: set[str],
) -> dict[str, Any]:
    try:
        text, model_used, generation_metadata = generate_with_fallback(
            args,
            model=model,
            prompt=prompt,
            max_output_tokens=max_output_tokens,
            model_ids=model_ids,
            purpose=f"panel model {model}",
        )
        result: dict[str, Any] = {"model": model, "status": "success", "output_text": text.strip()}
        if model_used != model:
            result["model_used"] = model_used
        result["generation"] = generation_metadata
        if generation_metadata.get("usage"):
            result["usage"] = generation_metadata["usage"]
        if generation_metadata.get("elapsed_ms") is not None:
            result["elapsed_ms"] = generation_metadata["elapsed_ms"]
        return result
    except Exception as exc:
        return {"model": model, "status": "error", "error": redact_sensitive_text(str(exc))}


def panel_results_for_record(panel_results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    trimmed: list[dict[str, Any]] = []
    for result in panel_results:
        item = dict(result)
        output_text = item.pop("output_text", None)
        if isinstance(output_text, str) and output_text:
            item["output_preview"] = output_text[:RUN_OUTPUT_PREVIEW_CHARS]
            item["output_chars"] = len(output_text)
        trimmed.append(item)
    return trimmed


def sanitize_panel_results_for_display(panel_results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    sanitized: list[dict[str, Any]] = []
    for result in panel_results:
        item = dict(result)
        if "error" in item:
            item["error"] = redact_sensitive_text(str(item["error"]))
        sanitized.append(item)
    return sanitized


def format_usage(usage: Any) -> str:
    normalized = normalize_usage(usage)
    if not normalized:
        return ""
    parts = []
    if "input_tokens" in normalized:
        parts.append(f"in {normalized['input_tokens']}")
    if "output_tokens" in normalized:
        parts.append(f"out {normalized['output_tokens']}")
    if "total_tokens" in normalized:
        parts.append(f"total {normalized['total_tokens']}")
    return ", ".join(parts)


def format_latency(elapsed_ms: Any) -> str:
    if isinstance(elapsed_ms, int) and elapsed_ms >= 0:
        return f"{elapsed_ms} ms"
    return ""


def print_panel_result(
    *,
    panel_mode: str,
    base_url: str,
    judge_model: str,
    panel_models: list[str],
    panel_results: list[dict[str, Any]],
    text: str,
    caveats: list[str],
    metadata: dict[str, Any],
    output_json: bool,
    output_mode: str = "prose",
    findings: dict[str, Any] | None = None,
) -> None:
    safe_gateway = redact_sensitive_text(base_url)
    judge_model = redact_sensitive_text(judge_model)
    panel_models = [redact_sensitive_text(model) for model in panel_models]
    panel_results = sanitize_json(sanitize_panel_results_for_display(panel_results))
    findings = sanitize_json(findings) if findings is not None else None
    text, caveats, metadata = presentable_result(
        text=text,
        caveats=caveats,
        metadata=metadata,
        sanitizer=sanitize_json,
    )
    if output_json:
        print(
            json.dumps(
                {
                    "mode": "panel",
                    "panel_mode": panel_mode,
                    "gateway": safe_gateway,
                    "judge_model": judge_model,
                    "panel_models": panel_models,
                    "panel_results": panel_results,
                    "caveats": caveats,
                    "findings": findings,
                    "metadata": metadata,
                    "output_text": text.strip(),
                },
                indent=2,
                sort_keys=True,
            )
        )
        return
    if output_mode == "findings":
        print(json.dumps(findings or fallback_findings_contract(text, caveats), indent=2, sort_keys=True))
        return

    print(f"## Antigravity panel ({panel_mode})")
    print(f"- Gateway: {safe_gateway}")
    print(f"- Panel models: {', '.join(panel_models)}")
    print(f"- Judge model: {judge_model}")
    if metadata.get("scope"):
        print(f"- Scope: {metadata['scope']}")
    if metadata.get("status"):
        print(f"- Status: {metadata['status']}")
    if metadata.get("collaboration_profile"):
        print(f"- Collaboration: {metadata['collaboration_profile']}")
    for result in panel_results:
        stats = "; ".join(
            part
            for part in [
                format_latency(result.get("elapsed_ms") or result.get("generation", {}).get("elapsed_ms")),
                format_usage(result.get("usage") or result.get("generation", {}).get("usage")),
            ]
            if part
        )
        suffix = f" ({stats})" if stats else ""
        if result["status"] == "success":
            print(f"- {result['model']}: success{suffix}")
        else:
            print(f"- {result['model']}: error: {result.get('error', 'unknown error')}")
    for caveat in caveats:
        print(f"- Caveat: {caveat}")
    print()
    print(text.strip())
    totals = format_usage(metadata.get("usage_totals"))
    judge_stats = "; ".join(
        part
        for part in [
            format_latency(metadata.get("judge_generation", {}).get("elapsed_ms")),
            format_usage(metadata.get("judge_generation", {}).get("usage")),
        ]
        if part
    )
    if totals or judge_stats:
        print()
        print("## Usage And Latency")
        if totals:
            print(f"- Token totals: {totals}")
        if judge_stats:
            print(f"- Judge: {judge_stats}")


def panel_review_summary_model(panel_models: list[str]) -> str:
    for model in panel_models:
        if model == "claude-3.5-sonnet" or "sonnet" in model:
            return model
    return panel_models[0]


def maybe_summarize_panel_review(
    *,
    args: argparse.Namespace,
    prompt: str,
    caveats: list[str],
    metadata: dict[str, Any],
    panel_models: list[str],
) -> tuple[str, list[str], dict[str, Any]]:
    context = metadata.pop("_review_context", None)
    if args.mode != "review" or not isinstance(context, dict):
        return prompt, caveats, metadata
    if not should_run_chunked_review(args, metadata):
        return prompt, caveats, metadata

    raw_prompt_chars = len(prompt)
    summary_model = panel_review_summary_model(panel_models)
    prompt_budget = prompt_budget_for_model(args, summary_model)
    progress(args, f"panel review: summarizing broad review context with {summary_model} before fan-out")
    summary_text, summary_caveats, summary_metadata = run_chunked_review(
        args=args,
        context=context,
        model=summary_model,
        base_metadata=metadata,
        max_prompt_chars=prompt_budget,
    )
    prompt = "\n\n".join(
        [
            "This panel review context was summarized by Anti before multi-model fan-out to avoid silently truncating a large review scope.",
            "Panel lanes must treat the summary as bounded context, not as proof of the omitted raw source.",
            "## Bounded Review Summary\n" + summary_text.strip(),
        ]
    )
    fanout_prompt_budget = prompt_budget_for_panel_source(args, panel_models)
    prompt = apply_prompt_limit(prompt, fanout_prompt_budget, summary_caveats)
    metadata = {
        **metadata,
        "panel_review_context": "chunked-summary",
        "panel_review_summary_model": summary_model,
        "raw_review_prompt_chars": raw_prompt_chars,
        "prompt_chars": len(prompt),
        "prompt_budget_chars": fanout_prompt_budget,
        "review_summary_chars": len(summary_text),
        "review_summary_metadata": summary_metadata,
    }
    summary_caveats.append(
        "Panel review used a bounded chunked summary instead of sending the full raw review context to every lane."
    )
    return prompt, summary_caveats, metadata


def command_panel(args: argparse.Namespace) -> int:
    if args.output not in PANEL_OUTPUT_MODES:
        raise AntiError(f"unsupported panel output mode: {args.output}")
    collab_profile = normalize_collab_profile(getattr(args, "collab", "none"))
    panel_models = resolve_panel_models(args.model, collab_profile=collab_profile)
    args.resolved_panel_models = panel_models
    judge_model = resolve_model(args.judge, default=DEFAULT_PANEL_JUDGE_MODEL)
    min_successes = args.min_successes
    if min_successes is None:
        min_successes = 2 if len(panel_models) >= 2 else 1
    if min_successes > len(panel_models):
        raise AntiError("--min-successes cannot exceed the number of panel models")

    prompt, caveats, metadata = assemble_panel_source_prompt(args)
    disclosure = byok_repo_context_disclosure(panel_models, judge_model, args)
    if disclosure:
        caveats.append(disclosure)
        metadata.setdefault("privacy_disclosures", []).append(disclosure)
        if not args.print_prompt:
            eprint(f"[anti] {redact_sensitive_text(disclosure)}")
    if args.print_prompt:
        metadata.pop("_review_context", None)
    metadata.update(
        {
            "panel_mode": args.mode,
            "panel_models": panel_models,
            "judge_model": judge_model,
            "min_successes": min_successes,
            "max_parallel": args.max_parallel,
            "prompt_chars": len(prompt),
        }
    )
    if collab_profile != "none":
        metadata["collaboration_profile"] = collab_profile
    if args.print_prompt:
        payload = {"prompt": prompt, "metadata": metadata, "caveats": caveats}
        if args.json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            print(prompt)
            if caveats:
                print("\n## Assembly Caveats")
                for caveat in caveats:
                    print(f"- {caveat}")
        return 0

    ensure_run_id(args)
    if getattr(args, "run_id", None):
        metadata["run_id"] = args.run_id
        metadata["request_log_correlation_id"] = args.run_id

    required_models = [judge_model]
    if getattr(args, "fallback_model", None):
        fallback_model = resolve_model(args.fallback_model, default=args.fallback_model)
        if fallback_model not in required_models:
            required_models.append(fallback_model)
    model_ids = ensure_models_available(
        base_url=args.base_url,
        models=required_models,
        timeout=args.timeout,
        token_env=args.gateway_token_env,
    )
    missing_panel_models = [model for model in panel_models if model not in model_ids]
    available_panel_models = [model for model in panel_models if model in model_ids]
    if len(available_panel_models) < min_successes:
        sample = ", ".join(sorted(model_ids)[:12])
        raise AntiError(
            "panel model(s) not advertised by /v1/models: "
            + ", ".join(missing_panel_models)
            + f"; only {len(available_panel_models)} panel model(s) available, "
            + f"below --min-successes {min_successes}. Available sample: {sample}"
        )

    prompt, caveats, metadata = maybe_summarize_panel_review(
        args=args,
        prompt=prompt,
        caveats=caveats,
        metadata=metadata,
        panel_models=available_panel_models or panel_models,
    )
    metadata["prompt_chars"] = len(prompt)

    panel_results: list[dict[str, Any]] = [
        {"model": model, "status": "error", "error": "model not advertised by /v1/models"}
        if model in missing_panel_models
        else {}
        for model in panel_models
    ]
    max_workers = min(args.max_parallel, len(available_panel_models))
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(
                run_panel_call,
                args=args,
                model=model,
                prompt=prompt,
                max_output_tokens=args.max_output_tokens,
                model_ids=model_ids,
            ): index
            for index, model in enumerate(panel_models)
            if model not in missing_panel_models
        }
        for future in concurrent.futures.as_completed(futures):
            panel_results[futures[future]] = future.result()

    successes = [result for result in panel_results if result["status"] == "success"]
    failures = [result for result in panel_results if result["status"] != "success"]
    if failures:
        caveats.extend(
            f"Panel model {result['model']} failed: {redact_sensitive_text(result.get('error', 'unknown error'))}"
            for result in failures
        )
    metadata["successful_models"] = [result["model"] for result in successes]
    metadata["failed_models"] = [result["model"] for result in failures]
    metadata["success_count"] = len(successes)

    if len(successes) < min_successes:
        metadata["panel_results"] = panel_results_for_record(panel_results)
        error = f"panel had {len(successes)} successful model(s), below --min-successes {min_successes}"
        try:
            write_run_record(
                args,
                mode="panel",
                status="failed",
                models=panel_models,
                base_url=args.base_url,
                prompt_text=prompt,
                caveats=caveats,
                metadata=metadata,
                error=error,
            )
        except AntiError:
            pass
        args.run_record_written = True
        raise AntiError(error)

    synthesis_prompt, synthesis_caveats, synthesis_metadata = build_panel_synthesis_prompt(
        panel_mode=args.mode,
        source_prompt=prompt,
        panel_results=panel_results,
        metadata=metadata,
        caveats=caveats,
        roles=args.role or [],
        max_chars=args.max_synthesis_chars,
        output_mode=args.output,
    )
    caveats.extend(synthesis_caveats)
    metadata.update(synthesis_metadata)
    judge_text, judge_model_used, judge_generation = generate_with_fallback(
        args,
        model=judge_model,
        prompt=synthesis_prompt,
        max_output_tokens=args.judge_output_tokens,
        model_ids=model_ids,
        purpose="panel judge",
    )
    metadata["judge_model_used"] = judge_model_used
    metadata["judge_generation"] = judge_generation
    metadata["panel_usage_totals"] = sum_usage([result.get("generation", {}) for result in panel_results])
    metadata["usage_totals"] = sum_usage([result.get("generation", {}) for result in panel_results], judge_generation)
    findings, findings_caveat = parse_panel_findings(judge_text)
    if findings_caveat:
        caveats.append(findings_caveat)
        metadata["findings_status"] = "fallback"
        findings = fallback_findings_contract(judge_text, [findings_caveat])
        display_text = judge_text
    else:
        metadata["findings_status"] = "parsed"
        display_text = render_panel_findings(findings or {}, caveats)
    metadata["findings"] = findings
    write_run_record(
        args,
        mode="panel",
        status="success",
        models=[*panel_models, str(judge_model_used)],
        base_url=args.base_url,
        prompt_text=prompt,
        output_text=display_text,
        caveats=caveats,
        metadata=metadata,
    )
    print_panel_result(
        panel_mode=args.mode,
        base_url=args.base_url,
        judge_model=str(judge_model_used),
        panel_models=panel_models,
        panel_results=panel_results,
        text=display_text,
        caveats=caveats,
        metadata=metadata,
        output_json=args.json,
        output_mode=args.output,
        findings=findings,
    )
    return 0


def command_consult(args: argparse.Namespace) -> int:
    model = resolve_model(args.model, default=DEFAULT_CONSULT_MODEL)
    prompt = read_prompt(args)
    caveats: list[str] = []
    prompt = apply_prompt_limit(prompt, args.max_prompt_chars, caveats)
    ensure_run_id(args)
    text, model_used, generation_metadata = generate_with_fallback(
        args,
        model=model,
        prompt=prompt,
        max_output_tokens=args.max_output_tokens,
        purpose="consult",
    )
    metadata = {"prompt_chars": len(prompt), **generation_metadata}
    if getattr(args, "run_id", None):
        metadata["run_id"] = args.run_id
        metadata["request_log_correlation_id"] = args.run_id
    execution_ledger = metadata.pop("_execution_ledger", None)
    recorded_prompt = prompts_as_text(execution_ledger) if execution_ledger else prompt
    write_run_record(
        args,
        mode="consult",
        status="success",
        models=[model_used],
        base_url=args.base_url,
        prompt_text=recorded_prompt,
        output_text=text,
        caveats=caveats,
        metadata=metadata,
        execution_ledger=execution_ledger,
    )
    print_result(
        mode="consult",
        model=model_used,
        base_url=args.base_url,
        text=text,
        caveats=caveats,
        output_json=args.json,
        metadata=metadata,
    )
    return 0


def command_review(args: argparse.Namespace) -> int:
    model = resolve_model(args.model, default=DEFAULT_REVIEW_MODEL)
    prompt_budget = prompt_budget_for_model(args, model)
    claude_guardrail_available = claude_guardrail_would_apply(args, model, prompt_budget)
    context = collect_review_context(args)
    prompt, _paths, caveats, metadata = assemble_review_prompt_from_context(
        context,
        max_prompt_chars=prompt_budget,
    )
    if args.print_prompt:
        if args.json:
            print(json.dumps({"prompt": prompt, "metadata": metadata, "caveats": caveats}, indent=2, sort_keys=True))
            return 0
        print(prompt)
        if caveats:
            print("\n## Assembly Caveats")
            for caveat in caveats:
                print(f"- {caveat}")
        return 0
    ensure_run_id(args)
    chunked_review = should_run_chunked_review(args, metadata)
    claude_guardrail_used = claude_guardrail_available and chunked_review
    if claude_guardrail_used:
        add_claude_guardrail_caveat(caveats, prompt_budget=prompt_budget)
        context["caveats"] = list(caveats)
    if chunked_review:
        text, caveats, metadata = run_chunked_review(
            args=args,
            context=context,
            model=model,
            base_metadata=metadata,
            max_prompt_chars=prompt_budget,
        )
        model_used = metadata.get("synthesis_model_used", model)
    else:
        text, model_used, generation_metadata = generate_with_fallback(
            args,
            model=model,
            prompt=prompt,
            max_output_tokens=args.max_output_tokens,
            purpose="review",
        )
        metadata = {**metadata, "chunked": False, "prompt_budget_chars": prompt_budget, **generation_metadata}
    metadata["claude_prompt_guardrail"] = claude_guardrail_used
    if getattr(args, "run_id", None):
        metadata["run_id"] = args.run_id
        metadata["request_log_correlation_id"] = args.run_id
    execution_ledger = metadata.pop("_execution_ledger", None)
    recorded_prompt = prompts_as_text(execution_ledger) if execution_ledger else prompt
    write_run_record(
        args,
        mode="review",
        status="success",
        models=[str(model_used)],
        base_url=args.base_url,
        prompt_text=recorded_prompt,
        output_text=text,
        caveats=caveats,
        metadata=metadata,
        execution_ledger=execution_ledger,
    )
    print_result(
        mode="review",
        model=str(model_used),
        base_url=args.base_url,
        text=text,
        caveats=caveats,
        output_json=args.json,
        metadata=metadata,
    )
    return 0


def command_plan(args: argparse.Namespace) -> int:
    model = resolve_model(args.model, default=DEFAULT_PLAN_MODEL)
    prompt_budget = prompt_budget_for_model(args, model)
    claude_guardrail_available = claude_guardrail_would_apply(args, model, prompt_budget)
    prompt, caveats = assemble_plan_prompt(args, apply_limit=False)
    recorded_prompt = prompt
    if args.print_prompt:
        printable_caveats = list(caveats)
        printable_prompt = apply_prompt_limit(prompt, prompt_budget, printable_caveats)
        if args.json:
            print(json.dumps({"prompt": printable_prompt, "caveats": printable_caveats}, indent=2, sort_keys=True))
            return 0
        print(printable_prompt)
        if printable_caveats:
            print("\n## Assembly Caveats")
            for caveat in printable_caveats:
                print(f"- {caveat}")
        return 0
    ensure_run_id(args)
    claude_guardrail_used = claude_guardrail_available and prompt_budget > 0 and len(prompt) > prompt_budget
    if claude_guardrail_used:
        add_claude_guardrail_caveat(caveats, prompt_budget=prompt_budget)
    if should_chunk_plan(args, prompt, max_prompt_chars=prompt_budget):
        if prompt_budget > 0:
            recorded_prompt = prompt[:prompt_budget]
        text, caveats, metadata, model_used = run_chunked_plan(
            args=args,
            model=model,
            prompt=prompt,
            caveats=caveats,
            max_prompt_chars=prompt_budget,
        )
    else:
        limited_prompt = apply_prompt_limit(prompt, prompt_budget, caveats)
        recorded_prompt = limited_prompt
        text, model_used, generation_metadata = generate_with_fallback(
            args,
            model=model,
            prompt=limited_prompt,
            max_output_tokens=args.max_output_tokens,
            purpose="plan",
        )
        metadata = {"prompt_chars": len(limited_prompt), "chunked": False, "prompt_budget_chars": prompt_budget, **generation_metadata}
    metadata["claude_prompt_guardrail"] = claude_guardrail_used
    if getattr(args, "run_id", None):
        metadata["run_id"] = args.run_id
        metadata["request_log_correlation_id"] = args.run_id
    execution_ledger = metadata.pop("_execution_ledger", None)
    if execution_ledger:
        recorded_prompt = prompts_as_text(execution_ledger)
    write_run_record(
        args,
        mode="plan",
        status="success",
        models=[str(model_used)],
        base_url=args.base_url,
        prompt_text=recorded_prompt,
        output_text=text,
        caveats=caveats,
        metadata=metadata,
        execution_ledger=execution_ledger,
    )
    print_result(
        mode="plan",
        model=str(model_used),
        base_url=args.base_url,
        text=text,
        caveats=caveats,
        output_json=args.json,
        metadata=metadata,
    )
    return 0


def command_smoke(args: argparse.Namespace) -> int:
    ok = True
    statuses: dict[str, Any] = {
        "mode": args.mode,
        "cli_available": False,
        "models_reachable": False,
        "sidecar_ready": False,
        "codex_backend_ready": None,
        "blocking": False,
        "checks": [],
    }
    try:
        cmd, _cwd = find_cli()
        statuses["cli_available"] = True
        statuses["checks"].append({"name": "cli", "status": "pass", "detail": " ".join(cmd)})
        if not args.json:
            print(f"[PASS] codex-antigravity CLI: {' '.join(cmd)}")
    except AntiError as exc:
        ok = False
        error = redact_sensitive_text(str(exc))
        statuses["checks"].append({"name": "cli", "status": "fail", "detail": error})
        if not args.json:
            print(f"[FAIL] codex-antigravity CLI: {error}")

    try:
        ids = fetch_model_ids(args.base_url, timeout=args.timeout, token_env=args.gateway_token_env)
        statuses["models_reachable"] = True
        statuses["checks"].append({"name": "models", "status": "pass", "count": len(ids)})
        if not args.json:
            print(f"[PASS] Gateway /v1/models: {len(ids)} model(s)")
        requested_models = args.model or ["opus", "sonnet"]
        missing_models = []
        for model in [resolve_model(item, default=item) for item in requested_models]:
            if model in ids:
                statuses["checks"].append({"name": "model", "status": "pass", "model": model})
                if not args.json:
                    print(f"[PASS] Model available: {model}")
            else:
                ok = False
                missing_models.append(model)
                statuses["checks"].append({"name": "model", "status": "fail", "model": model})
                if not args.json:
                    print(f"[FAIL] Model missing: {model}")
        statuses["sidecar_ready"] = not missing_models and ok
    except AntiError as exc:
        ok = False
        error = redact_sensitive_text(str(exc))
        statuses["checks"].append({"name": "models", "status": "fail", "detail": error})
        if not args.json:
            print(f"[FAIL] Gateway /v1/models: {error}")

    should_run_doctor = args.mode in {"full", "codex-backend"} and not args.skip_doctor
    if should_run_doctor:
        if not args.json:
            print("[*] Running codex-antigravity doctor...")
        doctor_args = [
            "doctor",
            "--gateway-base-url",
            args.base_url,
            "--config",
            args.config,
            "--provider",
            args.provider,
        ]
        doctor_rc = run_cli_quiet(doctor_args) if args.json else run_cli(doctor_args)
        if doctor_rc != 0:
            ok = False
            statuses["codex_backend_ready"] = False
            statuses["checks"].append({"name": "doctor", "status": "fail", "detail": "doctor reported hard failures"})
            if not args.json:
                print("[FAIL] doctor reported hard failures")
        else:
            statuses["codex_backend_ready"] = True
            statuses["checks"].append({"name": "doctor", "status": "pass"})
            if not args.json:
                print("[PASS] doctor")
    elif args.mode == "sidecar" and not args.skip_doctor:
        statuses["codex_backend_ready"] = None
        statuses["checks"].append(
            {
                "name": "doctor",
                "status": "skipped",
                "detail": "sidecar mode does not require active Codex backend configuration",
            }
        )
        if not args.json:
            print("[INFO] doctor skipped in sidecar mode; use --mode full to require Codex backend config")

    statuses["blocking"] = not ok
    if args.json:
        print(json.dumps(sanitize_json(statuses), indent=2, sort_keys=True))

    return 0 if ok else 1


def command_start(args: argparse.Namespace) -> int:
    base_url = normalize_base_url(args.base_url or f"http://{args.host}:{args.port}/v1")
    if check_gateway(base_url, timeout=args.timeout, token_env=args.gateway_token_env):
        print(f"[PASS] Gateway already reachable at {base_url}")
        return 0

    cmd, cwd = find_cli()
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    start_args = [*cmd, "start", "--host", args.host, "--port", str(args.port)]
    if args.allow_remote:
        start_args.append("--allow-remote")
    with LOG_FILE.open("ab") as log_handle:
        proc = subprocess.Popen(
            start_args,
            cwd=cwd,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    PID_FILE.write_text(str(proc.pid), encoding="utf-8")
    print(f"[*] Started gateway process pid={proc.pid}; log={LOG_FILE}")

    for _ in range(30):
        if proc.poll() is not None:
            print(f"[FAIL] Gateway exited early with code {proc.returncode}; see {LOG_FILE}")
            return 1
        if check_gateway(base_url, timeout=args.timeout, token_env=args.gateway_token_env):
            print(f"[PASS] Gateway reachable at {base_url}")
            return 0
        time.sleep(0.25)
    print(f"[FAIL] Gateway did not become reachable at {base_url}; see {LOG_FILE}")
    return 1


def command_setup_google(args: argparse.Namespace) -> int:
    model = resolve_model(args.model, default=DEFAULT_CONSULT_MODEL)
    cli_args = [
        "setup-google",
        "--accounts",
        str(args.accounts),
        "--config",
        args.config,
        "--model",
        model,
        "--provider",
        args.provider,
        "--provider-name",
        args.provider_name,
        "--port",
        str(args.port),
    ]
    if args.base_url:
        cli_args.extend(["--base-url", args.base_url])
    if args.skip_codex_config:
        cli_args.append("--skip-codex-config")
    if args.skip_doctor:
        cli_args.append("--skip-doctor")
    return run_cli(cli_args)


def command_configure_codex(args: argparse.Namespace) -> int:
    model = resolve_model(args.model, default=DEFAULT_CONSULT_MODEL)
    cli_args = [
        "configure-codex",
        "--write",
        "--config",
        args.config,
        "--model",
        model,
        "--provider",
        args.provider,
        "--provider-name",
        args.provider_name,
        "--base-url",
        args.base_url,
    ]
    return run_cli(cli_args)


def command_doctor(args: argparse.Namespace) -> int:
    cli_args = [
        "doctor",
        "--gateway-base-url",
        args.base_url,
        "--config",
        args.config,
        "--provider",
        args.provider,
    ]
    if args.byok_only:
        cli_args.append("--byok-only")
    return run_cli(cli_args)


def workflow_scope(args: argparse.Namespace, *, default: str) -> str:
    return default if args.scope == "auto" else args.scope


def append_if_present(argv: list[str], flag: str, value: str | None) -> None:
    if value:
        argv.extend([flag, value])


def append_each(argv: list[str], flag: str, values: list[str] | None) -> None:
    for value in values or []:
        argv.extend([flag, value])


def workflow_command_for_progress(argv: list[str]) -> str:
    redacted: list[str] = []
    redact_next = False
    for item in argv:
        if redact_next:
            redacted.append("<redacted>")
            redact_next = False
            continue
        redacted.append(item)
        if item in {"--prompt", "--prompt-file"}:
            redact_next = True
    return shlex.join(["anti.py", *redacted])


def workflow_expansion(args: argparse.Namespace) -> list[str]:
    common = [
        "--base-url",
        args.base_url,
        "--timeout",
        str(args.timeout),
        "--gateway-token-env",
        args.gateway_token_env,
        "--retry",
        str(args.retry),
        "--max-prompt-chars",
        str(args.max_prompt_chars),
        "--fallback-policy",
        args.fallback_policy,
        "--save-output",
        args.save_output,
    ]
    if args.max_output_tokens is not None:
        common.extend(["--max-output-tokens", str(args.max_output_tokens)])
    if args.fallback_model:
        common.extend(["--fallback-model", args.fallback_model])
    if args.progress:
        common.append("--progress")
    if args.run_label:
        common.extend(["--run-label", args.run_label])
    if args.json:
        common.append("--json")
    if args.print_prompt:
        common.append("--print-prompt")

    if args.name == "review-ready":
        scope = workflow_scope(args, default="staged")
        argv = [
            "panel",
            "--mode",
            "review",
            "--scope",
            scope,
            "--judge",
            args.judge,
            "--judge-output-tokens",
            str(args.judge_output_tokens),
            "--max-synthesis-chars",
            str(args.max_synthesis_chars),
            "--chunked",
            args.chunked,
            "--max-review-chunks",
            str(args.max_review_chunks),
            "--chunk-output-tokens",
            str(args.chunk_output_tokens),
            "--max-parallel",
            str(args.max_parallel),
            "--output",
            args.output,
            *common,
        ]
        if args.min_successes is not None:
            argv.extend(["--min-successes", str(args.min_successes)])
        for role in args.role or ["correctness", "security", "tests", "install-docs"]:
            argv.extend(["--role", role])
        for model in args.model or []:
            argv.extend(["--model", model])
    elif args.name == "plan-deep":
        scope = workflow_scope(args, default="working-tree")
        if scope == "diff":
            raise AntiError("workflow plan-deep does not support --scope diff; use working-tree, staged, files, or none")
        if args.base:
            raise AntiError("workflow plan-deep does not support --base")
        if args.changed_files_range:
            raise AntiError("workflow plan-deep does not support --changed-files")
        if args.files_from:
            raise AntiError("workflow plan-deep does not support --files-from")
        argv = [
            "plan",
            "--model",
            args.model[0] if args.model else "opus",
            "--scope",
            scope,
            "--chunked",
            args.chunked,
            "--max-plan-chunks",
            str(args.max_plan_chunks),
            "--chunk-output-tokens",
            str(args.chunk_output_tokens),
            "--max-synthesis-chars",
            str(args.max_synthesis_chars),
            *common,
        ]
        if not args.fallback_model:
            argv.extend(["--fallback-model", "sonnet", "--fallback-policy", "on-retryable"])
    elif args.name == "ship-gate":
        scope = workflow_scope(args, default="staged")
        argv = [
            "panel",
            "--mode",
            "review",
            "--scope",
            scope,
            "--judge",
            args.judge,
            "--judge-output-tokens",
            str(args.judge_output_tokens),
            "--max-synthesis-chars",
            str(args.max_synthesis_chars),
            "--chunked",
            args.chunked,
            "--max-review-chunks",
            str(args.max_review_chunks),
            "--chunk-output-tokens",
            str(args.chunk_output_tokens),
            "--max-parallel",
            str(args.max_parallel),
            "--output",
            args.output,
            *common,
            "--prompt",
            "Assess merge readiness. Focus on concrete blockers, install/use regressions, missing tests, "
            "release caveats, and what native Codex must verify locally before commit or merge.",
        ]
        if args.min_successes is not None:
            argv.extend(["--min-successes", str(args.min_successes)])
        for role in args.role or ["correctness", "security", "tests", "install", "release"]:
            argv.extend(["--role", role])
        for model in args.model or []:
            argv.extend(["--model", model])
    elif args.name == "security-review":
        scope = workflow_scope(args, default="staged")
        argv = [
            "panel",
            "--mode",
            "review",
            "--scope",
            scope,
            "--judge",
            args.judge,
            "--judge-output-tokens",
            str(args.judge_output_tokens),
            "--max-synthesis-chars",
            str(args.max_synthesis_chars),
            "--chunked",
            args.chunked,
            "--max-review-chunks",
            str(args.max_review_chunks),
            "--chunk-output-tokens",
            str(args.chunk_output_tokens),
            "--max-parallel",
            str(args.max_parallel),
            "--output",
            args.output,
            *common,
            "--prompt",
            "Run a security-focused review. Prioritize prompt-injection surfaces, secret handling, authorization and trust boundaries, dependency/config exposure, and concrete local verification steps.",
        ]
        if args.min_successes is not None:
            argv.extend(["--min-successes", str(args.min_successes)])
        for role in args.role or ["injection", "secrets-handling", "authz", "dependency-surface"]:
            argv.extend(["--role", role])
        for model in args.model or []:
            argv.extend(["--model", model])
    elif args.name == "provider-compare":
        if (
            args.base
            or args.changed_files_range
            or args.file
            or args.files_from
            or workflow_scope(args, default="none") != "none"
        ):
            raise AntiError(
                "workflow provider-compare is prompt-only; omit --scope/--base/--changed-files/--file/--files-from"
            )
        argv = [
            "panel",
            "--mode",
            "ask",
            "--judge",
            args.judge,
            "--judge-output-tokens",
            str(args.judge_output_tokens),
            "--max-synthesis-chars",
            str(args.max_synthesis_chars),
            "--max-parallel",
            str(args.max_parallel),
            "--output",
            args.output,
            *common,
        ]
        if args.min_successes is not None:
            argv.extend(["--min-successes", str(args.min_successes)])
        for model in args.model or ["sonnet", "opus"]:
            argv.extend(["--model", model])
    elif args.name == "debug-consensus":
        if (
            args.base
            or args.changed_files_range
            or args.file
            or args.files_from
            or workflow_scope(args, default="none") != "none"
        ):
            raise AntiError(
                "workflow debug-consensus is prompt-only; omit --scope/--base/--changed-files/--file/--files-from"
            )
        argv = [
            "panel",
            "--mode",
            "ask",
            "--judge",
            args.judge,
            "--judge-output-tokens",
            str(args.judge_output_tokens),
            "--max-synthesis-chars",
            str(args.max_synthesis_chars),
            "--max-parallel",
            str(args.max_parallel),
            "--output",
            args.output,
            *common,
            "--prompt",
            (
                "Produce a debug consensus: ranked hypotheses, the evidence that would distinguish them, "
                "the cheapest discriminating tests to run first, and what would falsify the leading theory.\n\n"
                + (args.prompt or " ".join(args.prompt_parts or []).strip())
            ).strip(),
        ]
        if args.min_successes is not None:
            argv.extend(["--min-successes", str(args.min_successes)])
        for role in args.role or ["root-cause", "regression-risk", "discriminating-tests"]:
            argv.extend(["--role", role])
        for model in args.model or ["sonnet", "opus"]:
            argv.extend(["--model", model])
    elif args.name == "claude-grok":
        panel_mode = args.panel_mode
        if panel_mode == "review":
            scope = workflow_scope(args, default="staged")
        elif panel_mode == "plan":
            scope = workflow_scope(args, default="working-tree")
            if scope == "diff":
                raise AntiError("workflow claude-grok --panel-mode plan does not support --scope diff")
            if args.base:
                raise AntiError("workflow claude-grok --panel-mode plan does not support --base")
            if args.changed_files_range:
                raise AntiError("workflow claude-grok --panel-mode plan does not support --changed-files")
            if args.files_from:
                raise AntiError("workflow claude-grok --panel-mode plan does not support --files-from")
        else:
            scope = workflow_scope(args, default="none")
            if args.base or args.changed_files_range or args.file or args.files_from or scope != "none":
                raise AntiError(
                    "workflow claude-grok --panel-mode ask is prompt-only; omit --scope/--base/--changed-files/--file/--files-from"
                )
        argv = [
            "panel",
            "--mode",
            panel_mode,
            "--collab",
            "claude-grok",
            "--judge",
            args.judge,
            "--judge-output-tokens",
            str(args.judge_output_tokens),
            "--max-synthesis-chars",
            str(args.max_synthesis_chars),
            "--max-parallel",
            str(args.max_parallel),
            "--output",
            args.output,
            *common,
        ]
        if panel_mode in {"review", "plan"}:
            argv.extend(["--scope", scope])
        if panel_mode == "review":
            argv.extend(
                [
                    "--chunked",
                    args.chunked,
                    "--max-review-chunks",
                    str(args.max_review_chunks),
                    "--chunk-output-tokens",
                    str(args.chunk_output_tokens),
                ]
            )
        if args.min_successes is not None:
            argv.extend(["--min-successes", str(args.min_successes)])
        default_roles = {
            "review": ["Claude/Grok collaboration", "code-correctness", "runtime-surprises", "verification-tests"],
            "plan": ["Claude/Grok collaboration", "architecture", "execution-risk", "checkpoint-verification"],
            "ask": ["Claude/Grok collaboration", "tradeoffs", "adversarial-cross-check", "verification"],
        }[panel_mode]
        for role in args.role or default_roles:
            argv.extend(["--role", role])
        for model in args.model or CLAUDE_GROK_PANEL_MODELS:
            argv.extend(["--model", model])
    else:
        raise AntiError(f"unknown workflow: {args.name}")

    if args.name in {"review-ready", "ship-gate", "security-review"} or (
        args.name == "claude-grok" and args.panel_mode == "review"
    ):
        append_if_present(argv, "--base", args.base)
        append_if_present(argv, "--changed-files", args.changed_files_range)
        append_each(argv, "--file", args.file)
        append_each(argv, "--files-from", args.files_from)
    elif args.name == "plan-deep" or (args.name == "claude-grok" and args.panel_mode == "plan"):
        append_each(argv, "--file", args.file)
    if args.name != "debug-consensus":
        append_if_present(argv, "--prompt-file", args.prompt_file)
    prompt = args.prompt or " ".join(args.prompt_parts or []).strip()
    if args.name == "claude-grok":
        if args.panel_mode == "plan" and not prompt and not args.prompt_file:
            prompt = (
                "Create a Claude/Grok collaboration plan. Use Claude lanes for codebase architecture and execution sequencing, "
                "Grok lanes for adversarial assumption checks and user/runtime surprises, and return verification checkpoints."
            )
        elif args.panel_mode == "ask" and not prompt and not args.prompt_file:
            raise AntiError("workflow claude-grok --panel-mode ask requires --prompt, --prompt-file, or positional prompt text")
    if args.name in {"plan-deep", "provider-compare", "debug-consensus"} and not prompt and not args.prompt_file:
        if args.name == "plan-deep":
            prompt = (
                "Create a decision-complete autonomous implementation plan for the current Codex task. "
                "Include phases, risks, validation commands, fallback choices, and non-claims."
            )
        elif args.name == "provider-compare":
            raise AntiError("provider-compare requires --prompt, --prompt-file, or positional prompt text")
        else:
            raise AntiError("debug-consensus requires --prompt, --prompt-file, or positional prompt text")
    if prompt and args.name != "debug-consensus":
        argv.extend(["--prompt", prompt])
    elif args.prompt_file and args.name == "debug-consensus":
        append_if_present(argv, "--prompt-file", args.prompt_file)
        return argv
    return argv


def command_workflow(args: argparse.Namespace) -> int:
    args.workflow_name = args.name
    if not getattr(args, "run_label", None):
        args.run_label = args.name
    expanded = workflow_expansion(args)
    progress(args, "workflow expands to: " + workflow_command_for_progress(expanded))
    parser = build_parser()
    expanded_args = parser.parse_args(expanded)
    expanded_args.workflow_name = args.name
    if not getattr(expanded_args, "run_label", None):
        expanded_args.run_label = args.run_label or args.name
    if hasattr(expanded_args, "base_url") and expanded_args.base_url is not None:
        expanded_args.base_url = normalize_base_url(expanded_args.base_url)
    try:
        return int(expanded_args.func(expanded_args))
    finally:
        if getattr(expanded_args, "run_record_written", False):
            args.run_record_written = True


def iter_run_records() -> list[Path]:
    if RUNS_DIR.is_symlink():
        raise AntiError(f"refusing to read Anti run records through symlinked directory: {RUNS_DIR}")
    if not RUNS_DIR.exists():
        return []
    records: list[Path] = []
    for path in sorted(RUNS_DIR.glob("*.json"), reverse=True):
        if path.is_symlink() or not path.is_file():
            eprint(f"[anti] skipping non-regular run record: {path}")
            continue
        records.append(path)
    return records


def load_run_record(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise AntiError(f"could not read run record {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise AntiError(f"run record {path} is not a JSON object")
    return data


def resolve_run_record_path(run_id: str) -> Path:
    if not RUN_ID_RE.fullmatch(run_id):
        raise AntiError("run id must contain only letters, numbers, '_' or '-'")
    if not RUNS_DIR.exists():
        raise AntiError(f"run record not found: {run_id}")
    if RUNS_DIR.is_symlink():
        raise AntiError(f"refusing to read Anti run records through symlinked directory: {RUNS_DIR}")

    root = RUNS_DIR.resolve()
    path = RUNS_DIR / f"{run_id}.json"
    if not path.exists():
        matches = list(RUNS_DIR.glob(f"{run_id}*.json"))
        if len(matches) == 1:
            path = matches[0]
    if not path.exists():
        raise AntiError(f"run record not found: {run_id}")

    resolved = path.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise AntiError(f"run record path escaped Anti run directory: {run_id}") from exc
    return resolved


def command_runs(args: argparse.Namespace) -> int:
    if args.runs_command == "list":
        rows = []
        for path in iter_run_records()[: args.limit]:
            data = load_run_record(path)
            rows.append(
                {
                    "id": data.get("id") or path.stem,
                    "created_at": data.get("created_at"),
                    "mode": data.get("mode"),
                    "status": data.get("status"),
                    "workflow": data.get("workflow"),
                    "models": data.get("models", []),
                    "run_label": data.get("run_label"),
                }
            )
        if args.json:
            print(json.dumps(rows, indent=2, sort_keys=True))
        else:
            if not rows:
                print(f"[*] No Anti run records found in {RUNS_DIR}")
            for row in rows:
                models = ", ".join(row.get("models") or [])
                workflow = f" workflow={row['workflow']}" if row.get("workflow") else ""
                label = f" label={row['run_label']}" if row.get("run_label") else ""
                print(f"{row['created_at']} {row['id']} {row['mode']} {row['status']}{workflow}{label} [{models}]")
        return 0
    if args.runs_command == "show":
        path = resolve_run_record_path(args.id)
        print(json.dumps(load_run_record(path), indent=2, sort_keys=True))
        return 0
    if args.runs_command == "clean":
        cutoff = time.time() - (args.older_than * 86400)
        removed = 0
        for path in iter_run_records():
            if path.stat().st_mtime < cutoff:
                if args.dry_run:
                    print(f"[*] Would remove {path.name}")
                else:
                    path.unlink()
                removed += 1
        verb = "Would remove" if args.dry_run else "Removed"
        print(f"[+] {verb} {removed} Anti run record(s) older than {args.older_than} day(s)")
        return 0
    raise AntiError(f"unknown runs command: {args.runs_command}")


def add_gateway_args(
    parser: argparse.ArgumentParser,
    *,
    default_base_url: str | None = DEFAULT_BASE_URL,
    default_timeout: float = 15.0,
) -> None:
    parser.add_argument("--base-url", default=default_base_url, help="Gateway base URL ending in /v1")
    parser.add_argument("--timeout", type=float, default=default_timeout, help="HTTP timeout in seconds")
    parser.add_argument(
        "--gateway-token-env",
        default=DEFAULT_TOKEN_ENV,
        help="Env var containing bearer token for remote gateway access",
    )


def add_generation_control_args(
    parser: argparse.ArgumentParser,
    *,
    default_save_output: str = "never",
) -> None:
    parser.add_argument("--fallback-model", help="Fallback model alias/id for retryable or timeout failures")
    parser.add_argument(
        "--fallback-policy",
        choices=sorted(FALLBACK_POLICIES),
        default="never",
        help="When to use --fallback-model",
    )
    parser.add_argument("--progress", action="store_true", help="Print long-call progress to stderr")
    parser.add_argument("--run-label", help="Optional label for saved Anti run metadata")
    parser.add_argument("--run-id", help="Stable run/correlation id for saved and gateway records")
    parser.add_argument(
        "--save-output",
        choices=sorted(SAVE_OUTPUT_MODES),
        default=default_save_output,
        help="Save sanitized run metadata under ~/.codex/anti-runs",
    )


def add_codex_config_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", default="~/.codex/config.toml", help="Codex config path")
    parser.add_argument("--provider", default="antigravity", help="Codex provider id")
    parser.add_argument("--provider-name", default="Google Antigravity", help="Codex provider display name")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Antigravity Opus/Sonnet sidecar helper for Codex")
    sub = parser.add_subparsers(dest="command", required=True)

    panel = sub.add_parser(
        "panel",
        aliases=["moa", "fusion"],
        help="Run a bounded multi-model advisory panel for review, planning, or focused questions",
    )
    add_gateway_args(panel, default_timeout=120.0)
    add_generation_control_args(panel)
    panel.add_argument("--mode", choices=["review", "plan", "ask"], default="review")
    panel.add_argument("--collab", choices=sorted(COLLAB_PROFILES), default="none", help="Optional collaboration profile such as claude-grok")
    panel.add_argument("--model", action="append", help="Panel model alias/id; repeatable; defaults to sonnet + opus")
    panel.add_argument("--judge", default="opus", help="Judge model alias/id; defaults to opus")
    panel.add_argument("--role", action="append", help="Review/planning lens such as security, correctness, tests, ux")
    panel.add_argument("--scope", choices=["none", "working-tree", "staged", "files", "diff"], default="working-tree")
    panel.add_argument("--base", help="Base ref for --mode review --scope diff; uses <base>...HEAD")
    panel.add_argument("--changed-files", dest="changed_files_range", help="Git revision range for --mode review --scope diff")
    panel.add_argument("--file", action="append", help="Add or limit repository file context; repeatable")
    panel.add_argument("--files-from", action="append", help="Read review paths from a newline- or NUL-delimited file; use - for stdin")
    panel.add_argument("--prompt", help="Ask/planning prompt text")
    panel.add_argument("--prompt-file", help="Read ask/planning prompt text from file")
    panel.add_argument("--max-output-tokens", type=positive_int, default=2048, help="Max output tokens per panel model")
    panel.add_argument("--judge-output-tokens", type=positive_int, default=4096, help="Max output tokens for judge synthesis")
    panel.add_argument("--max-prompt-chars", type=non_negative_int, default=DEFAULT_MAX_PROMPT_CHARS, help=MAX_PROMPT_CHARS_HELP)
    panel.add_argument("--max-synthesis-chars", type=non_negative_int, default=DEFAULT_MAX_SYNTHESIS_CHARS)
    panel.add_argument(
        "--chunked",
        choices=["auto", "always", "off"],
        default="auto",
        help="Summarize broad review scopes before panel fan-out when needed",
    )
    panel.add_argument("--max-review-chunks", type=positive_int, default=8, help="Maximum chunk calls before panel fan-out")
    panel.add_argument("--chunk-output-tokens", type=positive_int, default=2048, help="Max output tokens per review chunk")
    panel.add_argument("--min-successes", type=positive_int, help="Minimum successful panel model calls before judging")
    panel.add_argument("--max-parallel", type=positive_int, default=3, help="Maximum concurrent panel model calls")
    panel.add_argument("--retry", type=non_negative_int, default=1, help="Retry transient gateway/backend failures")
    panel.add_argument("--output", choices=sorted(PANEL_OUTPUT_MODES), default="prose", help="Render prose or findings JSON")
    panel.add_argument("--json", action="store_true", help="Emit structured JSON output")
    panel.add_argument("--print-prompt", action="store_true", help="Print assembled source prompt without contacting gateway")
    panel.add_argument("prompt_parts", nargs="*", help="Positional ask/planning prompt text")
    panel.set_defaults(func=command_panel)

    consult = sub.add_parser("consult", aliases=["ask"], help="Ask Antigravity an explicit prompt")
    add_gateway_args(consult, default_timeout=120.0)
    add_generation_control_args(consult)
    consult.add_argument("--model", default="sonnet", help="opus, sonnet, or full model id")
    consult.add_argument("--prompt", help="Prompt text")
    consult.add_argument("--prompt-file", help="Read prompt text from file")
    consult.add_argument("--max-output-tokens", type=positive_int, default=2048)
    consult.add_argument("--max-prompt-chars", type=non_negative_int, default=DEFAULT_MAX_PROMPT_CHARS, help="Maximum prompt chars before truncation; use 0 for unlimited")
    consult.add_argument("--retry", type=non_negative_int, default=1, help="Retry transient gateway/backend failures")
    consult.add_argument("--json", action="store_true", help="Emit structured JSON output")
    consult.add_argument("prompt_parts", nargs="*", help="Positional prompt text")
    consult.set_defaults(func=command_consult)

    plan = sub.add_parser(
        "plan",
        aliases=["deep-plan", "work-plan"],
        help="Ask Antigravity Opus for a deep autonomous work plan",
    )
    add_gateway_args(plan, default_timeout=120.0)
    add_generation_control_args(plan)
    plan.add_argument("--model", default="opus", help="opus, sonnet, or full model id")
    plan.add_argument("--prompt", help="Planning goal text")
    plan.add_argument("--prompt-file", help="Read planning goal from file")
    plan.add_argument("--scope", choices=["none", "working-tree", "staged", "files"], default="none")
    plan.add_argument("--file", action="append", help="Add repository file context; repeatable")
    plan.add_argument("--max-output-tokens", type=positive_int, default=6144)
    plan.add_argument("--max-prompt-chars", type=non_negative_int, default=DEFAULT_MAX_PROMPT_CHARS, help=MAX_PROMPT_CHARS_HELP)
    plan.add_argument("--chunked", choices=["auto", "always", "off"], default="auto")
    plan.add_argument("--max-plan-chunks", type=positive_int, default=6)
    plan.add_argument("--chunk-output-tokens", type=positive_int, default=2048)
    plan.add_argument("--max-synthesis-chars", type=non_negative_int, default=DEFAULT_MAX_SYNTHESIS_CHARS)
    plan.add_argument("--retry", type=non_negative_int, default=1, help="Retry transient gateway/backend failures")
    plan.add_argument("--json", action="store_true", help="Emit structured JSON output")
    plan.add_argument("--print-prompt", action="store_true", help="Print assembled prompt without contacting gateway")
    plan.add_argument("prompt_parts", nargs="*", help="Positional planning goal text")
    plan.set_defaults(func=command_plan)

    review = sub.add_parser("review", help="Review git diffs or selected files with Antigravity")
    add_gateway_args(review, default_timeout=120.0)
    add_generation_control_args(review)
    review.add_argument("--model", default="opus", help="opus, sonnet, or full model id")
    review.add_argument("--scope", choices=["working-tree", "staged", "files", "diff"], default="working-tree")
    review.add_argument("--base", help="Base ref for --scope diff; uses <base>...HEAD")
    review.add_argument("--changed-files", dest="changed_files_range", help="Git revision range for --scope diff")
    review.add_argument("--file", action="append", help="Limit review to path; repeatable")
    review.add_argument("--files-from", action="append", help="Read review paths from a newline- or NUL-delimited file; use - for stdin")
    review.add_argument("--max-output-tokens", type=positive_int, default=4096)
    review.add_argument("--max-prompt-chars", type=non_negative_int, default=DEFAULT_MAX_PROMPT_CHARS, help=MAX_PROMPT_CHARS_HELP)
    review.add_argument("--retry", type=non_negative_int, default=1, help="Retry transient gateway/backend failures")
    review.add_argument(
        "--chunked",
        choices=["auto", "always", "off"],
        default="auto",
        help="Split broad reviews into multiple model calls when needed",
    )
    review.add_argument("--max-review-chunks", type=positive_int, default=8, help="Maximum chunk calls before synthesis")
    review.add_argument("--chunk-output-tokens", type=positive_int, default=2048, help="Max output tokens per chunk review")
    review.add_argument(
        "--max-synthesis-chars",
        type=non_negative_int,
        default=DEFAULT_MAX_SYNTHESIS_CHARS,
        help="Maximum synthesis prompt chars after chunk outputs; use 0 for unlimited",
    )
    review.add_argument("--json", action="store_true", help="Emit structured JSON output")
    review.add_argument("--print-prompt", action="store_true", help="Print assembled prompt without contacting gateway")
    review.set_defaults(func=command_review)

    workflow = sub.add_parser("workflow", help="Run a named V2 Anti workflow preset")
    add_gateway_args(workflow, default_timeout=120.0)
    add_generation_control_args(workflow, default_save_output="summary")
    workflow.add_argument(
        "name",
        choices=[
            "review-ready",
            "plan-deep",
            "ship-gate",
            "provider-compare",
            "security-review",
            "debug-consensus",
            "claude-grok",
        ],
    )
    workflow.add_argument("--panel-mode", choices=["review", "plan", "ask"], default="review", help="Panel mode for collaboration workflows")
    workflow.add_argument("--model", action="append", help="Model alias/id for the workflow; repeatable for panels")
    workflow.add_argument("--judge", default="opus")
    workflow.add_argument("--role", action="append")
    workflow.add_argument("--scope", choices=["auto", "none", "working-tree", "staged", "files", "diff"], default="auto")
    workflow.add_argument("--base")
    workflow.add_argument("--changed-files", dest="changed_files_range", help="Git revision range for --scope diff")
    workflow.add_argument("--file", action="append")
    workflow.add_argument("--files-from", action="append")
    workflow.add_argument("--prompt")
    workflow.add_argument("--prompt-file")
    workflow.add_argument(
        "--max-output-tokens",
        type=positive_int,
        default=None,
        help="Override the expanded command's own default when set",
    )
    workflow.add_argument("--judge-output-tokens", type=positive_int, default=4096)
    workflow.add_argument("--max-prompt-chars", type=non_negative_int, default=DEFAULT_MAX_PROMPT_CHARS, help=MAX_PROMPT_CHARS_HELP)
    workflow.add_argument("--max-synthesis-chars", type=non_negative_int, default=DEFAULT_MAX_SYNTHESIS_CHARS)
    workflow.add_argument("--min-successes", type=positive_int)
    workflow.add_argument("--max-parallel", type=positive_int, default=3)
    workflow.add_argument("--retry", type=non_negative_int, default=1)
    workflow.add_argument("--chunked", choices=["auto", "always", "off"], default="auto")
    workflow.add_argument("--max-review-chunks", type=positive_int, default=8)
    workflow.add_argument("--max-plan-chunks", type=positive_int, default=6)
    workflow.add_argument("--chunk-output-tokens", type=positive_int, default=2048)
    workflow.add_argument("--output", choices=sorted(PANEL_OUTPUT_MODES), default="prose")
    workflow.add_argument("--json", action="store_true")
    workflow.add_argument("--print-prompt", action="store_true")
    workflow.add_argument("prompt_parts", nargs="*")
    workflow.set_defaults(func=command_workflow)

    runs = sub.add_parser("runs", help="List, show, or clean sanitized Anti run records")
    runs_sub = runs.add_subparsers(dest="runs_command", required=True)
    runs_list = runs_sub.add_parser("list")
    runs_list.add_argument("--limit", type=positive_int, default=20)
    runs_list.add_argument("--json", action="store_true")
    runs_show = runs_sub.add_parser("show")
    runs_show.add_argument("id")
    runs_clean = runs_sub.add_parser("clean")
    runs_clean.add_argument("--older-than", type=positive_int, required=True, help="Delete records older than N days")
    runs_clean.add_argument("--dry-run", action="store_true", help="List records that would be removed without deleting")
    runs.set_defaults(func=command_runs)

    smoke = sub.add_parser("smoke", help="Check CLI, gateway, models, and doctor readiness")
    add_gateway_args(smoke)
    add_codex_config_args(smoke)
    smoke.add_argument(
        "--mode",
        choices=["sidecar", "full", "codex-backend"],
        default="sidecar",
        help="sidecar checks CLI/gateway/models; full/codex-backend also require doctor/Codex config",
    )
    smoke.add_argument("--model", action="append", help="Required model alias/id; defaults to opus and sonnet")
    smoke.add_argument("--skip-doctor", action="store_true")
    smoke.add_argument("--json", action="store_true", help="Emit structured JSON readiness output")
    smoke.set_defaults(func=command_smoke)

    start = sub.add_parser("start", help="Start gateway in background if it is not reachable")
    add_gateway_args(start, default_base_url=None, default_timeout=2.0)
    start.add_argument("--host", default="127.0.0.1")
    start.add_argument("--port", type=int, default=51122)
    start.add_argument("--allow-remote", action="store_true")
    start.set_defaults(func=command_start)

    setup = sub.add_parser("setup-google", help="Run guided Google Antigravity setup")
    setup.add_argument("--accounts", type=int, default=1)
    setup.add_argument("--model", default="sonnet")
    setup.add_argument("--port", type=int, default=51122)
    setup.add_argument("--base-url")
    setup.add_argument("--skip-codex-config", action="store_true")
    setup.add_argument("--skip-doctor", action="store_true")
    add_codex_config_args(setup)
    setup.set_defaults(func=command_setup_google)

    configure = sub.add_parser("configure-codex", help="Write Codex provider config for Antigravity")
    configure.add_argument("--model", default="sonnet")
    add_gateway_args(configure)
    add_codex_config_args(configure)
    configure.set_defaults(func=command_configure_codex)

    doctor = sub.add_parser("doctor", help="Run codex-antigravity doctor")
    add_gateway_args(doctor)
    add_codex_config_args(doctor)
    doctor.add_argument("--byok-only", action="store_true")
    doctor.set_defaults(func=command_doctor)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if hasattr(args, "base_url") and args.base_url is not None:
            args.base_url = normalize_base_url(args.base_url)
        return int(args.func(args))
    except KeyboardInterrupt:
        if hasattr(args, "save_output") and not getattr(args, "run_record_written", False):
            try:
                run_id = getattr(args, "run_id", None)
                correlation = {"request_log_correlation_id": run_id} if run_id else {}
                write_run_record(
                    args,
                    mode=getattr(args, "command", "unknown"),
                    status="interrupted",
                    models=[],
                    base_url=getattr(args, "base_url", None),
                    metadata=correlation,
                    error="Interrupted",
                )
            except Exception:
                pass
        eprint("Interrupted")
        return 130
    except AntiError as exc:
        if hasattr(args, "save_output") and not getattr(args, "run_record_written", False):
            try:
                write_run_record(
                    args,
                    mode=getattr(args, "command", "unknown"),
                    status="error",
                    models=[],
                    base_url=getattr(args, "base_url", None),
                    caveats=[],
                    metadata={},
                    error=str(exc),
                )
            except Exception:
                pass
        eprint(f"[anti] {redact_sensitive_text(str(exc))}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
