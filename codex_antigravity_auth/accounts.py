import time
import threading
from typing import Any
from .storage import load_accounts, save_accounts, get_accounts_json_path
from .oauth import refresh_access_token
from .fingerprint import generate_fingerprint
from .redaction import redact_secret_text

class AccountManager:
    def __init__(self):
        self._lock = threading.RLock()
        self._failures = {} # email -> failure count
        self._cooldowns = {} # email -> cooldown end timestamp

    def _sync_state_from_storage(self, data: dict[str, Any]) -> None:
        state = data.get("accountState", {})
        if isinstance(state, dict):
            failures = state.get("failures", {})
            cooldowns = state.get("cooldowns", {})
            if isinstance(failures, dict):
                self._failures.update({str(k): int(v) for k, v in failures.items() if isinstance(v, (int, float))})
            if isinstance(cooldowns, dict):
                self._cooldowns.update({str(k): float(v) for k, v in cooldowns.items() if isinstance(v, (int, float))})

    def _save_state_to_storage(self) -> None:
        data = load_accounts()
        if not data.get("accounts") and not get_accounts_json_path().exists():
            return
        data["accountState"] = {
            "failures": self._failures,
            "cooldowns": self._cooldowns,
        }
        save_accounts(data)

    @staticmethod
    def _normalize_expires_at(value: Any) -> float:
        try:
            expires_at = float(value or 0)
        except (TypeError, ValueError):
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
            data = load_accounts()
            self._sync_state_from_storage(data)
            accounts = data.get("accounts", [])
            if not accounts:
                return None
            dirty = False

            family = "claude" if "claude" in model.lower() else "gemini"
            family_map = data.setdefault("activeIndexByFamily", {"claude": 0, "gemini": 0})
            
            # Simple rotation/selection strategy:
            # Let's check the accounts one by one starting from the preferred active index for this family.
            # If the preferred index is invalid or in cooldown, rotate to the next one that is not in cooldown.
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
                
                # Check cooldown
                cooldown_end = self._cooldowns.get(email, 0)
                if cooldown_end > current_time:
                    continue # Account is cooling down, skip
                if cooldown_end:
                    self._cooldowns.pop(email, None)
                    self._failures.pop(email, None)
                    dirty = True
                
                # Ensure it has a fingerprint, generate one if not
                if not acc.get("fingerprint"):
                    acc["fingerprint"] = generate_fingerprint()
                    dirty = True

                # Check if access token is missing or expired, auto-refresh if so
                expires_at = self._normalize_expires_at(acc.get("expiresAt", 0))
                if expires_at != acc.get("expiresAt", 0):
                    acc["expiresAt"] = expires_at
                    dirty = True
                if not acc.get("accessToken") or expires_at < current_time + 300: # 5 min buffer
                    refresh_tok = acc.get("refreshToken")
                    if refresh_tok:
                        try:
                            refreshed = refresh_access_token(refresh_tok)
                            acc["accessToken"] = refreshed["access_token"]
                            acc["expiresAt"] = current_time + refreshed.get("expires_in", 3600)
                            # update refresh token if Google rotates it
                            if refreshed.get("refresh_token"):
                                acc["refreshToken"] = refreshed["refresh_token"]
                            dirty = True
                        except Exception as e:
                            # If token refresh fails (e.g. invalid grant), mark failure/cooldown
                            self.mark_failure(email, f"Token refresh failed: {e}")
                            continue
                    else:
                        self.mark_failure(email, "Token expired and no refresh token is available")
                        continue

                # Found a viable account, make it sticky/active
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
                if dirty:
                    save_accounts(data)
                return acc

            return None

    def mark_failure(self, email: str, reason: str, retry_after_seconds: float | None = None) -> None:
        with self._lock:
            if not email:
                return
            self._failures[email] = self._failures.get(email, 0) + 1
            # Cooldown for 2 minutes on first failure, exponentially backing off
            backoff_factor = min(self._failures[email], 5)
            cooldown_duration = 120 * (2 ** (backoff_factor - 1))
            if retry_after_seconds and retry_after_seconds > 0:
                cooldown_duration = max(cooldown_duration, min(float(retry_after_seconds), 86_400.0))
            self._cooldowns[email] = time.time() + cooldown_duration
            self._save_state_to_storage()
            
            # Print warning
            print(f"[*] Account {email} flagged as cooling down for {cooldown_duration}s. Reason: {redact_secret_text(reason)}")

    def clear_failures(self, email: str) -> None:
        with self._lock:
            self._failures.pop(email, None)
            self._cooldowns.pop(email, None)
            self._save_state_to_storage()
