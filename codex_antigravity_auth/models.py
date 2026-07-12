from __future__ import annotations

from dataclasses import dataclass
import ast
import json
import os
import re
import threading
import warnings
from pathlib import Path
from typing import Any
from .response_protocol import ProviderCapabilities
from .secure_store import SecureStore

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised on Python 3.10
    tomllib = None


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
_OVERLAY_CACHE_LOCK = threading.RLock()
_OVERLAY_CACHE_KEY: tuple[str, int, int] | None = None
_OVERLAY_CACHE_MODELS: tuple[NativeModel, ...] = ()
_OVERLAY_CACHE_ERROR: ValueError | None = None
_OVERLAY_CACHE_WARNED_KEY: tuple[str, int, int] | None = None


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


def _strip_toml_comment(value: str) -> str:
    quote = ""
    escaped = False
    for index, char in enumerate(value):
        if quote:
            if escaped:
                escaped = False
            elif char == "\\" and quote == '"':
                escaped = True
            elif char == quote:
                quote = ""
            continue
        if char in {"'", '"'}:
            quote = char
            continue
        if char == "#":
            return value[:index].rstrip()
    return value.strip()


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
    if tomllib is not None:
        try:
            parsed = tomllib.loads(text)
        except Exception as exc:
            raise ValueError(f"Invalid model overlay TOML: {exc}") from exc
        entries = parsed.get("models", [])
        if entries in (None, ""):
            return []
        if not isinstance(entries, list):
            raise ValueError("model overlay must define [[models]] tables")
        if not all(isinstance(entry, dict) for entry in entries):
            raise ValueError("model overlay entries must be tables")
        return [validate_overlay_model(entry) for entry in entries]

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
        current[key.strip()] = _parse_toml_scalar(_strip_toml_comment(raw_value))
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


def _overlay_cache_key(path: Path) -> tuple[str, int, int]:
    if not path.exists():
        return (str(path), 0, 0)
    if path.is_symlink():
        raise ValueError(f"Refusing to use symlinked model overlay file: {path}")
    if not path.is_file():
        raise ValueError(f"Refusing to use non-file model overlay path: {path}")
    stat_result = path.stat()
    return (str(path), int(stat_result.st_mtime_ns), int(stat_result.st_size))


def _warn_overlay_error_once(cache_key: tuple[str, int, int], error: ValueError) -> None:
    global _OVERLAY_CACHE_WARNED_KEY
    if _OVERLAY_CACHE_WARNED_KEY == cache_key:
        return
    _OVERLAY_CACHE_WARNED_KEY = cache_key
    warnings.warn(
        f"Ignoring invalid Codex Antigravity model overlay; built-in models remain available: {error}",
        RuntimeWarning,
        stacklevel=3,
    )


def invalidate_model_overlay_cache() -> None:
    global _OVERLAY_CACHE_KEY, _OVERLAY_CACHE_MODELS, _OVERLAY_CACHE_ERROR, _OVERLAY_CACHE_WARNED_KEY
    with _OVERLAY_CACHE_LOCK:
        _OVERLAY_CACHE_KEY = None
        _OVERLAY_CACHE_MODELS = ()
        _OVERLAY_CACHE_ERROR = None
        _OVERLAY_CACHE_WARNED_KEY = None


def load_model_overlays(*, strict: bool = True) -> list[NativeModel]:
    path = model_overlay_path()
    with _OVERLAY_CACHE_LOCK:
        try:
            cache_key = _overlay_cache_key(path)
        except ValueError as exc:
            if strict:
                raise
            cache_key = (str(path), -1, -1)
            _warn_overlay_error_once(cache_key, exc)
            return []
        global _OVERLAY_CACHE_KEY, _OVERLAY_CACHE_MODELS, _OVERLAY_CACHE_ERROR
        if _OVERLAY_CACHE_KEY == cache_key:
            if _OVERLAY_CACHE_ERROR is not None:
                if strict:
                    raise ValueError(str(_OVERLAY_CACHE_ERROR))
                _warn_overlay_error_once(cache_key, _OVERLAY_CACHE_ERROR)
                return []
            return list(_OVERLAY_CACHE_MODELS)

        error: ValueError | None = None
        models: tuple[NativeModel, ...] = ()
        if cache_key[1] != 0 or cache_key[2] != 0:
            try:
                models = tuple(parse_model_overlay_toml(path.read_text(encoding="utf-8")))
            except (OSError, ValueError) as exc:
                error = ValueError(str(exc))
        _OVERLAY_CACHE_KEY = cache_key
        _OVERLAY_CACHE_MODELS = models
        _OVERLAY_CACHE_ERROR = error
        if error is not None:
            if strict:
                raise ValueError(str(error))
            _warn_overlay_error_once(cache_key, error)
            return []
        return list(models)


def save_model_overlays(models: list[NativeModel]) -> None:
    path = model_overlay_path()
    if path.is_symlink():
        raise ValueError(f"Refusing to overwrite symlinked model overlay file: {path}")
    SecureStore().atomic_write_text(path, render_model_overlay_toml(models), mode=0o600)
    invalidate_model_overlay_cache()


def all_native_models(*, include_overlays: bool = True, strict_overlays: bool = False) -> tuple[NativeModel, ...]:
    model_order = [model.id for model in NATIVE_MODELS]
    models_by_id = {model.id: model for model in NATIVE_MODELS}
    if include_overlays:
        for model in load_model_overlays(strict=strict_overlays):
            if model.id not in models_by_id:
                model_order.append(model.id)
            models_by_id[model.id] = model
    return tuple(models_by_id[model_id] for model_id in model_order)


def _alias_map(*, include_overlays: bool = True, strict_overlays: bool = False) -> dict[str, str]:
    alias_map: dict[str, str] = {}
    for native_model in all_native_models(include_overlays=include_overlays, strict_overlays=strict_overlays):
        alias_map.setdefault(native_model.id.lower(), native_model.id)
        alias_map.setdefault(native_model.backend_id.lower(), native_model.id)
        for alias in native_model.aliases:
            alias_map.setdefault(alias.lower(), native_model.id)
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


def native_model_capabilities(model: str) -> ProviderCapabilities:
    definition = native_model_definition(model)
    return ProviderCapabilities(
        native_responses=False,
        parallel_tool_calls=(
            definition.supports_parallel_tool_calls if definition is not None else True
        ),
        structured_output=True,
        stop_sequences=True,
        reasoning=True,
        streaming_usage=True,
    )


def native_model_catalog(*, strict_overlays: bool = False) -> list[dict[str, Any]]:
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
        for model in all_native_models(strict_overlays=strict_overlays)
    ]


def model_identifier_collisions(
    model: NativeModel,
    existing_models: tuple[NativeModel, ...],
    *,
    allow_same_id_shadow: bool = False,
) -> dict[str, str]:
    identifiers: dict[str, str] = {}
    for existing in existing_models:
        if allow_same_id_shadow and existing.id.lower() == model.id.lower():
            continue
        for value in (existing.id, existing.backend_id, *existing.aliases):
            identifiers.setdefault(value.lower(), existing.id)
    collisions: dict[str, str] = {}
    for label, value in (
        ("id", model.id),
        ("backend_id", model.backend_id),
        *((f"alias:{alias}", alias) for alias in model.aliases),
    ):
        owner = identifiers.get(value.lower())
        if owner:
            collisions[label] = owner
    return collisions


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
    overlays = [existing for existing in load_model_overlays(strict=True) if existing.id != model.id]
    collisions = model_identifier_collisions(
        model,
        tuple(NATIVE_MODELS) + tuple(overlays),
        allow_same_id_shadow=force and model.id in NATIVE_MODEL_BY_ID,
    )
    if collisions and not force:
        formatted = ", ".join(f"{label} -> {owner}" for label, owner in sorted(collisions.items()))
        raise ValueError(f"model identifiers shadow existing model identifiers; pass --force to allow: {formatted}")
    overlays.append(model)
    save_model_overlays(overlays)
    return overlays


def remove_model_overlay(model_id: str) -> bool:
    canonical = validate_model_id(model_id)
    overlays = load_model_overlays(strict=True)
    kept = [model for model in overlays if model.id != canonical]
    if len(kept) == len(overlays):
        return False
    save_model_overlays(kept)
    return True
