import copy
import json
import threading
import time
from unittest.mock import patch

import pytest

from codex_antigravity_auth.account_binding import AccountBinding, GATEWAY_INSTANCE, parse_account_binding_header
from codex_antigravity_auth.accounts import AccountManager
from codex_antigravity_auth.process_logs import account_ref


def account_data():
    now = time.time() + 3600
    return {
        "accounts": [
            {"email": "primary@example.invalid", "accessToken": "a", "refreshToken": "r1", "expiresAt": now, "projectId": "p"},
            {"email": "secondary@example.invalid", "accessToken": "b", "refreshToken": "r2", "expiresAt": now, "projectId": "p"},
        ],
        "activeIndex": 0,
        "activeIndexByFamily": {"claude": 0, "gemini": 0},
        "accountState": {"schemaVersion": 2, "failures": {}, "cooldowns": {}, "counters": {}},
    }


def binding_for(manager, index=1):
    inventory = manager.binding_inventory("gemini-3.8-flash")
    return AccountBinding.from_mapping({
        "schemaVersion": 1,
        "gatewayInstance": inventory["gatewayInstance"],
        "accountRef": inventory["accounts"][index]["accountRef"],
        "inventorySha256": inventory["inventorySha256"],
    })


def test_binding_inventory_is_opaque_stable_and_process_scoped():
    data = account_data()
    with patch("codex_antigravity_auth.accounts.load_accounts", return_value=data), patch("codex_antigravity_auth.accounts.update_accounts"):
        manager = AccountManager()
        first = manager.binding_inventory("gemini-3.8-flash")
        second = manager.binding_inventory("gemini-3.8-flash")
    encoded = json.dumps(first)
    assert "@example.invalid" not in encoded
    assert first["gatewayInstance"] == GATEWAY_INSTANCE == second["gatewayInstance"]
    assert first["inventorySha256"] == second["inventorySha256"]
    assert first["accounts"][0]["accountRef"].startswith("acct_")


def test_exact_acquisition_preserves_preference_and_prevents_duplicate_lease():
    data = account_data()
    with patch("codex_antigravity_auth.accounts.load_accounts", return_value=data), patch("codex_antigravity_auth.accounts.update_accounts"):
        manager = AccountManager()
        binding = binding_for(manager)
        selected = manager.acquire_bound_account("gemini-3.8-flash", binding)
        assert selected["email"] == "secondary@example.invalid"
        assert data["activeIndex"] == 0
        assert manager.in_flight_count("secondary@example.invalid") == 1
        with pytest.raises(ValueError, match="lease|inventory|eligible"):
            manager.acquire_bound_account("gemini-3.8-flash", binding)


def test_stale_inventory_reorder_or_expired_account_refuses_without_refresh():
    data = account_data()
    with patch("codex_antigravity_auth.accounts.load_accounts", return_value=data), patch("codex_antigravity_auth.accounts.update_accounts"), patch("codex_antigravity_auth.accounts.refresh_access_token") as refresh:
        manager = AccountManager()
        binding = binding_for(manager)
        data["accounts"] = list(reversed(data["accounts"]))
        with pytest.raises(ValueError, match="inventory"):
            manager.acquire_bound_account("gemini-3.8-flash", binding)
        data["accounts"] = list(reversed(data["accounts"]))
        data["accounts"][1]["expiresAt"] = time.time() - 1
        expired = AccountManager()
        expired_binding = binding_for(expired)
        with pytest.raises(ValueError, match="eligible|inventory|token"):
            expired.acquire_bound_account("gemini-3.8-flash", expired_binding)
        refresh.assert_not_called()


def test_restart_or_unsupported_route_refuses_binding():
    data = account_data()
    with patch("codex_antigravity_auth.accounts.load_accounts", return_value=data), patch("codex_antigravity_auth.accounts.update_accounts"):
        manager = AccountManager()
        binding = binding_for(manager)
        with patch("codex_antigravity_auth.accounts.GATEWAY_INSTANCE", "new-instance"):
            with pytest.raises(ValueError, match="instance"):
                manager.acquire_bound_account("gemini-3.8-flash", binding)
        with pytest.raises(ValueError, match="Gemini|family|route"):
            manager.acquire_bound_account("claude-3.5-sonnet", binding)


def test_missing_duplicate_or_stale_reference_refuses():
    data = account_data()
    with patch("codex_antigravity_auth.accounts.load_accounts", return_value=data), patch("codex_antigravity_auth.accounts.update_accounts"):
        manager = AccountManager()
        binding = binding_for(manager)
        missing = AccountBinding.from_mapping({**binding.as_dict(), "accountRef": "acct_" + "c" * 12})
        with pytest.raises(ValueError, match="missing|eligible"):
            manager.acquire_bound_account("gemini-3.8-flash", missing)

        duplicate = account_data()
        duplicate["accounts"][1]["email"] = duplicate["accounts"][0]["email"]
        with patch("codex_antigravity_auth.accounts.load_accounts", return_value=duplicate):
            with pytest.raises(ValueError, match="duplicate"):
                manager.binding_inventory("gemini-3.8-flash")

        reversed_data = account_data()
        reversed_data["accounts"].reverse()
        with patch("codex_antigravity_auth.accounts.load_accounts", return_value=reversed_data):
            with pytest.raises(ValueError, match="inventory"):
                manager.acquire_bound_account("gemini-3.8-flash", binding)


def test_expiring_disabled_cooled_or_busy_account_refuses_without_refresh():
    cases = (
        ("expiring", lambda data, email: data["accounts"][1].update(expiresAt=time.time() + 60)),
        ("disabled", lambda data, email: data["accountState"].setdefault("disabled", {}).update({email: {"reason": "fixture"}})),
        ("cooled", lambda data, email: data["accountState"].setdefault("cooldowns", {}).update({email: {"account": time.time() + 60}})),
        ("busy", None),
    )
    for name, mutate in cases:
        data = account_data()
        email = data["accounts"][1]["email"]
        with patch("codex_antigravity_auth.accounts.load_accounts", return_value=data), patch("codex_antigravity_auth.accounts.update_accounts"), patch("codex_antigravity_auth.accounts.refresh_access_token") as refresh:
            manager = AccountManager()
            if mutate:
                mutate(data, email)
            elif name == "busy":
                manager._in_flight[email] = 1
            inventory = manager.binding_inventory("gemini-3.8-flash")
            row = next(item for item in inventory["accounts"] if item["accountRef"] == account_ref(email))
            if name == "busy":
                assert row["inFlight"] == 1
            else:
                assert row["eligible"] is False
            binding = binding_for(manager)
            before = copy.deepcopy(data)
            with pytest.raises(ValueError, match="eligible|token"):
                manager.acquire_bound_account("gemini-3.8-flash", binding)
            assert data == before
            refresh.assert_not_called()


def test_same_owner_lock_prevents_two_bound_leases():
    data = account_data()
    with patch("codex_antigravity_auth.accounts.load_accounts", return_value=data), patch("codex_antigravity_auth.accounts.update_accounts"):
        manager = AccountManager()
        binding = binding_for(manager)
        ready = threading.Barrier(2)
        results = []

        def acquire():
            ready.wait()
            try:
                selected = manager.acquire_bound_account("gemini-3.8-flash", binding)
                results.append(("ok", selected["email"]))
            except ValueError as exc:
                results.append(("refused", str(exc)))

        threads = [threading.Thread(target=acquire) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert [kind for kind, _ in results].count("ok") == 1
        assert [kind for kind, _ in results].count("refused") == 1
        assert manager.in_flight_count(data["accounts"][1]["email"]) == 1


def test_header_parser_bounds_and_redacts_fields():
    valid = {
        "schemaVersion": 1,
        "gatewayInstance": "a" * 32,
        "accountRef": "acct_" + "b" * 12,
        "inventorySha256": "c" * 64,
    }
    parsed = parse_account_binding_header(json.dumps(valid))
    assert parsed.as_dict() == valid
    with pytest.raises(ValueError):
        parse_account_binding_header(json.dumps({**valid, "email": "primary@example.invalid"}))
    with pytest.raises(ValueError):
        parse_account_binding_header("x" * 2049)
