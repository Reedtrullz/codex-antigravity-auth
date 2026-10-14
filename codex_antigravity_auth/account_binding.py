"""Private, instance-scoped account binding for one gateway process."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
import uuid
from typing import Any

GATEWAY_INSTANCE = uuid.uuid4().hex
_INSTANCE_RE = re.compile(r"^[0-9a-f]{32}$")
_ACCOUNT_REF_RE = re.compile(r"^acct_[0-9a-f]{12}$")
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class AccountBinding:
    schemaVersion: int
    gatewayInstance: str
    accountRef: str
    inventorySha256: str

    @classmethod
    def from_mapping(cls, value: Any) -> "AccountBinding":
        if not isinstance(value, dict) or set(value) != {"schemaVersion", "gatewayInstance", "accountRef", "inventorySha256"}:
            raise ValueError("binding fields are not exact")
        if type(value["schemaVersion"]) is not int or value["schemaVersion"] != 1:
            raise ValueError("unsupported binding version")
        if not isinstance(value["gatewayInstance"], str) or not _INSTANCE_RE.fullmatch(value["gatewayInstance"]):
            raise ValueError("invalid gateway instance")
        if not isinstance(value["accountRef"], str) or not _ACCOUNT_REF_RE.fullmatch(value["accountRef"]):
            raise ValueError("invalid account reference")
        if not isinstance(value["inventorySha256"], str) or not _HASH_RE.fullmatch(value["inventorySha256"]):
            raise ValueError("invalid inventory hash")
        return cls(**value)

    def as_dict(self) -> dict[str, Any]:
        return {
            "schemaVersion": self.schemaVersion,
            "gatewayInstance": self.gatewayInstance,
            "accountRef": self.accountRef,
            "inventorySha256": self.inventorySha256,
        }


def canonical_sha256(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def parse_account_binding_header(value: str) -> AccountBinding:
    if not isinstance(value, str) or len(value.encode("utf-8")) > 2048:
        raise ValueError("account binding header exceeds bound")
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("account binding header must be JSON") from exc
    return AccountBinding.from_mapping(parsed)


def validate_binding_route(route: str, family: str | None = None) -> None:
    if route != "antigravity" or (family is not None and family != "gemini"):
        raise ValueError("Account binding requires a native Antigravity Gemini route")
