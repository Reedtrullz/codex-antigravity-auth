from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class NativeModel:
    id: str
    backend_id: str
    display_name: str
    context_window: int
    family: str
    default_reasoning_level: str = "high"
    supports_parallel_tool_calls: bool = True
    aliases: tuple[str, ...] = ()


DEFAULT_CLAUDE_MODEL_ID = "claude-3.5-sonnet"
DEFAULT_GEMINI_MODEL_ID = "gemini-3.5-flash-high"
DEFAULT_CODEX_MODEL_ID = DEFAULT_CLAUDE_MODEL_ID


NATIVE_MODELS: tuple[NativeModel, ...] = (
    NativeModel(
        id="gemini-3.5-flash-high",
        backend_id="gemini-3-flash-agent",
        display_name="Gemini 3.5 Flash (Agent High)",
        context_window=1_000_000,
        family="gemini",
    ),
    NativeModel(
        id="gemini-3.5-flash-medium",
        backend_id="gemini-3.5-flash-low",
        display_name="Gemini 3.5 Flash (General)",
        context_window=1_000_000,
        family="gemini",
        aliases=("gemini-3.5-flash", "gemini-3.5-flash-low"),
    ),
    NativeModel(
        id="gemini-3.1-pro-high",
        backend_id="gemini-3.1-pro-high",
        display_name="Gemini 3.1 Pro (Reasoning)",
        context_window=1_000_000,
        family="gemini",
    ),
    NativeModel(
        id="claude-3.5-sonnet",
        backend_id="claude-sonnet-4-6",
        display_name="Claude Sonnet 4.6 (Google)",
        context_window=200_000,
        family="claude",
        aliases=("sonnet", "claude-sonnet", "claude-3-5-sonnet", "claude-sonnet-4-6"),
    ),
    NativeModel(
        id="claude-opus-4-6",
        backend_id="claude-opus-4-6-thinking",
        display_name="Claude Opus 4.6 (Google)",
        context_window=200_000,
        family="claude",
        default_reasoning_level="xhigh",
        aliases=("opus", "claude-opus", "claude-opus-4-6-thinking"),
    ),
)


RESERVED_GOOGLE_MODEL_PREFIXES = {"openai", "openai-responses"}
LEGACY_BACKEND_ALIASES = {
    "gemini-3.1-pro": "gemini-3.1-pro-low",
    "gemini-3.1-pro-low": "gemini-3.1-pro-low",
}
NATIVE_MODEL_BY_ID = {model.id: model for model in NATIVE_MODELS}
MODEL_ALIAS_MAP: dict[str, str] = {}
for native_model in NATIVE_MODELS:
    MODEL_ALIAS_MAP[native_model.id.lower()] = native_model.id
    MODEL_ALIAS_MAP[native_model.backend_id.lower()] = native_model.id
    for alias in native_model.aliases:
        MODEL_ALIAS_MAP[alias.lower()] = native_model.id


def _slug_variants(value: str) -> set[str]:
    return {
        value,
        value.replace("-3-5-", "-3.5-"),
        value.replace("-3-1-", "-3.1-"),
        value.replace("-4-6", "-4.6"),
        value.replace("-4.6", "-4-6"),
    }


def canonical_model_id(model: str) -> str:
    """Resolve a user-facing Google/Antigravity alias to the canonical Codex model id."""
    lower = str(model).strip().lower()
    if "/" in lower:
        prefix, rest = lower.split("/", 1)
        if prefix in RESERVED_GOOGLE_MODEL_PREFIXES:
            return canonical_model_id(rest)
        return lower
    for variant in _slug_variants(lower):
        if variant in MODEL_ALIAS_MAP:
            return MODEL_ALIAS_MAP[variant]
    return lower


def native_model_definition(model: str) -> NativeModel | None:
    return NATIVE_MODEL_BY_ID.get(canonical_model_id(model))


def native_model_family(model: str) -> str:
    definition = native_model_definition(model)
    if definition:
        return definition.family
    return "claude" if "claude" in str(model).lower() else "gemini"


def native_model_catalog() -> list[dict[str, Any]]:
    return [
        {
            "id": model.id,
            "backend_id": model.backend_id,
            "display_name": model.display_name,
            "context_window": model.context_window,
            "family": model.family,
            "default_reasoning_level": model.default_reasoning_level,
            "supports_parallel_tool_calls": model.supports_parallel_tool_calls,
            "aliases": list(model.aliases),
        }
        for model in NATIVE_MODELS
    ]


def resolve_backend_model(model: str) -> str:
    """Resolve Codex-provided model name to official Antigravity backend model."""
    lower = str(model).strip().lower()
    if "/" in lower:
        prefix, rest = lower.split("/", 1)
        if prefix in RESERVED_GOOGLE_MODEL_PREFIXES:
            return resolve_backend_model(rest)
        return lower
    for variant in _slug_variants(lower):
        if variant in LEGACY_BACKEND_ALIASES:
            return LEGACY_BACKEND_ALIASES[variant]
    canonical = canonical_model_id(lower)
    definition = NATIVE_MODEL_BY_ID.get(canonical)
    if definition:
        return definition.backend_id
    return canonical
