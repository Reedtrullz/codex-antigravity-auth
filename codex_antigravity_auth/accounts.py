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
        self._counters = {} # email -> family -> sanitized usage counters
        self._in_flight = {} # email -> process-local request count

    def _sync_state_from_storage(self, data: dict[str, Any]) -> bool:
        state_missing = "accountState" not in data
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
            counters = state.get("counters", {})
            if state_missing:
                if self._failures:
                    failures = {
                        **(failures if isinstance(failures, dict) else {}),
                        **self._failures,
                    }
                if self._cooldowns:
                    cooldowns = {
                        **(cooldowns if isinstance(cooldowns, dict) else {}),
                        **self._cooldowns,
                    }
                if self._counters:
                    counters = {
                        **(counters if isinstance(counters, dict) else {}),
                        **self._counters,
                    }
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
            cleaned_counters: dict[str, dict[str, dict[str, Any]]] = {}
            if isinstance(counters, dict):
                for email_key, raw_families in counters.items():
                    email = str(email_key)
                    if email not in account_emails or not isinstance(raw_families, dict):
                        continue
                    family_counters: dict[str, dict[str, Any]] = {}
                    for family_key, raw_counter in raw_families.items():
                        family = str(family_key)
                        if family not in {"claude", "gemini"} or not isinstance(raw_counter, dict):
                            continue
                        family_counters[family] = self._sanitize_counter(raw_counter)
                    if family_counters:
                        cleaned_counters[email] = family_counters
            self._counters = cleaned_counters
        else:
            self._failures = {}
            self._cooldowns = {}
            self._counters = {}
        cleaned_state = {
            "failures": self._failures,
            "cooldowns": self._cooldowns,
            "counters": self._counters,
        }
        return previous_state != cleaned_state

    def _save_state_to_storage(self) -> None:
        if not get_accounts_json_path().exists():
            return

        def mutate(data: dict[str, Any]) -> None:
            data["accountState"] = {
                "failures": self._failures,
                "cooldowns": self._cooldowns,
                "counters": self._counters,
            }

        update_accounts(mutate)

    @staticmethod
    def _sanitize_counter(raw_counter: dict[str, Any]) -> dict[str, Any]:
        sanitized: dict[str, Any] = {}
        integer_fields = (
            "total_requests",
            "successes",
            "failures",
            "rate_limits",
            "input_tokens",
            "output_tokens",
            "total_tokens",
        )
        for field in integer_fields:
            value = raw_counter.get(field, 0)
            if isinstance(value, bool):
                value = 0
            try:
                parsed = int(value)
            except (TypeError, ValueError):
                parsed = 0
            sanitized[field] = max(0, parsed)
        for field in ("last_success", "last_failure", "last_failure_class"):
            value = raw_counter.get(field)
            if isinstance(value, str) and not any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
                sanitized[field] = redact_secret_text(value)[:200]
        return sanitized

    @staticmethod
    def _model_family(model: str) -> str:
        return "claude" if "claude" in str(model).lower() else "gemini"

    def _counter_for(self, email: str, family: str) -> dict[str, Any]:
        account_counters = self._counters.setdefault(email, {})
        counter = account_counters.setdefault(
            family,
            {
                "total_requests": 0,
                "successes": 0,
                "failures": 0,
                "rate_limits": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
            },
        )
        account_counters[family] = self._sanitize_counter(counter)
        return account_counters[family]

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

    def _select_active_account(self, model: str, *, acquire: bool) -> dict[str, Any] | None:
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

                # Prefer the sticky active index when load is tied, but spread
                # concurrent requests across accounts with fewer in-flight calls.
                start_index = family_map.get(family, 0)
                if not isinstance(start_index, int) or start_index < 0 or start_index >= len(accounts):
                    start_index = 0

                current_time = time.time()
                candidates: list[tuple[int, int, int, dict[str, Any]]] = []
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

                    try:
                        in_flight = int(self._in_flight.get(str(email), 0))
                    except (TypeError, ValueError):
                        in_flight = 0
                    candidates.append((max(0, in_flight), i, idx, acc))
                if candidates:
                    _in_flight, _order, idx, acc = min(candidates, key=lambda item: (item[0], item[1]))
                    if family_map.get(family) != idx:
                        dirty = True
                    family_map[family] = idx
                    if data.get("activeIndex") != idx:
                        dirty = True
                    data["activeIndex"] = idx
                    state_payload = {
                        "failures": self._failures,
                        "cooldowns": self._cooldowns,
                        "counters": self._counters,
                    }
                    if self._failures or self._cooldowns or self._counters or data.get("accountState"):
                        if data.get("accountState") != state_payload:
                            dirty = True
                        data["accountState"] = state_payload
                    selected = acc
                    return dirty
                if dirty:
                    data["accountState"] = {
                        "failures": self._failures,
                        "cooldowns": self._cooldowns,
                        "counters": self._counters,
                    }
                return dirty

            update_accounts(mutate)
            if acquire and selected and selected.get("email"):
                email = str(selected["email"])
                try:
                    current = int(self._in_flight.get(email, 0))
                except (TypeError, ValueError):
                    current = 0
                self._in_flight[email] = max(0, current) + 1
            return selected

    def select_active_account(self, model: str) -> dict[str, Any] | None:
        return self._select_active_account(model, acquire=False)

    def acquire_account(self, model: str) -> dict[str, Any] | None:
        return self._select_active_account(model, acquire=True)

    def release_account(self, email: str | None) -> None:
        if not email:
            return
        with self._lock:
            key = str(email)
            try:
                current = int(self._in_flight.get(key, 0))
            except (TypeError, ValueError):
                current = 0
            next_value = max(0, current - 1)
            if next_value:
                self._in_flight[key] = next_value
            else:
                self._in_flight.pop(key, None)

    def in_flight_count(self, email: str | None) -> int:
        if not email:
            return 0
        with self._lock:
            try:
                return max(0, int(self._in_flight.get(str(email), 0)))
            except (TypeError, ValueError):
                return 0

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
            cooldown_duration = self._record_failure(email, retry_after_seconds)
            self._save_state_to_storage()
            
            # Print warning
            print(f"[*] Account {email} flagged as cooling down for {cooldown_duration}s. Reason: {redact_secret_text(reason)}")

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
        with self._lock:
            self._record_request_locked(
                email,
                model,
                status=status,
                status_code=status_code,
                error_class=error_class,
                usage=usage,
                persist=True,
            )

    def _record_request_locked(
        self,
        email: str,
        model: str,
        *,
        status: str,
        status_code: int | None = None,
        error_class: str | None = None,
        usage: dict[str, Any] | None = None,
        persist: bool,
    ) -> None:
        if not email:
            return
        family = self._model_family(model)
        counter = self._counter_for(email, family)
        counter["total_requests"] = int(counter.get("total_requests", 0)) + 1
        now_text = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        if status == "success":
            counter["successes"] = int(counter.get("successes", 0)) + 1
            counter["last_success"] = now_text
        else:
            counter["failures"] = int(counter.get("failures", 0)) + 1
            if status_code == 429:
                counter["rate_limits"] = int(counter.get("rate_limits", 0)) + 1
            counter["last_failure"] = now_text
            if error_class:
                counter["last_failure_class"] = redact_secret_text(str(error_class))[:200]
        if usage:
            for usage_field, counter_field in (
                ("input_tokens", "input_tokens"),
                ("output_tokens", "output_tokens"),
                ("total_tokens", "total_tokens"),
            ):
                value = usage.get(usage_field)
                if isinstance(value, bool):
                    continue
                try:
                    parsed = int(value)
                except (TypeError, ValueError):
                    continue
                if parsed > 0:
                    counter[counter_field] = int(counter.get(counter_field, 0)) + parsed
        self._counters[email][family] = self._sanitize_counter(counter)
        if persist:
            self._save_state_to_storage()

    def refresh_expiring_accounts(self, window_seconds: int = 300) -> dict[str, int]:
        with self._lock:
            summary = {"checked": 0, "refreshed": 0, "failed": 0}
            if not get_accounts_json_path().exists():
                return summary
            current_time = time.time()

            def mutate(data: dict[str, Any]) -> bool:
                dirty = self._sync_state_from_storage(data)
                accounts = data.get("accounts", [])
                if not isinstance(accounts, list):
                    return dirty
                for acc in accounts:
                    if not isinstance(acc, dict):
                        continue
                    email = acc.get("email")
                    refresh_tok = acc.get("refreshToken")
                    if not email or not refresh_tok:
                        continue
                    summary["checked"] += 1
                    expires_at = self._normalize_expires_at(acc.get("expiresAt", 0))
                    if expires_at > current_time + max(0, int(window_seconds)):
                        continue
                    try:
                        refreshed = refresh_access_token(refresh_tok)
                        acc["accessToken"] = refreshed["access_token"]
                        acc["expiresAt"] = current_time + token_expires_in_seconds(refreshed)
                        if refreshed.get("refresh_token"):
                            acc["refreshToken"] = refreshed["refresh_token"]
                        summary["refreshed"] += 1
                        dirty = True
                    except Exception:
                        self._record_failure(str(email), retry_after_seconds=None)
                        summary["failed"] += 1
                        dirty = True
                if dirty:
                    data["accountState"] = {
                        "failures": self._failures,
                        "cooldowns": self._cooldowns,
                        "counters": self._counters,
                    }
                return dirty

            update_accounts(mutate)
            return summary

    def clear_failures(self, email: str) -> None:
        with self._lock:
            self._failures.pop(email, None)
            self._cooldowns.pop(email, None)
            self._save_state_to_storage()
