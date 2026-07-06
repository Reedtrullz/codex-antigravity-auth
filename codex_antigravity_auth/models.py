from __future__ import annotations

from dataclasses import dataclass
import ast
import json
import os
import re
from pathlib import Path
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
MODEL_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
MODEL_FAMILY_VALUES = {"claude", "gemini"}
REASONING_LEVEL_VALUES = {"low", "medium", "high", "xhigh"}
MODEL_OVERLAY_FILE = "~/.codex/antigravity-models.toml"


def _slug_variants(value: str) -> set[str]:
    return {
        value,
        value.replace("-3-5-", "-3.5-"),
        value.replace("-3-1-", "-3.1-"),
        value.replace("-4-6", "-4.6"),
        value.replace("-4.6", "-4-6"),
    }


def model_overlay_path() -> Path:
    return Path(os.path.expanduser(MODEL_OVERLAY_FILE))


def _validate_model_text(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string")
    text = value.strip()
    if not text:
        raise ValueError(f"{label} must not be empty")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in text):
        raise ValueError(f"{label} must not contain control characters")
    return text


def validate_model_id(value: Any, label: str = "model id") -> str:
    text = _validate_model_text(value, label)
    if not MODEL_ID_RE.fullmatch(text):
        raise ValueError(f"{label} may contain only letters, numbers, dots, underscores, and hyphens")
    return text


def validate_model_aliases(value: Any) -> tuple[str, ...]:
    if value in (None, ""):
        return ()
    if not isinstance(value, list):
        raise ValueError("aliases must be a list of strings")
    aliases = []
    for alias in value:
        aliases.append(validate_model_id(alias, "model alias"))
    return tuple(dict.fromkeys(aliases))


def validate_overlay_model(data: dict[str, Any]) -> NativeModel:
    model_id = validate_model_id(data.get("id"))
    backend_id = _validate_model_text(data.get("backend_id") or data.get("backendId"), "backend id")
    display_name = _validate_model_text(data.get("display_name") or data.get("displayName") or model_id, "display name")
    try:
        context_window = int(data.get("context_window") or data.get("contextWindow"))
    except (TypeError, ValueError) as exc:
        raise ValueError("context_window must be a positive integer") from exc
    if context_window <= 0:
        raise ValueError("context_window must be a positive integer")
    family = _validate_model_text(data.get("family"), "family").lower()
    if family not in MODEL_FAMILY_VALUES:
        raise ValueError("family must be claude or gemini")
    default_reasoning_level = str(data.get("default_reasoning_level") or data.get("defaultReasoningLevel") or "high").lower()
    if default_reasoning_level not in REASONING_LEVEL_VALUES:
        raise ValueError("default_reasoning_level must be low, medium, high, or xhigh")
    supports_parallel_tool_calls = data.get("supports_parallel_tool_calls")
    if supports_parallel_tool_calls is None:
        supports_parallel_tool_calls = data.get("supportsParallelToolCalls", True)
    if not isinstance(supports_parallel_tool_calls, bool):
        raise ValueError("supports_parallel_tool_calls must be a boolean")
    aliases = validate_model_aliases(data.get("aliases"))
    return NativeModel(
        id=model_id,
        backend_id=backend_id,
        display_name=display_name,
        context_window=context_window,
        family=family,
        default_reasoning_level=default_reasoning_level,
        supports_parallel_tool_calls=supports_parallel_tool_calls,
        aliases=aliases,
    )


def _parse_toml_scalar(value: str) -> Any:
    value = value.strip()
    if value.lower() == "true":
        return True
    if value.lower() == "false":
        return False
    if value.startswith("[") and value.endswith("]"):
        try:
            parsed = ast.literal_eval(value)
        except Exception as exc:
            raise ValueError(f"Invalid TOML list value: {value}") from exc
        if not isinstance(parsed, list):
            raise ValueError(f"Invalid TOML list value: {value}")
        return parsed
    if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
        try:
            return ast.literal_eval(value)
        except Exception as exc:
            raise ValueError(f"Invalid TOML string value: {value}") from exc
    try:
        return int(value)
    except ValueError:
        return value


def parse_model_overlay_toml(text: str) -> list[NativeModel]:
    entries: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line == "[[models]]":
            current = {}
            entries.append(current)
            continue
        if current is None:
            raise ValueError(f"Unexpected content before [[models]] at line {line_number}")
        if "=" not in line:
            raise ValueError(f"Invalid model overlay line {line_number}")
        key, raw_value = line.split("=", 1)
        current[key.strip()] = _parse_toml_scalar(raw_value.split(" #", 1)[0].strip())
    return [validate_overlay_model(entry) for entry in entries]


def render_model_overlay_toml(models: list[NativeModel]) -> str:
    lines = [
        "# User-defined Codex Antigravity model catalog overlay.",
        "# Managed by `codex-antigravity models ...`; the CLI rejects built-in collisions unless --force is used.",
        "",
    ]
    for model in models:
        lines.extend(
            [
                "[[models]]",
                f"id = {json.dumps(model.id)}",
                f"backend_id = {json.dumps(model.backend_id)}",
                f"display_name = {json.dumps(model.display_name)}",
                f"family = {json.dumps(model.family)}",
                f"context_window = {model.context_window}",
                f"default_reasoning_level = {json.dumps(model.default_reasoning_level)}",
                f"supports_parallel_tool_calls = {'true' if model.supports_parallel_tool_calls else 'false'}",
                "aliases = [" + ", ".join(json.dumps(alias) for alias in model.aliases) + "]",
                "",
            ]
        )
    return "\n".join(lines)


def load_model_overlays() -> list[NativeModel]:
    path = model_overlay_path()
    if not path.is_file():
        return []
    if path.is_symlink():
        raise ValueError(f"Refusing to use symlinked model overlay file: {path}")
    return parse_model_overlay_toml(path.read_text(encoding="utf-8"))


def save_model_overlays(models: list[NativeModel]) -> None:
    path = model_overlay_path()
    if path.is_symlink():
        raise ValueError(f"Refusing to overwrite symlinked model overlay file: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_model_overlay_toml(models), encoding="utf-8")
    os.chmod(path, 0o600)


def all_native_models(*, include_overlays: bool = True) -> tuple[NativeModel, ...]:
    model_order = [model.id for model in NATIVE_MODELS]
    models_by_id = {model.id: model for model in NATIVE_MODELS}
    if include_overlays:
        for model in load_model_overlays():
            if model.id not in models_by_id:
                model_order.append(model.id)
            models_by_id[model.id] = model
    return tuple(models_by_id[model_id] for model_id in model_order)


def _alias_map(*, include_overlays: bool = True) -> dict[str, str]:
    alias_map: dict[str, str] = {}
    for native_model in all_native_models(include_overlays=include_overlays):
        alias_map[native_model.id.lower()] = native_model.id
        alias_map[native_model.backend_id.lower()] = native_model.id
        for alias in native_model.aliases:
            alias_map[alias.lower()] = native_model.id
    return alias_map


def canonical_model_id(model: str) -> str:
    """Resolve a user-facing Google/Antigravity alias to the canonical Codex model id."""
    lower = str(model).strip().lower()
    if "/" in lower:
        prefix, rest = lower.split("/", 1)
        if prefix in RESERVED_GOOGLE_MODEL_PREFIXES:
            return canonical_model_id(rest)
        return lower
    alias_map = _alias_map()
    for variant in _slug_variants(lower):
        if variant in alias_map:
            return alias_map[variant]
    return lower


def native_model_definition(model: str) -> NativeModel | None:
    canonical = canonical_model_id(model)
    for native_model in all_native_models():
        if native_model.id == canonical:
            return native_model
    return None


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
        for model in all_native_models()
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
    definition = native_model_definition(canonical)
    if definition:
        return definition.backend_id
    return canonical


def add_model_overlay(model: NativeModel, *, force: bool = False) -> list[NativeModel]:
    if not force and model.id in NATIVE_MODEL_BY_ID:
        raise ValueError(f"{model.id} is a built-in model; pass --force to shadow it in the overlay file")
    overlays = [existing for existing in load_model_overlays() if existing.id != model.id]
    overlays.append(model)
    save_model_overlays(overlays)
    return overlays


def remove_model_overlay(model_id: str) -> bool:
    canonical = validate_model_id(model_id)
    overlays = load_model_overlays()
    kept = [model for model in overlays if model.id != canonical]
    if len(kept) == len(overlays):
        return False
    save_model_overlays(kept)
    return True
