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
import signal
import subprocess
import sys
import time
import hashlib
import random
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
from anti_lib.verifier import verify_findings
from anti_lib.reflections import record_review, get_summary, list_records, clear_records, prune_reflections_older_than


DEFAULT_BASE_URL = "http://127.0.0.1:51122/v1"
DEFAULT_TOKEN_ENV = "ANTIGRAVITY_GATEWAY_TOKEN"
MODEL_ALIASES = {
    "opus": "claude-opus-4-6-thinking",
    "claude-opus": "claude-opus-4-6",
    "claude-opus-4-6": "claude-opus-4-6-thinking",
    "claude-opus-4-6-thinking": "claude-opus-4-6-thinking",
    "sonnet": "claude-sonnet-4-6",
    "claude-sonnet": "claude-sonnet-4-6",
    "claude-sonnet-4-6": "claude-sonnet-4-6",
    "claude-3.5-sonnet": "claude-sonnet-4-6",
    "claude-3-5-sonnet": "claude-sonnet-4-6",
    "deepseek-v4-pro": "deepseek:deepseek-v4-pro",
    "deepseek-v4-flash": "deepseek:deepseek-v4-flash",
    # Gemini Antigravity (free, fast, 1M context)
    "flash-3.7": "gemini-3.7-flash",
    "gemini-3.7-flash": "gemini-3.7-flash",
    "flash-high": "gemini-3.7-flash",
    "flash": "gemini-3.5-flash-medium",
    "flash-medium": "gemini-3.5-flash-medium",
    "gemini-flash": "gemini-3.5-flash-medium",
    "gemini-pro": "gemini-3.1-pro",
    "gemini-3.1-pro": "gemini-3.1-pro",
    "gpt-oss-120b": "gpt-oss-120b-medium",
    # Gemini 3.6 Flash (newer, more efficient)
    "flash-3.6": "gemini-3.6-flash-high",
    "flash-3.6-high": "gemini-3.6-flash-high",
    "flash-3.6-medium": "gemini-3.6-flash-medium",
    "gemini-3.6-flash": "gemini-3.6-flash-medium",
    # OpenRouter free tier (BYOK)
    "nemotron-super": "openrouter:nvidia/nemotron-3-super-120b-a12b:free",
    "nemotron-ultra": "openrouter:nvidia/nemotron-3-ultra-550b-a55b:free",
    "free": "openrouter/free",
    "poolside": "openrouter:poolside/laguna-s-2.1:free",
    "gemma-4": "openrouter:google/gemma-4-31b-it:free",
    # OpenRouter free vision models (for vision sidecar / direct image tasks)
    "nemotron-vl": "openrouter:nvidia/nemotron-nano-12b-v2-vl:free",
    "nemotron-nano-vl": "openrouter:nvidia/nemotron-nano-12b-v2-vl:free",
    "nemotron-omni": "openrouter:nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free",
    "gemma-4-vision": "openrouter:google/gemma-4-31b-it:free",
    # Ollama local inference
    "gpt-oss": "ollama:gpt-oss:20b",
    "qwen3": "ollama:qwen3:8b",
}

# Model capabilities: what each model supports
MODEL_CAPABILITIES: dict[str, dict[str, bool]] = {
    # Gemini Antigravity (Google backend)
    "gemini-3.7-flash": {"images": True, "video": True, "audio": True, "tools": True, "streaming": True, "json_mode": True},
    "gemini-3.1-flash-image": {"images": True, "video": False, "audio": False, "tools": False, "streaming": True, "json_mode": False},
    "gemini-3.5-flash-high": {"images": True, "video": True, "audio": True, "tools": True, "streaming": True, "json_mode": True},
    "gemini-3.5-flash-medium": {"images": True, "video": True, "audio": True, "tools": True, "streaming": True, "json_mode": True},
    "gemini-3.6-flash-high": {"images": True, "video": True, "audio": True, "tools": True, "streaming": True, "json_mode": True},
    "gemini-3.6-flash-medium": {"images": True, "video": True, "audio": True, "tools": True, "streaming": True, "json_mode": True},
    "gemini-3.1-pro": {"images": True, "video": True, "audio": True, "tools": True, "streaming": True, "json_mode": True},
    "gemini-3.1-pro-high": {"images": True, "video": True, "audio": True, "tools": True, "streaming": True, "json_mode": True},
    # Claude Antigravity (Google backend)
    "claude-sonnet-4-6": {"images": True, "video": False, "audio": False, "tools": True, "streaming": True, "json_mode": True},
    "claude-opus-4-6-thinking": {"images": True, "video": False, "audio": False, "tools": True, "streaming": True, "json_mode": True},
    "claude-3.5-sonnet": {"images": True, "video": False, "audio": False, "tools": True, "streaming": True, "json_mode": True},
    "claude-opus-4-6": {"images": True, "video": False, "audio": False, "tools": True, "streaming": True, "json_mode": True},
    "gpt-oss-120b-medium": {"images": False, "video": False, "audio": False, "tools": False, "streaming": True, "json_mode": True},
    # OpenRouter free tier (BYOK)
    "openrouter:nvidia/nemotron-3-super-120b-a12b:free": {"images": False, "video": False, "audio": False, "tools": True, "streaming": True, "json_mode": True},
    "openrouter:nvidia/nemotron-3-ultra-550b-a55b:free": {"images": False, "video": False, "audio": False, "tools": True, "streaming": True, "json_mode": True},
    "openrouter/free": {"images": True, "video": False, "audio": False, "tools": True, "streaming": True, "json_mode": True},
    "openrouter:poolside/laguna-s-2.1:free": {"images": False, "video": False, "audio": False, "tools": True, "streaming": True, "json_mode": True},
    "openrouter:google/gemma-4-31b-it:free": {"images": True, "video": False, "audio": False, "tools": True, "streaming": True, "json_mode": True},
    # xAI OAuth
    # Official DeepSeek API (metered, API-key route)
    "deepseek:deepseek-v4-pro": {"images": False, "video": False, "audio": False, "tools": True, "streaming": True, "json_mode": True},
    "deepseek:deepseek-v4-flash": {"images": False, "video": False, "audio": False, "tools": True, "streaming": True, "json_mode": True},
    # BluesMinds API-key routes (metered; currently fail closed until live-health gate passes)
    # OpenRouter free vision models (verified: support image input)
    "openrouter:nvidia/nemotron-nano-12b-v2-vl:free": {"images": True, "video": False, "audio": False, "tools": True, "streaming": True, "json_mode": True},
    "openrouter:nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free": {"images": True, "video": False, "audio": False, "tools": True, "streaming": True, "json_mode": True},
    # Ollama local
    "ollama:gpt-oss:20b": {"images": False, "video": False, "audio": False, "tools": True, "streaming": True, "json_mode": True},
    "ollama:qwen3:8b": {"images": False, "video": False, "audio": False, "tools": True, "streaming": True, "json_mode": True},
}

# Cost tiers: free < quota < paid
# free = no metering (OpenRouter free tier and Ollama local)
# quota = Google Antigravity quota (shared across accounts)
# paid = metered billing (not currently in rotation)
MODEL_COST_TIER: dict[str, str] = {
    "gemini-3.7-flash": "quota",
    "gemini-3.1-flash-image": "quota",
    "gemini-3.5-flash-high": "quota",
    "gemini-3.5-flash-medium": "quota",
    "gemini-3.6-flash-high": "quota",
    "gemini-3.6-flash-medium": "quota",
    "gemini-3.1-pro": "quota",
    "gemini-3.1-pro-high": "quota",
    "gpt-oss-120b-medium": "quota",
    "claude-sonnet-4-6": "quota",
    "claude-opus-4-6-thinking": "quota",
    "claude-3.5-sonnet": "quota",
    "claude-opus-4-6": "quota",
    "openrouter:nvidia/nemotron-3-super-120b-a12b:free": "free",
    "openrouter:nvidia/nemotron-3-ultra-550b-a55b:free": "free",
    "openrouter/free": "free",
    "openrouter:poolside/laguna-s-2.1:free": "free",
    "openrouter:google/gemma-4-31b-it:free": "free",
    "openrouter:nvidia/nemotron-nano-12b-v2-vl:free": "free",
    "openrouter:nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free": "free",
    "deepseek:deepseek-v4-pro": "paid",
    "deepseek:deepseek-v4-flash": "paid",
    "ollama:gpt-oss:20b": "free",
    "ollama:qwen3:8b": "free",
}

# Rough cost estimates per 1K tokens (prompt + output combined). These are
# internal cost units, not USD, and are intentionally conservative defaults.
COST_PER_1K_TOKENS: dict[str, float] = {
    "free": 0.0,
    "quota": 0.002,
    "paid": 0.01,
}

def estimate_call_cost(model: str, prompt_chars: int, max_output_tokens: int) -> float:
    """Estimate the cost of a single model call in arbitrary cost units."""
    tier = MODEL_COST_TIER.get(model, "quota")
    cost_per_1k = COST_PER_1K_TOKENS.get(tier, 0.002)
    prompt_tokens = prompt_chars / 4
    total_tokens = prompt_tokens + max_output_tokens
    return (total_tokens / 1000) * cost_per_1k

def actual_call_cost(model: str, generation: dict[str, Any] | None, *, prompt_chars: int, max_output_tokens: int) -> float:
    usage = normalize_usage((generation or {}).get("usage"))
    if usage and usage.get("total_tokens") is not None:
        tier = MODEL_COST_TIER.get(model, "quota")
        return (int(usage["total_tokens"]) / 1000) * COST_PER_1K_TOKENS.get(tier, 0.002)
    return estimate_call_cost(model, prompt_chars, max_output_tokens)

# Relative quality ranking for cost-aware selection (higher = better for code tasks)
MODEL_QUALITY_RANK: dict[str, int] = {
    "claude-opus-4-6-thinking": 100,
    "claude-opus-4-6": 100,
    "gemini-3.1-pro": 90,
    "gemini-3.1-pro-high": 90,
    "claude-sonnet-4-6": 85,
    "claude-3.5-sonnet": 85,
    "gemini-3.7-flash": 85,
    "gemini-3.1-flash-image": 50,
    "gpt-oss-120b-medium": 65,
    "gemini-3.6-flash-high": 82,
    "gemini-3.6-flash-medium": 70,
    "gemini-3.5-flash-high": 80,
    "openrouter:nvidia/nemotron-3-ultra-550b-a55b:free": 70,
    "openrouter/free": 65,
    "gemini-3.5-flash-medium": 68,
    "openrouter:nvidia/nemotron-3-super-120b-a12b:free": 65,
    "openrouter:poolside/laguna-s-2.1:free": 60,
    "ollama:gpt-oss:20b": 50,
    "openrouter:google/gemma-4-31b-it:free": 55,
    "openrouter:nvidia/nemotron-nano-12b-v2-vl:free": 60,
    "openrouter:nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free": 65,
    "deepseek:deepseek-v4-pro": 88,
    "deepseek:deepseek-v4-flash": 74,
    "ollama:qwen3:8b": 40,
}


def resolve_auto_model(
    *,
    scope: str | None = None,
    diff_lines: int = 0,
    file_paths: list[str] | None = None,
    default: str = "sonnet",
) -> tuple[str, str]:
    """Pick the cheapest adequate model from diff size and file risk."""
    high_risk_patterns = [
        "auth", "crypto", "security", "migration", "schema",
        "oauth", "token", "credential",
    ]
    risk_level = "low"
    for file_path in file_paths or []:
        if any(pattern in file_path.lower() for pattern in high_risk_patterns):
            risk_level = "high"
            break

    if diff_lines > 1000 or risk_level == "high":
        return ("opus", f"large diff ({diff_lines} lines) or high-risk files")
    if diff_lines > 200:
        return ("sonnet", f"medium diff ({diff_lines} lines)")
    if diff_lines > 0:
        return ("flash-3.6", f"small diff ({diff_lines} lines), low risk")
    return (default, "no diff context, using default")

DEFAULT_REVIEW_MODEL = "claude-opus-4-6-thinking"
DEFAULT_CONSULT_MODEL = "claude-sonnet-4-6"
DEFAULT_PLAN_MODEL = "claude-opus-4-6-thinking"
DEFAULT_PANEL_MODELS = ["claude-sonnet-4-6", "claude-opus-4-6-thinking"]
DEFAULT_PANEL_JUDGE_MODEL = "claude-opus-4-6-thinking"
COLLAB_PROFILES = {"none"}
MAX_FILE_BYTES = 180_000
DEFAULT_MAX_PROMPT_CHARS = 120_000
DEFAULT_MAX_SYNTHESIS_CHARS = DEFAULT_MAX_PROMPT_CHARS
GIT_DIFF_TRUNCATION_CAVEAT = "Git diff truncated to fit max prompt budget"
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
# Deep review/panel lanes truncate at the output-token cap; the default used to
# be 2048, which cut Opus lanes mid-sentence. Raised defaults plus the retry
# logic below keep structured findings and lane coverage from silently dropping.
PANEL_LANE_RETRY_CEILING_TOKENS = 16_384
JUDGE_RETRY_CEILING_TOKENS = 16_384
PANEL_LANE_INSTRUCTION = (
    "You are one lane of an advisory panel. The task below is complete and self-contained: "
    "produce your independent review or answer directly from the supplied context. "
    "Do not ask for direction, restate the task, or ask clarifying questions."
)

# Phase 2: role-specific rubrics injected into panel lane prompts
ROLE_RUBRICS: dict[str, str] = {
    "correctness": (
        "Focus on logic errors, edge cases, type mismatches, off-by-one mistakes, "
        "null/undefined handling, race conditions, and incorrect algorithmic assumptions."
    ),
    "security": (
        "Focus on injection surfaces (SQL, command, template, prompt), secret handling "
        "(hardcoded keys, leaked env vars), authorization and trust boundaries, "
        "dependency/config exposure, and insecure defaults."
    ),
    "tests": (
        "Focus on missing test coverage for changed logic, untested edge cases, "
        "testable contracts that are not asserted, regression risk from missing "
        "or stale tests, and flaky test patterns."
    ),
    "performance": (
        "Focus on algorithmic complexity (O(n^2) on unbounded input), unnecessary "
        "allocations, N+1 queries, blocking I/O on hot paths, missing caching "
        "opportunities, and memory pressure."
    ),
    "ux": (
        "Focus on usability, accessibility (ARIA, keyboard nav, color contrast), "
        "error messages that are unclear or missing, edge-case UX flows, "
        "loading/empty/error states, and responsive layout issues."
    ),
    "protocol": (
        "Focus on API contract adherence, schema validation, wire format correctness, "
        "versioning, backward compatibility, and error response consistency."
    ),
    "install-docs": (
        "Focus on install regressions, missing or outdated documentation, "
        "broken examples, unclear setup instructions, and changelog accuracy."
    ),
    "injection": (
        "Focus on prompt injection, SQL injection, command injection, template "
        "injection, deserialization attacks, and untrusted input reaching dangerous sinks."
    ),
    "secrets-handling": (
        "Focus on hardcoded secrets, leaked credentials in logs or output, "
        "insecure secret storage, missing rotation, and env var exposure."
    ),
    "authz": (
        "Focus on authorization bypass, privilege escalation, missing access "
        "checks, insecure direct object references, and trust boundary violations."
    ),
    "dependency-surface": (
        "Focus on vulnerable dependencies, unused or shadowed dependencies, "
        "version pinning issues, and supply chain risk from lockfile drift."
    ),
    "root-cause": (
        "Focus on identifying the most likely root cause of the reported issue, "
        "distinguishing correlation from causation, and tracing the failure path."
    ),
    "regression-risk": (
        "Focus on what existing functionality could break from this change, "
        "identify affected code paths, and suggest regression tests."
    ),
    "discriminating-tests": (
        "Focus on proposing the cheapest, most informative tests or checks that "
        "would confirm or rule out each hypothesis."
    ),
}

NON_ANSWER_PHRASES = (
    "what would you like",
    "please provide",
    "could you provide",
    "can you provide",
    "no task",
    "no review",
    "nothing to review",
    "no specific task",
    "no goal",
)
NON_ANSWER_STRONG_PHRASES = (
    # These read as "the task is absent / tell me what to do" at any output
    # length; a real review does not phrase itself this way.
    "what would you like",
    "which direction",
    "how would you like",
    "no explicit task",
    "no task attached",
    "should i just",
    "let me know what",
    "is there anything else",
    "please let me know",
)
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
    run_id = getattr(args, "run_id", None)
    if run_id:
        if not RUN_ID_RE.fullmatch(str(run_id)):
            raise AntiError("run id must contain only letters, numbers, '_' or '-'")
    if save_output_mode(args) == "never":
        return None
    if run_id:
        # Mirror the auto-generated branch: downstream record writes and
        # metadata read args.run_id, so the explicit id must land there too.
        args.run_id = str(run_id)
        write_start_record(args, run_id=str(run_id))
        return str(run_id)
    run_id = new_run_id()
    args.run_id = run_id
    write_start_record(args, run_id=run_id)
    return run_id


def write_start_record(args: argparse.Namespace, *, run_id: str) -> None:
    """Durable heartbeat: a 'running' placeholder written before any model call.

    If the process dies before the final record, the placeholder remains and
    ``runs list`` flags it instead of showing nothing (B5).
    """
    write_run_record(
        args,
        mode=getattr(args, "command", "unknown"),
        status="running",
        models=[],
        base_url=getattr(args, "base_url", None),
        metadata={"request_log_correlation_id": run_id},
    )



def progress(args: argparse.Namespace, message: str) -> None:
    if getattr(args, "progress", True):
        t = time.strftime("%H:%M:%S", time.localtime())
        eprint(f"[anti {t}] {redact_sensitive_text(message)}")


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
    force_full_output: bool = False,
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
    # B7: split run lifecycle from scope coverage so consumers never confuse
    # "the command ran" with "the requested scope was fully reviewed".
    record["runStatus"] = status
    scope_status: str | None = None
    if isinstance(metadata, dict):
        # Panel ``status`` is an integrity result (for example
        # ``degraded_single_model``), while review/plan ``scope_status`` keeps
        # the older complete/incomplete coverage contract for run records.
        metadata_status = metadata.get("scope_status") or metadata.get("status")
        if metadata_status == "incomplete":
            scope_status = "partial"
        elif metadata_status == "complete":
            scope_status = "complete"
        omitted_items = metadata.get("omitted_files") or metadata.get("chunk_omitted_items") or []
        manifest_file_count = metadata.get("omitted_file_count")
        record["omittedFileCount"] = int(
            manifest_file_count if manifest_file_count is not None else len(omitted_items)
        )
        record["omittedChunkCount"] = int(metadata.get("omitted_chunk_count") or 0)
    if scope_status:
        record["scopeStatus"] = scope_status
    if error:
        record["error"] = error
    if output_mode == "summary" and output_text:
        record["output_preview"] = output_text[:RUN_OUTPUT_PREVIEW_CHARS]
        if force_full_output:
            record["output_text"] = output_text
    elif output_mode == "full":
        if prompt_text is not None:
            record["prompt_text"] = prompt_text
        if output_text is not None:
            record["output_text"] = output_text
        if execution_ledger is not None:
            record["execution_ledger"] = execution_ledger

    record = sanitize_json(record)
    # The record id is generated by us or validated by RUN_ID_RE; never let
    # value redaction mangle it (e.g. a run id shaped like user_12345678).
    record["id"] = str(record_id)
    if record.get("metadata", {}).get("request_log_correlation_id") is not None:
        record["metadata"]["request_log_correlation_id"] = str(record_id)
    path = RUNS_DIR / f"{record['id']}.json"
    if path.exists() and path.is_symlink():
        raise AntiError(f"refusing to overwrite symlinked run record: {path}")
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(tmp_path, flags, 0o600)
    except FileExistsError:
        if tmp_path.is_symlink():
            raise
        tmp_path.unlink()
        fd = os.open(tmp_path, flags, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(record, indent=2, sort_keys=True) + "\n")
    # A 'running' placeholder is not a final record; lifecycle handlers may
    # still overwrite it with interrupted/error/failed status.
    args.run_record_written = status != "running"
    os.replace(tmp_path, path)
    progress(args, f"saved sanitized run record: {path}")
    return path


def error_is_retryable(error: str) -> bool:
    lowered = error.lower()
    if "status 'failed'" in lowered:
        return True
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
    enriched = error + diagnostic if diagnostic else error
    return enrich_validation_required_error(enriched)


def extract_validation_url(error_text: str) -> str | None:
    """Return the Google account-validation URL from a 403 error body, if present."""
    match = re.search(r'validation_url"?\s*[:=]?\s*"?(https://accounts\.google\.com/[^"\x27\s,}]+)', error_text)
    return match.group(1) if match else None


def enrich_validation_required_error(error: str) -> str:
    """Surface actionable recovery steps for Google VALIDATION_REQUIRED 403 errors."""
    if "VALIDATION_REQUIRED" not in error:
        return error
    url = extract_validation_url(error)
    if url:
        # Redact user-specific tokens from the original error body too.
        safe_base = url.split("?")[0]
        error = error.replace(url, safe_base)
    hint = (
        " This is an account-level block (VALIDATION_REQUIRED), not a code or gateway bug."
        " Open the validation URL in a browser to re-authorize the account,"
        " then retry. If it persists after re-auth, upgrade the installed gateway:"
        " pip install --upgrade codex-antigravity-auth"
    )
    if url:
        # Strip query params (which may contain user-specific tokens) before display.
        safe_url = url.split("?")[0]
        return f"{error}\n[ACTION REQUIRED] Verify your Google account: {safe_url}{hint}"
    return f"{error}{hint}"


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
    if parsed.scheme not in {"http", "https"}:
        raise AntiError(f"base URL scheme must be http or https, not {parsed.scheme!r}")
    if not parsed.netloc:
        raise AntiError("base URL must include a host")
    return value.rstrip("/")


def resolve_model(value: str | None, *, default: str) -> str:
    raw = (value or default).strip()
    return MODEL_ALIASES.get(raw.lower(), raw)


def provider_for_model(model: str | None) -> str | None:
    """Return the provider portion of a model id.

    Gateway-native Antigravity ids have no explicit provider prefix, while
    BYOK ids use ``provider:model`` (and some model ids contain additional
    colons).  Keeping this derivation in one place lets panel diversity be
    calculated from the model that actually ran, not the requested alias.
    """
    if not model:
        return None
    provider, separator, _model = str(model).partition(":")
    return provider if separator and provider else "google-antigravity"


def panel_model_identity(
    *,
    requested_model: str,
    actual_model: str | None,
    fallback_chain: list[str] | None = None,
    fallback_used: bool = False,
    primary_error: str | None = None,
    fallback_error: str | None = None,
    fallback_reason: str | None = None,
) -> dict[str, Any]:
    """Return the stable, explicit model identity contract for a panel lane.

    The snake-case keys are used internally and by existing consumers.  The
    camel-case aliases mirror the public contract documented for panel output
    and make it difficult to mistake the requested lane for the model that
    actually produced the text.
    """
    requested_model = str(requested_model)
    actual = str(actual_model) if actual_model else None
    requested_provider = provider_for_model(requested_model)
    actual_provider = provider_for_model(actual)
    chain = list(fallback_chain or [requested_model])
    execution_status = "fallback" if fallback_used else ("primary" if actual else "failed")
    identity = {
        "requestedModel": requested_model,
        "actualModel": actual,
        "provider": actual_provider,
        "status": execution_status,
        "primaryError": primary_error,
        "fallbackReason": fallback_reason,
        "fallbackChain": chain,
    }
    return {
        "requested_model": requested_model,
        "requestedModel": requested_model,
        "requested_provider": requested_provider,
        "requestedProvider": requested_provider,
        "actual_model": actual,
        "actualModel": actual,
        "actual_provider": actual_provider,
        "actualProvider": actual_provider,
        "provider": actual_provider,
        "primary_error": primary_error,
        "primaryError": primary_error,
        "fallback_error": fallback_error,
        "fallbackError": fallback_error,
        "fallback_reason": fallback_reason,
        "fallbackReason": fallback_reason,
        "fallback_chain": chain,
        "fallbackChain": chain,
        "fallback_used": bool(fallback_used),
        "fallbackUsed": bool(fallback_used),
        "fallback_attempted": len(chain) > 1,
        "fallbackAttempted": len(chain) > 1,
        "execution_status": execution_status,
        "model_identity": identity,
        "modelIdentity": identity,
    }


def model_cost_tier(model_id: str) -> str:
    """Return cost tier: 'free', 'quota', or 'paid'."""
    return MODEL_COST_TIER.get(model_id, "paid")


def model_supports(model_id: str, feature: str) -> bool:
    """Check if a model supports a feature (images, video, audio, tools, streaming, json_mode)."""
    caps = MODEL_CAPABILITIES.get(model_id, {})
    return caps.get(feature, False)


def cheapest_models_for_task(
    *,
    available: list[str],
    require_images: bool = False,
    require_video: bool = False,
    require_audio: bool = False,
    min_quality: int = 0,
    prefer_free: bool = True,
) -> list[str]:
    """Return models sorted by cost (free first) then quality (highest first).

    Filters by capability requirements and minimum quality threshold.
    When prefer_free=True, free models are listed before quota/paid models
    regardless of quality rank, so callers can burn free quota first.
    """
    candidates: list[tuple[str, int, int]] = []
    for raw_model_id in available:
        model_id = resolve_model(raw_model_id, default=raw_model_id)
        if require_images and not model_supports(model_id, "images"):
            continue
        if require_video and not model_supports(model_id, "video"):
            continue
        if require_audio and not model_supports(model_id, "audio"):
            continue
        quality = MODEL_QUALITY_RANK.get(model_id, 0)
        if quality < min_quality:
            continue
        tier = model_cost_tier(model_id)
        tier_order = {"free": 0, "quota": 1, "paid": 2}.get(tier, 3)
        candidates.append((model_id, tier_order, quality))
    # Sort: free first, then by quality descending
    candidates.sort(key=lambda x: (x[1] if prefer_free else 2, -x[2]))
    return [m[0] for m in candidates]



def estimate_tokens(text: str) -> int:
    """Rough token count: ~4 chars per token for English text."""
    return max(1, len(text) // 4)


def estimate_cost(
    *,
    model: str,
    prompt_chars: int,
    estimated_output_tokens: int = 0,
) -> dict[str, Any]:
    """Estimate token usage and cost tier for a model call without contacting gateway."""
    input_tokens = max(1, prompt_chars // 4) if prompt_chars > 0 else 0
    total_tokens = input_tokens + estimated_output_tokens
    tier = model_cost_tier(model)
    quality = MODEL_QUALITY_RANK.get(model, 0)
    return {
        "model": model,
        "cost_tier": tier,
        "quality_rank": quality,
        "estimated_input_tokens": input_tokens,
        "estimated_output_tokens": estimated_output_tokens,
        "estimated_total_tokens": total_tokens,
        "prompt_chars": prompt_chars,
    }


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
        req_headers = dict(headers)
        req = urllib.request.Request(url, data=body, headers=req_headers, method=method)
        # Authorization must not follow HTTP redirects to a different host.
        req.add_unredirected_header("Authorization", f"Bearer {token}")
    else:
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


def normalize_catalog_model_id(model_id: str) -> str:
    """Fold provider-prefix drift such as ``openrouter:openrouter/x`` -> ``openrouter:x``.

    Mirrors the gateway's normalize rule: strip repeated self-referential
    ``openrouter/`` segments while a vendor path remains, so
    ``openrouter:openrouter/auto`` and ``openrouter:openrouter/openrouter/x``
    fold the same way the gateway canonicalizes them.
    """
    model_id = str(model_id)
    if model_id.startswith("openrouter:"):
        model = model_id[len("openrouter:") :]
        while model.startswith("openrouter/") and "/" in model[len("openrouter/") :]:
            model = model[len("openrouter/") :]
        return "openrouter:" + model
    return model_id


def catalog_model_matches(requested: str, advertised: str) -> bool:
    return normalize_catalog_model_id(requested) == normalize_catalog_model_id(advertised)


def closest_catalog_models(requested: str, advertised: set[str], *, limit: int = 5) -> list[str]:
    """Suggest advertised ids ranked by shared prefix with the requested id."""
    normalized_requested = normalize_catalog_model_id(requested)

    def rank(model_id: str) -> tuple[int, str]:
        normalized = normalize_catalog_model_id(model_id)
        shared = 0
        for left, right in zip(normalized, normalized_requested):
            if left != right:
                break
            shared += 1
        return (-shared, normalized)

    return sorted(advertised, key=rank)[:limit]


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


def fetch_gateway_package_version(base_url: str, *, timeout: float, token_env: str) -> str:
    normalized = normalize_base_url(base_url)
    health_root = normalized[:-3] if normalized.endswith("/v1") else normalized
    status, payload = request_json(
        "GET",
        f"{health_root}/health",
        timeout=timeout,
        token_env=token_env,
    )
    if status != 200:
        raise AntiError(f"/health returned HTTP {status}")
    version = payload.get("package_version")
    if not isinstance(version, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+!-]{0,127}", version):
        raise AntiError("/health returned no usable package_version")
    return version


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


def extract_response_model(payload: Any) -> str | None:
    """Return a model id explicitly reported by the gateway response."""
    if not isinstance(payload, dict):
        return None
    candidates: list[Any] = [payload.get("model")]
    response = payload.get("response")
    if isinstance(response, dict):
        candidates.append(response.get("model"))
    for candidate in candidates:
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
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
            # The current generation is already represented by its top-level
            # usage entry.  Retry histories are evidence metadata, not extra
            # generations to add to token totals.
            for key, item in value.items():
                if key in {"attempts", "panel_attempts", "judge_attempts", "judgeAttempts"}:
                    continue
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
    matched_model = next(
        (candidate for candidate in available_model_ids if catalog_model_matches(model, candidate)),
        None,
    )
    if matched_model is None:
        sample = ", ".join(sorted(available_model_ids)[:12])
        suggestions = closest_catalog_models(model, available_model_ids)
        suggestion_note = f" Closest advertised: {', '.join(suggestions)}." if suggestions else ""
        raise AntiError(f"model {model!r} is not advertised by /v1/models.{suggestion_note} Available sample: {sample}")
    if matched_model != model:
        eprint(f"[anti] model alias {model!r} matched catalog id {matched_model!r}; forwarding the catalog id")
        model = matched_model
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
            text = extract_response_text(decoded)
            # Guard: model-level failure signalled via status field
            if isinstance(decoded, dict) and decoded.get("status") == "failed":
                error_msg = (
                    decoded.get("error", {}).get("message", "")
                    if isinstance(decoded.get("error"), dict)
                    else ""
                )
                raise AntiError(f"model {model} returned status 'failed': {error_msg}".rstrip(": "))
            # Guard: no meaningful text in output content blocks
            if isinstance(decoded, dict) and isinstance(decoded.get("output"), list):
                all_text: list[str] = []
                for item in decoded["output"]:
                    if isinstance(item, dict):
                        for ci in item.get("content", []):
                            if isinstance(ci, dict) and isinstance(ci.get("text"), str):
                                all_text.append(ci["text"])
                if all_text and not any(t.strip() for t in all_text):
                    raise AntiError(f"model {model} returned empty output with no meaningful content")
            # Guard: warn when extract_response_text fell back to JSON dump
            if text and text[0] == "{" and isinstance(decoded, dict):
                eprint(f"[anti] extract_response_text fell back to JSON dump for model {model} "
                       f"(status={decoded.get('status', 'unknown')}); "
                       f"the response may be malformed or empty")
            response_metadata: dict[str, Any] = {"attempts": attempt}
            response_model = extract_response_model(decoded)
            if response_model:
                response_metadata["backend_model"] = response_model
            return ResponseText(
                text,
                usage=extract_usage(decoded),
                elapsed_ms=int((time.monotonic() - started) * 1000),
                response_metadata=response_metadata,
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


def _pre_flight_cost_suggestion(
    args: Any,
    model: str,
    model_ids: set[str] | None,
    prompt: str,
) -> None:
    """Emit a cost-awareness suggestion if a free model could handle this task."""
    tier = model_cost_tier(model)
    if tier == "free":
        return
    if not model_ids:
        return
    quality = MODEL_QUALITY_RANK.get(model, 0)
    alternatives = [
        m for m in model_ids
        if model_cost_tier(m) == "free" and MODEL_QUALITY_RANK.get(m, 0) >= quality - 15
    ]
    if not alternatives:
        return
    alternatives.sort(key=lambda m: -MODEL_QUALITY_RANK.get(m, 0))
    top = alternatives[:3]
    eprint(
        f"[anti] cost hint: {model} is {tier}-tier. "
        f"Free alternative(s) available: {', '.join(top)}. "
        f"Use --model <alias> to switch."
    )


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

    _pre_flight_cost_suggestion(args, model, model_ids, prompt)
    failures: list[dict[str, str]] = []

    def identity_metadata(
        *,
        actual_model: str | None,
        fallback_used: bool,
        fallback_chain: list[str],
        primary_error: str | None = None,
        fallback_error: str | None = None,
        fallback_reason: str | None = None,
    ) -> dict[str, Any]:
        """Build explicit requested/actual identity metadata for every call.

        ``model_used`` is retained for compatibility with older consumers, but
        callers should use ``requested_model`` and ``actual_model``.  Keeping
        the error and chain next to those identities prevents a successful
        fallback from looking like a successful primary generation.
        """
        metadata = panel_model_identity(
            requested_model=model,
            actual_model=actual_model,
            fallback_chain=fallback_chain,
            fallback_used=fallback_used,
            primary_error=primary_error,
            fallback_error=fallback_error,
            fallback_reason=fallback_reason,
        )
        metadata.update(
            {
                # These legacy fields are still consumed by consult/plan
                # callers and older run records.
                "model_used": actual_model,
                "primary_model": model,
                "fallback_model": fallback_model,
                "fallback_policy": fallback_policy,
                "fallback_used": fallback_used,
                "generation_failures": list(failures),
            }
        )
        return metadata

    def raise_with_metadata(message: str, cause: BaseException, metadata: dict[str, Any]) -> None:
        # Preserve structured failure details even though this API historically
        # raises AntiError for failed generations.  run_panel_call consumes the
        # attribute when it records an error lane.
        error = AntiError(message)
        error.generation_metadata = metadata  # type: ignore[attr-defined]
        raise error from cause

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
        actual_model = str(call_metadata.get("backend_model") or model)
        progress(args, f"{purpose}: {model} completed ({len(text)} output chars)")
        metadata = identity_metadata(
            actual_model=actual_model,
            fallback_used=False,
            fallback_chain=[model],
        )
        metadata.update(call_metadata)
        # Identity fields are authoritative even if a backend response happens
        # to contain similarly named metadata keys.
        metadata.update(identity_metadata(actual_model=actual_model, fallback_used=False, fallback_chain=[model]))
        return text, actual_model, metadata
    except AntiError as exc:
        error = redact_sensitive_text(str(exc))
        failures.append({"model": model, "error": error})
        if not fallback_model or fallback_model == model or not should_use_fallback(error, fallback_policy):
            failure_metadata = identity_metadata(
                actual_model=None,
                fallback_used=False,
                fallback_chain=[model],
                primary_error=error,
            )
            raise_with_metadata(enrich_generation_error(args, error), exc, failure_metadata)
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
            failure_metadata = identity_metadata(
                actual_model=None,
                fallback_used=False,
                fallback_chain=[model, fallback_model],
                primary_error=error,
                fallback_error=fallback_error,
                fallback_reason=f"primary model failed with {fallback_policy}; fallback generation also failed",
            )
            raise_with_metadata(
                f"{purpose} failed on primary model {model} and fallback model {fallback_model}. "
                f"Primary error: {error}. Fallback error: {enriched_fallback_error}",
                fallback_exc,
                failure_metadata,
            )
        text = str(raw_text)
        call_metadata = response_call_metadata(raw_text)
        actual_model = str(call_metadata.get("backend_model") or fallback_model)
        progress(args, f"{purpose}: fallback {fallback_model} completed ({len(text)} output chars)")
        metadata = identity_metadata(
            actual_model=actual_model,
            fallback_used=True,
            fallback_chain=[model, fallback_model],
            primary_error=error,
            fallback_reason=f"primary model failed with {fallback_policy}; fallback model was used",
        )
        metadata.update(call_metadata)
        metadata.update(
            identity_metadata(
                actual_model=actual_model,
                fallback_used=True,
                fallback_chain=[model, fallback_model],
                primary_error=error,
                fallback_reason=f"primary model failed with {fallback_policy}; fallback model was used",
            )
        )
        return text, actual_model, metadata


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
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=root,
            timeout=60,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except subprocess.TimeoutExpired:
        raise AntiError(f"git {' '.join(args)} timed out after 60s")
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
    try:
        proc = subprocess.run(
            ["git", "ls-files", "--error-unmatch", "--", rel_path],
            cwd=root,
            timeout=60,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except subprocess.TimeoutExpired:
        raise AntiError(f"git ls-files timed out after 60s")
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



def extract_file_paths_from_prompt(prompt: str) -> list[str]:
    """Extract file paths from a prompt string."""
    paths: list[str] = []
    seen: set[str] = set()
    
    def add_path(path: str) -> None:
        """Add path after normalizing to avoid duplicates."""
        # Normalize: remove ./ prefix, expand ~
        normalized = path
        if normalized.startswith("./"):
            normalized = normalized[2:]
        if normalized not in seen:
            seen.add(normalized)
            paths.append(path)  # Keep original form for display
    
    # Pattern 1: Absolute or home-relative paths to files
    # Matches /path/to/file.py or ~/path/to/file.py
    for match in re.finditer(r'(?<![.~\w/])([~]?/(?:[\w.@-]+/)*[\w.@-]+\.\w+)', prompt):
        add_path(match.group(1))
    
    # Pattern 2: Directory path followed by filenames in parentheses
    # Matches /path/to/dir/ (file1.py, file2.py)
    for dir_paren_match in re.finditer(r'([~]?/(?:[\w.@-]+/)+)\s*\(([^)]+)\)', prompt):
        dir_path = dir_paren_match.group(1)
        filenames = dir_paren_match.group(2)
        for item in filenames.split(','):
            item = item.strip()
            if item and '.' in item:
                add_path(dir_path + item)
    
    # Pattern 3: Relative paths like ./file.py (must start with ./)
    for match in re.finditer(r'(?:(?<=\s)|(?<=\())(\./[\w.@-]+(?:/[\w.@-]+)*\.\w+)', prompt):
        add_path(match.group(1))
    
    # Pattern 4: Paths in backticks (common in Codex prompts)
    for match in re.finditer(r'`(/[\w./@-]+\.\w+)`', prompt):
        add_path(match.group(1))
    
    # Pattern 5: Windows drive-letter paths (C:\Users\...\file.py)
    for match in re.finditer(r'(?<![~\w\\])([A-Za-z]:\\(?:[~\w.@-]+\\)*[~\w.@-]+\.\w+)', prompt):
        add_path(match.group(1))
    
    # Pattern 6: Common extensionless files (Dockerfile, Makefile, etc.)
    _EXTENSIONLESS_NAMES = (
        'Dockerfile', 'Containerfile', 'Makefile', 'Gemfile', 'Rakefile',
        'Procfile', 'Vagrantfile', 'Brewfile', 'Justfile', 'Taskfile', 'Earthfile',
        'LICENSE', 'CHANGELOG', 'README', 'CONTRIBUTING', 'NOTICE',
        '.dockerignore', '.gitignore', '.gitattributes', '.editorconfig',
    )
    _escaped = '|'.join(_EXTENSIONLESS_NAMES)
    for match in re.finditer(
        rf'(?:(?<=\s)|(?<=\()|(?<=`))(\.?/?(?:[\w.@-]+/)*)({_escaped})(?=\s|\)|`|$)', prompt
    ):
        add_path(match.group(1) + match.group(2))
    for match in re.finditer(rf'`([~]?/(?:[\w.@-]+/)*({_escaped}))`', prompt):
        add_path(match.group(1))
    return paths


def build_consult_file_context(
    prompt: str,
    max_prompt_chars: int,
) -> tuple[str, list[str], list[str]]:
    """Read files mentioned in the prompt, inject contents to prevent hallucination."""
    file_paths = extract_file_paths_from_prompt(prompt)
    if not file_paths:
        return prompt, [], []
    
    caveats: list[str] = []
    file_blocks: list[str] = []
    read_files: list[str] = []
    
    workspace_root = find_repo_root(Path.cwd()) or Path.cwd().resolve()
    for file_path_str in file_paths:
        raw_path = Path(file_path_str).expanduser()
        if raw_path.is_symlink():
            caveats.append(f"Skipped symlink: {file_path_str}")
            continue
        
        file_path = raw_path.resolve()
        # Safety: constrain pre-reads to the workspace
        try:
            rel = relative_safe_path(workspace_root, str(file_path))
        except AntiError:
            caveats.append(f"Skipped file outside workspace: {file_path_str}")
            continue
        if not file_path.is_file():
            caveats.append(f"File not found: {file_path_str}")
            continue
        if path_is_excluded(rel):
            caveats.append(f"Skipped excluded path: {file_path_str}")
            continue
        
        text, note = read_text_file(file_path.parent, file_path.name)
        if note:
            caveats.append(note)
        if text:
            file_blocks.append(f"### {file_path_str}\n```text\n{text}\n```")
            read_files.append(file_path_str)
    
    if not file_blocks:
        return prompt, caveats, []
    
    file_context = "## File Contents\n" + "\n\n".join(file_blocks)
    enhanced_prompt = f"{file_context}\n\n## User Request\n{prompt}"
    if max_prompt_chars > 0 and len(enhanced_prompt) > max_prompt_chars:
        caveats.append(f"File contents omitted: combined prompt ({len(enhanced_prompt)} chars) exceeds max ({max_prompt_chars} chars)")
        return prompt, caveats, []
    
    return enhanced_prompt, caveats, read_files
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
                f"{GIT_DIFF_TRUNCATION_CAVEAT} ({len(diff)} original chars, {len(diff_for_prompt)} included)"
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
        "diff_original_chars": len(diff),
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


def empty_review_scope_error(scope: str) -> AntiError:
    if scope == "staged":
        message = "no staged changes to review; stage files with git add, or use --scope working-tree, --scope files, or --scope diff"
    elif scope == "diff":
        message = "no diff found for the requested revision range; check --base/--changed-files"
    elif scope == "files":
        message = "no readable file content in the requested file set; check --file/--files-from paths"
    else:
        message = "no working-tree changes to review; the tree is clean or the selected paths are unchanged"
    return AntiError(message + " (nothing was sent to the model)")


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


def diff_part_prompt_budget(
    *,
    scope_line: str,
    excluded: list[str],
    caveats: list[str],
    max_prompt_chars: int,
) -> int:
    """Per-diff-part char budget that leaves room for prompt scaffolding.

    Splitting against this budget means each part fits a bounded prompt without
    being silently re-truncated inside build_review_prompt (B2).
    """
    if max_prompt_chars <= 0:
        return 0
    base_parts = review_prompt_parts(
        scope_line=scope_line,
        diff="",
        included_files=[],
        omitted_files=[],
        excluded=excluded,
        caveats=caveats,
    )
    overhead = len("\n\n".join(base_parts)) + len("## Git Diff\n```diff\n\n```")
    budget = max_prompt_chars - overhead - 200
    if budget < 500:
        # Tiny budgets cannot hold even a small diff part next to the
        # scaffolding; keep the whole diff as one part and let the caller
        # record the truncation as an omission instead of silently splitting
        # into 1-char parts or silently cutting the diff inside a chunk.
        return 0
    return budget


def build_review_chunk_prompts(
    context: dict[str, Any],
    *,
    max_prompt_chars: int,
    max_chunks: int,
    priority_paths: list[str] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Build the full chunk plan first, then apply the chunk cap.

    ``max_chunks <= 0`` means unlimited: review everything, however many chunks
    it takes. Omitted items are only recorded for scope that did not fit the
    cap, never for silent truncation, so the manifest's ``status`` is honest.
    """
    unlimited = max_chunks <= 0
    file_chunk_budget = max(1200, max_prompt_chars - 1800) if max_prompt_chars > 0 else 0
    all_chunks: list[dict[str, Any]] = []
    omitted_items: list[str] = []

    def append_chunk(kind: str, label: str, prompt: str, metadata: dict[str, Any]) -> None:
        all_chunks.append(
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
        diff_budget = diff_part_prompt_budget(
            scope_line=context["scope_line"],
            excluded=context["excluded"],
            caveats=context["caveats"],
            max_prompt_chars=max_prompt_chars,
        )
        diff_parts = split_text_by_budget(diff, diff_budget)
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
            if not prompt_fits(prompt, max_prompt_chars) or metadata.get("diff_truncated"):
                metadata["diff_truncated"] = True
                omitted_items.append(f"{label} (diff part exceeds {max_prompt_chars} chars)")
                continue
            append_chunk("diff", label, prompt, metadata)

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
        text_parts = split_text_by_budget(text, file_chunk_budget)
        for index, text_part in enumerate(text_parts, start=1):
            label = f"{rel} part {index}/{len(text_parts)}"
            file_items.append((label, text_part))

    priority = [str(path) for path in (priority_paths or [])]
    if priority:
        def _item_matches_priority(item_label: str) -> bool:
            return any(item_label == rel or item_label.startswith(rel + " part ") for rel in priority)

        ordered_items = [item for item in file_items if _item_matches_priority(item[0])]
        ordered_items.extend(item for item in file_items if not _item_matches_priority(item[0]))
        file_items = ordered_items

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
            if prompt_fits(current_prompt, max_prompt_chars):
                append_chunk("files", label, current_prompt, current_metadata)
            else:
                omitted_items.append(f"{label} (prompt still exceeds {max_prompt_chars} chars)")
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
            append_chunk("files", label, current_prompt, current_metadata)
        else:
            omitted_items.append(f"{label} (prompt still exceeds {max_prompt_chars} chars)")

    planned_chunk_count = len(all_chunks)
    if not unlimited and len(all_chunks) > max_chunks:
        chunks = all_chunks[:max_chunks]
        omitted_items.extend(chunk["label"] for chunk in all_chunks[max_chunks:])
    else:
        chunks = all_chunks
    metadata = chunk_manifest(
        chunks,
        omitted_items,
        max_chunks=max_chunks,
        planned_chunk_count=planned_chunk_count,
    )
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
    chunks: list[dict[str, Any]] | None = None,
    chunk_metadata: dict[str, Any] | None = None,
) -> tuple[str, list[str], dict[str, Any]]:
    if chunks is None or chunk_metadata is None:
        chunks, chunk_metadata = build_review_chunk_prompts(
            context,
            max_prompt_chars=max_prompt_chars,
            max_chunks=args.max_review_chunks,
            priority_paths=getattr(args, "priority_file", None),
        )
    if not chunks:
        omitted_count = len(chunk_metadata.get("omitted_items", []))
        raise AntiError(
            "chunked review produced no reviewable chunks"
            + (f"; {omitted_count} item(s) would be omitted" if omitted_count else "")
            + "; narrow the file set or raise --max-prompt-chars"
        )
    planned_chunk_count = int(chunk_metadata.get("planned_chunk_count") or len(chunks))
    omitted_items = chunk_metadata.get("omitted_items", [])
    if omitted_items and not getattr(args, "allow_partial", False):
        raise AntiError(
            "review scope needs "
            f"{planned_chunk_count} chunk(s) but --max-review-chunks={args.max_review_chunks}; "
            f"{len(omitted_items)} item(s) would be omitted "
            f"({chunk_metadata.get('omitted_file_count', 0)} file(s)). "
            "Pass --allow-partial to continue with a partial review, "
            "raise --max-review-chunks, or narrow the file set."
        )
    plan_labels = ", ".join(chunk["label"] for chunk in chunks[:10])
    if len(chunks) > 10:
        plan_labels += ", ..."
    plan_message = f"review chunk plan: {len(chunks)}/{planned_chunk_count} chunk(s)"
    if omitted_items:
        plan_message += f"; {len(omitted_items)} item(s) omitted"
    progress(args, plan_message + f"; labels: {plan_labels}")

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
    # The single-prompt assembly truncates the diff to fit one prompt; chunked
    # mode re-budgets the FULL diff across chunks, so that caveat is stale here.
    caveats = [
        caveat
        for caveat in context["caveats"]
        if not caveat.startswith(GIT_DIFF_TRUNCATION_CAVEAT)
    ]
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
        "diff_original_chars": base_metadata.get("diff_original_chars"),
        "omitted_files": chunk_metadata["omitted_items"],
        "chunk_count": len(chunks),
        "planned_chunk_count": chunk_metadata.get("planned_chunk_count", len(chunks)),
        "omitted_chunk_count": chunk_metadata.get("omitted_chunk_count", 0),
        "omitted_file_count": chunk_metadata.get("omitted_file_count", 0),
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


def detect_repo_profile(root: Path) -> str:
    """Build a short language/framework preamble from top-level manifests."""
    lines: list[str] = []
    manifest_checks = [
        ("pyproject.toml", "Python project (pyproject.toml present)"),
        ("setup.py", "Python project (setup.py present)"),
        ("setup.cfg", "Python project (setup.cfg present)"),
        ("package.json", "JavaScript/TypeScript project (package.json present)"),
        ("Cargo.toml", "Rust project (Cargo.toml present)"),
        ("go.mod", "Go project (go.mod present)"),
        ("Gemfile", "Ruby project (Gemfile present)"),
        ("pom.xml", "Java project (pom.xml present)"),
        ("build.gradle", "JVM project (build.gradle present)"),
        ("*.csproj", ".NET project"),
    ]
    for pattern, label in manifest_checks:
        if pattern.startswith("*."):
            if list(root.glob(pattern)):
                lines.append(label)
        elif (root / pattern).is_file():
            lines.append(label)
    try:
        top_entries = sorted(
            entry.name
            for entry in root.iterdir()
            if not entry.name.startswith(".")
            and entry.is_dir()
            and entry.name not in {"node_modules", "__pycache__", ".venv", "venv", "dist", "build", "target"}
        )[:15]
    except OSError:
        top_entries = []
    if top_entries:
        lines.append(f"Top-level directories: {', '.join(top_entries)}")
    return "\n".join(lines)


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
        repo_profile = detect_repo_profile(root)
        if repo_profile:
            context_parts.insert(1, f"## Repository Profile\n{repo_profile}")
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
        try:
            decoded = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise AntiError(f"prompt file {path!s} is not valid UTF-8") from exc
        pieces.append(decoded)
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


def format_dry_run(
    *,
    mode: str,
    model: str,
    prompt_chars: int,
    max_output_tokens: int,
    extra_models: list[str] | None = None,
    output_json: bool = False,
) -> str:
    """Format a dry-run summary with token and cost estimates."""
    estimates = [estimate_cost(model=model, prompt_chars=prompt_chars,
                               estimated_output_tokens=max_output_tokens)]
    for m in (extra_models or []):
        estimates.append(estimate_cost(model=m, prompt_chars=prompt_chars,
                                       estimated_output_tokens=max_output_tokens))
    if output_json:
        return json.dumps({"mode": mode, "estimates": estimates}, indent=2, sort_keys=True)
    lines = [
        f"[dry-run] {mode} with model(s): {', '.join(e['model'] for e in estimates)}",
        f"  prompt: {prompt_chars} chars (~{estimates[0]['estimated_input_tokens']} tokens)",
    ]
    for e in estimates:
        lines.append(
            f"  {e['model']}: ~{e['estimated_total_tokens']} total tokens "
            f"({e['cost_tier']} tier, quality {e['quality_rank']})"
        )
    return "\n".join(lines)


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
        scope_status = None
        if metadata.get("status") == "incomplete":
            scope_status = "partial"
        elif metadata.get("status") == "complete":
            scope_status = "complete"
        omitted_items = metadata.get("omitted_files") or metadata.get("chunk_omitted_items") or []
        derived_metadata = dict(metadata)
        if scope_status:
            derived_metadata["scopeStatus"] = scope_status
        manifest_file_count = metadata.get("omitted_file_count")
        derived_metadata["omittedFileCount"] = int(
            manifest_file_count if manifest_file_count is not None else len(omitted_items)
        )
        derived_metadata["omittedChunkCount"] = int(metadata.get("omitted_chunk_count") or 0)
        print(
            json.dumps(
                {
                    "mode": mode,
                    "model": model,
                    "gateway": safe_gateway,
                    "caveats": caveats,
                    "metadata": derived_metadata,
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
    if metadata.get("status") == "incomplete":
        omitted_items = metadata.get("omitted_files") or []
        print(f"- Scope: PARTIAL ({len(omitted_items)} item(s) not reviewed; see metadata.omitted_items)")
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


def resolve_panel_models(values: list[str] | None, *, collab_profile: str | None = None) -> list[str]:
    raw_values = values or list(DEFAULT_PANEL_MODELS)
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
    missing = [
        model
        for model in models
        if not any(catalog_model_matches(model, candidate) for candidate in model_ids)
    ]
    if missing:
        sample = ", ".join(sorted(model_ids)[:12])
        suggestions = closest_catalog_models(missing[0], model_ids)
        suggestion_note = (
            f" Closest advertised for {missing[0]!r}: {', '.join(suggestions)}." if suggestions else ""
        )
        raise AntiError(
            "model(s) not advertised by /v1/models: "
            + ", ".join(missing)
            + f".{suggestion_note} Available sample: {sample}"
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
    lines = ["Panel role lenses requested: " + ", ".join(clean_roles) + "."]
    for role in clean_roles:
        rubric = ROLE_RUBRICS.get(role.lower())
        if rubric:
            lines.append(f"- {role}: {rubric}")
    lines.append("Apply these as focused review/planning perspectives, but do not invent findings just to fill a role.")
    return "\n".join(lines)


def model_is_byok(model: str) -> bool:
    provider, separator, provider_model = model.partition(":")
    return bool(separator and provider and provider_model)


def generation_models_for_disclosure(models: list[str], args: argparse.Namespace) -> list[str]:
    candidates = list(models)
    fallback_raw = getattr(args, "fallback_model", None)
    if fallback_raw:
        candidates.append(resolve_model(fallback_raw, default=fallback_raw))
    resolved: list[str] = []
    seen: set[str] = set()
    for model in candidates:
        if model in seen:
            continue
        seen.add(model)
        resolved.append(model)
    return resolved


def panel_receives_repo_context(args: argparse.Namespace) -> bool:
    if args.mode == "review":
        return args.scope != "none"
    if args.mode == "plan":
        return args.scope not in {"none", "prompt"}
    return False


def byok_repo_context_disclosure(
    models: list[str],
    *,
    receives_repo_context: bool,
) -> str | None:
    if not receives_repo_context:
        return None
    provider_models = [model for model in models if model_is_byok(model)]
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


def _balance_json_prefix(prefix: str) -> str:
    """Close the unclosed brackets of a JSON prefix, ignoring brackets inside strings."""
    stack: list[str] = []
    in_string = False
    escape = False
    for char in prefix:
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in "[{":
            stack.append(char)
        elif char in "]}":
            if stack:
                stack.pop()
    closers = {"[": "]", "{": "}"}
    return prefix + "".join(closers[opener] for opener in reversed(stack))


def repair_truncated_json(text: str) -> Any:
    """Recover the longest valid JSON object prefix when the tail was cut off."""
    stripped = text.strip()
    start = stripped.find("{")
    if start < 0:
        raise ValueError("no JSON object found to repair")
    body = stripped[start:]
    closes = [index for index, char in enumerate(body) if char in "}]"]
    # ponytail: capped at the last 80 closing brackets; an adversarial 10MB
    # response of repeated brackets would otherwise try O(n) json.loads calls.
    candidates = [body, *[body[: index + 1] for index in reversed(closes[-80:])]]
    best: Any = None
    best_len = -1
    for candidate in candidates:
        if not candidate.strip():
            continue
        attempts = [candidate, _balance_json_prefix(candidate)]
        for attempt in attempts:
            try:
                parsed = json.loads(attempt)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict) and len(attempt) > best_len:
                best = parsed
                best_len = len(attempt)
    if best is None:
        raise ValueError("could not repair truncated JSON")
    return best


def strip_fenced_json_blocks(text: str) -> str:
    return re.sub(
        r"```(?:json)?\s*\n.*?(?:```|$)",
        "[structured JSON block omitted because it could not be parsed]",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )


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
    # Phase 1: enriched findings schema
    try:
        confidence = float(value.get("confidence", 0.5))
    except (TypeError, ValueError):
        confidence = 0.5
    confidence = max(0.0, min(1.0, confidence))
    file_path = clean_string(value.get("file"), max_chars=500) or None
    line = value.get("line")
    if isinstance(line, (int, float)) and line > 0:
        line = int(line)
    else:
        line = None
    evidence = clean_string(value.get("evidence"), max_chars=2000) or "unverified"
    # Build fingerprint for cross-lane dedup
    fp_key = f"{file_path or ''}:{line or ''}:{claim.lower().strip()}"
    fingerprint = "sha256:" + hashlib.sha256(fp_key.encode("utf-8")).hexdigest()[:16]
    return {
        "id": finding_id,
        "claim": claim,
        "severity": severity,
        "lanes": lanes,
        "verify": verify,
        "confidence": confidence,
        "file": file_path,
        "line": line,
        "evidence": evidence,
        "fingerprint": fingerprint,
    }


def parse_panel_findings(text: str) -> tuple[dict[str, Any] | None, str | None, dict[str, Any]]:
    diagnostics: dict[str, Any] = {"repaired": False, "parse_error": None}
    try:
        parsed = extract_json_object(text)
    except Exception as exc:
        diagnostics["parse_error"] = str(exc)
        try:
            parsed = repair_truncated_json(text)
            diagnostics["repaired"] = True
        except Exception:
            return (
                None,
                "Judge did not return valid structured findings JSON; falling back to prose synthesis "
                f"({diagnostics['parse_error']})",
                diagnostics,
            )
    if not isinstance(parsed, dict):
        return (
            None,
            "Judge structured findings output was not a JSON object; falling back to prose synthesis",
            diagnostics,
        )

    raw_findings = parsed.get("findings")
    if not isinstance(raw_findings, list):
        return (
            None,
            "Judge structured findings JSON did not contain a findings list; falling back to prose synthesis",
            diagnostics,
        )

    findings: list[dict[str, Any]] = []
    dropped = 0
    for index, item in enumerate(raw_findings, start=1):
        normalized = normalize_finding_item(item, index)
        if normalized is None:
            dropped += 1
        else:
            findings.append(normalized)
    # Phase 1: dedup by fingerprint — merge lanes, keep highest severity, average confidence
    if findings:
        SEVERITY_ORDER = {"critical": 4, "high": 3, "medium": 2, "low": 1, "info": 0}
        deduped: dict[str, dict[str, Any]] = {}
        for f in findings:
            fp = f.get("fingerprint", "")
            if fp in deduped:
                existing = deduped[fp]
                # merge lanes
                for lane in f.get("lanes", []):
                    if lane not in existing["lanes"]:
                        existing["lanes"].append(lane)
                # keep highest severity
                if SEVERITY_ORDER.get(f["severity"], 0) > SEVERITY_ORDER.get(existing["severity"], 0):
                    existing["severity"] = f["severity"]
                # average confidence
                existing["confidence"] = round((existing["confidence"] + f["confidence"]) / 2, 2)
                # prefer non-default evidence
                if f.get("evidence", "unverified") != "unverified":
                    existing["evidence"] = f["evidence"]
                dropped += 1
            else:
                deduped[fp] = f
        findings = list(deduped.values())
    parse_warning = None
    if diagnostics["repaired"]:
        parse_warning = (
            "Judge JSON was malformed or truncated and was repaired by truncating to the last complete "
            "JSON value; findings may be incomplete."
        )
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
        "parse_warning": parse_warning,
        "findings_total": len(raw_findings),
        "findings_dropped": dropped,
    }
    return sanitize_json(contract), None, diagnostics


def fallback_findings_contract(
    text: str,
    caveats: list[str],
    *,
    parse_warning: str | None = None,
) -> dict[str, Any]:
    cleaned = strip_fenced_json_blocks(text).strip()
    if cleaned.startswith(("{", "[")) and '"' in cleaned:
        summary = (
            "Judge returned a structured response that could not be parsed; see parse_warning. "
            "No usable prose summary was produced."
        )
    else:
        summary = cleaned
    return sanitize_json(
        {
            "summary": clean_string(summary, max_chars=4000),
            "disagreements": [],
            "findings": [],
            "unverifiable": [],
            "recommended_next_actions": [],
            "caveats": caveats,
            "parse_warning": parse_warning,
            "findings_total": 0,
            "findings_dropped": 0,
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

    parse_warning = findings.get("parse_warning")
    rendered_caveats = clean_string_list(
        [*(findings.get("caveats") or []), *([parse_warning] if parse_warning else []), *caveats],
        max_items=80,
        max_chars=1000,
    )
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
        if not context["diff"].strip() and not context["file_texts"]:
            raise empty_review_scope_error(args.scope)
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
            context["caveats"] = [
                caveat for caveat in caveats if not caveat.startswith(GIT_DIFF_TRUNCATION_CAVEAT)
            ]
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
    # Collaboration instructions removed (no active profiles); kept for future extensibility
    # if collab_instruction:
    #     prompt = "\n\n".join([collab_instruction, prompt])
    prompt = "\n\n".join([PANEL_LANE_INSTRUCTION, gpt_complement_instruction(), prompt])
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
    anonymize: bool = True,
) -> tuple[str, list[str], dict[str, Any]]:
    panel_status = str(metadata.get("status") or metadata.get("panel_status") or "unknown")
    distinct_actual_models = list(metadata.get("distinct_actual_models") or [])
    distinct_actual_providers = list(metadata.get("distinct_actual_providers") or [])
    manifest = {
        "panel_mode": panel_mode,
        "roles": roles,
        "panel_status": panel_status,
        "panel_models": [result["model"] for result in panel_results],
        "requested_models": [result.get("requested_model", result["model"]) for result in panel_results],
        "successful_models": [result["model"] for result in panel_results if result["status"] == "success"],
        "successful_actual_models": [
            result.get("actual_model")
            for result in panel_results
            if result["status"] in ("success", "truncated") and result.get("actual_model")
        ],
        "successful_actual_providers": [
            result.get("provider")
            for result in panel_results
            if result["status"] in ("success", "truncated") and result.get("provider")
        ],
        "fallback_models": [
            result.get("requested_model", result["model"])
            for result in panel_results
            if panel_result_fallback_attempted(result)
        ],
        "primary_failed_models": [
            result.get("requested_model", result["model"])
            for result in panel_results
            if result.get("primary_error")
            or (isinstance(result.get("generation"), dict) and result["generation"].get("primary_error"))
        ],
        "distinct_actual_models": distinct_actual_models,
        "distinct_actual_model_count": int(
            metadata.get("distinct_actual_model_count", len(distinct_actual_models))
        ),
        "distinct_actual_identity_count": int(
            metadata.get("distinct_actual_identity_count", len(distinct_actual_models))
        ),
        "distinct_actual_providers": distinct_actual_providers,
        "failed_models": [
            result["model"]
            for result in panel_results
            if result["status"] not in ("success", "truncated")
        ],
        "truncated_models": [result["model"] for result in panel_results if result["status"] == "truncated"],
        "source_metadata": metadata,
        "source_caveats": caveats,
        "requested_output": output_mode,
    }

    def render(source: str, outputs: list[str]) -> str:
        # Phase 3: anonymize lane labels and shuffle before judging
        render_results = list(panel_results)
        render_outputs = list(outputs)
        # H-3 fix: reuse existing mapping on retry to avoid inconsistent shuffle
        anonymize_mapping = dict(metadata.get("anonymize_mapping", {}))
        if anonymize and len(render_results) > 1:
            if not anonymize_mapping:
                paired = list(zip(render_results, render_outputs))
                random.shuffle(paired)
                render_results, render_outputs = zip(*paired) if paired else ([], [])
                render_results = list(render_results)
                render_outputs = list(render_outputs)
                for i, result in enumerate(render_results):
                    lane_label = chr(ord("A") + i)
                    anonymize_mapping[f"Lane {lane_label}"] = result["model"]
                metadata["anonymize_mapping"] = anonymize_mapping
                metadata["anonymized"] = True
            else:
                # Reorder results to match existing mapping
                reverse_map = {v: k for k, v in anonymize_mapping.items()}
                reorder_key = lambda r: reverse_map.get(r["model"], "")
                paired = sorted(zip(render_results, render_outputs), key=lambda p: reorder_key(p[0]))
                if paired:
                    render_results, render_outputs = zip(*paired)
                    render_results = list(render_results)
                    render_outputs = list(render_outputs)
        result_sections = []
        for result, output in zip(render_results, render_outputs):
            # Use anonymized label if anonymizing
            display_model = result["model"]
            if anonymize and len(render_results) > 1:
                rev_map = {v: k for k, v in anonymize_mapping.items()}
                display_model = rev_map.get(result["model"], result["model"])
            lines = [
                f"## Panel Model (requested): {display_model}",
                f"- status: {result['status']}",
                f"- requested_model: {result.get('requested_model', result['model'])}",
                f"- requested_provider: {result.get('requested_provider') or 'unknown'}",
            ]
            actual_model = result.get("actual_model") or result.get("model_used")
            if actual_model:
                lines.append(f"- actual_model: {actual_model}")
                lines.append(f"- provider: {result.get('provider') or provider_for_model(str(actual_model)) or 'unknown'}")
            if result.get("model_used") and result.get("model_used") != result["model"]:
                lines.append(f"- model_used (compatibility alias): {result['model_used']}")
            if result.get("fallback_chain"):
                lines.append(f"- fallback_chain: {json.dumps(result['fallback_chain'], sort_keys=True)}")
            if result.get("primary_error"):
                lines.append(f"- primary_error: {result['primary_error']}")
            if result.get("fallback_error"):
                lines.append(f"- fallback_error: {result['fallback_error']}")
            if result.get("fallback_reason"):
                lines.append(f"- fallback_reason: {result['fallback_reason']}")
            if result.get("attempts"):
                lines.append(f"- panel_attempts: {json.dumps(result['attempts'], sort_keys=True)}")
            if result.get("model_identity"):
                lines.append(f"- model_identity: {json.dumps(result['model_identity'], sort_keys=True)}")
            lines.append(f"- independent_of_other_lanes: {bool(result.get('independent'))}")
            if result["status"] == "success":
                lines.append(output.strip() or "(empty output)")
            elif result["status"] == "truncated":
                lines.append("(lane output truncated at the token cap; partial content below is incomplete)")
                lines.append(output.strip() or "(no content)")
            else:
                lines.append("error: " + str(result.get("error", "unknown error")))
            result_sections.append("\n".join(lines))
        return "\n\n".join(
            [
                "You are synthesizing an Antigravity multi-model advisory panel for a Codex coding session.",
                "Use only the source prompt/context and panel outputs below. Do not claim local verification, tool execution, or proof that is not present.",
                "Prioritize disagreements, contradictions, and unique insights before consensus. Consensus is only a prioritization signal, not proof.",
                (
                    f"Panel integrity status is {panel_status}. Requested model identity is separate from actual model identity. "
                    f"Distinct successful actual models/providers: {json.dumps(distinct_actual_models)} / {json.dumps(distinct_actual_providers)}. "
                    "Treat a lane as independent only when its actual provider/model identity is unique. "
                    "If status is degraded_single_model, explicitly state that no independent multi-model consensus was established "
                    "and never describe repeated fallback output as agreement."
                ),
                "Return one JSON object and no surrounding prose. The object must contain: summary (string), disagreements (array of strings), findings (array of objects), unverifiable (array of strings), recommended_next_actions (array of strings), and caveats (array of strings).",
                "Each findings item must contain: id (stable short string), claim (specific claim), severity (critical|high|medium|low|info), lanes (array of model ids that support it), verify (a concrete local check Codex should run before acting), confidence (float 0.0-1.0 indicating how certain you are), file (path to the file if applicable), line (line number if applicable), and evidence (any concrete evidence like test output or type error, or 'unverified').",
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


def lane_retry_instruction() -> str:
    return (
        "This is a complete, self-contained review task. Produce the requested output directly now. "
        "Do not ask for direction, restate the task, or ask clarifying questions."
    )


def lane_output_status(output_text: str, usage: dict[str, Any] | None, max_output_tokens: int) -> str:
    """Classify a panel lane's output as answered, truncated, empty, or a non-answer."""
    text = (output_text or "").strip()
    if not text:
        return "empty"
    normalized = normalize_usage(usage)
    output_tokens = int(normalized["output_tokens"]) if normalized and normalized.get("output_tokens") is not None else None
    if output_tokens is not None and output_tokens >= max_output_tokens:
        return "truncated"
    lowered = text.lower()
    if any(phrase in lowered for phrase in NON_ANSWER_STRONG_PHRASES):
        return "non_answer"
    if len(text) < 140 and (text.rstrip().endswith("?") or any(phrase in lowered for phrase in NON_ANSWER_PHRASES)):
        return "non_answer"
    return "success"


def run_panel_call(
    *,
    args: argparse.Namespace,
    model: str,
    prompt: str,
    max_output_tokens: int,
    model_ids: set[str],
) -> dict[str, Any]:
    retry_cap = min(PANEL_LANE_RETRY_CEILING_TOKENS, max_output_tokens * 2)
    attempts: list[dict[str, Any]] = []
    failure_generation_metadata: dict[str, Any] = {}

    def attach_attempt_history(
        result: dict[str, Any],
        generation_metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Retain fallback evidence when a lane retry changes the final route.

        A retry can first succeed through a fallback and then produce the
        final answer through the requested model (or vice versa).  The final
        model remains the answer's ``actual_model``, while the complete
        attempt history records every route and error that influenced the
        lane's reliability classification.
        """
        fallback_attempts = [
            attempt
            for attempt in attempts
            if len(attempt.get("fallback_chain") or []) > 1
            or bool(attempt.get("fallback_used"))
        ]
        primary_errors: list[str] = []
        fallback_errors: list[str] = []
        fallback_reasons: list[str] = []
        for attempt in attempts:
            for key, target in (
                ("primary_error", primary_errors),
                ("fallback_error", fallback_errors),
                ("fallback_reason", fallback_reasons),
            ):
                value = attempt.get(key)
                if isinstance(value, str) and value and value not in target:
                    target.append(value)
        fallback_attempted = bool(fallback_attempts)
        result["panel_attempts"] = attempts
        result["fallback_attempted"] = fallback_attempted
        result["fallbackAttempted"] = fallback_attempted
        result["attempt_fallback_chains"] = [
            list(attempt.get("fallback_chain") or []) for attempt in fallback_attempts
        ]
        result["attemptFallbackChains"] = result["attempt_fallback_chains"]
        if primary_errors:
            result["primary_errors"] = primary_errors
            result["primaryErrors"] = primary_errors
            if not result.get("primary_error"):
                result["primary_error"] = primary_errors[0]
                result["primaryError"] = primary_errors[0]
        if fallback_errors:
            result["fallback_errors"] = fallback_errors
            result["fallbackErrors"] = fallback_errors
            if not result.get("fallback_error"):
                result["fallback_error"] = fallback_errors[0]
                result["fallbackError"] = fallback_errors[0]
        if fallback_reasons:
            result["fallback_reasons"] = fallback_reasons
            result["fallbackReasons"] = fallback_reasons
            if not result.get("fallback_reason"):
                result["fallback_reason"] = fallback_reasons[0]
                result["fallbackReason"] = fallback_reasons[0]
        identity = result.get("model_identity")
        if isinstance(identity, dict):
            identity["fallbackAttempted"] = fallback_attempted
            if primary_errors and not identity.get("primaryError"):
                identity["primaryError"] = primary_errors[0]
            if fallback_errors and not identity.get("fallbackError"):
                identity["fallbackError"] = fallback_errors[0]
            if fallback_reasons and not identity.get("fallbackReason"):
                identity["fallbackReason"] = fallback_reasons[0]
        if isinstance(generation_metadata, dict):
            generation_metadata["panel_attempts"] = attempts
            generation_metadata["fallback_attempted"] = fallback_attempted
            generation_metadata["fallbackAttempted"] = fallback_attempted
            generation_metadata["attempt_fallback_chains"] = result["attempt_fallback_chains"]
            generation_metadata["primary_errors"] = primary_errors
            generation_metadata["fallback_errors"] = fallback_errors
            generation_metadata["fallback_reasons"] = fallback_reasons
            result["generation"] = generation_metadata
        return result

    for attempt in (1, 2):
        cap = max_output_tokens if attempt == 1 else retry_cap
        call_prompt = prompt
        if attempt > 1:
            call_prompt = prompt + "\n\n" + lane_retry_instruction()
        purpose = f"panel model {model}" + ("" if attempt == 1 else f" (retry {attempt - 1})")
        try:
            text, model_used, generation_metadata = generate_with_fallback(
                args,
                model=model,
                prompt=call_prompt,
                max_output_tokens=cap,
                model_ids=model_ids,
                purpose=purpose,
            )
        except Exception as exc:
            error = redact_sensitive_text(str(exc))
            failure_metadata = getattr(exc, "generation_metadata", {})
            if isinstance(failure_metadata, dict):
                failure_generation_metadata = dict(failure_metadata)
            attempt_record: dict[str, Any] = {
                "attempt": attempt,
                "status": "error",
                "error": error,
                "requested_model": model,
                "requested_provider": provider_for_model(model),
                "actual_model": None,
                "provider": None,
                "fallback_chain": [model],
            }
            if isinstance(failure_metadata, dict):
                for key in (
                    "actual_model",
                    "provider",
                    "fallback_chain",
                    "primary_error",
                    "fallback_error",
                    "fallback_reason",
                    "fallback_used",
                ):
                    if key in failure_metadata:
                        attempt_record[key] = failure_metadata[key]
            attempts.append(attempt_record)
            break
        status = lane_output_status(text, generation_metadata.get("usage"), cap)
        attempts.append(
            {
                "attempt": attempt,
                "status": status,
                "output_chars": len(text.strip()),
                "requested_model": model,
                "requested_provider": provider_for_model(model),
                "actual_model": model_used,
                "provider": provider_for_model(model_used),
                "fallback_chain": generation_metadata.get("fallback_chain", [model]),
                "fallback_used": bool(generation_metadata.get("fallback_used")),
                "primary_error": generation_metadata.get("primary_error"),
            }
        )
        result: dict[str, Any] = {
            # ``model`` remains the requested lane for compatibility.  The
            # explicit names below prevent a fallback from masquerading as it.
            "model": model,
            "requested_model": model,
            "requested_provider": provider_for_model(model),
            "actual_model": model_used,
            "provider": provider_for_model(model_used),
            "status": status,
            "output_text": text.strip(),
            "fallback_chain": generation_metadata.get("fallback_chain", [model]),
            "primary_error": generation_metadata.get("primary_error"),
            "fallback_reason": generation_metadata.get("fallback_reason"),
            "independent": None,
        }
        result.update(
            panel_model_identity(
                requested_model=model,
                actual_model=model_used,
                fallback_chain=generation_metadata.get("fallback_chain", [model]),
                fallback_used=bool(generation_metadata.get("fallback_used")),
                primary_error=generation_metadata.get("primary_error"),
                fallback_error=generation_metadata.get("fallback_error"),
                fallback_reason=generation_metadata.get("fallback_reason"),
            )
        )
        if model_used != model:
            result["model_used"] = model_used
        result["generation"] = generation_metadata
        generation_metadata["panel_attempts"] = attempts
        result["panel_attempts"] = attempts
        if generation_metadata.get("usage"):
            result["usage"] = generation_metadata["usage"]
        if generation_metadata.get("elapsed_ms") is not None:
            result["elapsed_ms"] = generation_metadata["elapsed_ms"]
        result["attempts"] = attempts
        if attempt == 1 and status == "success":
            return attach_attempt_history(result, generation_metadata)
        if attempt == 2:
            if status == "truncated":
                result["error"] = "output truncated at the token cap after retry; partial content may be incomplete"
            elif status == "non_answer":
                result["error"] = "model asked for direction instead of delivering the requested output"
            elif status == "empty":
                result["error"] = "model returned empty output after retry"
            return attach_attempt_history(result, generation_metadata)
    last_error = attempts[-1].get("error", "unknown error") if attempts else "unknown error"
    failure_metadata = attempts[-1] if attempts else {}
    result = {
        "model": model,
        "requested_model": model,
        "requested_provider": provider_for_model(model),
        "actual_model": failure_metadata.get("actual_model"),
        "provider": failure_metadata.get("provider"),
        "status": "error",
        "error": redact_sensitive_text(str(last_error)),
        "fallback_chain": failure_metadata.get("fallback_chain", [model]),
        "primary_error": failure_metadata.get("primary_error"),
        "fallback_reason": failure_metadata.get("fallback_reason"),
        "independent": False,
        "attempts": attempts,
    }
    result.update(
        panel_model_identity(
            requested_model=model,
            actual_model=failure_metadata.get("actual_model"),
            fallback_chain=failure_metadata.get("fallback_chain", [model]),
            fallback_used=bool(failure_metadata.get("fallback_used")),
            primary_error=failure_metadata.get("primary_error"),
            fallback_error=failure_metadata.get("fallback_error"),
            fallback_reason=failure_metadata.get("fallback_reason"),
        )
    )
    if failure_generation_metadata:
        result["generation"] = failure_generation_metadata
    return attach_attempt_history(result, failure_generation_metadata or None)


def panel_result_actual_model(result: dict[str, Any]) -> str | None:
    """Return the model that produced a usable lane result, if known."""
    actual = result.get("actual_model") or result.get("model_used")
    if isinstance(actual, str) and actual.strip():
        return actual.strip()
    return None


def panel_result_fallback_attempted(result: dict[str, Any]) -> bool:
    """Whether any usable or failed attempt in a lane traversed a fallback."""
    generation = result.get("generation") if isinstance(result.get("generation"), dict) else {}
    if result.get("fallback_attempted") or generation.get("fallback_attempted"):
        return True
    if result.get("fallback_used") or generation.get("fallback_used"):
        return True
    attempts = result.get("panel_attempts") or result.get("attempts") or generation.get("panel_attempts") or []
    return any(
        isinstance(attempt, dict)
        and (len(attempt.get("fallback_chain") or []) > 1 or bool(attempt.get("fallback_used")))
        for attempt in attempts
    )


def panel_result_identity(result: dict[str, Any]) -> tuple[str, str] | None:
    """Return a stable provider/model key for diversity checks."""
    actual = panel_result_actual_model(result)
    if not actual:
        return None
    return provider_for_model(actual) or "unknown", normalize_catalog_model_id(actual)


def panel_result_identity_label(result: dict[str, Any]) -> str | None:
    actual = panel_result_actual_model(result)
    if not actual:
        return None
    # The canonical model id already carries a BYOK prefix when one exists;
    # avoid producing the misleading ``openrouter:openrouter:...`` form.
    return normalize_catalog_model_id(actual)


def annotate_panel_results(panel_results: list[dict[str, Any]]) -> tuple[list[str], list[str], str]:
    """Attach independence metadata and classify the panel's integrity.

    Diversity is based on actual provider/model execution identities for usable
    (success or truncated) lanes.  Logical requested lanes and fallback
    completions are intentionally not counted as independent evidence.
    """
    usable = [result for result in panel_results if result.get("status") in {"success", "truncated"}]
    identity_counts: dict[tuple[str, str], int] = {}
    for result in usable:
        identity = panel_result_identity(result)
        if identity:
            identity_counts[identity] = identity_counts.get(identity, 0) + 1

    distinct_actual_models: list[str] = []
    distinct_actual_providers: list[str] = []
    for result in usable:
        label = panel_result_identity_label(result)
        if label and label not in distinct_actual_models:
            distinct_actual_models.append(label)
        provider = provider_for_model(panel_result_actual_model(result))
        if provider and provider not in distinct_actual_providers:
            distinct_actual_providers.append(provider)

    for result in panel_results:
        identity = panel_result_identity(result)
        result["independent"] = bool(identity and identity_counts.get(identity, 0) == 1)
        result.setdefault("requested_model", result.get("model"))
        result.setdefault("requested_provider", provider_for_model(result.get("requested_model")))
        result.setdefault("actual_model", panel_result_actual_model(result))
        result.setdefault("provider", provider_for_model(result.get("actual_model")))
        result.setdefault("fallback_chain", [result.get("requested_model")])
        generation = result.get("generation") if isinstance(result.get("generation"), dict) else {}
        primary_error = result.get("primary_error") or generation.get("primary_error")
        fallback_error = result.get("fallback_error") or generation.get("fallback_error")
        fallback_reason = result.get("fallback_reason") or generation.get("fallback_reason")
        fallback_used = bool(result.get("fallback_used") or generation.get("fallback_used"))
        result.update(
            panel_model_identity(
                requested_model=str(result.get("requested_model") or result.get("model") or "unknown"),
                actual_model=result.get("actual_model"),
                fallback_chain=result.get("fallback_chain"),
                fallback_used=fallback_used,
                primary_error=primary_error,
                fallback_error=fallback_error,
                fallback_reason=fallback_reason,
            )
        )
        fallback_attempted = panel_result_fallback_attempted(result)
        result["fallback_attempted"] = fallback_attempted
        result["fallbackAttempted"] = fallback_attempted
        identity_status = "fallback" if fallback_used else (
            "primary" if result.get("status") in {"success", "truncated"} else result.get("status", "error")
        )
        result["model_identity"] = {
            "requestedModel": result.get("requested_model"),
            "actualModel": result.get("actual_model"),
            "requestedProvider": result.get("requested_provider"),
            "actualProvider": result.get("provider"),
            "provider": result.get("provider"),
            "status": identity_status,
            "fallbackChain": result.get("fallback_chain"),
            "fallbackAttempted": fallback_attempted,
            "primaryError": primary_error,
            "fallbackReason": fallback_reason,
            "independent": result.get("independent"),
        }
        # Top-level camelCase aliases make the structured lane record usable by
        # consumers that do not know the helper's legacy snake_case schema.
        result["requestedModel"] = result.get("requested_model")
        result["actualModel"] = result.get("actual_model")
        result["requestedProvider"] = result.get("requested_provider")
        result["actualProvider"] = result.get("provider")
        result["fallbackChain"] = result.get("fallback_chain")
        result["primaryError"] = primary_error
        result["fallbackReason"] = fallback_reason
        result["independentOfOtherLanes"] = result.get("independent")

    fallback_lanes = any(panel_result_fallback_attempted(result) for result in usable)
    if not usable:
        status = "failed"
    elif len(identity_counts) <= 1:
        status = "degraded_single_model"
    elif len(usable) < len(panel_results) or fallback_lanes or len(identity_counts) < len(usable):
        status = "partial_multi_model"
    else:
        status = "complete_multi_model"
    return distinct_actual_models, distinct_actual_providers, status


def panel_actual_identity_count(panel_results: list[dict[str, Any]]) -> int:
    """Count distinct usable provider/model pairs, not logical lanes."""
    return len(
        {
            identity
            for result in panel_results
            if result.get("status") in {"success", "truncated"}
            for identity in [panel_result_identity(result)]
            if identity is not None
        }
    )


def panel_integrity_notice(
    *,
    status: str,
    requested_models: list[str],
    distinct_actual_models: list[str],
) -> str:
    """Return deterministic disclosure text for degraded/partial panels."""
    if status == "complete_multi_model":
        return ""
    requested = ", ".join(requested_models) or "none"
    actual = ", ".join(distinct_actual_models) or "none"
    if status == "degraded_single_model":
        return (
            "Panel integrity notice: degraded_single_model. "
            f"Requested models: {requested}. Successful actual model/provider: {actual}. "
            "No independent multi-model consensus was established."
        )
    if status == "partial_multi_model":
        return (
            "Panel integrity notice: partial_multi_model. "
            f"Requested models: {requested}. Distinct successful actual models/providers: {actual}. "
            "Some requested lanes failed, were truncated, or used fallback; do not treat this as complete consensus."
        )
    return (
        "Panel integrity notice: failed. "
        f"Requested models: {requested}. No successful actual model/provider was available."
    )


def apply_panel_integrity_to_findings(
    findings: dict[str, Any] | None,
    *,
    panel_status: str,
    integrity_notice: str,
    caveats: list[str],
) -> dict[str, Any] | None:
    """Make integrity warnings part of the findings contract, not just stderr."""
    if not isinstance(findings, dict):
        return findings
    findings_caveats = findings.setdefault("caveats", [])
    if not isinstance(findings_caveats, list):
        findings_caveats = []
        findings["caveats"] = findings_caveats
    for caveat in [integrity_notice, *caveats]:
        if caveat and caveat not in findings_caveats:
            findings_caveats.append(caveat)
    if panel_status == "degraded_single_model":
        # Do not allow a model-generated summary to be the only visible label
        # for a collapsed panel. Preserve it as an explicitly unverified note.
        original_summary = str(findings.get("summary") or "").strip()
        findings["summary"] = (
            "DEGRADED_SINGLE_MODEL: no independent multi-model consensus was established. "
            "The judge output is single-actual-model synthesis and is advisory only."
        )
        if original_summary and original_summary != findings["summary"]:
            unverifiable = findings.setdefault("unverifiable", [])
            if isinstance(unverifiable, list):
                note = f"Original judge summary (single-actual-model, unverified): {original_summary}"
                if note not in unverifiable:
                    unverifiable.insert(0, note)
    return findings


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


def panel_lane_status_label(result: dict[str, Any]) -> str:
    status = result.get("status", "error")
    if status == "success":
        return "success"
    if status == "truncated":
        return "truncated (output may be incomplete)"
    if status == "non_answer":
        return "non_answer: asked for direction instead of reviewing"
    if status == "empty":
        return "empty: returned no output"
    if status == "skipped_budget":
        return f"skipped_budget: {result.get('error', 'budget cap reached')}"
    return f"error: {result.get('error', 'unknown error')}"


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
    if metadata.get("judge_actual_model") and metadata.get("judge_actual_model") != judge_model:
        print(f"- Judge actual model: {metadata['judge_actual_model']}")
    if metadata.get("scope"):
        print(f"- Scope: {metadata['scope']}")
    if metadata.get("status"):
        print(f"- Status: {metadata['status']}")
    if metadata.get("distinct_actual_models") is not None:
        actual_models = ", ".join(str(model) for model in metadata.get("distinct_actual_models") or []) or "none"
        print(f"- Distinct actual model/provider(s): {actual_models}")
    if metadata.get("collaboration_profile"):
        print(f"- Collaboration: {metadata['collaboration_profile']}")
    if metadata.get("budget_limit") is not None:
        print(f"- Estimated cost: {float(metadata.get('estimated_cost', metadata.get('estimated_total', 0.0))):.4f} / budget {float(metadata['budget_limit']):.4f}")
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
        requested = result.get("requested_model", result["model"])
        actual = result.get("actual_model") or result.get("model_used")
        identity_suffix = f"; actual: {actual}" if actual and actual != requested else ""
        chain = result.get("fallback_chain") or []
        chain_suffix = f"; fallback chain: {' -> '.join(str(item) for item in chain)}" if len(chain) > 1 else ""
        print(f"- {requested}: {panel_lane_status_label(result)}{identity_suffix}{chain_suffix}{suffix}")
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
        if model in {"claude-3.5-sonnet", "claude-sonnet-4-6"} or "sonnet" in model:
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

    # Fail fast on chunk-cap overflow before spending any model call.
    pre_chunks, pre_chunk_metadata = build_review_chunk_prompts(
        context,
        max_prompt_chars=prompt_budget_for_model(args, panel_review_summary_model(panel_models)),
        max_chunks=args.max_review_chunks,
        priority_paths=getattr(args, "priority_file", None),
    )
    planned_count = int(pre_chunk_metadata.get("planned_chunk_count") or len(pre_chunks))
    omitted = pre_chunk_metadata.get("omitted_items", [])
    if omitted and not getattr(args, "allow_partial", False):
        raise AntiError(
            f"review scope needs {planned_count} chunk(s) but --max-review-chunks={args.max_review_chunks}; "
            f"{len(omitted)} item(s) would be omitted "
            f"({pre_chunk_metadata.get('omitted_file_count', 0)} file(s)). "
            "Pass --allow-partial to continue with a partial review, "
            "raise --max-review-chunks, or narrow the file set."
        )

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
        chunks=pre_chunks,
        chunk_metadata=pre_chunk_metadata,
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
    merged_caveats = list(caveats)
    for caveat in summary_caveats:
        if caveat not in merged_caveats:
            merged_caveats.append(caveat)
    merged_caveats.append(
        "Panel review used a bounded chunked summary instead of sending the full raw review context to every lane."
    )
    return prompt, merged_caveats, metadata


def command_panel(args: argparse.Namespace) -> int:
    if args.output not in PANEL_OUTPUT_MODES:
        raise AntiError(f"unsupported panel output mode: {args.output}")
    collab_profile = normalize_collab_profile(getattr(args, "collab", "none"))
    auto_route_model = None
    auto_route_reason = None
    if getattr(args, "auto_route", False) and args.model is None:
        diff_lines = 0
        file_paths: list[str] = []
        if args.mode == "review":
            auto_context = collect_review_context(args)
            diff_lines = len(auto_context["diff"].splitlines())
            file_paths = list(auto_context["paths"])
        auto_route_model, auto_route_reason = resolve_auto_model(
            scope=getattr(args, "scope", None),
            diff_lines=diff_lines,
            file_paths=file_paths,
            default="sonnet",
        )
        args.model = [auto_route_model]
    panel_models = resolve_panel_models(args.model, collab_profile=collab_profile)
    args.resolved_panel_models = panel_models
    judge_model = resolve_model(args.judge, default=DEFAULT_PANEL_JUDGE_MODEL)
    min_successes = args.min_successes
    if min_successes is None:
        min_successes = 2 if len(panel_models) >= 2 else 1
    if min_successes > len(panel_models):
        raise AntiError("--min-successes cannot exceed the number of panel models")

    prompt, caveats, metadata = assemble_panel_source_prompt(args)
    disclosure = byok_repo_context_disclosure(
        generation_models_for_disclosure([*panel_models, judge_model], args),
        receives_repo_context=panel_receives_repo_context(args),
    )
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
            "budget_limit": args.budget,
        }
    )
    if auto_route_model:
        metadata["auto_route_decision"] = auto_route_model
        metadata["auto_route_reason"] = auto_route_reason
    if collab_profile != "none":
        metadata["collaboration_profile"] = collab_profile
    if args.dry_run:
        print(format_dry_run(mode=f"panel {args.mode}", model=panel_models[0],
            prompt_chars=len(prompt), max_output_tokens=args.max_output_tokens,
            extra_models=panel_models[1:] + [judge_model], output_json=args.json))
        return 0
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
        if args.dry_run:
            eprint(format_dry_run(mode=f"panel {args.mode}", model=panel_models[0],
                prompt_chars=len(prompt), max_output_tokens=args.max_output_tokens,
                extra_models=panel_models[1:] + [judge_model], output_json=args.json))
        return 0

    ensure_run_id(args)
    if getattr(args, "run_id", None):
        metadata["run_id"] = args.run_id
        metadata["request_log_correlation_id"] = args.run_id
    progress(args, f"panel {args.mode}: starting fan-out across {len(panel_models)} model(s) [{', '.join(panel_models)}], judge={judge_model}")

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
    def _catalog_member(model_id: str) -> str | None:
        return next(
            (candidate for candidate in model_ids if catalog_model_matches(model_id, candidate)),
            None,
        )

    if _catalog_member(judge_model) is None:
        raise AntiError(f"judge model {judge_model} is not advertised by /v1/models")
    missing_panel_models = [model for model in panel_models if _catalog_member(model) is None]
    available_panel_models = [model for model in panel_models if _catalog_member(model) is not None]
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
    running_cost = 0.0
    estimated_total = 0.0
    budget_exceeded = False
    futures: dict[concurrent.futures.Future, int] = {}
    reserved_models: dict[int, str] = {}  # H-1: track reserved model per lane
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        for index, model in enumerate(panel_models):
            if model in missing_panel_models:
                continue
            estimated = estimate_call_cost(model, len(prompt), args.max_output_tokens)
            if args.budget is not None and running_cost + estimated > args.budget:
                panel_results[index] = {
                    "model": model,
                    "requested_model": model,
                    "status": "skipped_budget",
                    "error": f"budget cap reached (estimated {running_cost + estimated:.4f} > {args.budget:.4f})",
                }
                budget_exceeded = True
                continue
            running_cost += estimated
            estimated_total += estimated
            reserved_models[index] = model  # H-1: remember reserved model
            futures[executor.submit(
                run_panel_call,
                args=args,
                model=model,
                prompt=prompt,
                max_output_tokens=args.max_output_tokens,
                model_ids=model_ids,
            )] = index
        for future in concurrent.futures.as_completed(futures):
            index = futures[future]
            result = future.result()
            panel_results[index] = result
            # H-1 fix: use reserved model for subtraction (what was originally budgeted)
            # and actual model for addition (what was actually consumed)
            reserved = reserved_models.get(index, panel_models[index])
            actual_model = panel_results[index].get("model", reserved)
            running_cost -= estimate_call_cost(reserved, len(prompt), args.max_output_tokens)
            running_cost += actual_call_cost(actual_model, result.get("generation"), prompt_chars=len(prompt), max_output_tokens=args.max_output_tokens)
    metadata["estimated_total"] = estimated_total
    metadata["estimated_cost"] = running_cost
    metadata["budget_exceeded"] = budget_exceeded

    successes = [result for result in panel_results if result["status"] == "success"]
    truncated = [result for result in panel_results if result["status"] == "truncated"]
    failures = [result for result in panel_results if result["status"] not in {"success", "truncated"}]
    usable = [*successes, *truncated]
    distinct_actual_models, distinct_actual_providers, panel_status = annotate_panel_results(panel_results)
    distinct_actual_identity_count = panel_actual_identity_count(panel_results)
    if failures:
        caveats.extend(
            f"Panel model {result['model']} failed: {redact_sensitive_text(result.get('error', 'unknown error'))}"
            for result in failures
        )
    for result in panel_results:
        generation = result.get("generation") if isinstance(result.get("generation"), dict) else {}
        if not panel_result_fallback_attempted(result):
            continue
        requested = result.get("requested_model", result.get("model"))
        actual = result.get("actual_model") or result.get("model_used") or "unknown"
        primary_error = result.get("primary_error") or generation.get("primary_error") or "unknown error"
        if result.get("fallback_used") or generation.get("fallback_used"):
            caveats.append(
                f"Panel requested model {requested} failed and used fallback {actual}; "
                f"primary error: {redact_sensitive_text(str(primary_error))}"
            )
        else:
            caveats.append(
                f"Panel requested model {requested} encountered a fallback during an earlier attempt; "
                f"final actual model: {actual}; primary error: {redact_sensitive_text(str(primary_error))}"
            )
    if truncated:
        caveats.extend(
            f"Panel model {result['model']} output was truncated at the token cap after retry; "
            "partial content may be incomplete"
            for result in truncated
        )
    integrity_notice = panel_integrity_notice(
        status=panel_status,
        requested_models=panel_models,
        distinct_actual_models=distinct_actual_models,
    )
    if integrity_notice:
        caveats.append(integrity_notice)
    metadata["successful_models"] = [result["model"] for result in successes]
    metadata["failed_models"] = [result["model"] for result in failures]
    metadata["truncated_models"] = [result["model"] for result in truncated]
    metadata["fallback_models"] = [
        result.get("requested_model", result.get("model"))
        for result in panel_results
        if panel_result_fallback_attempted(result)
    ]
    metadata["primary_failed_models"] = [
        result.get("requested_model", result.get("model"))
        for result in panel_results
        if result.get("primary_error")
        or (isinstance(result.get("generation"), dict) and result["generation"].get("primary_error"))
    ]
    metadata["retried_models"] = [
        result["model"] for result in panel_results if len(result.get("attempts", [])) > 1
    ]
    metadata["success_count"] = len(successes)
    metadata["usable_count"] = len(usable)
    if metadata.get("status") in {"complete", "incomplete"}:
        metadata["scope_status"] = metadata["status"]
    metadata["logical_success_count"] = len(successes)
    metadata["logical_usable_count"] = len(usable)
    metadata["successful_actual_models"] = [
        result.get("actual_model")
        for result in usable
        if result.get("actual_model")
    ]
    metadata["successful_actual_providers"] = [
        result.get("provider")
        for result in usable
        if result.get("provider")
    ]
    metadata["min_successes_basis"] = "distinct_actual_model_provider"
    metadata["min_successes_met"] = distinct_actual_identity_count >= min_successes
    metadata["status"] = panel_status
    metadata["panel_status"] = panel_status
    metadata["requested_models"] = list(panel_models)
    metadata["actual_models"] = [
        result.get("actual_model")
        for result in usable
        if result.get("actual_model")
    ]
    metadata["actual_providers"] = [
        result.get("provider")
        for result in usable
        if result.get("provider")
    ]
    metadata["distinct_actual_models"] = distinct_actual_models
    metadata["distinct_actual_model_count"] = len(distinct_actual_models)
    metadata["distinct_actual_identity_count"] = distinct_actual_identity_count
    metadata["distinct_actual_providers"] = distinct_actual_providers
    metadata["distinct_actual_provider_count"] = len(distinct_actual_providers)
    metadata["requestedModels"] = list(panel_models)
    metadata["actualModels"] = list(metadata["actual_models"])
    metadata["actualProviders"] = list(metadata["actual_providers"])
    metadata["distinctActualModels"] = list(distinct_actual_models)
    metadata["distinctActualModelCount"] = len(distinct_actual_models)
    metadata["distinctActualIdentityCount"] = distinct_actual_identity_count
    metadata["distinctActualProviders"] = list(distinct_actual_providers)
    metadata["distinctActualProviderCount"] = len(distinct_actual_providers)
    metadata["panelStatus"] = panel_status

    # ``--min-successes`` is a minimum amount of independent evidence.  A
    # fallback may make multiple logical lanes usable, but it cannot satisfy a
    # diversity requirement when all outputs came from one actual model.
    if distinct_actual_identity_count < min_successes:
        metadata["panel_results"] = panel_results_for_record(panel_results)
        error = (
            f"panel had {len(successes)} successful and {len(truncated)} truncated model(s) "
            f"from {distinct_actual_identity_count} distinct actual model/provider(s), "
            f"below --min-successes {min_successes} (truncated lanes still count as usable; "
            "fallback lanes sharing an actual model are not independent)"
        )
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
        # A fail-closed diversity gate still needs to expose the lane-level
        # evidence to machine consumers.  Keep the non-zero exit status, but
        # emit the same structured panel envelope when JSON/findings output
        # was requested instead of reducing the fallback chain to stderr.
        metadata["panel_error"] = error
        if args.json or args.output == "findings":
            print_panel_result(
                panel_mode=args.mode,
                base_url=args.base_url,
                judge_model=str(judge_model),
                panel_models=panel_models,
                panel_results=panel_results,
                text="",
                caveats=caveats,
                metadata=metadata,
                output_json=args.json,
                output_mode=args.output,
                findings=None,
            )
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
        anonymize=not getattr(args, "no_anonymize", False),
    )
    caveats.extend(synthesis_caveats)
    metadata.update(synthesis_metadata)

    def run_judge(prompt: str, max_output_tokens: int) -> tuple[str, str, dict[str, Any]]:
        return generate_with_fallback(
            args,
            model=judge_model,
            prompt=prompt,
            max_output_tokens=max_output_tokens,
            model_ids=model_ids,
            purpose="panel judge",
        )

    judge_cap = args.judge_output_tokens
    judge_estimate = estimate_call_cost(judge_model, len(synthesis_prompt), judge_cap)
    if args.budget is not None and running_cost + judge_estimate > args.budget:
        budget_exceeded = True
        metadata["budget_exceeded"] = True
        metadata["estimated_total"] = estimated_total + judge_estimate
        metadata["estimated_cost"] = running_cost
        metadata["budget_limit"] = args.budget
        caveats.append(
            f"Panel judge skipped: budget cap reached (estimated {running_cost + judge_estimate:.4f} > {args.budget:.4f})"
        )
        error = "panel judge skipped because the budget cap was reached"
        metadata["panel_error"] = error
        try:
            write_run_record(args, mode="panel", status="failed", models=panel_models,
                base_url=args.base_url, prompt_text=prompt, caveats=caveats,
                metadata=metadata, error=error)
        except AntiError:
            pass
        raise AntiError(error)
    running_cost += judge_estimate
    estimated_total += judge_estimate
    judge_text, judge_model_used, judge_generation = run_judge(synthesis_prompt, judge_cap)
    judge_attempts: list[dict[str, Any]] = [
        dict(judge_generation, attempt=1)
    ]
    findings, findings_caveat, parse_diagnostics = parse_panel_findings(judge_text)
    judge_retried = False
    if findings_caveat:
        retry_cap = min(JUDGE_RETRY_CEILING_TOKENS, judge_cap * 2)
        progress(args, "panel judge: structured findings parse failed; retrying with a stricter JSON-only instruction")
        judge_retried = True
        retry_prompt = (
            "Your previous response was discarded because it was not valid JSON and could not be repaired.\n"
            "Return ONLY one valid JSON object with no markdown code fence, no surrounding prose, and no trailing text. "
            "Prefer a few short high-signal findings over long prose so the response fits in the output budget.\n\n"
            + synthesis_prompt
        )
        retry_estimate = estimate_call_cost(judge_model, len(retry_prompt), retry_cap)
        # H-2 fix: check budget before judge retry
        if args.budget is not None and running_cost + retry_estimate > args.budget:
            caveats.append("Judge retry skipped: budget cap reached")
            progress(args, "panel judge: retry skipped due to budget cap")
        else:
            judge_text, judge_model_used, judge_generation = run_judge(retry_prompt, retry_cap)
            running_cost += retry_estimate
            estimated_total += retry_estimate
        judge_attempts.append(dict(judge_generation, attempt=2))
        judge_cap = retry_cap
        findings, findings_caveat, parse_diagnostics = parse_panel_findings(judge_text)

    judge_primary_errors: list[str] = []
    judge_fallback_errors: list[str] = []
    judge_fallback_reasons: list[str] = []
    for attempt in judge_attempts:
        for key, target in (
            ("primary_error", judge_primary_errors),
            ("fallback_error", judge_fallback_errors),
            ("fallback_reason", judge_fallback_reasons),
        ):
            value = attempt.get(key)
            if isinstance(value, str) and value and value not in target:
                target.append(value)
    judge_fallback_attempted = any(
        bool(attempt.get("fallback_used"))
        or len(attempt.get("fallback_chain") or []) > 1
        for attempt in judge_attempts
    )
    if judge_primary_errors and not judge_generation.get("primary_error"):
        judge_generation["primary_error"] = judge_primary_errors[0]
    if judge_fallback_errors and not judge_generation.get("fallback_error"):
        judge_generation["fallback_error"] = judge_fallback_errors[0]
    if judge_fallback_reasons and not judge_generation.get("fallback_reason"):
        judge_generation["fallback_reason"] = judge_fallback_reasons[0]
    judge_generation["judge_attempts"] = judge_attempts
    judge_generation["judgeAttempts"] = judge_attempts
    judge_generation["fallback_attempted"] = judge_fallback_attempted
    judge_generation["fallbackAttempted"] = judge_fallback_attempted
    judge_generation["primary_errors"] = judge_primary_errors
    judge_generation["fallback_errors"] = judge_fallback_errors
    judge_generation["fallback_reasons"] = judge_fallback_reasons

    judge_usage = normalize_usage(judge_generation.get("usage"))
    judge_truncated = bool(
        judge_usage and judge_usage.get("output_tokens") is not None and int(judge_usage["output_tokens"]) >= judge_cap
    )
    metadata["judge_requested_model"] = judge_model
    metadata["judge_model_used"] = judge_model_used
    metadata["judge_actual_model"] = judge_model_used
    metadata["judge_provider"] = provider_for_model(judge_model_used)
    metadata["judge_fallback_chain"] = judge_generation.get("fallback_chain", [judge_model])
    metadata["judge_fallback_attempted"] = judge_fallback_attempted
    metadata["judge_attempts"] = judge_attempts
    metadata["judge_primary_error"] = judge_generation.get("primary_error") or (judge_primary_errors[0] if judge_primary_errors else None)
    metadata["judge_fallback_reason"] = judge_generation.get("fallback_reason") or (judge_fallback_reasons[0] if judge_fallback_reasons else None)
    metadata["judge_identity"] = panel_model_identity(
        requested_model=judge_model,
        actual_model=judge_model_used,
        fallback_chain=judge_generation.get("fallback_chain", [judge_model]),
        fallback_used=bool(judge_generation.get("fallback_used")),
        primary_error=judge_generation.get("primary_error"),
        fallback_error=judge_generation.get("fallback_error"),
        fallback_reason=judge_generation.get("fallback_reason"),
    )
    metadata["judge_identity"]["fallbackAttempted"] = judge_fallback_attempted
    if judge_generation.get("fallback_used"):
        judge_notice = (
            f"Judge fallback: requested {judge_model} failed; synthesis was produced by "
            f"{judge_model_used}."
        )
        caveats.append(judge_notice)
    elif judge_fallback_attempted:
        caveats.append(
            f"Judge fallback was attempted during an earlier synthesis attempt; final actual model: "
            f"{judge_model_used}."
        )
    metadata["judge_generation"] = judge_generation
    metadata["judge_retried"] = judge_retried
    metadata["judge_json_repaired"] = bool(parse_diagnostics.get("repaired"))
    metadata["judge_truncated"] = judge_truncated
    metadata["estimated_total"] = estimated_total
    metadata["estimated_cost"] = running_cost
    metadata["budget_exceeded"] = budget_exceeded
    metadata["panel_usage_totals"] = sum_usage([result.get("generation", {}) for result in panel_results])
    metadata["usage_totals"] = sum_usage([result.get("generation", {}) for result in panel_results], judge_generation)
    if findings_caveat:
        warning = findings_caveat
        if judge_truncated:
            warning += " Judge output also hit the output-token cap; the prose fallback may itself be incomplete."
        caveats.append(warning)
        metadata["findings_status"] = "fallback"
        findings = fallback_findings_contract(judge_text, [warning], parse_warning=warning)
        display_text = judge_text
    else:
        metadata["findings_status"] = "parsed"
        if findings is not None:
            if judge_truncated:
                warning = "Judge output hit the output-token cap; structured findings may be incomplete."
                existing = findings.get("parse_warning") or ""
                findings["parse_warning"] = " ".join(part for part in [existing, warning] if part).strip()
                caveats.append("Judge output hit the output-token cap; structured findings may be incomplete.")
            if parse_diagnostics.get("repaired"):
                caveats.append(
                    "Judge JSON was malformed or truncated and was repaired by truncating to the last complete JSON value."
                )
        display_text = render_panel_findings(findings or {}, caveats)

    # Make the panel-integrity disclosure part of the structured findings
    # contract as well as the top-level caveats.  Consumers using
    # ``--output findings`` must not be able to miss a collapsed or partial
    # panel merely because the judge returned valid JSON.  For parsed output,
    # render again so the deterministic summary/caveats are visible in the
    # human-readable text too; malformed/legacy judge output keeps its
    # historical prose payload while the findings object remains authoritative.
    findings = apply_panel_integrity_to_findings(
        findings,
        panel_status=panel_status,
        integrity_notice=integrity_notice,
        caveats=caveats,
    )
    # Phase 6: evidence-linked verification of findings
    if (
        not getattr(args, "no_verify", False)
        and isinstance(findings, dict)
        and findings.get("findings")
    ):
        review_ctx = metadata.get("_review_context") or {}
        workspace = Path(review_ctx.get("workspace_root") or Path.cwd())
        raw_findings = findings.get("findings", [])
        if isinstance(raw_findings, list):
            verified = verify_findings(raw_findings, workspace)
            findings["findings"] = verified
            verified_count = sum(1 for f in verified if f.get("evidence", "unverified") != "unverified")
            if verified_count:
                caveats.append(f"Verification: {verified_count}/{len(verified)} findings received tool-backed evidence")
    if metadata.get("findings_status") == "parsed" and isinstance(findings, dict):
        display_text = render_panel_findings(findings, [])

    # Keep the historical prose payload stable for a partially failed panel;
    # its structured metadata/caveat still carries the authoritative warning.
    # A fully completed but collapsed panel needs the notice inside the final
    # synthesis because otherwise the judge text could look like consensus.
    output_integrity_notice = (
        integrity_notice
        if panel_status == "degraded_single_model" and len(usable) == len(panel_results)
        else ""
    )
    if output_integrity_notice and output_integrity_notice not in display_text:
        if findings is not None:
            findings_caveats = findings.setdefault("caveats", [])
            if isinstance(findings_caveats, list) and output_integrity_notice not in findings_caveats:
                findings_caveats.insert(0, output_integrity_notice)
        display_text = "\n\n".join(part for part in [output_integrity_notice, display_text] if part).strip()
    metadata["findings"] = findings
    write_run_record(
        args,
        mode="panel",
        status="success",
        models=list(dict.fromkeys([*panel_models, str(judge_model), str(judge_model_used)])),
        base_url=args.base_url,
        prompt_text=prompt,
        output_text=display_text,
        caveats=caveats,
        metadata=metadata,
    )
    # Phase 8: passively record reflection data
    try:
        review_ctx = metadata.get("_review_context") or {}
        workspace = Path(review_ctx.get("workspace_root") or Path.cwd())
        record_review(
            repo_path=workspace,
            findings=findings.get("findings", []) if isinstance(findings, dict) else [],
            models=list(dict.fromkeys([*panel_models, str(judge_model)])),
            panel_status=panel_status,
            mode=args.mode,
            scope=metadata.get("scope", ""),
        )
    except Exception:
        pass  # Reflection recording is best-effort
    print_panel_result(
        panel_mode=args.mode,
        base_url=args.base_url,
        # Keep the public judge_model field as the requested lane.  The
        # metadata carries judge_actual_model/judge_model_used separately when
        # the judge itself falls back.
        judge_model=str(judge_model),
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
    progress(args, f"consult: querying model {getattr(args, 'model', 'sonnet')}")
    auto_route_model = None
    auto_route_reason = None
    if getattr(args, "auto_route", False) and args.model is None:
        auto_route_model, auto_route_reason = resolve_auto_model(default="sonnet")
        model = resolve_model(auto_route_model, default=DEFAULT_CONSULT_MODEL)
    else:
        model = resolve_model(args.model, default=DEFAULT_CONSULT_MODEL)
    prompt = read_prompt(args)
    caveats: list[str] = []
    
    # Pre-read files mentioned in the prompt to prevent hallucination
    read_files: list[str] = []
    if not getattr(args, "no_pre_read", False):
        prompt, file_caveats, read_files = build_consult_file_context(
            prompt, args.max_prompt_chars
        )
        caveats.extend(file_caveats)
    else:
        file_caveats: list[str] = []
    if read_files:
        progress(args, f"consult: pre-read {len(read_files)} file(s) for context")
    
    prompt = apply_prompt_limit(prompt, args.max_prompt_chars, caveats)
    estimated_cost = estimate_call_cost(model, len(prompt), args.max_output_tokens)
    if args.dry_run:
        print(format_dry_run(mode="consult", model=model,
            prompt_chars=len(prompt), max_output_tokens=args.max_output_tokens,
            output_json=args.json))
        if not read_files:
            print()
            print(prompt)
        return 0
    if args.budget is not None and estimated_cost > args.budget:
        raise AntiError(
            f"budget cap reached (estimated {estimated_cost:.4f} > {args.budget:.4f}); consult skipped"
        )
    ensure_run_id(args)
    text, model_used, generation_metadata = generate_with_fallback(
        args,
        model=model,
        prompt=prompt,
        max_output_tokens=args.max_output_tokens,
        purpose="consult",
    )
    attempts_metadata: list[dict[str, Any]] = [generation_metadata]
    usage = generation_metadata.get("usage")
    output_status = lane_output_status(text, usage, args.max_output_tokens)
    last_prompt_chars = len(prompt)
    if output_status == "truncated":
        retry_cap = min(PANEL_LANE_RETRY_CEILING_TOKENS, args.max_output_tokens * 2)
        progress(
            args,
            f"consult output hit the {args.max_output_tokens}-token cap; retrying once at {retry_cap} tokens",
        )
        retry_prompt = prompt + "\n\n" + lane_retry_instruction()
        last_prompt_chars = len(retry_prompt)
        text, model_used, retry_metadata = generate_with_fallback(
            args,
            model=model,
            prompt=retry_prompt,
            max_output_tokens=retry_cap,
            purpose="consult (retry)",
        )
        attempts_metadata.append(retry_metadata)
        usage = retry_metadata.get("usage")
        output_status = lane_output_status(text, usage, retry_cap)
        caveats.append("Consult output was truncated at the token cap and retried once at a higher cap")
        if output_status == "truncated":
            caveats.append(
                "Consult output still truncated at the higher output cap; raise --max-output-tokens for the full answer"
            )
    metadata = {
        "prompt_chars": last_prompt_chars,
        "budget_limit": args.budget,
        "estimated_total": actual_call_cost(model_used, attempts_metadata[-1], prompt_chars=last_prompt_chars, max_output_tokens=args.max_output_tokens),
        "budget_exceeded": False,
        **attempts_metadata[-1],
        "consult_attempts": attempts_metadata,
    }
    if auto_route_model:
        metadata["auto_route_decision"] = auto_route_model
        metadata["auto_route_reason"] = auto_route_reason
    if output_status != "success":
        metadata["status"] = output_status
    if read_files:
        metadata["pre_read_files"] = read_files
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
        force_full_output=output_status == "truncated",
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
    progress(args, f"review: analyzing scope '{getattr(args, 'scope', 'working-tree')}' with {getattr(args, 'model', 'opus')}")
    auto_route_model = None
    auto_route_reason = None
    context = None
    if getattr(args, "auto_route", False) and args.model is None:
        context = collect_review_context(args)
        auto_route_model, auto_route_reason = resolve_auto_model(
            scope=args.scope,
            diff_lines=len(context["diff"].splitlines()),
            file_paths=list(context["paths"]),
            default="sonnet",
        )
        model = resolve_model(auto_route_model, default=DEFAULT_REVIEW_MODEL)
    else:
        model = resolve_model(args.model, default=DEFAULT_REVIEW_MODEL)
    prompt_budget = prompt_budget_for_model(args, model)
    claude_guardrail_available = claude_guardrail_would_apply(args, model, prompt_budget)
    context = context or collect_review_context(args)
    if not context["diff"].strip() and not context["file_texts"]:
        raise empty_review_scope_error(args.scope)
    prompt, _paths, caveats, metadata = assemble_review_prompt_from_context(
        context,
        max_prompt_chars=prompt_budget,
    )
    if auto_route_model:
        metadata["auto_route_decision"] = auto_route_model
        metadata["auto_route_reason"] = auto_route_reason
    chunked_review = should_run_chunked_review(args, metadata)
    disclosure = byok_repo_context_disclosure(
        generation_models_for_disclosure([model], args),
        receives_repo_context=True,
    )
    if disclosure:
        caveats.append(disclosure)
        metadata.setdefault("privacy_disclosures", []).append(disclosure)
        if not args.print_prompt:
            eprint(f"[anti] {redact_sensitive_text(disclosure)}")
    if args.dry_run:
        print(format_dry_run(mode="review", model=model,
            prompt_chars=len(prompt), max_output_tokens=args.max_output_tokens,
            output_json=args.json))
        if chunked_review:
            plan_chunks, plan_metadata = build_review_chunk_prompts(
                context,
                max_prompt_chars=prompt_budget,
                max_chunks=args.max_review_chunks,
                priority_paths=getattr(args, "priority_file", None),
            )
            eprint(
                f"[anti] dry-run chunk plan: {len(plan_chunks)}/"
                f"{plan_metadata.get('planned_chunk_count', len(plan_chunks))} chunk(s)"
                + (
                    f"; {len(plan_metadata['omitted_items'])} item(s) would be omitted "
                    f"({plan_metadata.get('omitted_file_count', 0)} file(s)); "
                    "pass --allow-partial to continue partial"
                    if plan_metadata.get("omitted_items")
                    else ""
                )
            )
            for chunk in plan_chunks:
                eprint(f"  - {chunk['kind']}: {chunk['label']} ({chunk['prompt_chars']} chars)")
        return 0
    if args.print_prompt:
        if args.json:
            print(json.dumps({"prompt": prompt, "metadata": metadata, "caveats": caveats}, indent=2, sort_keys=True))
            return 0
        if args.dry_run:
            eprint(format_dry_run(mode="review", model=model,
                prompt_chars=len(prompt), max_output_tokens=args.max_output_tokens,
                output_json=args.json))
            return 0
        print(prompt)
        if caveats:
            print("\n## Assembly Caveats")
            for caveat in caveats:
                print(f"- {caveat}")
        return 0
    ensure_run_id(args)
    claude_guardrail_used = claude_guardrail_available and chunked_review
    if claude_guardrail_used:
        add_claude_guardrail_caveat(caveats, prompt_budget=prompt_budget)
    # The single-prompt assembly may have truncated the diff to fit one prompt;
    # chunked mode re-budgets the full diff, so that stale caveat must not leak
    # into every chunk prompt's manifest (B2).
    context["caveats"] = [
        caveat for caveat in caveats if not caveat.startswith(GIT_DIFF_TRUNCATION_CAVEAT)
    ]
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
    omitted_items = metadata.get("omitted_files") or []
    if metadata.get("status") == "incomplete" and omitted_items:
        omitted_file_count = int(metadata.get("omitted_file_count") or 0)
        if omitted_file_count:
            banner = (
                f"⚠ INCOMPLETE — {len(omitted_items)} item(s) / {omitted_file_count} file(s) NOT reviewed; "
                "this synthesis covers only the reviewed chunks, not the full requested scope "
                "(see metadata.omitted_items for the manifest)."
            )
        else:
            banner = (
                f"⚠ INCOMPLETE — {len(omitted_items)} item(s) NOT reviewed; "
                "this synthesis covers only the reviewed chunks, not the full requested scope "
                "(see metadata.omitted_items for the manifest)."
            )
        text = banner + "\n\n" + text
        caveats.insert(0, banner)
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
    # Phase 8: passively record reflection data
    try:
        scope_line = metadata.get("scope", "")
        workspace = Path.cwd()
        record_review(
            repo_path=workspace,
            findings=[],  # Reviews don't produce structured findings
            models=[str(model_used)],
            panel_status="single_model",
            mode="review",
            scope=scope_line,
        )
    except Exception:
        pass
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
    progress(args, f"plan: generating plan for scope '{getattr(args, 'scope', 'none')}' with {getattr(args, 'model', 'opus')}")
    model = resolve_model(args.model, default=DEFAULT_PLAN_MODEL)
    prompt_budget = prompt_budget_for_model(args, model)
    claude_guardrail_available = claude_guardrail_would_apply(args, model, prompt_budget)
    prompt, caveats = assemble_plan_prompt(args, apply_limit=False)
    disclosure = byok_repo_context_disclosure(
        generation_models_for_disclosure([model], args),
        receives_repo_context=args.scope != "none",
    )
    if disclosure:
        caveats.append(disclosure)
        if not args.print_prompt:
            eprint(f"[anti] {redact_sensitive_text(disclosure)}")
    recorded_prompt = prompt
    if args.dry_run:
        print(format_dry_run(mode="plan", model=model,
            prompt_chars=len(prompt), max_output_tokens=args.max_output_tokens,
            output_json=args.json))
        return 0
    if args.print_prompt:
        printable_caveats = list(caveats)
        printable_prompt = apply_prompt_limit(prompt, prompt_budget, printable_caveats)
        if args.json:
            print(json.dumps({"prompt": printable_prompt, "caveats": printable_caveats}, indent=2, sort_keys=True))
            return 0
        if args.dry_run:
            eprint(format_dry_run(mode="plan", model=model,
                prompt_chars=len(prompt), max_output_tokens=args.max_output_tokens,
                output_json=args.json))
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
    if disclosure:
        metadata.setdefault("privacy_disclosures", []).append(disclosure)
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
        "gateway_package_version": None,
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
        package_version = fetch_gateway_package_version(
            args.base_url,
            timeout=args.timeout,
            token_env=args.gateway_token_env,
        )
        statuses["gateway_package_version"] = package_version
        statuses["checks"].append(
            {
                "name": "health",
                "status": "pass",
                "package_version": package_version,
            }
        )
        if not args.json:
            print(f"[PASS] Gateway package version: {package_version}")
        # Version drift check: warn when installed gateway is older than the
        # repo checkout (which may contain fixes for known upstream bugs).
        try:
            import tomllib  # type: ignore[import-not-found]
        except ImportError:
            try:
                import tomli as tomllib  # type: ignore[no-redef, import-not-found]
            except ImportError:
                tomllib = None  # type: ignore[assignment, no-redef]
        if tomllib is not None:
            # Walk upward from the script location to find pyproject.toml.
            # This works for both the repo copy and the installed skill copy.
            pyproject = None
            for parent in Path(__file__).resolve().parents:
                candidate = parent / "pyproject.toml"
                if candidate.is_file():
                    pyproject = candidate
                    break
            if pyproject is not None and pyproject.is_file():
                try:
                    if tomllib is not None:
                        with open(pyproject, "rb") as fh:
                            repo_version = str(tomllib.load(fh).get("project", {}).get("version", "")).strip()
                    else:
                        # Minimal fallback for environments without tomllib/tomli
                        # (Python 3.10 without extras): parse the project version
                        # line directly instead of silently skipping drift checks.
                        text_content = pyproject.read_text(encoding="utf-8")
                        version_match = re.search(
                            r'^version\s*=\s*["\']([^"\']+)["\']',
                            text_content,
                            re.MULTILINE,
                        )
                        repo_version = version_match.group(1).strip() if version_match else ""
                    if repo_version and package_version != repo_version:
                        def _vkey(v: str) -> tuple[int, ...]:
                            try:
                                parts = tuple(int(x) for x in re.findall(r"\d+", v)[:3])
                                return parts if parts else (0,)
                            except (ValueError, TypeError):
                                return (0,)
                        if _vkey(package_version) < _vkey(repo_version):
                            statuses["version_drift"] = {
                                "installed": package_version,
                                "repo": repo_version,
                                "direction": "installed_older",
                            }
                            statuses["checks"].append({
                                "name": "version_drift",
                                "status": "warn",
                                "detail": (
                                    f"Installed gateway {package_version} is older than repo checkout {repo_version}. "
                                    "Known bug fixes may be missing. Consider: pip install --upgrade codex-antigravity-auth "
                                    f"or pip install -e '{pyproject.parent}'"
                                ),
                            })
                            if not args.json:
                                print(f"[WARN] Version drift: installed {package_version} < repo {repo_version}")
                except Exception:
                    pass
    except AntiError as exc:
        warning = redact_sensitive_text(str(exc))
        statuses["checks"].append({"name": "health", "status": "warn", "detail": warning})
        if not args.json:
            print(f"[WARN] Gateway /health: {warning}")

    try:
        ids = fetch_model_ids(args.base_url, timeout=args.timeout, token_env=args.gateway_token_env)
        statuses["models_reachable"] = True
        statuses["checks"].append({"name": "models", "status": "pass", "count": len(ids)})
        if not args.json:
            print(f"[PASS] Gateway /v1/models: {len(ids)} model(s)")
        requested_models = args.model or ["opus", "sonnet"]
        missing_models = []
        for model in [resolve_model(item, default=item) for item in requested_models]:
            if any(catalog_model_matches(model, advertised) for advertised in ids):
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

    if getattr(args, "check_documented", False) and statuses.get("models_reachable"):
        documented = sorted({resolve_model(alias, default=alias) for alias in MODEL_ALIASES})
        missing_documented = [
            model_id
            for model_id in documented
            if not any(catalog_model_matches(model_id, advertised) for advertised in ids)
        ]
        # openrouter:openrouter/auto is a legitimate OpenRouter id; only warn
        # when a vendor path remains after the duplicated prefix (the form the
        # upstream API rejects, mirroring the gateway's normalize rule).
        double_prefixed = sorted(
            advertised
            for advertised in ids
            if advertised.startswith("openrouter:openrouter/")
            and "/" in advertised[len("openrouter:openrouter/"):]
        )
        if missing_documented:
            statuses["checks"].append(
                {
                    "name": "documented-models",
                    "status": "warn",
                    "detail": "documented model ids not advertised by /v1/models",
                    "missing": missing_documented,
                }
            )
            if not args.json:
                print(
                    "[WARN] Documented model(s) not advertised by /v1/models: "
                    + ", ".join(missing_documented)
                )
        if double_prefixed:
            statuses["checks"].append(
                {
                    "name": "catalog-prefix",
                    "status": "warn",
                    "detail": (
                        "gateway advertises double-prefixed openrouter ids that upstream is likely to reject"
                    ),
                    "ids": double_prefixed,
                }
            )
            if not args.json:
                print(
                    "[WARN] Gateway advertises double-prefixed openrouter ids (likely rejected upstream): "
                    + ", ".join(double_prefixed)
                )
        if not missing_documented and not double_prefixed:
            statuses["checks"].append(
                {"name": "documented-models", "status": "pass", "count": len(documented)}
            )
            if not args.json:
                print(f"[PASS] All {len(documented)} documented model ids advertised or alias-normalized")

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


def _install_run_signal_handlers(args: argparse.Namespace) -> None:
    """Write an interrupted record (over the running placeholder) on SIGTERM/SIGHUP."""
    if not hasattr(args, "save_output") or save_output_mode(args) == "never":
        return

    def handler(signum: int, _frame: Any) -> None:
        if not getattr(args, "run_record_written", False):
            try:
                write_run_record(
                    args,
                    mode=getattr(args, "command", "unknown"),
                    status="interrupted",
                    models=[],
                    base_url=getattr(args, "base_url", None),
                    metadata={"request_log_correlation_id": getattr(args, "run_id", None)},
                    error=f"terminated by signal {signum}",
                )
            except Exception:
                pass
        raise SystemExit(128 + signum)

    for signum in (signal.SIGTERM, getattr(signal, "SIGHUP", None)):
        if signum is None:
            continue
        try:
            signal.signal(signum, handler)
        except (OSError, ValueError):
            pass


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


def _panel_argv(
    *,
    mode: str,
    scope: str | None,
    common: list[str],
    args: argparse.Namespace,
    roles: list[str],
    models: list[str],
    prompt: str | None = None,
    extra: list[str] | None = None,
) -> list[str]:
    argv = ["panel", "--mode", mode]
    if args.allow_partial:
        argv.append("--allow-partial")
    if scope is not None:
        argv.extend(["--scope", scope])
    argv.extend(["--judge", args.judge,
            "--judge-output-tokens", str(args.judge_output_tokens),
            "--max-synthesis-chars", str(args.max_synthesis_chars),
            "--max-parallel", str(args.max_parallel),
            "--output", args.output, *common])
    if mode == "review":
        argv.extend(["--chunked", args.chunked,
                      "--max-review-chunks", str(args.max_review_chunks),
                      "--chunk-output-tokens", str(args.chunk_output_tokens)])
    if extra:
        argv.extend(extra)
    if args.min_successes is not None:
        argv.extend(["--min-successes", str(args.min_successes)])
    for role in args.role or roles:
        argv.extend(["--role", role])
    for model in args.model or models:
        argv.extend(["--model", model])
    if prompt:
        argv.extend(["--prompt", prompt])
    if getattr(args, "no_anonymize", False):
        argv.append("--no-anonymize")
    if getattr(args, "no_verify", False):
        argv.append("--no-verify")
    return argv


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
    if args.budget is not None:
        common.extend(["--budget", str(args.budget)])
    if args.max_output_tokens is not None:
        common.extend(["--max-output-tokens", str(args.max_output_tokens)])
    if args.fallback_model:
        common.extend(["--fallback-model", args.fallback_model])
    if args.run_id:
        common.extend(["--run-id", args.run_id])
    if args.progress:
        common.append("--progress")
    if args.run_label:
        common.extend(["--run-label", args.run_label])
    if args.json:
        common.append("--json")
    if args.print_prompt:
        common.append("--print-prompt")
    if args.dry_run:
        common.append("--dry-run")
        common.append("--print-prompt")

    if args.name == "review-ready":
        scope = workflow_scope(args, default="staged")
        argv = _panel_argv(
            mode="review", scope=scope, common=common, args=args,
            roles=["correctness", "security", "tests", "install-docs"],
            models=args.model or [],
        )
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
        if not args.fallback_model and args.fallback_policy != "never":
            argv.extend(["--fallback-model", "sonnet"])
    elif args.name == "ship-gate":
        scope = workflow_scope(args, default="staged")
        argv = _panel_argv(
            mode="review", scope=scope, common=common, args=args,
            roles=["correctness", "security", "tests", "install", "release"],
            models=args.model or [],
            prompt="Assess merge readiness. Focus on concrete blockers, install/use regressions, missing tests, release caveats, and what native Codex must verify locally before commit or merge.",
        )
    elif args.name == "security-review":
        scope = workflow_scope(args, default="staged")
        argv = _panel_argv(
            mode="review", scope=scope, common=common, args=args,
            roles=["injection", "secrets-handling", "authz", "dependency-surface"],
            models=args.model or [],
            prompt="Run a security-focused review. Prioritize prompt-injection surfaces, secret handling, authorization and trust boundaries, dependency/config exposure, and concrete local verification steps.",
        )
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
        argv = _panel_argv(
            mode="ask", scope=None, common=common, args=args,
            roles=args.role or [],
            models=args.model or ["sonnet", "opus"],
        )
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
    elif args.name == "quick-check":
        scope = workflow_scope(args, default="staged")
        # Fast pre-commit gate: cheap models, short timeout, no opus
        cheap_common = [a for a in common if a not in ("--timeout",)]
        cheap_common.extend(["--timeout", "60", "--max-prompt-chars", "20000"])
        argv = ["panel", "--mode", "review", "--scope", scope]
        argv.extend(cheap_common)
        argv.extend(["--judge", "nemotron-ultra",
                      "--judge-output-tokens", "2048",
                      "--max-parallel", "2",
                      "--output", args.output])
        if args.allow_partial:
            argv.append("--allow-partial")
        for role in ["correctness", "security"]:
            argv.extend(["--role", role])
        for model in args.model or ["flash-3.6", "poolside"]:
            argv.extend(["--model", model])
        prompt = args.prompt or "Quick pre-commit check. Flag only high-confidence blockers. Be terse."
        argv.extend(["--prompt", prompt])
        append_each(argv, "--file", args.file)
        append_each(argv, "--files-from", args.files_from)
        append_each(argv, "--priority-file", args.priority_file)
    elif args.name == "consensus":
        scope = workflow_scope(args, default="staged")
        # H-5: consensus requires at least 2 models to detect disagreements
        consensus_models = args.model or ["sonnet", "opus", "flash-3.6"]
        # Don't pass prompt here; let post-expansion handle user prompt or default
        argv = _panel_argv(
            mode="review", scope=scope, common=common, args=args,
            roles=["correctness", "security", "tests"],
            models=consensus_models,
        )
        # Ensure min-successes is at least 2 for meaningful disagreement detection
        if args.min_successes is None or args.min_successes < 2:
            argv.extend(["--min-successes", "2"])
    else:
        raise AntiError(f"unknown workflow: {args.name}")

    if args.name in {"review-ready", "ship-gate", "security-review", "quick-check", "consensus"}:
        append_if_present(argv, "--base", args.base)
        append_if_present(argv, "--changed-files", args.changed_files_range)
        append_each(argv, "--file", args.file)
        append_each(argv, "--files-from", args.files_from)
        append_each(argv, "--priority-file", args.priority_file)
    elif args.name == "plan-deep":
        append_each(argv, "--file", args.file)
    if args.name != "debug-consensus":
        append_if_present(argv, "--prompt-file", args.prompt_file)
    prompt = args.prompt or " ".join(args.prompt_parts or []).strip()
    if args.name in {"plan-deep", "provider-compare", "debug-consensus", "quick-check", "consensus"} and not prompt and not args.prompt_file:
        if args.name == "plan-deep":
            prompt = (
                "Create a decision-complete autonomous implementation plan for the current Codex task. "
                "Include phases, risks, validation commands, fallback choices, and non-claims."
            )
        elif args.name == "provider-compare":
            raise AntiError("provider-compare requires --prompt, --prompt-file, or positional prompt text")
        elif args.name == "quick-check":
            prompt = "Quick pre-commit check. Flag only high-confidence blockers. Be terse."
        elif args.name == "consensus":
            prompt = "Focus on disagreements between reviewers. For consensus items, state briefly and move on. For unique insights from each perspective, elaborate."
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
    # Rebind signal handlers to the expanded args so SIGTERM/SIGHUP during the
    # inner command overwrites the inner placeholder (auto-generated ids are
    # unknown to the outer args until the finally-block runs).
    _install_run_signal_handlers(expanded_args)
    try:
        return int(expanded_args.func(expanded_args))
    finally:
        # Propagate the inner run id so lifecycle handlers on the outer args
        # overwrite the same placeholder instead of orphaning it (B5).
        if getattr(expanded_args, "run_id", None):
            args.run_id = expanded_args.run_id
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
            size = path.stat().st_size
            if size == 0:
                rows.append(
                    {
                        "id": path.stem,
                        "created_at": None,
                        "mode": None,
                        "status": "interrupted",
                        "workflow": None,
                        "models": [],
                        "run_label": None,
                        "size": 0,
                        "interrupted": True,
                    }
                )
                continue
            try:
                data = load_run_record(path)
            except AntiError:
                rows.append(
                    {
                        "id": path.stem,
                        "created_at": None,
                        "mode": None,
                        "status": "corrupt",
                        "workflow": None,
                        "models": [],
                        "run_label": None,
                        "size": size,
                        "interrupted": True,
                    }
                )
                continue
            rows.append(
                {
                    "id": data.get("id") or path.stem,
                    "created_at": data.get("created_at"),
                    "mode": data.get("mode"),
                    "status": data.get("status"),
                    "workflow": data.get("workflow"),
                    "models": data.get("models", []),
                    "run_label": data.get("run_label"),
                    "size": size,
                    "interrupted": data.get("status") == "running",
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
                flag = ""
                if row.get("interrupted"):
                    flag = " (no final record; run may have been interrupted)"
                print(f"{row['created_at']} {row['id']} {row['mode']} {row['status']}{workflow}{label} [{models}]{flag}")
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
        if RUNS_DIR.exists():
            for path in RUNS_DIR.glob("*.json.tmp"):
                if path.stat().st_mtime < cutoff:
                    if args.dry_run:
                        print(f"[*] Would remove {path.name}")
                    else:
                        path.unlink()
                    removed += 1
        reflection_removed = prune_reflections_older_than(cutoff, dry_run=args.dry_run)
        verb = "Would remove" if args.dry_run else "Removed"
        print(f"[+] {verb} {removed} Anti run record(s) older than {args.older_than} day(s)")
        print(f"[+] {verb} {reflection_removed} reflection record file(s) older than {args.older_than} day(s)")
        return 0
    if args.runs_command == "reflections":
        repo = Path(args.repo).resolve()
        if args.clear:
            count = clear_records(repo)
            print(f"[+] Cleared {count} reflection record(s) for {repo}")
            return 0
        summary = get_summary(repo)
        if summary.get("records", 0) == 0:
            print(f"[*] No reflection records for {repo}")
            return 0
        print(f"## Reflection Summary: {repo.name}")
        print(f"- Records: {summary['records']}")
        print(f"- Total findings: {summary['total_findings']}")
        print(f"- Recurring fingerprints: {summary['recurring_fingerprints']}")
        if summary.get("date_range"):
            print(f"- Date range: {summary['date_range'][0]} to {summary['date_range'][1]}")
        if summary.get("top_recurring"):
            print(f"- Most recurring findings:")
            for fp, count in summary["top_recurring"]:
                print(f"  - {fp[:16]}... ({count} times)")
        if summary.get("most_reviewed_files"):
            print(f"- Most reviewed files:")
            for fname, count in summary["most_reviewed_files"][:5]:
                print(f"  - {fname} ({count} findings)")
        if summary.get("severity_distribution"):
            print(f"- Severity distribution: {summary['severity_distribution']}")
        if summary.get("models_used"):
            print(f"- Models used: {summary['models_used']}")
        # Show recent records
        records = list_records(repo, limit=args.limit)
        if records:
            print(f"\n## Recent Records (last {len(records)})")
            for r in records:
                ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(r.get("timestamp", 0)))
                print(f"  [{ts}] {r.get('mode','?')} | {r.get('panel_status','?')} | {r.get('findings_count',0)} findings | models: {', '.join(r.get('models',[]))}")
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
    parser.add_argument("--auto-route", action="store_true", help="Automatically pick the cheapest adequate model based on diff size and risk")
    parser.add_argument("--fallback-model", help="Fallback model alias/id for retryable or timeout failures")
    parser.add_argument(
        "--fallback-policy",
        choices=sorted(FALLBACK_POLICIES),
        default="never",
        help="When to use --fallback-model",
    )
    parser.add_argument("--progress", action=argparse.BooleanOptionalAction, default=True, help="Print long-call progress to stderr (default: true; use --no-progress to disable)")
    parser.add_argument("--run-label", help="Optional label for saved Anti run metadata")
    parser.add_argument("--run-id", help="Stable run/correlation id for saved and gateway records")
    parser.add_argument("--budget", type=float, default=None, help="Maximum estimated cost for the entire run (cost units); skip remaining models if exceeded")
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
    # --collab removed: no active collaboration profiles
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
    panel.add_argument("--max-output-tokens", type=positive_int, default=6144, help="Max output tokens per panel model")
    panel.add_argument("--judge-output-tokens", type=positive_int, default=8192, help="Max output tokens for judge synthesis")
    panel.add_argument("--max-prompt-chars", type=non_negative_int, default=DEFAULT_MAX_PROMPT_CHARS, help=MAX_PROMPT_CHARS_HELP)
    panel.add_argument("--max-synthesis-chars", type=non_negative_int, default=DEFAULT_MAX_SYNTHESIS_CHARS)
    panel.add_argument(
        "--chunked",
        choices=["auto", "always", "off"],
        default="auto",
        help="Summarize broad review scopes before panel fan-out when needed",
    )
    panel.add_argument(
        "--max-review-chunks",
        type=non_negative_int,
        default=8,
        help="Maximum chunk calls before panel fan-out; 0 = review everything, however many chunks it takes",
    )
    panel.add_argument(
        "--allow-partial",
        action="store_true",
        help="Continue with a partial chunked summary when the scope exceeds --max-review-chunks",
    )
    panel.add_argument("--priority-file", action="append", help="Review these paths first when chunking; repeatable")
    panel.add_argument("--chunk-output-tokens", type=positive_int, default=4096, help="Max output tokens per review chunk")
    panel.add_argument(
        "--min-successes",
        type=positive_int,
        help="Minimum distinct actual provider/model identities before judging",
    )
    panel.add_argument("--max-parallel", type=positive_int, default=3, help="Maximum concurrent panel model calls")
    panel.add_argument("--retry", type=non_negative_int, default=1, help="Retry transient gateway/backend failures")
    panel.add_argument("--output", choices=sorted(PANEL_OUTPUT_MODES), default="prose", help="Render prose or findings JSON")
    panel.add_argument("--json", action="store_true", help="Emit structured JSON output")
    panel.add_argument("--print-prompt", action="store_true", help="Print assembled source prompt without contacting gateway")
    panel.add_argument("--dry-run", action="store_true", help="Print assembled prompt with token and cost estimates without contacting gateway")
    panel.add_argument("--no-anonymize", action="store_true", help="Do not anonymize lane labels before judge synthesis")
    panel.add_argument("--no-verify", action="store_true", help="Skip evidence-linked verification of findings")
    panel.add_argument("prompt_parts", nargs="*", help="Positional ask/planning prompt text")
    panel.set_defaults(func=command_panel)

    consult = sub.add_parser("consult", aliases=["ask"], help="Ask Antigravity an explicit prompt")
    add_gateway_args(consult, default_timeout=120.0)
    add_generation_control_args(consult)
    consult.add_argument("--model", default=None, help="opus, sonnet, or full model id")
    consult.add_argument("--prompt", help="Prompt text")
    consult.add_argument("--prompt-file", help="Read prompt text from file")
    consult.add_argument("--no-pre-read", action="store_true", dest="no_pre_read", help="Disable automatic file pre-reading for consult prompts")
    consult.add_argument("--max-output-tokens", type=positive_int, default=4096)
    consult.add_argument("--max-prompt-chars", type=non_negative_int, default=DEFAULT_MAX_PROMPT_CHARS, help="Maximum prompt chars before truncation; use 0 for unlimited")
    consult.add_argument("--retry", type=non_negative_int, default=1, help="Retry transient gateway/backend failures")
    consult.add_argument("--dry-run", action="store_true", help="Print assembled prompt with token and cost estimates without contacting gateway")
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
    plan.add_argument("--dry-run", action="store_true", help="Print assembled prompt with token and cost estimates without contacting gateway")
    plan.add_argument("prompt_parts", nargs="*", help="Positional planning goal text")
    plan.set_defaults(func=command_plan)

    review = sub.add_parser("review", help="Review git diffs or selected files with Antigravity")
    add_gateway_args(review, default_timeout=120.0)
    add_generation_control_args(review)
    review.add_argument("--model", default=None, help="opus, sonnet, or full model id")
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
    review.add_argument(
        "--max-review-chunks",
        type=non_negative_int,
        default=8,
        help="Maximum chunk calls before synthesis; 0 = review everything, however many chunks it takes",
    )
    review.add_argument(
        "--allow-partial",
        action="store_true",
        help="Continue with a partial review when the scope exceeds --max-review-chunks",
    )
    review.add_argument(
        "--priority-file",
        action="append",
        help="Review these paths first when chunking; repeatable",
    )
    review.add_argument("--chunk-output-tokens", type=positive_int, default=4096, help="Max output tokens per chunk review")
    review.add_argument(
        "--max-synthesis-chars",
        type=non_negative_int,
        default=DEFAULT_MAX_SYNTHESIS_CHARS,
        help="Maximum synthesis prompt chars after chunk outputs; use 0 for unlimited",
    )
    review.add_argument("--json", action="store_true", help="Emit structured JSON output")
    review.add_argument("--print-prompt", action="store_true", help="Print assembled prompt without contacting gateway")
    review.add_argument("--dry-run", action="store_true", help="Print assembled prompt with token and cost estimates without contacting gateway")
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
            "quick-check",
            "consensus",
        ],
    )
    workflow.add_argument("--panel-mode", choices=["review", "plan", "ask"], default="review", help="Panel mode for collaboration workflows")
    workflow.add_argument("--no-anonymize", action="store_true", help="Do not anonymize lane labels before judge synthesis")
    workflow.add_argument("--no-verify", action="store_true", help="Skip evidence-linked verification of findings")
    workflow.add_argument("--model", action="append", help="Model alias/id for the workflow; repeatable for panels")
    workflow.add_argument("--judge", default="opus")
    workflow.add_argument("--role", action="append")
    workflow.add_argument("--scope", choices=["auto", "none", "working-tree", "staged", "files", "diff"], default="auto")
    workflow.add_argument("--base")
    workflow.add_argument("--changed-files", dest="changed_files_range", help="Git revision range for --scope diff")
    workflow.add_argument("--file", action="append")
    workflow.add_argument("--files-from", action="append")
    workflow.add_argument("--priority-file", action="append")
    workflow.add_argument("--allow-partial", action="store_true")
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
    workflow.add_argument(
        "--max-review-chunks",
        type=non_negative_int,
        default=8,
        help="Maximum chunk calls before synthesis; 0 = review everything, however many chunks it takes",
    )
    workflow.add_argument("--max-plan-chunks", type=positive_int, default=6)
    workflow.add_argument("--chunk-output-tokens", type=positive_int, default=2048)
    workflow.add_argument("--output", choices=sorted(PANEL_OUTPUT_MODES), default="prose")
    workflow.add_argument("--json", action="store_true")
    workflow.add_argument("--print-prompt", action="store_true")
    workflow.add_argument("--dry-run", action="store_true")
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
    runs_reflections = runs_sub.add_parser("reflections", help="Show repo-level reflection history")
    runs_reflections.add_argument("--repo", default=".", help="Repository path (default: cwd)")
    runs_reflections.add_argument("--limit", type=positive_int, default=10)
    runs_reflections.add_argument("--clear", action="store_true", help="Delete all reflection records for this repo")
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
    smoke.add_argument(
        "--check-documented",
        action="store_true",
        help="Diff the documented model alias table against /v1/models and report drift",
    )
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


def _extract_error_diagnostics(exc: AntiError, args: argparse.Namespace) -> dict[str, Any]:
    """Extract structured diagnostics from an AntiError for run records.

    Returns requested models, token usage estimates, and failed-lane summaries
    so failed runs carry the same observability as successful ones.
    """
    gen_meta = getattr(exc, "generation_metadata", None) or {}
    requested_models = getattr(args, "resolved_panel_models", None) or []
    if not requested_models and isinstance(gen_meta, dict):
        chain = gen_meta.get("fallbackChain") or gen_meta.get("fallback_chain") or []
        if chain:
            requested_models = list(chain)
    prompt_chars = 0
    output_chars = 0
    failed_lanes: list[dict[str, Any]] = []
    if isinstance(gen_meta, dict):
        try:
            prompt_chars = int(gen_meta.get("prompt_chars") or 0)
        except (ValueError, TypeError):
            pass
        try:
            output_chars = int(gen_meta.get("output_chars") or 0)
        except (ValueError, TypeError):
            pass
        failed_lanes = [
            {
                "model": item.get("model"),
                "error": redact_sensitive_text(item.get("error", "")),
            }
            for item in (gen_meta.get("generation_failures") or [])
            if isinstance(item, dict)
        ]
    return {
        "requested_models": requested_models,
        "prompt_chars": prompt_chars,
        "output_chars": output_chars,
        "failed_lanes": failed_lanes,
    }


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        _install_run_signal_handlers(args)
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
                run_id = getattr(args, "run_id", None)
                correlation = {"request_log_correlation_id": run_id} if run_id else {}
                gen_meta = getattr(exc, "generation_metadata", None) or {}
                diagnostics = _extract_error_diagnostics(exc, args)
                requested = diagnostics["requested_models"]
                prompt_chars = diagnostics["prompt_chars"]
                output_chars = diagnostics["output_chars"]
                write_run_record(
                    args,
                    mode=getattr(args, "command", "unknown"),
                    status="error",
                    models=requested,
                    base_url=getattr(args, "base_url", None),
                    caveats=[],
                    metadata={
                        **correlation,
                        "failed_lanes": diagnostics["failed_lanes"],
                        "requested_model": gen_meta.get("requestedModel") or gen_meta.get("primary_model"),
                        "actual_model": gen_meta.get("actualModel"),
                        "fallback_used": bool(gen_meta.get("fallbackUsed")),
                        "prompt_chars": prompt_chars,
                        "output_chars": output_chars,
                    },
                    error=str(exc),
                )
            except Exception:
                pass
        eprint(f"[anti] {redact_sensitive_text(str(exc))}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
