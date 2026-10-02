"""Refresh failures use synthetic OAuth responses and encrypted temporary stores."""

import copy
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import MagicMock
from urllib.error import URLError

import pytest

from codex_antigravity_auth import accounts, oauth, storage


@pytest.fixture(autouse=True)
def synthetic_client(monkeypatch):
    monkeypatch.setattr(oauth, "require_credentials", lambda: ("synthetic-client", "synthetic-secret"))


@pytest.mark.parametrize("status,payload,kind", [
    (0, {"error": "network_error"}, "transport"),
    (408, {}, "transport"),
    (500, {"error": "invalid_grant"}, "transport"),
    (503, {}, "transport"),
    (429, {"error": "invalid_grant"}, "throttle"),
    (400, {"error": "invalid_grant"}, "credential_rejected"),
    (400, {"error": "invalid_grant", "error_subtype": "invalid_rapt"}, "reauth_required"),
    (400, {"error": "invalid_grant", "error_subtype": "new_policy"}, "reauth_required"),
    (400, {"error": "invalid_grant", "error_subtype": False}, "malformed_response"),
    (400, {"error": "invalid_grant", "error_subtype": None}, "malformed_response"),
    (401, {"error": "invalid_client"}, "client_configuration"),
    (400, {"error": "invalid_request"}, "client_configuration"),
    (403, {"error": "admin_policy_enforced"}, "reauth_required"),
    (400, {"error": {}}, "malformed_response"),
    (200, {}, "malformed_response"),
    (200, {"access_token": ""}, "malformed_response"),
    (200, {"access_token": "bad\nheader"}, "malformed_response"),
    (200, {"access_token": "synthetic", "error": "invalid_grant"}, "malformed_response"),
    (200, {"access_token": "synthetic", "expires_in": float("inf")}, "malformed_response"),
    (200, {"access_token": "synthetic", "expires_in": False}, "malformed_response"),
    (200, {"access_token": "synthetic", "refresh_token": {}}, "malformed_response"),
    (200, {"access_token": "synthetic", "refresh_token": "bad\nheader"}, "malformed_response"),
])
def test_refresh_preserves_typed_outcome_without_echoing_credentials(monkeypatch, status, payload, kind):
    payload = {**payload, "error_description": "synthetic-secret synthetic-refresh"}
    monkeypatch.setattr(oauth, "post_form", lambda *args, **kwargs: (status, payload))
    with pytest.raises(oauth.OAuthRefreshError) as exc:
        oauth.refresh_access_token("synthetic-refresh")
    assert exc.value.kind == kind
    assert "synthetic-secret" not in str(exc.value)
    assert "synthetic-refresh" not in str(exc.value)


@pytest.mark.parametrize("error", [TimeoutError("timeout"), ConnectionResetError("reset"), URLError(socket.gaierror("DNS fixture"))])
def test_real_post_form_network_errors_become_transport(monkeypatch, error):
    monkeypatch.setattr("urllib.request.urlopen", MagicMock(side_effect=error))
    with pytest.raises(oauth.OAuthRefreshError) as exc:
        oauth.refresh_access_token("synthetic-refresh")
    assert exc.value.kind == "transport"


def test_refresh_cancellation_is_not_converted_to_auth_failure(monkeypatch):
    monkeypatch.setattr(oauth, "post_form", MagicMock(side_effect=KeyboardInterrupt))
    with pytest.raises(KeyboardInterrupt):
        oauth.refresh_access_token("synthetic-refresh")


@pytest.fixture
def account_store(monkeypatch, tmp_path):
    clock = [time.time()]
    monkeypatch.setattr(accounts, "time", SimpleNamespace(time=lambda: clock[0]))
    monkeypatch.setattr(accounts, "_refresh_locks", {})
    monkeypatch.setattr(storage, "ANTIGRAVITY_ACCOUNTS_FILE", str(tmp_path / "accounts.json"))
    data = {
        "accounts": [{"email": "fixture@example.invalid", "accessToken": "synthetic-old", "refreshToken": "synthetic-refresh", "expiresAt": 0, "projectId": "synthetic-project"}],
        "accountState": {"schemaVersion": 2, "failures": {}, "cooldowns": {}, "counters": {}},
    }
    storage.save_accounts(data)
    return clock, "fixture@example.invalid"


@pytest.mark.parametrize("mode", ["select", "background"])
@pytest.mark.parametrize("failure", [
    (0, {"error": "network_error"}),
    (500, {"error": "server_error"}),
    (429, {"error": "throttled"}),
    (200, {"access_token": None}),
    (400, {"error": "invalid_grant", "error_subtype": "invalid_rapt"}),
    (401, {"error": "invalid_client"}),
])
def test_transient_failures_never_disable_and_recovery_needs_no_login(monkeypatch, account_store, mode, failure):
    clock, email = account_store
    response = [failure]
    post = MagicMock(side_effect=lambda *args, **kwargs: response[0])
    monkeypatch.setattr(oauth, "post_form", post)
    manager = accounts.AccountManager()
    perform = manager.refresh_expiring_accounts if mode == "background" else lambda: manager.select_active_account("gemini-3.8-flash")
    for _ in range(6):
        perform()
        state = storage.load_accounts_read_only()["accountState"]
        assert not state.get("authStrikes")
        assert not state.get("disabled")
        deadline = state["cooldowns"][email]["account"]
        assert clock[0] < deadline <= clock[0] + 1920
        count = post.call_count
        perform()
        assert post.call_count == count  # Persisted cooldown is honored.
        clock[0] = deadline + 1
    response[0] = (200, {"access_token": "synthetic-fresh", "expires_in": 3600})
    perform()
    selected = manager.select_active_account("gemini-3.8-flash")
    assert selected["accessToken"] == "synthetic-fresh"
    assert not storage.load_accounts_read_only()["accountState"].get("disabled")


@pytest.mark.parametrize("mode", ["select", "background"])
def test_verified_token_rejection_still_disables_at_threshold(monkeypatch, account_store, mode):
    clock, email = account_store
    post = MagicMock(return_value=(400, {"error": "invalid_grant"}))
    monkeypatch.setattr(oauth, "post_form", post)
    manager = accounts.AccountManager()
    perform = manager.refresh_expiring_accounts if mode == "background" else lambda: manager.select_active_account("gemini-3.8-flash")
    for attempt in range(3):
        perform()
        state = storage.load_accounts_read_only()["accountState"]
        if attempt < 2:
            assert state["authStrikes"][email] == attempt + 1
        clock[0] = state["cooldowns"][email]["account"] + 1
    assert email in state["disabled"]
    post.return_value = (200, {"access_token": "synthetic-fresh", "expires_in": 3600})
    perform()
    assert post.call_count == 3
    assert email in storage.load_accounts_read_only()["accountState"]["disabled"]


@pytest.mark.parametrize("credential_failure", [False, True])
def test_concurrent_background_refreshes_do_not_multiply_one_failure(monkeypatch, account_store, credential_failure):
    _, email = account_store
    entered, release = threading.Event(), threading.Event()
    calls = []

    def fail(*args, **kwargs):
        calls.append(1)
        entered.set()
        assert release.wait(2)
        return (400, {"error": "invalid_grant"}) if credential_failure else (503, {})

    monkeypatch.setattr(oauth, "post_form", fail)
    managers = [accounts.AccountManager(), accounts.AccountManager()]
    with ThreadPoolExecutor(max_workers=2) as workers:
        first = workers.submit(managers[0].refresh_expiring_accounts)
        assert entered.wait(1)
        second = workers.submit(managers[1].refresh_expiring_accounts)
        release.set()
        results = [first.result(timeout=3), second.result(timeout=3)]
    assert len(calls) == 1
    assert sum(result["failed"] for result in results) == 1
    state = storage.load_accounts_read_only()["accountState"]
    assert state.get("authStrikes", {}).get(email, 0) == int(credential_failure)


def test_preexisting_disabled_state_is_preserved_without_refresh(monkeypatch, account_store):
    _, email = account_store
    def disable(data):
        data["accountState"]["disabled"] = {email: {"reason": "operator-disabled", "errorClass": "auth_failure", "since": "fixture"}}
    storage.update_accounts(disable)
    original = copy.deepcopy(storage.load_accounts_read_only()["accountState"]["disabled"])
    post = MagicMock(side_effect=AssertionError("disabled account must not refresh"))
    monkeypatch.setattr(oauth, "post_form", post)
    manager = accounts.AccountManager()
    manager.refresh_expiring_accounts()
    assert manager.select_active_account("gemini-3.8-flash") is None
    assert storage.load_accounts_read_only()["accountState"]["disabled"] == original
    post.assert_not_called()
