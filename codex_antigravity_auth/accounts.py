import logging
import copy
import math
import threading
import time
from typing import Any, Callable

from .account_state import AccountState, scoped_cooldown_expiry
from .oauth import refresh_access_token, token_expires_in_seconds
from .redaction import redact_secret_text
from .process_logs import account_ref
from .response_protocol import AttemptOutcome
from .storage import (
    accounts_json_path_read_only,
    get_accounts_json_path,
    load_accounts,
    update_accounts,
)

_log = logging.getLogger(__name__)

def _default_fingerprint() -> dict:
    """Build a fingerprint matching the real Antigravity IDE client.

    The User-Agent must use the IDE format (``antigravity/ide/<ver>``); the
    Electron/Chrome UA previously sent here causes 403 VALIDATION_REQUIRED
    errors from the Cloud Code Assist backend.
    """
    from .google_transport import ide_user_agent
    return {
        "deviceId": "generated-fingerprint-000000000000",
        "sessionToken": "00000000000000000000000000000000",
        "userAgent": ide_user_agent(),
        "createdAt": int(time.time() * 1000),
    }


FINGERPRINT: dict = _default_fingerprint()



_refresh_locks: dict[str, threading.Lock] = {}
_refresh_locks_lock = threading.Lock()


def _get_refresh_lock(email: str) -> threading.Lock:
    """Return a per-account lock for serializing token refresh attempts."""
    with _refresh_locks_lock:
        if email not in _refresh_locks:
            _refresh_locks[email] = threading.Lock()
        return _refresh_locks[email]


def _refresh_blocked(data: dict[str, Any], email: str) -> bool:
    state = data.get("accountState") or {}
    return bool(state.get("disabled", {}).get(email)) or scoped_cooldown_expiry(
        state.get("cooldowns", {}).get(email, {}), "account",
    ) > time.time()


class AccountManager:
    """Compatibility facade over the production AccountState owner."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._in_flight: dict[str, int] = {}
        self._runtime_data: dict[str, Any] = {"accounts": []}
        self._state_owner = AccountState(
            self._runtime_data, now=time.time, in_flight=self._in_flight
        )
        self._bind_compatibility_views()

    def _bind_compatibility_views(self) -> None:
        self._failures = self._state_owner.state["failures"]
        self._cooldowns = self._state_owner.state["cooldowns"]
        self._counters = self._state_owner.state["counters"]

    def _sync_state_from_storage(self, data: dict[str, Any]) -> bool:
        state_missing = "accountState" not in data
        if state_missing and (self._failures or self._cooldowns or self._counters):
            failures = {
                email: value if isinstance(value, dict) else {"account": value}
                for email, value in self._failures.items()
            }
            cooldowns = {
                email: value if isinstance(value, dict) else {"account": value}
                for email, value in self._cooldowns.items()
            }
            data["accountState"] = {
                "schemaVersion": 2,
                "failures": failures,
                "cooldowns": cooldowns,
                "counters": self._counters,
            }
        self._runtime_data = data
        self._state_owner = AccountState(data, now=time.time, in_flight=self._in_flight)
        self._bind_compatibility_views()
        return state_missing or self._state_owner.migration_changed

    def _mutate_state(self, mutation: Callable[[AccountState], None]) -> None:
        if not get_accounts_json_path().exists():
            mutation(self._state_owner)
            self._bind_compatibility_views()
            return

        invoked = False

        def mutate(data: dict[str, Any]) -> bool:
            nonlocal invoked
            invoked = True
            self._sync_state_from_storage(data)
            mutation(self._state_owner)
            self._bind_compatibility_views()
            return True

        update_accounts(mutate)
        if not invoked:
            mutation(self._state_owner)
            self._bind_compatibility_views()

    @staticmethod
    def _model_family(model: str) -> str:
        return "claude" if "claude" in str(model).lower() else "gemini"

    @staticmethod
    def _normalize_expires_at(value: Any) -> float:
        try:
            expires_at = float(value or 0)
        except (TypeError, ValueError):
            return 0
        if not math.isfinite(expires_at):
            return 0
        if expires_at > 10_000_000_000:
            expires_at /= 1000
        return expires_at

    def get_accounts(self) -> list[dict[str, Any]]:
        with self._lock:
            return load_accounts().get("accounts", [])

    @classmethod
    def _same_credentials(cls, account: dict, snapshot: dict) -> bool:
        return all(account.get(key) == snapshot.get(key) for key in (
            "email", "refreshToken", "accessToken",
        )) and cls._normalize_expires_at(account.get("expiresAt")) == cls._normalize_expires_at(snapshot.get("expiresAt"))

    def _refresh_snapshot(
        self, snapshot: dict, *, family: str = "gemini",
        stop_event: threading.Event | None = None,
    ) -> str:
        """Single-process ownership, with network work outside mutation locks.

        Never wait on an account owner: other selections can rotate immediately.
        Recheck both before dispatch and before merge, including failed refreshes.
        """
        email = str(snapshot.get("email", ""))
        lock = _get_refresh_lock(email)
        if not lock.acquire(blocking=False):
            return "busy"

        def stopped() -> bool:
            return stop_event is not None and stop_event.is_set()

        try:
            current = None
            with self._lock:
                def recheck(data: dict[str, Any]) -> bool:
                    nonlocal current
                    account = next((item for item in data.get("accounts", [])
                                    if item.get("email") == email), None)
                    if (not stopped() and account is not None
                            and self._same_credentials(account, snapshot)
                            and not _refresh_blocked(data, email)):
                        current = copy.deepcopy(account)
                    return False
                update_accounts(recheck)
            if current is None or stopped():
                return "changed"

            failure = False
            try:
                refreshed = refresh_access_token(current["refreshToken"])
                token = refreshed["access_token"]
                expires_at = time.time() + token_expires_in_seconds(refreshed)
                discovered_project = None
                if not stopped() and not current.get("projectId"):
                    try:
                        from .oauth import discover_project_id
                        discovered_project = discover_project_id(token)
                    except Exception:
                        _log.warning("Project discovery failed during token refresh")
            except Exception:
                failure = True

            if stopped():
                return "stopped"
            result = "changed"
            with self._lock:
                def merge(data: dict[str, Any]) -> bool:
                    nonlocal result
                    account = next((item for item in data.get("accounts", [])
                                    if item.get("email") == email), None)
                    if (stopped() or account is None
                            or not self._same_credentials(account, current)
                            or _refresh_blocked(data, email)):
                        return False
                    if failure:
                        self._sync_state_from_storage(data)
                        self._state_owner.apply_cooldown(
                            email, family, AttemptOutcome(scope="account", category="auth"),
                        )
                        result = "failed"
                    else:
                        account["accessToken"] = token
                        account["expiresAt"] = expires_at
                        if refreshed.get("refresh_token"):
                            account["refreshToken"] = refreshed["refresh_token"]
                        if discovered_project and not account.get("projectId"):
                            account["projectId"] = discovered_project
                        result = "refreshed"
                    return True
                update_accounts(merge)
            return result
        finally:
            lock.release()

    def _select_active_account(self, model: str, *, acquire: bool) -> dict[str, Any] | None:
        family = self._model_family(model)
        excluded: set[str] = set()
        attempted: dict[str, list[dict]] = {}
        refreshed_emails: set[str] = set()
        while True:
            selected = None
            snapshot = None
            with self._lock:
                def mutate(data: dict[str, Any]) -> bool:
                    nonlocal selected, snapshot
                    dirty = self._sync_state_from_storage(data)
                    while True:
                        active_index_before = data.get("activeIndex")
                        family_index_before = data.get("activeIndexByFamily", {}).get(family)
                        cooldowns_before = copy.deepcopy(self._state_owner.state["cooldowns"])
                        lease = (
                            self._state_owner.acquire(family, exclude_emails=excluded)
                            if acquire
                            else self._state_owner.select(family, exclude_emails=excluded)
                        )
                        dirty = dirty or (
                            data.get("activeIndex") != active_index_before
                            or data.get("activeIndexByFamily", {}).get(family) != family_index_before
                            or self._state_owner.state["cooldowns"] != cooldowns_before
                        )
                        if lease is None:
                            return dirty
                        account = lease.account
                        email = str(account.get("email", ""))
                        if not account.get("fingerprint"):
                            account["fingerprint"] = FINGERPRINT
                            dirty = True
                        raw_expires_at = account.get("expiresAt", 0)
                        expires_at = self._normalize_expires_at(raw_expires_at)
                        if isinstance(raw_expires_at, bool) or raw_expires_at != expires_at:
                            account["expiresAt"] = expires_at
                            dirty = True
                        if account.get("accessToken") and (
                            expires_at >= time.time() + 300
                            or (expires_at > time.time() + 10 and (
                                not account.get("refreshToken") or email in refreshed_emails
                            ))
                        ):
                            selected = copy.deepcopy(account)
                            return dirty

                        # A refresh candidate is not a request lease. Selection
                        # re-reads eligibility and acquires only after merge.
                        if acquire:
                            self._state_owner.release(lease)
                        # Retry one changed credential identity, but bound churn
                        # and never refresh the same snapshot twice per selection.
                        prior_snapshots = attempted.get(email, [])
                        if (email in refreshed_emails or len(prior_snapshots) >= 2
                                or any(self._same_credentials(account, prior) for prior in prior_snapshots)):
                            excluded.add(email)
                            continue
                        if not account.get("refreshToken"):
                            self._state_owner.apply_cooldown(
                                email, family, AttemptOutcome(scope="account", category="auth"),
                            )
                            dirty = True
                            excluded.add(email)
                            continue
                        snapshot = copy.deepcopy(account)
                        return dirty
                update_accounts(mutate)
            if selected is not None or snapshot is None:
                return selected
            email = str(snapshot["email"])
            attempted.setdefault(email, []).append(snapshot)
            # Neither the manager lock nor the cross-process store lock is held.
            result = self._refresh_snapshot(snapshot, family=family)
            if result == "refreshed":
                refreshed_emails.add(email)
            if result in {"busy", "failed", "stopped"}:
                excluded.add(email)

    def select_active_account(self, model: str) -> dict[str, Any] | None:
        return self._select_active_account(model, acquire=False)

    def acquire_account(self, model: str) -> dict[str, Any] | None:
        return self._select_active_account(model, acquire=True)

    def release_account(self, email: str | None) -> None:
        if not email:
            return
        with self._lock:
            self._state_owner.release_email(str(email))

    def in_flight_count(self, email: str | None) -> int:
        if not email:
            return 0
        with self._lock:
            return self._state_owner.in_flight(str(email))

    def mark_failure(
        self,
        email: str,
        reason: str,
        retry_after_seconds: float | None = None,
        *,
        model: str | None = None,
        status_code: int | None = None,
    ) -> None:
        with self._lock:
            if not email:
                return
            normalized_reason = str(reason).lower()
            family_limited = status_code == 429 or any(
                marker in normalized_reason
                for marker in ("rate limit", "quota", "resource_exhausted")
            )
            family = self._model_family(model or "")
            outcome = AttemptOutcome(
                scope="family" if family_limited and model else "account",
                category="rate_limit" if family_limited else "auth",
                retry_after_seconds=(
                    None if isinstance(retry_after_seconds, bool) else retry_after_seconds
                ),
            )
            duration = 0.0

            def mutation(state: AccountState) -> None:
                nonlocal duration
                duration = state.apply_cooldown(email, family, outcome)

            self._mutate_state(mutation)
            _log.warning("Account %s cooling down for %ss (scope=%s category=%s)",
                         account_ref(email), duration, outcome.scope, outcome.category)

    def record_attempt(
        self,
        email: str,
        model: str,
        outcome: AttemptOutcome,
        *,
        status_code: int | None = None,
        error_class: str | None = None,
        usage: dict[str, Any] | None = None,
        curable_auth: bool = False,
    ) -> None:
        del status_code
        if not email:
            return
        with self._lock:
            self._mutate_state(
                lambda state: state.record_email(
                    email,
                    self._model_family(model),
                    outcome,
                    usage=usage,
                    error_class=(
                        redact_secret_text(str(error_class))[:200] if error_class else None
                    ),
                    curable_auth=curable_auth,
                )
            )

    def record_request(
        self,
        email: str,
        model: str,
        *,
        status: str,
        status_code: int | None = None,
        error_class: str | None = None,
        usage: dict[str, Any] | None = None,
    ) -> None:
        category = "success" if status == "success" else (
            "rate_limit" if status_code == 429 else "transport"
        )
        self.record_attempt(
            email,
            model,
            AttemptOutcome(scope="none", category=category),
            status_code=status_code,
            error_class=error_class,
            usage=usage,
        )

    def refresh_expiring_accounts(
        self, window_seconds: int = 300, *, stop_event: threading.Event | None = None,
    ) -> dict[str, int]:
        summary = {"checked": 0, "refreshed": 0, "failed": 0}
        if ((stop_event is not None and stop_event.is_set())
                or not accounts_json_path_read_only().exists()):
            return summary
        with self._lock:
            current = load_accounts()
            candidates = [copy.deepcopy(account) for account in current.get("accounts", [])
                          if isinstance(account, dict) and account.get("email")
                          and account.get("refreshToken")]
        summary["checked"] = len(candidates)
        for snapshot in candidates:
            if stop_event is not None and stop_event.is_set():
                break
            if self._normalize_expires_at(snapshot.get("expiresAt")) > time.time() + max(0, int(window_seconds)):
                continue
            try:
                result = self._refresh_snapshot(snapshot, stop_event=stop_event)
                if result in {"refreshed", "failed"}:
                    summary[result] += 1
            except Exception:
                summary["failed"] += 1
        return summary

    def clear_failures(self, email: str, family: str | None = None) -> None:
        with self._lock:
            self._mutate_state(lambda state: state.clear_failures(email, family))


def is_validation_required_error(status_code: int, body: str | None = None) -> bool:
    """Check if an error is a VALIDATION_REQUIRED auth issue rather than rate limit.

    Structured rejection reasons such as RESTRICTED_AGE also carry
    PERMISSION_DENIED status strings; those are account-eligibility blocks,
    not the re-authentication flow this predicate gates.
    """
    if status_code == 403:
        if body and "RESTRICTED_AGE" in body:
            return False
        if body and "VALIDATION_REQUIRED" in body:
            return True
        if body and "permission_denied" in body.lower():
            return True
    return False


def classify_backend_status(status_code: int, body: str | None = None) -> str:
    """Classify a backend HTTP status into an error category string.
    
    Returns one of: 'auth', 'rate_limit', 'invalid_request', 'transport'.
    """
    if status_code == 429:
        return "rate_limit"
    if is_validation_required_error(status_code, body):
        return "auth"
    if status_code in (401, 403):
        return "auth"
    if 400 <= status_code < 500:
        return "invalid_request"
    return "transport"
