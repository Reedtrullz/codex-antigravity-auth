"""Read-only, shareable explanations of local Google account eligibility."""

from __future__ import annotations

import copy
import hashlib
import os
import time
from pathlib import Path
from typing import Any

from . import constants, storage
from .account_state import AccountState, UnsupportedAccountStateVersion
from .accounts import AccountManager
from .models import canonical_model_id
from .unified import classify_route


SELECTION_RULE = (
    "Among accounts without routing exclusions, choose the lowest current lease count; "
    "break ties in cyclic store order starting at the family's preferred account. "
    "A lease lowers preference but does not exclude an account. Token refresh may "
    "then fail or defer selection. This is sticky preference, not round-robin."
)


def _account_id(index: int | None) -> str | None:
    return f"account-{index + 1}" if index is not None else None


def explain_account_data(data: dict[str, Any], model: str, *, now: float | None = None,
                         in_flight: dict[str, int] | None = None) -> dict[str, Any]:
    """Inspect a copy; in_flight=None means live leases are unobservable."""
    current = time.time() if now is None else now
    family = AccountManager._model_family(canonical_model_id(model))
    snapshot = copy.deepcopy(data)
    owner = AccountState(snapshot, now=lambda: current, in_flight=copy.deepcopy(in_flight))
    selection = owner.selection_snapshot(family)
    rows = []
    for item in selection["accounts"]:
        index = item["index"]
        account = snapshot["accounts"][index]
        account = account if isinstance(account, dict) else {}
        expires_at = AccountManager._normalize_expires_at(account.get("expiresAt"))
        remaining = expires_at - current
        has_access, has_refresh = bool(account.get("accessToken")), bool(account.get("refreshToken"))
        token_state = ("missing" if not has_access else "expired" if remaining <= 0 else
                       "expiring_imminently" if remaining <= 10 else "expiring" if remaining < 300 else "current")
        refresh = ("not_needed" if token_state == "current" else
                   "on_selection" if has_refresh else
                   "unavailable" if token_state == "expiring" else "login_required")
        reasons = item["exclusion_reasons"]
        email = str(account.get("email", ""))
        last_failure = owner.state["counters"].get(email, {}).get(family, {}).get("last_failure_class")
        # Historical evidence only: it need not explain the current cooldown.
        last_failure = last_failure if last_failure in {
            "transport", "rate_limit", "auth", "auth_failure", "invalid_request", "quota", "capacity",
        } else "unknown"
        actions = []
        if "missing_identity" in reasons:
            actions.append("Run login to create a complete account entry.")
        if "disabled" in reasons:
            actions.append("Reauthenticate with login; disabled accounts require explicit recovery.")
        if item["cooldown_remaining_seconds"]:
            actions.append("Wait for the listed cooldowns; their cause is not inferred from the expiry.")
        if refresh in {"login_required", "unavailable"}:
            actions.append("Run login to restore refresh capability.")
        elif refresh == "on_selection":
            actions.append("Selection will attempt refresh; success has not been checked.")
        rows.append({
            "account_id": _account_id(index),
            "preference_order": item["order"] + 1,
            "routing_eligible": not reasons,
            "exclusion_reasons": reasons,
            "cooldown_remaining_seconds": item["cooldown_remaining_seconds"],
            "lease_count": item["in_flight"] if in_flight is not None else None,
            "token_state": token_state,
            "refresh_available": has_refresh,
            "refresh": refresh,
            "auth_strikes": owner.auth_strikes(email),
            "last_recorded_failure_category": last_failure,
            "next_actions": actions,
        })
    return {
        "ok": True, "family": family,
        "selection_rule": SELECTION_RULE,
        "lease_visibility": "provided_snapshot" if in_flight is not None else "unknown_other_process",
        "preferred_account": _account_id(selection["preferred_index"]),
        "candidate_before_refresh": _account_id(selection["candidate_index"]) if in_flight is not None else None,
        "routing_eligible_count": sum(row["routing_eligible"] for row in rows),
        "accounts": rows,
    }


def _display_path(path: Path, default_name: str) -> str:
    if path == Path.home() / ".codex" / default_name:
        return f"~/.codex/{default_name}"
    # Even a path below HOME may contain private account/profile components.
    identity = hashlib.sha256(str(path.absolute()).encode("utf-8", "surrogatepass")).hexdigest()[:12]
    return f"<configured path {identity}>"


def account_eligibility_report(model: str) -> dict[str, Any]:
    namespace = {
        "account_store": _display_path(storage.accounts_json_path_read_only(), "antigravity-accounts.json"),
        "oauth_client_file": _display_path(Path(os.path.expanduser(constants.CREDENTIALS_FILE)), "antigravity-credentials.json"),
        "oauth_client_environment": ["ANTIGRAVITY_CLIENT_ID", "ANTIGRAVITY_CLIENT_SECRET"],
        "keyring_service": storage.KEYRING_SERVICE_NAME,
        "keyring_key": storage.KEYRING_KEY_NAME,
    }
    if not model.strip() or any(ch.isspace() or ord(ch) < 0x20 or ord(ch) == 0x7f for ch in model.strip()):
        return {"ok": False, "error_class": "unsupported_route", "namespace": namespace,
                "message": "Choose a non-empty model ID without whitespace or control characters."}
    model = model.strip()
    routed_model = canonical_model_id(model) if ":" not in model else model
    try:
        routes = {mode: classify_route(routed_model, unified_enabled=enabled, read_only=True)
                  for mode, enabled in (("classic", False), ("unified", True))}
    except Exception:
        return {"ok": False, "error_class": "route_configuration_unavailable", "namespace": namespace,
                "message": "Cannot inspect provider routing configuration; check its store and encryption key."}
    if "antigravity" not in routes.values():
        return {"ok": False, "error_class": "unsupported_route", "namespace": namespace, "route_by_mode": routes,
                "message": "This model does not use the Google account pool in either gateway mode."}
    try:
        report = explain_account_data(storage.load_accounts_read_only(), model)
    except Exception as exc:
        # The read-only loader wraps format errors. Never echo arbitrary store
        # contents from the exception, and never inspect the store a second time.
        cause = exc
        while cause.__cause__ is not None:
            cause = cause.__cause__
        unsupported = isinstance(cause, UnsupportedAccountStateVersion)
        report = {
            "ok": False,
            "error_class": "unsupported_account_state_version" if unsupported else "account_store_unavailable",
            "message": (
                "Use a compatible gateway version or matching backup with the gateway stopped; do not reset this store."
                if unsupported else "Cannot inspect the account store. Check its path, permissions and configured encryption key."
            ),
        }
    return {**report, "namespace": namespace, "route_by_mode": routes,
            "gateway_mode": "unknown_other_process"}


def account_eligibility_lines(report: dict[str, Any]) -> list[str]:
    lines = [f"{key}: {value}" for key, value in report["namespace"].items()]
    if "route_by_mode" in report:
        lines.append(f"Routes by gateway mode: {report['route_by_mode']}; the running gateway mode is not observed.")
    if not report["ok"]:
        return lines + [f"{report['error_class']}: {report['message']}"]
    lines += [f"Family: {report['family']}", report["selection_rule"],
              f"Live leases: {report['lease_visibility']}; next selection cannot be predicted by this CLI.",
              f"Preferred account: {report['preferred_account'] or 'none'}; routing eligible: {report['routing_eligible_count']}"]
    if not report["accounts"]:
        lines.append("No configured accounts. Run login to add an account.")
    for row in report["accounts"]:
        lines.append(
            f"{row['account_id']}: routing_eligible={str(row['routing_eligible']).lower()}; "
            f"exclusions={','.join(row['exclusion_reasons']) or 'none'}; "
            f"token={row['token_state']}; refresh={row['refresh']}; auth_strikes={row['auth_strikes']}; "
            f"refresh_available={str(row['refresh_available']).lower()}; last_recorded_failure={row['last_recorded_failure_category']}; "
            f"preference_order={row['preference_order']}; lease_count={row['lease_count'] if row['lease_count'] is not None else 'unknown'}"
        )
        if row["cooldown_remaining_seconds"]:
            lines.append(f"  Cooldown seconds: {row['cooldown_remaining_seconds']}")
        lines.extend(f"  {action}" for action in row["next_actions"])
    return lines
