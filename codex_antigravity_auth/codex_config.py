"""Semantic, style-preserving edits to the managed Codex provider settings."""
from __future__ import annotations

from collections.abc import Mapping, MutableMapping
from copy import deepcopy
import math
from typing import Any

import tomlkit
from tomlkit.exceptions import TOMLKitError
from tomlkit.items import InlineTable


def _parse(content: str):
    try:
        content.encode("utf-8")
        return tomlkit.parse(content)
    except (TOMLKitError, ValueError, UnicodeError, RecursionError) as exc:
        # Do not echo arbitrary config contents (which can include credentials).
        raise ValueError("Codex config is not valid TOML; no changes were written.") from exc


def _same_value(left: Any, right: Any) -> bool:
    """Compare native TOML values including NaN and scalar type distinctions."""
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(_same_value(left[key], right[key]) for key in left)
    if isinstance(left, list):
        return len(left) == len(right) and all(_same_value(a, b) for a, b in zip(left, right))
    if isinstance(left, float):
        return (math.isnan(left) and math.isnan(right)) or left.hex() == right.hex()
    return left == right


def merge_provider_config(content: str, *, model: str, provider_id: str, provider_name: str,
                          base_url: str, activate: bool) -> str:
    try:
        return _merge_provider_config(content, model=model, provider_id=provider_id,
                                      provider_name=provider_name, base_url=base_url, activate=activate)
    except (TOMLKitError, RecursionError) as exc:
        raise ValueError("Codex config cannot be edited safely; no changes were written.") from exc


def _merge_provider_config(content: str, *, model: str, provider_id: str, provider_name: str,
                           base_url: str, activate: bool) -> str:
    document = _parse(content)
    before = document.unwrap()
    expected = deepcopy(before)
    providers = document.get("model_providers")
    if providers is None and "model_providers" not in document:
        document["model_providers"] = tomlkit.table()
        providers = document["model_providers"]
    if not isinstance(providers, MutableMapping):
        raise ValueError("model_providers must be a TOML table; no changes were written.")
    table = providers.get(provider_id)
    if table is None and provider_id not in providers:
        providers[provider_id] = tomlkit.inline_table() if isinstance(providers, InlineTable) else tomlkit.table()
        table = providers[provider_id]
    if not isinstance(table, MutableMapping):
        raise ValueError("The selected model provider must be a TOML table; no changes were written.")
    values = {"name": provider_name, "base_url": base_url, "wire_api": "responses"}
    expected.setdefault("model_providers", {}).setdefault(provider_id, {}).update(values)
    for key, value in values.items():
        if table.get(key) != value:
            table[key] = value
    if activate:
        root_values = {"model": model, "model_provider": provider_id, "wire_api": "responses"}
        expected.update(root_values)
        for key, value in root_values.items():
            if document.get(key) != value:
                document[key] = value
    if _same_value(before, expected):
        return content
    updated = tomlkit.dumps(document)
    after = _parse(updated).unwrap()
    if not _same_value(after, expected):
        raise ValueError("Codex config edit would alter unrelated settings; no changes were written.")
    return updated


def parse_provider_config(content: str) -> dict[str, object]:
    """Read selectors from actual TOML tables, never string contents or comments."""
    document = _parse(content).unwrap()
    providers = document.get("model_providers", {})
    if not isinstance(providers, Mapping):
        raise ValueError("model_providers must be a TOML table.")
    # Keep TOML types so inspectors distinguish omitted selectors from invalid
    # explicit values (for example wire_api = false).
    tables = {name: dict(table) for name, table in providers.items() if isinstance(table, Mapping)}
    return {
        "active_provider": document.get("model_provider") if isinstance(document.get("model_provider"), str) else "",
        "active_model": document.get("model") if isinstance(document.get("model"), str) else "",
        "provider_tables": tables,
    }
