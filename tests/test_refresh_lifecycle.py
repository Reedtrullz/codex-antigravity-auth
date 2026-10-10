"""Synthetic refresh races and lifespan ownership; no OAuth calls or real stores."""
import asyncio
import copy
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from codex_antigravity_auth import accounts, server, storage

_schedule = server.schedule_refresh_accounts_ahead


def account(email, *, expires=0):
    return {"email": email, "refreshToken": "fixture-refresh-" + email,
            "accessToken": "fixture-access-" + email, "expiresAt": expires,
            "projectId": "fixture-project"}


@pytest.fixture
def pool(monkeypatch):
    data = {"accounts": [account("first@example.invalid"),
                         account("second@example.invalid", expires=time.time() + 3600)],
            "activeIndex": 0, "activeIndexByFamily": {"gemini": 0, "claude": 0},
            "accountState": {"schemaVersion": 2, "failures": {}, "cooldowns": {}, "counters": {}}}
    store_lock = threading.RLock()

    def update(mutator):
        with store_lock:
            return mutator(data)

    def load():
        with store_lock:
            return copy.deepcopy(data)

    monkeypatch.setattr(accounts, "update_accounts", update)
    monkeypatch.setattr(accounts, "load_accounts", load)
    monkeypatch.setattr(accounts, "accounts_json_path_read_only", lambda: type("Exists", (), {"exists": lambda _: True})())
    return data, update


@pytest.mark.parametrize("acquire", [False, True])
def test_delayed_foreground_allows_healthy_selection_and_removal(pool, monkeypatch, acquire):
    data, update = pool
    manager = accounts.AccountManager()
    entered, release = threading.Event(), threading.Event()

    def refresh(_token):
        entered.set()
        assert release.wait(3)
        return {"access_token": "fixture-new", "expires_in": 3600}

    monkeypatch.setattr(accounts, "refresh_access_token", refresh)
    select = manager.acquire_account if acquire else manager.select_active_account
    with ThreadPoolExecutor(3) as executor:
        pending = executor.submit(select, "gemini-3.8-flash")
        try:
            assert entered.wait(2)
            healthy = executor.submit(select, "gemini-3.8-flash").result(timeout=1)
            assert healthy["email"] == "second@example.invalid"
            executor.submit(update, lambda state: state["accounts"].pop(0)).result(timeout=1)
            assert manager.in_flight_count("first@example.invalid") == 0
        finally:
            release.set()
        assert pending.result(timeout=2)["email"] == "second@example.invalid"
    assert [a["email"] for a in data["accounts"]] == ["second@example.invalid"]
    assert data["accountState"]["cooldowns"] == {}


def test_only_expiring_account_reports_refresh_in_progress(pool):
    data, _update = pool
    data["accounts"] = data["accounts"][:1]
    email = data["accounts"][0]["email"]
    lock = accounts._get_refresh_lock(email)
    entered, release = threading.Event(), threading.Event()

    def background_refresh_owner():
        with lock:
            entered.set()
            assert release.wait(2)

    owner = threading.Thread(target=background_refresh_owner)
    owner.start()
    try:
        assert entered.wait(1)
        manager = accounts.AccountManager()
        with pytest.raises(accounts.AccountRefreshInProgress):
            manager.acquire_account("gemini-3.8-flash")
        assert manager.in_flight_count(email) == 0
        assert data["accountState"]["cooldowns"] == {}
    finally:
        release.set()
        owner.join(timeout=2)
    assert not owner.is_alive()


@pytest.mark.parametrize("background_first", [False, True])
def test_refresh_single_flight_across_managers_and_foreground_background(pool, monkeypatch, background_first):
    data, _update = pool
    managers = [accounts.AccountManager(), accounts.AccountManager()]
    entered, release = threading.Event(), threading.Event()
    calls = []

    def refresh(token):
        calls.append(token)
        entered.set()
        assert release.wait(3)
        return {"access_token": "fixture-new", "expires_in": 3600}

    monkeypatch.setattr(accounts, "refresh_access_token", refresh)
    first = (managers[0].refresh_expiring_accounts if background_first
             else lambda: managers[0].select_active_account("gemini-3.8-flash"))
    with ThreadPoolExecutor(2) as executor:
        pending = executor.submit(first)
        try:
            assert entered.wait(2)
            assert managers[1].refresh_expiring_accounts()["refreshed"] == 0
            assert managers[1].select_active_account("gemini-3.8-flash")["email"] == "second@example.invalid"
            assert len(calls) == 1
            assert data["accountState"]["cooldowns"] == {}
        finally:
            release.set()
        pending.result(timeout=2)
    assert data["accounts"][0]["accessToken"] == "fixture-new"
    assert managers[1].refresh_expiring_accounts()["refreshed"] == 0
    assert len(calls) == 1


@pytest.mark.parametrize("failure", [False, True])
@pytest.mark.parametrize("change", ["remove", "rotate", "new_access", "disable", "cooldown"])
def test_concurrent_state_wins_over_refresh_success_or_failure(pool, monkeypatch, failure, change):
    data, update = pool
    snapshot = copy.deepcopy(data["accounts"][0])

    def refresh(_token):
        def mutate(state):
            current = state["accounts"][0]
            if change == "remove":
                state["accounts"].pop(0)
            elif change == "rotate":
                current["refreshToken"] = "fixture-rotated"
            elif change == "new_access":
                current.update(accessToken="fixture-newer", expiresAt=time.time() + 3600)
            elif change == "disable":
                state["accountState"]["disabled"] = {current["email"]: {"reason": "fixture"}}
            else:
                state["accountState"]["cooldowns"][current["email"]] = {"account": time.time() + 600}
        update(mutate)
        if failure:
            raise RuntimeError("synthetic failure")
        return {"access_token": "fixture-stale", "refresh_token": "fixture-stale-rotated", "expires_in": 3600}

    monkeypatch.setattr(accounts, "refresh_access_token", refresh)
    assert accounts.AccountManager()._refresh_snapshot(snapshot) == "changed"
    assert all(a["accessToken"] != "fixture-stale" for a in data["accounts"])
    assert data["accountState"]["failures"] == {}


def test_changed_snapshot_never_dispatches_old_refresh(pool, monkeypatch):
    data, _update = pool
    snapshot = copy.deepcopy(data["accounts"][0])
    data["accounts"][0]["refreshToken"] = "fixture-rotated"
    monkeypatch.setattr(accounts, "refresh_access_token", lambda _: pytest.fail("old token dispatched"))
    assert accounts.AccountManager()._refresh_snapshot(snapshot) == "changed"


@pytest.mark.parametrize("failure", [False, True])
def test_stop_during_refresh_skips_discovery_merge_and_remaining_accounts(pool, monkeypatch, failure):
    data, _update = pool
    data["accounts"][0].pop("projectId")
    data["accounts"][1]["expiresAt"] = 0
    stop = threading.Event()
    calls = []

    def refresh(token):
        calls.append(token)
        stop.set()
        if failure:
            raise RuntimeError("synthetic failure")
        return {"access_token": "fixture-new", "expires_in": 3600}

    monkeypatch.setattr(accounts, "refresh_access_token", refresh)
    monkeypatch.setattr("codex_antigravity_auth.oauth.discover_project_id", lambda _: pytest.fail("discovery after stop"))
    before = copy.deepcopy(data)
    assert accounts.AccountManager().refresh_expiring_accounts(stop_event=stop) == {"checked": 2, "refreshed": 0, "failed": 0}
    assert len(calls) == 1
    assert data == before


def test_real_encrypted_store_removal_finishes_during_refresh(monkeypatch):
    storage.save_accounts({"accounts": [account("removed@example.invalid")], "activeIndex": 0})
    manager = accounts.AccountManager()
    entered, release = threading.Event(), threading.Event()

    def refresh(_token):
        entered.set()
        assert release.wait(3)
        return {"access_token": "fixture-new", "expires_in": 3600}

    monkeypatch.setattr(accounts, "refresh_access_token", refresh)
    with ThreadPoolExecutor(2) as executor:
        pending = executor.submit(manager.acquire_account, "gemini-3.8-flash")
        try:
            assert entered.wait(2)
            def remove(data):
                data["accounts"] = []
                return True
            executor.submit(storage.update_accounts, remove).result(timeout=1)
        finally:
            release.set()
        assert pending.result(timeout=2) is None
    assert storage.load_accounts()["accounts"] == []
    assert manager.in_flight_count("removed@example.invalid") == 0


def test_periodic_refresh_runs_while_idle_and_stops(monkeypatch):
    monkeypatch.setattr(server, "schedule_refresh_accounts_ahead", _schedule)
    monkeypatch.setattr(server, "REFRESH_AHEAD_THROTTLE_SECONDS", 0.01)
    calls = []

    def refresh(_window, *, stop_event):
        calls.append(stop_event)
        if len(calls) == 1:
            raise RuntimeError("synthetic first tick failure")

    monkeypatch.setattr(server.account_manager, "refresh_expiring_accounts", refresh)

    async def scenario():
        assert not _schedule(force=True)
        async with server.gateway_lifespan(server.app):
            owner = server._refresh_ahead_owner
            async def twice():
                while len(calls) < 2:
                    await asyncio.sleep(0.005)
            await asyncio.wait_for(twice(), 2)
        count = len(calls)
        await asyncio.sleep(0.03)
        assert len(calls) == count
        assert all(stop.is_set() for stop in calls)
        assert owner.timer.done() and owner.worker.done()
        assert server._refresh_ahead_owner is None
        assert not _schedule(force=True)
    asyncio.run(scenario())


@pytest.mark.parametrize("cancel_shutdown", [False, True])
def test_shutdown_drains_worker_and_disallows_new_work(monkeypatch, cancel_shutdown):
    monkeypatch.setattr(server, "schedule_refresh_accounts_ahead", _schedule)
    entered, release = threading.Event(), threading.Event()
    calls = []

    def refresh(_window, *, stop_event):
        calls.append(stop_event)
        entered.set()
        assert release.wait(3)
        assert stop_event.is_set()

    monkeypatch.setattr(server.account_manager, "refresh_expiring_accounts", refresh)

    async def scenario():
        context = server.gateway_lifespan(server.app)
        await context.__aenter__()
        owner = server._refresh_ahead_owner
        closing = None
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            assert not _schedule(force=True)
            with pytest.raises(RuntimeError, match="already running"):
                async with server.gateway_lifespan(server.app):
                    pass
            closing = asyncio.create_task(context.__aexit__(None, None, None))
            await asyncio.sleep(0.02)
            assert owner.stop.is_set()
            if cancel_shutdown:
                closing.cancel()
                await asyncio.sleep(0.01)
            assert not closing.done()
            assert not _schedule(force=True)
        finally:
            release.set()
            if closing is not None:
                if cancel_shutdown:
                    with pytest.raises(asyncio.CancelledError):
                        await closing
                else:
                    await closing
            else:
                await context.__aexit__(None, None, None)
        assert len(calls) == 1
        assert owner.timer.done() and owner.worker.done()
        assert server._refresh_ahead_owner is None
    asyncio.run(scenario())


def test_discovery_does_not_hold_selection_or_store_locks(pool, monkeypatch):
    data, update = pool
    data["accounts"][0].pop("projectId")
    entered, release = threading.Event(), threading.Event()
    manager = accounts.AccountManager()
    monkeypatch.setattr(accounts, "refresh_access_token", lambda _: {"access_token": "fixture-new", "expires_in": 3600})

    def discover(_token):
        entered.set()
        assert release.wait(3)
        return "fixture-discovered"

    monkeypatch.setattr("codex_antigravity_auth.oauth.discover_project_id", discover)
    with ThreadPoolExecutor(3) as executor:
        pending = executor.submit(manager.acquire_account, "gemini-3.8-flash")
        try:
            assert entered.wait(2)
            chosen = executor.submit(manager.select_active_account, "gemini-3.8-flash").result(timeout=1)
            assert chosen["email"] == "second@example.invalid"
            executor.submit(update, lambda state: state["accounts"][0].update(projectId="fixture-newer-project")).result(timeout=1)
        finally:
            release.set()
        pending.result(timeout=2)
    assert data["accounts"][0]["projectId"] == "fixture-newer-project"


def test_refresh_keeps_family_cooldown_and_rechecks_selection(pool, monkeypatch):
    data, _update = pool
    expiry = time.time() + 600

    def refresh(_token):
        data["accountState"]["cooldowns"][data["accounts"][0]["email"]] = {"claude": expiry}
        return {"access_token": "fixture-new", "expires_in": 3600}

    monkeypatch.setattr(accounts, "refresh_access_token", refresh)
    manager = accounts.AccountManager()
    assert manager.acquire_account("claude-sonnet-4-6")["email"] == "second@example.invalid"
    assert data["accountState"]["cooldowns"]["first@example.invalid"] == {"claude": expiry}
    assert data["accounts"][0]["accessToken"] == "fixture-new"
    assert manager.in_flight_count("first@example.invalid") == 0


def test_unusable_short_refresh_is_not_selected_or_retried_forever(pool, monkeypatch):
    calls = []
    def refresh(token):
        calls.append(token)
        return {"access_token": "fixture-too-short", "expires_in": 1}
    monkeypatch.setattr(accounts, "refresh_access_token", refresh)
    manager = accounts.AccountManager()
    assert manager.acquire_account("gemini-3.8-flash")["email"] == "second@example.invalid"
    assert len(calls) == 1
    assert manager.in_flight_count("first@example.invalid") == 0


@pytest.mark.parametrize("acquire", [False, True])
def test_foreground_retries_replaced_credential_before_dispatch(pool, monkeypatch, acquire):
    data, _update = pool
    data["accounts"] = data["accounts"][:1]
    manager = accounts.AccountManager()
    original_refresh = manager._refresh_snapshot
    snapshots = []
    tokens = []

    def rotate_then_refresh(snapshot, **kwargs):
        snapshots.append(snapshot)
        if len(snapshots) == 1:
            data["accounts"][0]["refreshToken"] = "fixture-rotated"
        return original_refresh(snapshot, **kwargs)

    def refresh(token):
        tokens.append(token)
        return {"access_token": "fixture-fresh", "expires_in": 3600}

    monkeypatch.setattr(manager, "_refresh_snapshot", rotate_then_refresh)
    monkeypatch.setattr(accounts, "refresh_access_token", refresh)
    select = manager.acquire_account if acquire else manager.select_active_account
    assert select("gemini-3.8-flash")["accessToken"] == "fixture-fresh"
    assert tokens == ["fixture-rotated"]
    assert len(snapshots) == 2
    assert manager.in_flight_count("first@example.invalid") == int(acquire)


def test_repeated_credential_replacement_has_bounded_progress(pool, monkeypatch):
    data, _update = pool
    data["accounts"] = data["accounts"][:1]
    manager = accounts.AccountManager()
    original_refresh = manager._refresh_snapshot
    snapshots = []

    def rotate_then_refresh(snapshot, **kwargs):
        snapshots.append(snapshot)
        data["accounts"][0]["refreshToken"] = "fixture-rotated-" + str(len(snapshots))
        return original_refresh(snapshot, **kwargs)

    monkeypatch.setattr(manager, "_refresh_snapshot", rotate_then_refresh)
    monkeypatch.setattr(accounts, "refresh_access_token", lambda _: pytest.fail("stale credentials dispatched"))
    assert manager.acquire_account("gemini-3.8-flash") is None
    assert len(snapshots) == 2
    assert manager.in_flight_count("first@example.invalid") == 0
