import logging
import copy
import math
import threading
import time
from typing import Any, Callable

from .account_state import AccountState
from .oauth import refresh_access_token, token_expires_in_seconds
from .redaction import redact_secret_text
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


class _RefreshBusy(RuntimeError):
    """A refresh is already running; exclude this account for this selection."""


def _get_refresh_lock(email: str) -> threading.Lock:
    """Return a per-account lock for serializing token refresh attempts."""
    with _refresh_locks_lock:
        if email not in _refresh_locks:
            _refresh_locks[email] = threading.Lock()
        return _refresh_locks[email]


def _apply_token_refresh(account: dict, refresh_token: str, *, wait: bool = True) -> bool:
    email = account.get("email", "")
    lock = _get_refresh_lock(email)
    # Block with a timeout rather than skipping: another thread may be
    # refreshing the same account, and skipping leaves the caller with
    # a stale token.  Wait for the in-progress refresh to finish.
    if wait:
        acquired = lock.acquire(blocking=True, timeout=30)
    else:
        # Selection already owns the account/store mutation lock. Never wait
        # on a background refresh here: fail closed and let selection rotate.
        acquired = lock.acquire(blocking=False)
    if not acquired:
        _log.warning("Refresh lock busy for %s; refusing stale-token selection", email)
        return False
    try:
        refreshed = refresh_access_token(refresh_token)
        account["accessToken"] = refreshed["access_token"]
        account["expiresAt"] = time.time() + token_expires_in_seconds(refreshed)
        if refreshed.get("refresh_token"):
            account["refreshToken"] = refreshed["refresh_token"]
        # Discover the Cloud Code Assist project if not already stored.
        # The backend rejects requests without a valid project id (403 VALIDATION_REQUIRED).
        if not account.get("projectId"):
            try:
                from .oauth import discover_project_id
                project_id = discover_project_id(refreshed["access_token"])
                if project_id:
                    account["projectId"] = project_id
                    _log.info("Discovered project ID %s for %s", project_id, email)
                else:
                    _log.warning("Project discovery returned empty for %s", email)
            except Exception as exc:
                _log.warning("Project discovery failed for %s: %s", email, exc)
        return True
    finally:
        lock.release()


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

    def _select_active_account(self, model: str, *, acquire: bool) -> dict[str, Any] | None:
        with self._lock:
            selected: dict[str, Any] | None = None
            family = self._model_family(model)
            temporarily_excluded: set[str] = set()

            def mutate(data: dict[str, Any]) -> bool:
                nonlocal selected
                dirty = self._sync_state_from_storage(data)
                while True:
                    active_index_before = data.get("activeIndex")
                    family_index_before = data.get("activeIndexByFamily", {}).get(family)
                    cooldowns_before = copy.deepcopy(self._state_owner.state["cooldowns"])
                    lease = (
                        self._state_owner.acquire(family, exclude_emails=temporarily_excluded)
                        if acquire
                        else self._state_owner.select(family, exclude_emails=temporarily_excluded)
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
                    if account.get("accessToken") and expires_at >= time.time() + 300:
                        selected = account
                        return dirty

                    refresh_token = account.get("refreshToken")
                    if account.get("accessToken") and expires_at > time.time() + 10:
                        try:
                            if refresh_token:
                                if not _apply_token_refresh(account, refresh_token, wait=False):
                                    raise _RefreshBusy("refresh already in progress")
                                dirty = True
                        except _RefreshBusy:
                            if acquire:
                                self._state_owner.release(lease)
                            temporarily_excluded.add(email)
                            continue
                        except Exception as exc:
                            # The remaining token lifetime (<=10s) cannot
                            # outlast a generation call, so selecting it would
                            # guarantee a 401 mid-request. Cool the account
                            # down and let the loop try the next one, matching
                            # the hard-refresh failure path.
                            if acquire:
                                self._state_owner.release(lease)
                            self._state_owner.apply_cooldown(
                                email,
                                family,
                                AttemptOutcome(scope="account", category="auth"),
                            )
                            dirty = True
                            print(
                                f"[*] Soft refresh failed for {email}, cooling down. Reason: "
                                f"{redact_secret_text(str(exc))}"
                            )
                            continue
                        selected = account
                        return dirty

                    try:
                        if not refresh_token:
                            raise RuntimeError("Token expired and no refresh token is available")
                        if not _apply_token_refresh(account, refresh_token, wait=False):
                            raise _RefreshBusy("refresh already in progress")
                        selected = account
                        return True
                    except _RefreshBusy:
                        if acquire:
                            self._state_owner.release(lease)
                        temporarily_excluded.add(email)
                        continue
                    except Exception as exc:
                        if acquire:
                            self._state_owner.release(lease)
                        self._state_owner.apply_cooldown(
                            email,
                            family,
                            AttemptOutcome(scope="account", category="auth"),
                        )
                        dirty = True
                        print(
                            f"[*] Account {email} flagged as cooling down. Reason: "
                            f"{redact_secret_text(str(exc))}"
                        )

            update_accounts(mutate)
            return selected

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
            print(
                f"[*] Account {email} flagged as cooling down for {duration}s. "
                f"Reason: {redact_secret_text(reason)}"
            )

    def record_attempt(
        self,
        email: str,
        model: str,
        outcome: AttemptOutcome,
        *,
        status_code: int | None = None,
        error_class: str | None = None,
        usage: dict[str, Any] | None = None,
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

    def refresh_expiring_accounts(self, window_seconds: int = 300) -> dict[str, int]:
        summary = {"checked": 0, "refreshed": 0, "failed": 0}
        if not accounts_json_path_read_only().exists():
            return summary
        now = time.time()
        with self._lock:
            current = load_accounts()
        all_candidates = [
            (
                str(account.get("email")),
                str(account.get("refreshToken")),
                str(account.get("accessToken")),
                self._normalize_expires_at(account.get("expiresAt", 0)),
            )
                for account in current.get("accounts", [])
                if isinstance(account, dict)
                and account.get("email")
                and account.get("refreshToken")
            ]
        summary["checked"] = len(all_candidates)
        candidates = [
            candidate for candidate in all_candidates
            if candidate[3] <= now + max(0, int(window_seconds))
        ]

        # Network refresh/discovery is deliberately outside both manager and
        # storage locks; merge only against the refresh-token identity we read.
        for email, refresh_token, captured_access_token, captured_expires_at in candidates:
            lock = _get_refresh_lock(email)
            with lock:
                try:
                    # Re-check after waiting for a concurrent refresh.  This
                    # avoids duplicate refreshes and stale same-token writes.
                    latest = load_accounts()
                    latest_account = next(
                        (item for item in latest.get("accounts", [])
                         if isinstance(item, dict) and str(item.get("email")) == email),
                        None,
                    )
                    if (
                        not latest_account
                        or str(latest_account.get("refreshToken")) != refresh_token
                        or str(latest_account.get("accessToken")) != captured_access_token
                        or self._normalize_expires_at(latest_account.get("expiresAt", 0)) != captured_expires_at
                    ):
                        if latest_account and self._normalize_expires_at(latest_account.get("expiresAt", 0)) > time.time() + max(0, int(window_seconds)):
                            continue
                        captured_access_token = str(latest_account.get("accessToken")) if latest_account else captured_access_token
                        captured_expires_at = self._normalize_expires_at(latest_account.get("expiresAt", 0)) if latest_account else captured_expires_at
                    try:
                        refreshed = refresh_access_token(refresh_token)
                        new_access_token = refreshed["access_token"]
                        new_expires_at = time.time() + token_expires_in_seconds(refreshed)
                        discovered_project = None
                        if not (latest_account or {}).get("projectId"):
                            try:
                                from .oauth import discover_project_id
                                discovered_project = discover_project_id(new_access_token)
                            except Exception:
                                _log.warning("Project discovery failed for %s during refresh", email)

                        merged = False
                        with self._lock:
                            def merge(data: dict[str, Any]) -> bool:
                                nonlocal merged
                                self._sync_state_from_storage(data)
                                account = next(
                                    (
                                        item for item in data.get("accounts", [])
                                        if isinstance(item, dict) and str(item.get("email")) == email
                                    ),
                                    None,
                                )
                                if (
                                    not account
                                    or str(account.get("refreshToken")) != refresh_token
                                    or str(account.get("accessToken")) != captured_access_token
                                    or self._normalize_expires_at(account.get("expiresAt", 0)) != captured_expires_at
                                ):
                                    return False
                                account["accessToken"] = new_access_token
                                account["expiresAt"] = new_expires_at
                                if refreshed.get("refresh_token"):
                                    account["refreshToken"] = refreshed["refresh_token"]
                                if discovered_project and not account.get("projectId"):
                                    account["projectId"] = discovered_project
                                merged = True
                                return True

                            update_accounts(merge)
                        if merged:
                            summary["refreshed"] += 1
                    except Exception:
                        with self._lock:
                            def mark_failed(data: dict[str, Any]) -> bool:
                                self._sync_state_from_storage(data)
                                account = next(
                                    (
                                        item for item in data.get("accounts", [])
                                        if isinstance(item, dict) and str(item.get("email")) == email
                                    ),
                                    None,
                                )
                                if (
                                    not account
                                    or str(account.get("refreshToken")) != refresh_token
                                    or str(account.get("accessToken")) != captured_access_token
                                    or self._normalize_expires_at(account.get("expiresAt", 0)) != captured_expires_at
                                ):
                                    return False
                                self._state_owner.apply_cooldown(
                                    email,
                                    "gemini",
                                    AttemptOutcome(scope="account", category="auth"),
                                )
                                return True

                            if update_accounts(mark_failed):
                                summary["failed"] += 1
                except Exception:
                    summary["failed"] += 1
        return summary

    def clear_failures(self, email: str, family: str | None = None) -> None:
        with self._lock:
            self._mutate_state(lambda state: state.clear_failures(email, family))


def is_validation_required_error(status_code: int, body: str | None = None) -> bool:
    """Check if an error is a VALIDATION_REQUIRED auth issue rather than rate limit."""
    if status_code == 403:
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
