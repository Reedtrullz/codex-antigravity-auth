import math
import time
import threading
from typing import Any
from .storage import load_accounts, get_accounts_json_path, update_accounts
from .oauth import refresh_access_token, token_expires_in_seconds
from .fingerprint import generate_fingerprint
from .redaction import redact_secret_text

class AccountManager:
    def __init__(self):
        self._lock = threading.RLock()
        self._failures = {} # email -> failure count
        self._cooldowns = {} # email -> cooldown end timestamp

    def _sync_state_from_storage(self, data: dict[str, Any]) -> bool:
        state = data.get("accountState", {})
        previous_state = state if isinstance(state, dict) else {}
        accounts = data.get("accounts", [])
        account_emails = {
            str(account.get("email"))
            for account in accounts
            if isinstance(account, dict) and account.get("email")
        }
        if isinstance(state, dict):
            failures = state.get("failures", {})
            cooldowns = state.get("cooldowns", {})
            active_cooldown_emails = set()
            cleaned_cooldowns = {}
            if isinstance(cooldowns, dict):
                current_time = time.time()
                for k, v in cooldowns.items():
                    email = str(k)
                    if email not in account_emails:
                        continue
                    if not isinstance(v, (int, float)) or isinstance(v, bool):
                        continue
                    cooldown_end = float(v)
                    if math.isfinite(cooldown_end) and cooldown_end > current_time:
                        cleaned_cooldowns[email] = cooldown_end
                        active_cooldown_emails.add(email)
            self._cooldowns = cleaned_cooldowns
            cleaned_failures = {}
            if isinstance(failures, dict):
                for k, v in failures.items():
                    email = str(k)
                    if email not in active_cooldown_emails:
                        continue
                    if not isinstance(v, (int, float)) or isinstance(v, bool):
                        continue
                    failure_number = float(v)
                    if not math.isfinite(failure_number):
                        continue
                    failure_count = int(failure_number)
                    if failure_count > 0:
                        cleaned_failures[email] = failure_count
            self._failures = cleaned_failures
        else:
            self._failures = {}
            self._cooldowns = {}
        cleaned_state = {
            "failures": self._failures,
            "cooldowns": self._cooldowns,
        }
        return previous_state != cleaned_state

    def _save_state_to_storage(self) -> None:
        if not get_accounts_json_path().exists():
            return

        def mutate(data: dict[str, Any]) -> None:
            data["accountState"] = {
                "failures": self._failures,
                "cooldowns": self._cooldowns,
            }

        update_accounts(mutate)

    @staticmethod
    def _normalize_expires_at(value: Any) -> float:
        try:
            expires_at = float(value or 0)
        except (TypeError, ValueError):
            return 0
        if not math.isfinite(expires_at):
            return 0
        # Epoch milliseconds are currently around 1.7e12; epoch seconds around 1.7e9.
        if expires_at > 10_000_000_000:
            expires_at = expires_at / 1000
        return expires_at

    def get_accounts(self) -> list[dict[str, Any]]:
        with self._lock:
            data = load_accounts()
            return data.get("accounts", [])

    def select_active_account(self, model: str) -> dict[str, Any] | None:
        with self._lock:
            selected: dict[str, Any] | None = None

            def mutate(data: dict[str, Any]) -> bool:
                nonlocal selected
                dirty = self._sync_state_from_storage(data)
                accounts = data.get("accounts", [])
                if not accounts:
                    if dirty:
                        data["accountState"] = {
                            "failures": self._failures,
                            "cooldowns": self._cooldowns,
                        }
                    return dirty

                family = "claude" if "claude" in model.lower() else "gemini"
                family_map = data.setdefault("activeIndexByFamily", {"claude": 0, "gemini": 0})

                # Simple rotation/selection strategy:
                # Check accounts from the preferred active index for this family.
                start_index = family_map.get(family, 0)
                if not isinstance(start_index, int) or start_index < 0 or start_index >= len(accounts):
                    start_index = 0

                current_time = time.time()
                for i in range(len(accounts)):
                    idx = (start_index + i) % len(accounts)
                    acc = accounts[idx]
                    email = acc.get("email")
                    if not email:
                        continue

                    cooldown_end = self._cooldowns.get(email, 0)
                    if cooldown_end > current_time:
                        continue
                    if cooldown_end:
                        self._cooldowns.pop(email, None)
                        self._failures.pop(email, None)
                        dirty = True

                    if not acc.get("fingerprint"):
                        acc["fingerprint"] = generate_fingerprint()
                        dirty = True

                    expires_at = self._normalize_expires_at(acc.get("expiresAt", 0))
                    if expires_at != acc.get("expiresAt", 0):
                        acc["expiresAt"] = expires_at
                        dirty = True
                    if not acc.get("accessToken") or expires_at < current_time + 300:
                        refresh_tok = acc.get("refreshToken")
                        if refresh_tok:
                            try:
                                refreshed = refresh_access_token(refresh_tok)
                                acc["accessToken"] = refreshed["access_token"]
                                acc["expiresAt"] = current_time + token_expires_in_seconds(refreshed)
                                if refreshed.get("refresh_token"):
                                    acc["refreshToken"] = refreshed["refresh_token"]
                                dirty = True
                            except Exception as e:
                                self._record_failure(email, retry_after_seconds=None)
                                print(f"[*] Account {email} flagged as cooling down. Reason: {redact_secret_text(f'Token refresh failed: {e}')}")
                                dirty = True
                                continue
                        else:
                            self._record_failure(email, retry_after_seconds=None)
                            print(f"[*] Account {email} flagged as cooling down. Reason: Token expired and no refresh token is available")
                            dirty = True
                            continue

                    if family_map.get(family) != idx:
                        dirty = True
                    family_map[family] = idx
                    if data.get("activeIndex") != idx:
                        dirty = True
                    data["activeIndex"] = idx
                    state_payload = {
                        "failures": self._failures,
                        "cooldowns": self._cooldowns,
                    }
                    if self._failures or self._cooldowns or data.get("accountState"):
                        if data.get("accountState") != state_payload:
                            dirty = True
                        data["accountState"] = state_payload
                    selected = acc
                    return dirty
                if dirty:
                    data["accountState"] = {
                        "failures": self._failures,
                        "cooldowns": self._cooldowns,
                    }
                return dirty

            update_accounts(mutate)
            return selected

    def _record_failure(self, email: str, retry_after_seconds: float | None = None) -> float:
        previous_failures = self._failures.get(email, 0)
        if not isinstance(previous_failures, int) or isinstance(previous_failures, bool) or previous_failures < 0:
            previous_failures = 0
        self._failures[email] = previous_failures + 1
        backoff_factor = min(self._failures[email], 5)
        cooldown_duration = 120 * (2 ** (backoff_factor - 1))
        try:
            retry_after = (
                float(retry_after_seconds)
                if retry_after_seconds is not None and not isinstance(retry_after_seconds, bool)
                else 0.0
            )
        except (TypeError, ValueError):
            retry_after = 0.0
        if math.isfinite(retry_after) and retry_after > 0:
            cooldown_duration = max(cooldown_duration, min(retry_after, 86_400.0))
        self._cooldowns[email] = time.time() + cooldown_duration
        return cooldown_duration

    def mark_failure(self, email: str, reason: str, retry_after_seconds: float | None = None) -> None:
        with self._lock:
            if not email:
                return
            cooldown_duration = self._record_failure(email, retry_after_seconds)
            self._save_state_to_storage()
            
            # Print warning
            print(f"[*] Account {email} flagged as cooling down for {cooldown_duration}s. Reason: {redact_secret_text(reason)}")

    def clear_failures(self, email: str) -> None:
        with self._lock:
            self._failures.pop(email, None)
            self._cooldowns.pop(email, None)
            self._save_state_to_storage()
