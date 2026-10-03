"""Synthetic stores must not be downgraded by an unsupported schema reader."""

import asyncio
import base64
import copy
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from unittest.mock import MagicMock

import pytest
from cryptography.fernet import Fernet

from codex_antigravity_auth import accounts, cli, server, storage
from codex_antigravity_auth.account_state import (
    AccountState,
    UnsupportedAccountStateVersion,
    migrate_account_state,
)
from codex_antigravity_auth.response_protocol import AttemptOutcome


EMAIL = "fixture@example.com"
# Preserve the real startup scheduler before the suite's autouse fixture disables it.
STARTUP_SCHEDULER = server.schedule_refresh_accounts_ahead
BAD_VERSIONS = [999, 3, 1, 0, -1, None, True, False, "2", "fixture-secret", 2.0, 2.5, [], {}]


def account_data(version=2):
    return {
        "accounts": [{
            "email": EMAIL, "accessToken": "fixture-access", "refreshToken": "fixture-refresh",
            "expiresAt": 0, "projectId": "fixture-project",
        }],
        "activeIndex": 0,
        "activeIndexByFamily": {"claude": 0, "gemini": 0},
        "accountState": {"schemaVersion": version, "failures": {}, "cooldowns": {}, "counters": {}},
    }


@pytest.fixture
def store(monkeypatch, tmp_path):
    path = tmp_path / "accounts.json"
    key = Fernet.generate_key()
    monkeypatch.setenv("ANTIGRAVITY_STORAGE_KEY", key.decode())
    monkeypatch.setattr(storage, "ANTIGRAVITY_ACCOUNTS_FILE", str(path))
    monkeypatch.setattr(accounts, "refresh_access_token", MagicMock(side_effect=AssertionError("unexpected upstream call")))
    monkeypatch.setattr("codex_antigravity_auth.oauth.discover_project_id", MagicMock(side_effect=AssertionError("unexpected upstream discovery")))

    def write(data, encrypted=True):
        raw = json.dumps(data).encode()
        path.write_bytes(Fernet(key).encrypt(raw) if encrypted else raw)
        path.chmod(0o600)
        return path.read_bytes(), path.stat().st_mtime_ns

    return path, write


@pytest.mark.parametrize("version", BAD_VERSIONS)
def test_invalid_versions_reject_without_mutating_input(version):
    for operation in (storage.normalize_accounts_data, AccountState, lambda data: migrate_account_state(data, now=100)):
        data = account_data(version)
        data["accounts"].append("would-be-filtered")
        original = copy.deepcopy(data)
        with pytest.raises(UnsupportedAccountStateVersion, match="compatible gateway version") as exc:
            operation(data)
        assert data == original
        assert "fixture-secret" not in str(exc.value)


@pytest.mark.parametrize("encrypted", [False, True])
@pytest.mark.parametrize("version", BAD_VERSIONS)
def test_all_store_entry_points_refuse_unsupported_bytes(store, version, encrypted):
    path, write = store
    original = write(account_data(version), encrypted)
    mutator = MagicMock(return_value=True)
    manager = accounts.AccountManager()
    actions = [
        storage.load_accounts,
        storage.load_accounts_read_only,
        manager.get_accounts,
        lambda: manager.select_active_account("gemini-3.8-flash"),
        lambda: manager.acquire_account("claude-sonnet-4-6"),
        lambda: manager.record_attempt(EMAIL, "gemini-3.8-flash", AttemptOutcome(scope="none", category="success")),
        lambda: cli.reset_google_account_state(EMAIL),
        lambda: cli.remove_google_account(EMAIL),
        lambda: storage.save_accounts(account_data()),
        lambda: storage.update_accounts(mutator),
        manager.refresh_expiring_accounts,
    ]
    for action in actions:
        with pytest.raises((RuntimeError, UnsupportedAccountStateVersion), match="Unsupported accountState.schemaVersion"):
            action()
        assert (path.read_bytes(), path.stat().st_mtime_ns) == original
    mutator.assert_not_called()
    accounts.refresh_access_token.assert_not_called()
    assert manager.in_flight_count(EMAIL) == 0


@pytest.mark.parametrize("encrypted", [False, True])
def test_supported_unversioned_state_migrates_deterministically(store, encrypted):
    path, write = store
    data = account_data()
    data["accountState"] = {"failures": {EMAIL: 2}, "cooldowns": {EMAIL: 9_999_999_999_000}}
    write(data, encrypted)
    loaded = storage.load_accounts()
    assert loaded["accountState"]["schemaVersion"] == 2
    assert loaded["accountState"]["failures"][EMAIL] == {"account": 2}
    assert loaded["accountState"]["cooldowns"][EMAIL] == {"account": 9_999_999_999}
    storage.save_accounts(loaded)
    assert storage.load_accounts() == loaded
    before = path.read_bytes()
    storage.load_accounts()
    assert path.read_bytes() == before


def test_current_state_preserves_additive_fields_on_migration_and_writes(store):
    path, write = store
    data = account_data()
    data["extension"] = {"opaque": [1, 2]}
    data["accountState"]["extension"] = {"opaque": [3, 4]}
    migrated, changed = migrate_account_state(data, now=100)
    assert not changed
    assert migrated == data
    migrated["accountState"]["extension"]["opaque"].append(5)
    assert data["accountState"]["extension"]["opaque"] == [3, 4]
    write(data)
    storage.update_accounts(lambda current: current.update(activeIndex=0))
    assert storage.load_accounts() == data


@pytest.mark.parametrize("version", BAD_VERSIONS)
def test_mutator_cannot_introduce_an_unsupported_version(store, version):
    path, write = store
    original = write(account_data())

    def change_version(data):
        data["accountState"]["schemaVersion"] = version
        return True

    with pytest.raises(RuntimeError, match="Unsupported accountState.schemaVersion"):
        storage.update_accounts(change_version)
    assert (path.read_bytes(), path.stat().st_mtime_ns) == original


@pytest.mark.parametrize("encrypted", [False, True])
def test_stale_snapshot_cannot_overwrite_a_newer_process_version(store, encrypted):
    path, write = store
    write(account_data(), encrypted)
    stale = storage.load_accounts()
    newer = account_data(999)
    newer["accountState"]["futurePolicy"] = {"preserve": True}
    original = write(newer, encrypted)
    stale["accounts"][0]["accessToken"] = "stale-fixture-token"
    with pytest.raises(RuntimeError, match="Unsupported accountState.schemaVersion"):
        storage.save_accounts(stale)
    assert (path.read_bytes(), path.stat().st_mtime_ns) == original


@pytest.mark.parametrize("encrypted", [False, True])
def test_waiting_writer_revalidates_after_another_process_upgrades(store, encrypted):
    path, write = store
    write(account_data(), encrypted)
    stale = storage.load_accounts()
    replacement = json.dumps(account_data(999)).encode()
    if encrypted:
        replacement = Fernet(os.environ["ANTIGRAVITY_STORAGE_KEY"].encode()).encrypt(replacement)
    child_code = """
import base64
import sys
from pathlib import Path
from codex_antigravity_auth.secure_store import file_lock
path = Path(sys.argv[1])
with file_lock(path):
    print('locked', flush=True)
    if sys.stdin.readline().strip() != 'upgrade':
        raise SystemExit(1)
    path.write_bytes(base64.b64decode(sys.argv[2]))
"""
    child = subprocess.Popen(
        [sys.executable, "-u", "-c", child_code, str(path), base64.b64encode(replacement).decode()],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        assert child.stdout.readline().strip() == "locked"
        with ThreadPoolExecutor(max_workers=1) as workers:
            waiting = workers.submit(storage.save_accounts, stale)
            try:
                with pytest.raises(FutureTimeout):
                    waiting.result(timeout=0.05)
            finally:
                _stdout, stderr = child.communicate("upgrade\n", timeout=5)
            assert child.returncode == 0, stderr
            with pytest.raises(RuntimeError, match="Unsupported accountState.schemaVersion"):
                waiting.result(timeout=2)
        assert path.read_bytes() == replacement
    finally:
        if child.poll() is None:
            child.kill()
            child.communicate(timeout=5)


@pytest.mark.parametrize("refresh_fails", [False, True])
@pytest.mark.parametrize("encrypted", [False, True])
def test_background_refresh_rechecks_version_before_success_or_failure_merge(monkeypatch, store, refresh_fails, encrypted):
    path, write = store
    write(account_data(), encrypted)
    upgraded = []

    def refresh(_token):
        upgraded.append(write(account_data(999), encrypted))
        if refresh_fails:
            raise TimeoutError("synthetic timeout")
        return {"access_token": "fresh-fixture-access", "expires_in": 3600}

    monkeypatch.setattr(accounts, "refresh_access_token", refresh)
    summary = accounts.AccountManager().refresh_expiring_accounts()
    assert summary == {"checked": 1, "refreshed": 0, "failed": 1}
    assert (path.read_bytes(), path.stat().st_mtime_ns) == upgraded[0]


@pytest.mark.parametrize("encrypted", [False, True])
def test_startup_refresh_preserves_unsupported_store(monkeypatch, store, encrypted):
    path, write = store
    original = write(account_data(999), encrypted)
    monkeypatch.setattr(server, "account_manager", accounts.AccountManager())
    monkeypatch.setattr(server, "schedule_refresh_accounts_ahead", STARTUP_SCHEDULER)
    monkeypatch.setattr(server, "_refresh_ahead_owner", None)

    async def startup():
        async with server.gateway_lifespan(server.app):
            owner = server._refresh_ahead_owner
            assert owner is not None and owner.worker is not None
            await owner.worker

    asyncio.run(startup())
    assert (path.read_bytes(), path.stat().st_mtime_ns) == original
    accounts.refresh_access_token.assert_not_called()


@pytest.mark.parametrize("version", BAD_VERSIONS)
@pytest.mark.parametrize("encrypted", [False, True])
def test_read_only_diagnostics_report_blocked_version_without_contents(store, version, encrypted):
    path, write = store
    original = write(account_data(version), encrypted)
    inventory = sorted(item.name for item in path.parent.iterdir())
    report = storage.account_store_diagnostics()
    assert not report["accessible"]
    assert report["migration"] == "blocked"
    assert report["error_class"] == "unsupported_account_state_version"
    assert "compatible gateway version" in report["error"]
    assert EMAIL not in json.dumps(report)
    assert "fixture-access" not in json.dumps(report)
    assert "fixture-refresh" not in json.dumps(report)
    assert "fixture-secret" not in json.dumps(report)
    assert (path.read_bytes(), path.stat().st_mtime_ns) == original
    assert sorted(item.name for item in path.parent.iterdir()) == inventory


def test_readiness_prints_actionable_unsupported_store_detail(monkeypatch, capsys, store):
    path, write = store
    original = write(account_data(999))
    monkeypatch.setenv("CODEX_ANTIGRAVITY_NO_UPDATE_CHECK", "1")
    monkeypatch.setattr(cli, "gateway_model_ids", lambda *args, **kwargs: set())
    monkeypatch.setattr(cli, "service_status", lambda **kwargs: {"installed": False, "active": False})
    monkeypatch.setattr(cli, "gateway_status_info", lambda **kwargs: {"running": False, "status": "stopped"})
    monkeypatch.setattr(cli, "add_gateway_reachability", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli, "vision_sidecar_readiness", lambda: {"ok": False, "checks": []})
    monkeypatch.setattr(sys, "argv", ["codex-antigravity", "doctor", "--codex-ready"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 1
    output = capsys.readouterr().out
    assert "[FAIL] account_store:" in output
    assert "Unsupported accountState.schemaVersion: 999" in output
    assert "compatible gateway version" in output
    assert EMAIL not in output
    assert (path.read_bytes(), path.stat().st_mtime_ns) == original
