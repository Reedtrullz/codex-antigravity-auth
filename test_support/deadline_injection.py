"""Arm short gateway deadlines at a chosen phase instead of at request start.

Deadline tests need a tiny budget so they run quickly, but a tiny budget that
starts with the request also covers request parsing, routing and account
selection. On a loaded runner those steps can consume the whole budget, so the
request then fails in an earlier phase than the one under test (or never
reaches it). These helpers keep the real budget generous until the fixture
reaches the phase under test and only then shorten the real timers. The
production deadline code still decides the outcome.
"""
from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace

import anyio

from codex_antigravity_auth import request_budget as budgets, route_lifecycle

# Hang guards only: generous so scheduler stalls cannot trip them, while a
# genuinely hung scenario still fails instead of blocking the suite.
HANG_GUARD_SECONDS = 10.0


def budget_deadline_on_entry(monkeypatch, timeout=0.08):
    """Shorten RequestBudget timers once ``arm()`` runs.

    Budget timers created before arming wait for it and then sleep at most
    ``timeout``; the active budget's deadline becomes ``now + timeout``. Create
    this inside the running event loop. ``arm()`` may be called from the loop
    or from a worker thread started by the request (for example a blocking
    synchronous preparation step); it reads the request's budget from the
    caller's context in both cases.
    """
    loop = asyncio.get_running_loop()
    armed = asyncio.Event()
    async def timer(delay):
        await armed.wait()
        await asyncio.sleep(min(delay, timeout))
    phase_asyncio = SimpleNamespace(**vars(asyncio))
    phase_asyncio.sleep = timer
    monkeypatch.setattr(budgets, "asyncio", phase_asyncio)
    def apply(budget):
        budget.deadline = time.monotonic() + timeout
        armed.set()
    def arm():
        budget = budgets.CURRENT_BUDGET.get()
        assert budget is not None, "arm() must run inside a request budget"
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            apply(budget)
        else:
            loop.call_soon_threadsafe(apply, budget)
    return arm


class DiagnosticWriteDeadline:
    """Shorten the request-log write timeout once ``arm()`` runs.

    ``route_lifecycle.write_route_lifecycle`` bounds each blocking log write
    with ``anyio.fail_after(min(2.0, remaining budget))``. When that timer
    expires before the terminal row is observed, production code records
    ``request_deadline_exceeded`` exactly as for a real request deadline. Once
    armed, later writes use at most ``timeout``. Arm it before the write under
    test starts, e.g. at provider dispatch. Thread safe.
    """

    def __init__(self, timeout=0.08):
        self.timeout = timeout
        self.armed = threading.Event()
        self.anyio = _AnyioProxy(self)

    def arm(self):
        self.armed.set()

    def install(self, monkeypatch):
        monkeypatch.setattr(route_lifecycle, "anyio", self.anyio)
        return self

    def patch(self):
        from unittest.mock import patch
        return patch.object(route_lifecycle, "anyio", self.anyio)


class _AnyioProxy:
    def __init__(self, owner):
        self._owner = owner

    def fail_after(self, delay, *args, **kwargs):
        if self._owner.armed.is_set():
            delay = min(delay, self._owner.timeout)
        return anyio.fail_after(delay, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(anyio, name)
