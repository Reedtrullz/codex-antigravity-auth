"""Versioned conservative gateway contract reader, usable without the package."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

CATALOG_VERSION = 1


class CapabilityRegistry:
    def __init__(self):
        try:
            self.snapshot = json.loads(Path(__file__).with_name("capabilities.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.snapshot = {}
        self.entries: dict[str, dict[str, Any]] = {}
        self.aliases: dict[str, str] = {}
        self.source = "snapshot"
        self.load(self.snapshot)

    def load(self, payload: dict[str, Any]) -> None:
        """Replace state atomically; unknown versions never retain stale support."""
        entries = {}
        if type(payload.get("capability_catalog_version")) is int and payload["capability_catalog_version"] == CATALOG_VERSION:
            for entry in payload.get("data", []) if isinstance(payload.get("data"), list) else []:
                if not isinstance(entry, dict) or not isinstance(entry.get("id"), str):
                    continue
                caps = entry.get("capabilities")
                if not isinstance(caps, dict) or (type(caps.get("version")) is not int or caps["version"] != CATALOG_VERSION):
                    continue
                canonical = caps.get("canonical_id")
                effective = caps.get("effective")
                if not isinstance(canonical, str) or not canonical or not isinstance(effective, dict):
                    continue
                modalities = effective.get("input_modalities")
                if not isinstance(modalities, list) or not all(isinstance(item, str) and item in {"text", "image"} for item in modalities):
                    continue
                entries[entry["id"].lower()] = caps
        # Canonical IDs win over aliases, matching the gateway's precedence.
        aliases = {identifier: caps["canonical_id"].lower() for identifier, caps in entries.items()}
        for identifier, caps in entries.items():
            canonical = caps["canonical_id"].lower()
            entries_for_alias = caps.get("aliases", [])
            if isinstance(entries_for_alias, list):
                for alias in entries_for_alias:
                    if isinstance(alias, str):
                        aliases.setdefault(alias.lower(), canonical)
        self.entries, self.aliases = entries, aliases

    def consume(self, payload: dict[str, Any]) -> None:
        if "capability_catalog_version" not in payload:
            # Older gateways have no capability contract. Snapshot is explicit
            # fallback evidence, never inferred from mere catalog membership.
            self.load(self.snapshot)
            self.source = "snapshot"
        else:
            self.load(payload)
            self.source = "gateway" if type(payload.get("capability_catalog_version")) is int and payload["capability_catalog_version"] == CATALOG_VERSION else "unsupported_version"

    def canonical(self, identifier: str) -> str:
        return self.aliases.get(identifier.lower(), identifier.lower())

    def features(self, identifier: str) -> dict[str, bool]:
        key = identifier.lower()
        caps = self.entries.get(key) or self.entries.get(self.canonical(key)) or {}
        effective = caps.get("effective", {})
        modalities = effective.get("input_modalities", [])
        return {"images": "image" in modalities,
                "audio": False, "video": False,
                "tools": effective.get("tools") is True,
                "streaming": effective.get("streaming") is True,
                "json_mode": effective.get("structured_output") is True}

    def context_limit(self, identifier: str) -> int | None:
        caps = self.entries.get(identifier.lower()) or self.entries.get(self.canonical(identifier)) or {}
        context = caps.get("context_limit", {})
        tokens = context.get("tokens") if isinstance(context, dict) else None
        if isinstance(context, dict) and context.get("known") is True and isinstance(tokens, int) and not isinstance(tokens, bool) and tokens > 0:
            return tokens
        return None
