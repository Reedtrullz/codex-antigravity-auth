import copy
import math
import threading
import time
from typing import Any, Callable

from .account_state import AccountState
from .fingerprint import generate_fingerprint
from .oauth import refresh_access_token, token_expires_in_seconds
from .redaction import redact_secret_text
from .response_protocol import AttemptOutcome
from .storage import (
    accounts_json_path_read_only,
    get_accounts_json_path,
    load_accounts,
    update_accounts,
)


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

            def mutate(data: dict[str, Any]) -> bool:
                nonlocal selected
                dirty = self._sync_state_from_storage(data)
                while True:
                    active_index_before = data.get("activeIndex")
                    family_index_before = data.get("activeIndexByFamily", {}).get(family)
                    cooldowns_before = copy.deepcopy(self._state_owner.state["cooldowns"])
                    lease = (
                        self._state_owner.acquire(family)
                        if acquire
                        else self._state_owner.select(family)
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
                        account["fingerprint"] = generate_fingerprint()
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
                    try:
                        if not refresh_token:
                            raise RuntimeError("Token expired and no refresh token is available")
                        refreshed = refresh_access_token(refresh_token)
                        account["accessToken"] = refreshed["access_token"]
                        account["expiresAt"] = time.time() + token_expires_in_seconds(refreshed)
                        if refreshed.get("refresh_token"):
                            account["refreshToken"] = refreshed["refresh_token"]
                        selected = account
                        return True
                    except Exception as exc:
                        if acquire:
                            self._state_owner.release(lease)
                        self._state_owner.apply_cooldown(
                            email,
                            family,
                            AttemptOutcome(scope="account", category="auth"),
                        )
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
        with self._lock:
            summary = {"checked": 0, "refreshed": 0, "failed": 0}
            if not accounts_json_path_read_only().exists():
                return summary
            current_time = time.time()

            def mutate(data: dict[str, Any]) -> bool:
                self._sync_state_from_storage(data)
                accounts = data.get("accounts", [])
                if not isinstance(accounts, list):
                    return True
                for account in accounts:
                    if not isinstance(account, dict):
                        continue
                    email = str(account.get("email", ""))
                    refresh_token = account.get("refreshToken")
                    if not email or not refresh_token:
                        continue
                    summary["checked"] += 1
                    expires_at = self._normalize_expires_at(account.get("expiresAt", 0))
                    if expires_at > current_time + max(0, int(window_seconds)):
                        continue
                    try:
                        refreshed = refresh_access_token(refresh_token)
                        account["accessToken"] = refreshed["access_token"]
                        account["expiresAt"] = current_time + token_expires_in_seconds(refreshed)
                        if refreshed.get("refresh_token"):
                            account["refreshToken"] = refreshed["refresh_token"]
                        summary["refreshed"] += 1
                    except Exception:
                        self._state_owner.apply_cooldown(
                            email,
                            "gemini",
                            AttemptOutcome(scope="account", category="auth"),
                        )
                        summary["failed"] += 1
                return True

            update_accounts(mutate)
            return summary

    def clear_failures(self, email: str, family: str | None = None) -> None:
        with self._lock:
            self._mutate_state(lambda state: state.clear_failures(email, family))
