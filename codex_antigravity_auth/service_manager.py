"""Observed service lifecycle result contracts."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Literal

from .redaction import redact_secret_text


class ServiceState(str, Enum):
    NOT_INSTALLED = "not_installed"
    INSTALLED_INACTIVE = "installed_inactive"
    ACTIVE_UNREACHABLE = "active_unreachable"
    READY = "ready"
    DEGRADED = "degraded"
    FAILED = "failed"


@dataclass(frozen=True)
class ServiceResult:
    action: Literal["install", "uninstall", "status"]
    state: ServiceState
    installed: bool
    active: bool
    reachable: bool
    changed: bool
    commands: tuple[dict[str, Any], ...] = ()
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "state": self.state.value,
            "installed": self.installed,
            "active": self.active,
            "reachable": self.reachable,
            "changed": self.changed,
            "commands": [dict(command) for command in self.commands],
            "error": self.error,
        }


def observed_service_result(
    *,
    action: Literal["install", "uninstall", "status"],
    installed: bool,
    active: bool,
    reachable: bool,
    changed: bool,
    commands: tuple[dict[str, Any], ...] = (),
    error: str | None = None,
) -> ServiceResult:
    sanitized_error = redact_secret_text(error) if error else None
    if sanitized_error:
        state = ServiceState.FAILED
    elif not installed:
        state = ServiceState.NOT_INSTALLED
    elif not active:
        state = ServiceState.INSTALLED_INACTIVE
    elif reachable:
        state = ServiceState.READY
    else:
        state = ServiceState.ACTIVE_UNREACHABLE
    return ServiceResult(
        action=action,
        state=state,
        installed=bool(installed),
        active=bool(active),
        reachable=bool(reachable),
        changed=bool(changed),
        commands=commands,
        error=sanitized_error,
    )
