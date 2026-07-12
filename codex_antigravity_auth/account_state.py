"""Versioned, family-aware Google account routing state."""

from __future__ import annotations

import copy
from dataclasses import dataclass
import math
import threading
import time
from typing import Any, Callable

from .response_protocol import AttemptOutcome

SCHEMA_VERSION = 2
FAMILIES = ("claude", "gemini")


def _number(value: Any) -> float:
    if isinstance(value, bool):
        return 0
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0
    return number if math.isfinite(number) and number > 0 else 0


def _epoch(value: Any, now: float) -> float:
    number = _number(value)
    if number > 10_000_000_000:
        number /= 1000
    return number if number > now else 0


def scoped_cooldown_expiry(value: object, family: str) -> float:
    if isinstance(value, dict):
        return max(_epoch(value.get("account"), 0), _epoch(value.get(family), 0))
    return _epoch(value, 0)


def _counter(value: object) -> dict[str, Any]:
    raw = value if isinstance(value, dict) else {}
    result = {
        field: int(_number(raw.get(field, 0)))
        for field in (
            "total_requests",
            "successes",
            "failures",
            "rate_limits",
            "input_tokens",
            "output_tokens",
            "total_tokens",
        )
    }
    for field in ("last_success", "last_failure", "last_failure_class"):
        text = raw.get(field)
        if isinstance(text, str) and text and not any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in text):
            result[field] = text[:200]
    return result


def _scoped_numbers(value: object, *, legacy: bool) -> dict[str, int]:
    if legacy:
        count = int(_number(value))
        return {"account": count} if count else {}
    if not isinstance(value, dict):
        return {}
    result = {}
    for scope in ("account", *FAMILIES):
        count = int(_number(value.get(scope)))
        if count:
            result[scope] = count
    return result


def _scoped_cooldowns(value: object, *, legacy: bool, now: float) -> dict[str, float]:
    if legacy:
        expiry = _epoch(value, now)
        return {"account": expiry} if expiry else {}
    if not isinstance(value, dict):
        return {}
    result = {}
    for scope in ("account", *FAMILIES):
        expiry = _epoch(value.get(scope), now)
        if expiry:
            result[scope] = expiry
    return result


def migrate_account_state(data: dict[str, Any], *, now: float) -> tuple[dict[str, Any], bool]:
    original = copy.deepcopy(data)
    normalized = copy.deepcopy(data) if isinstance(data, dict) else {}
    accounts = normalized.get("accounts")
    accounts = accounts if isinstance(accounts, list) else []
    emails = {
        str(account["email"])
        for account in accounts
        if isinstance(account, dict) and account.get("email")
    }
    raw_state = normalized.get("accountState")
    raw_state = raw_state if isinstance(raw_state, dict) else {}
    legacy = raw_state.get("schemaVersion") != SCHEMA_VERSION

    failures = {}
    raw_failures = raw_state.get("failures")
    if isinstance(raw_failures, dict):
        for email, value in raw_failures.items():
            email = str(email)
            scoped = _scoped_numbers(value, legacy=legacy)
            if email in emails and scoped:
                failures[email] = scoped

    cooldowns = {}
    raw_cooldowns = raw_state.get("cooldowns")
    if isinstance(raw_cooldowns, dict):
        for email, value in raw_cooldowns.items():
            email = str(email)
            scoped = _scoped_cooldowns(value, legacy=legacy, now=now)
            if email in emails and scoped:
                cooldowns[email] = scoped

    counters = {}
    raw_counters = raw_state.get("counters")
    if isinstance(raw_counters, dict):
        for email, families in raw_counters.items():
            email = str(email)
            if email not in emails or not isinstance(families, dict):
                continue
            clean = {family: _counter(families[family]) for family in FAMILIES if family in families}
            if clean:
                counters[email] = clean

    normalized["accountState"] = {
        "schemaVersion": SCHEMA_VERSION,
        "failures": failures,
        "cooldowns": cooldowns,
        "counters": counters,
    }
    return normalized, normalized != original


@dataclass(frozen=True)
class Lease:
    account: dict[str, Any]
    family: str


class AccountState:
    def __init__(
        self,
        data: dict[str, Any],
        *,
        now: Callable[[], float] = time.time,
        in_flight: dict[str, int] | None = None,
    ) -> None:
        self.data = data
        self._now = now
        self._lock = threading.RLock()
        self._in_flight = in_flight if in_flight is not None else {}
        migrated, _changed = migrate_account_state(data, now=now())
        data.clear()
        data.update(migrated)

    @property
    def state(self) -> dict[str, Any]:
        return self.data["accountState"]

    def _select(self, family: str, *, acquire: bool) -> Lease | None:
        if family not in FAMILIES:
            raise ValueError(f"unsupported model family: {family}")
        with self._lock:
            accounts = self.data.get("accounts")
            if not isinstance(accounts, list) or not accounts:
                return None
            family_map = self.data.setdefault("activeIndexByFamily", {name: 0 for name in FAMILIES})
            start = family_map.get(family, 0)
            if not isinstance(start, int) or start < 0 or start >= len(accounts):
                start = 0
            now = self._now()
            candidates = []
            for order in range(len(accounts)):
                index = (start + order) % len(accounts)
                account = accounts[index]
                if not isinstance(account, dict) or not account.get("email"):
                    continue
                email = str(account["email"])
                scoped = self.state["cooldowns"].get(email, {})
                if scoped.get("account", 0) > now or scoped.get(family, 0) > now:
                    continue
                for scope in ("account", family):
                    if scoped.get(scope) and scoped[scope] <= now:
                        scoped.pop(scope, None)
                candidates.append((self._in_flight.get(email, 0), order, index, account))
            if not candidates:
                return None
            _load, _order, index, account = min(candidates, key=lambda item: (item[0], item[1]))
            family_map[family] = index
            self.data["activeIndex"] = index
            email = str(account["email"])
            if acquire:
                self._in_flight[email] = self._in_flight.get(email, 0) + 1
            return Lease(account=account, family=family)

    def select(self, family: str) -> Lease | None:
        return self._select(family, acquire=False)

    def acquire(self, family: str) -> Lease | None:
        return self._select(family, acquire=True)

    def release(self, lease: Lease | None) -> None:
        if lease is not None:
            self.release_email(str(lease.account.get("email", "")))

    def release_email(self, email: str) -> None:
        if not email:
            return
        with self._lock:
            remaining = max(0, self._in_flight.get(email, 0) - 1)
            if remaining:
                self._in_flight[email] = remaining
            else:
                self._in_flight.pop(email, None)

    def in_flight(self, email: str) -> int:
        with self._lock:
            return max(0, self._in_flight.get(email, 0))

    def _counter_for(self, email: str, family: str) -> dict[str, Any]:
        families = self.state["counters"].setdefault(email, {})
        counter = families.setdefault(family, _counter({}))
        return counter

    def record(
        self,
        lease: Lease,
        outcome: AttemptOutcome,
        *,
        usage: dict[str, Any] | None = None,
        error_class: str | None = None,
    ) -> None:
        self.record_email(
            str(lease.account.get("email", "")),
            lease.family,
            outcome,
            usage=usage,
            error_class=error_class,
        )

    def record_email(
        self,
        email: str,
        family: str,
        outcome: AttemptOutcome,
        *,
        usage: dict[str, Any] | None = None,
        error_class: str | None = None,
    ) -> None:
        if family not in FAMILIES:
            raise ValueError(f"unsupported model family: {family}")
        with self._lock:
            if not email:
                return
            counter = self._counter_for(email, family)
            counter["total_requests"] += 1
            timestamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self._now()))
            if outcome.category == "success":
                counter["successes"] += 1
                counter["last_success"] = timestamp
            else:
                counter["failures"] += 1
                counter["last_failure"] = timestamp
                counter["last_failure_class"] = (error_class or outcome.category)[:200]
                if outcome.category == "rate_limit":
                    counter["rate_limits"] += 1
            if usage:
                for field in ("input_tokens", "output_tokens", "total_tokens"):
                    value = usage.get(field)
                    if isinstance(value, bool):
                        continue
                    try:
                        count = int(value)
                    except (TypeError, ValueError):
                        continue
                    if count > 0:
                        counter[field] += count
            if outcome.scope == "none":
                return
            self._apply_cooldown(email, family, outcome)

    def _apply_cooldown(self, email: str, family: str, outcome: AttemptOutcome) -> float:
        scope = "account" if outcome.scope == "account" else family
        failures = self.state["failures"].setdefault(email, {})
        if not isinstance(failures, dict):
            failures = {}
            self.state["failures"][email] = failures
        failures[scope] = failures.get(scope, 0) + 1
        backoff = 120 * (2 ** (min(failures[scope], 5) - 1))
        retry_after = outcome.retry_after_seconds or 0
        duration = max(backoff, min(float(retry_after), 86_400))
        cooldowns = self.state["cooldowns"].setdefault(email, {})
        if not isinstance(cooldowns, dict):
            cooldowns = {}
            self.state["cooldowns"][email] = cooldowns
        cooldowns[scope] = self._now() + duration
        return duration

    def apply_cooldown(self, email: str, family: str, outcome: AttemptOutcome) -> float:
        if outcome.scope == "none":
            return 0
        with self._lock:
            return self._apply_cooldown(email, family, outcome)

    def clear_failures(self, email: str, family: str | None = None) -> None:
        with self._lock:
            if family is None:
                self.state["failures"].pop(email, None)
                self.state["cooldowns"].pop(email, None)
                return
            for section in ("failures", "cooldowns"):
                scoped = self.state[section].get(email)
                if isinstance(scoped, dict):
                    scoped.pop(family, None)
                    if not scoped:
                        self.state[section].pop(email, None)
