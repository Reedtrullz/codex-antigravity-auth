import time
from typing import Any
from .storage import load_accounts, save_accounts
from .oauth import refresh_access_token
from .fingerprint import generate_fingerprint

class AccountManager:
    def __init__(self):
        self._lock = threading.RLock() if "threading" in globals() else None
        # Lazy imports are okay, but thread safety matters
        import threading
        self._lock = threading.RLock()
        self._failures = {} # email -> failure count
        self._cooldowns = {} # email -> cooldown end timestamp

    def get_accounts(self) -> list[dict[str, Any]]:
        with self._lock:
            data = load_accounts()
            return data.get("accounts", [])

    def select_active_account(self, model: str) -> dict[str, Any] | None:
        with self._lock:
            data = load_accounts()
            accounts = data.get("accounts", [])
            if not accounts:
                return None

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
                
                # Check cooldown
                cooldown_end = self._cooldowns.get(email, 0)
                if cooldown_end > current_time:
                    continue # Account is cooling down, skip
                
                # Ensure it has a fingerprint, generate one if not
                if not acc.get("fingerprint"):
                    acc["fingerprint"] = generate_fingerprint()
                    save_accounts(data)

                # Check if access token is missing or expired, auto-refresh if so
                expires_at = acc.get("expiresAt", 0) # epoch seconds
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
                            save_accounts(data)
                        except Exception as e:
                            # If token refresh fails (e.g. invalid grant), mark failure/cooldown
                            self.mark_failure(email, f"Token refresh failed: {e}")
                            continue

                # Found a viable account, make it sticky/active
                family_map[family] = idx
                data["activeIndex"] = idx
                save_accounts(data)
                return acc

            # If all accounts are cooling down or failed, fallback to the preferred one even if cooling down
            if start_index < len(accounts):
                return accounts[start_index]
            return None

    def mark_failure(self, email: str, reason: str) -> None:
        with self._lock:
            self._failures[email] = self._failures.get(email, 0) + 1
            # Cooldown for 2 minutes on first failure, exponentially backing off
            backoff_factor = min(self._failures[email], 5)
            cooldown_duration = 120 * (2 ** (backoff_factor - 1))
            self._cooldowns[email] = time.time() + cooldown_duration
            
            # Print warning
            print(f"[*] Account {email} flagged as cooling down for {cooldown_duration}s. Reason: {reason}")

    def clear_failures(self, email: str) -> None:
        with self._lock:
            self._failures.pop(email, None)
            self._cooldowns.pop(email, None)
