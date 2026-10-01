"""Account explanations use only synthetic state and never exercise credentials."""

import copy
import json
import os
import sys
from unittest.mock import MagicMock

import pytest
from cryptography.fernet import Fernet

from codex_antigravity_auth import account_diagnostics as diagnostics
from codex_antigravity_auth import accounts, byok, cli, constants, storage
from codex_antigravity_auth.account_state import AccountState


NOW = 1_800_000_000


def fixture_data():
    result = {
        "accounts": [{
            "email": f"fixture-{index}@example.invalid", "accessToken": "synthetic-access",
            "refreshToken": "synthetic-refresh", "expiresAt": NOW + 3600,
            "projectId": "synthetic-private-project", "fingerprint": {"deviceId": "synthetic-device"},
        } for index in range(5)],
        "activeIndexByFamily": {"claude": 2, "gemini": 0},
        "accountState": {
            "schemaVersion": 2, "failures": {}, "counters": {},
            "cooldowns": {"fixture-2@example.invalid": {"claude": NOW + 45}},
            "disabled": {"fixture-3@example.invalid": {"reason": "synthetic-private-reason", "errorClass": "synthetic-private-error"}},
            "authStrikes": {"fixture-4@example.invalid": 2},
        },
    }
    result["accounts"][1]["expiresAt"] = NOW - 1
    result["accounts"][4].pop("refreshToken")
    result["accounts"][4]["expiresAt"] = NOW - 1
    return result


def tree_snapshot(root):
    return {
        str(path.relative_to(root)): (path.stat().st_mode, path.stat().st_mtime_ns,
                                       path.read_bytes() if path.is_file() else None)
        for path in [root, *sorted(root.rglob("*"))]
    }


@pytest.fixture
def isolated_store(monkeypatch, tmp_path):
    path = tmp_path / ".codex" / "antigravity-accounts.json"
    monkeypatch.setattr(storage, "ANTIGRAVITY_ACCOUNTS_FILE", str(path))
    monkeypatch.setattr(constants, "CREDENTIALS_FILE", str(path.with_name("antigravity-credentials.json")))
    monkeypatch.setattr(diagnostics.time, "time", lambda: NOW)
    for module, name in ((storage, "save_accounts"), (storage, "update_accounts"),
                         (accounts, "refresh_access_token"), (accounts, "_apply_token_refresh"),
                         (constants, "resolve_oauth_credentials"), (AccountState, "acquire"), (AccountState, "select")):
        monkeypatch.setattr(module, name, MagicMock(side_effect=AssertionError(f"must not call {name}")))

    def write(data, *, encrypted=True):
        path.parent.mkdir(parents=True, exist_ok=True)
        raw = json.dumps(data).encode()
        key = os.environ["ANTIGRAVITY_STORAGE_KEY"].encode()
        path.write_bytes(Fernet(key).encrypt(raw) if encrypted else raw)
        path.chmod(0o644)  # Read-only diagnostics must not repair permissions.
        return path

    return tmp_path, path, write


def test_local_matrix_separates_routing_from_refresh_and_hides_private_content():
    data = fixture_data()
    original = copy.deepcopy(data)
    for _ in range(2):
        report = diagnostics.explain_account_data(data, "claude-sonnet-4-6", now=NOW)
        rows = report["accounts"]
        assert report["routing_eligible_count"] == 3
        assert report["preferred_account"] == "account-3"
        assert report["lease_visibility"] == "unknown_other_process"
        assert report["candidate_before_refresh"] is None
        assert all(row["lease_count"] is None for row in rows)
        assert rows[0]["token_state"] == "current" and rows[0]["refresh"] == "not_needed"
        assert rows[1]["token_state"] == "expired" and rows[1]["refresh"] == "on_selection"
        assert rows[1]["routing_eligible"] is True
        assert rows[2]["exclusion_reasons"] == ["claude_cooldown"]
        assert rows[2]["cooldown_remaining_seconds"] == {"claude": 45}
        assert rows[3]["exclusion_reasons"] == ["disabled"]
        assert rows[4]["refresh"] == "login_required" and rows[4]["auth_strikes"] == 2
        text = json.dumps(report)
        assert "synthetic-" not in text and "@example" not in text
        assert data == original


@pytest.mark.parametrize("remaining,access,refresh,expected", [
    (3600, True, True, ("current", "not_needed")),
    (300, True, True, ("current", "not_needed")),
    (299, True, True, ("expiring", "on_selection")),
    (11, True, False, ("expiring", "unavailable")),
    (10, True, False, ("expiring_imminently", "login_required")),
    (0, True, True, ("expired", "on_selection")),
    (3600, False, True, ("missing", "on_selection")),
    (3600, False, False, ("missing", "login_required")),
])
def test_token_lifetime_boundaries_match_selection(remaining, access, refresh, expected):
    account = {"email": "fixture@example.invalid", "expiresAt": (NOW + remaining) * 1000,
               "accessToken": "synthetic" if access else "", "refreshToken": "synthetic" if refresh else ""}
    row = diagnostics.explain_account_data({"accounts": [account]}, "gemini", now=NOW)["accounts"][0]
    assert (row["token_state"], row["refresh"]) == expected


def test_known_leases_affect_preference_but_never_exclude_or_mutate():
    data = fixture_data()
    leases = {"fixture-0@example.invalid": 2}
    owner = AccountState(data, now=lambda: NOW, in_flight=leases)
    before_data, before_leases = copy.deepcopy(data), copy.deepcopy(leases)
    for _ in range(2):
        snapshot = owner.selection_snapshot("gemini")
        assert snapshot["candidate_index"] == 1
        assert snapshot["accounts"][0]["exclusion_reasons"] == []
        report = diagnostics.explain_account_data(data, "gemini", now=NOW, in_flight=leases)
        assert report["candidate_before_refresh"] == "account-2"
        assert report["accounts"][0]["lease_count"] == 2
        assert data == before_data and leases == before_leases
    # Actual selection shares the same predicates/order; only this final call mutates.
    assert owner.select("gemini").account["email"] == "fixture-1@example.invalid"


def test_family_scopes_expiry_and_missing_identity():
    data = fixture_data()
    data["accounts"].append({"accessToken": "synthetic"})
    data["accountState"]["cooldowns"]["fixture-0@example.invalid"] = {"account": NOW + 20}
    report = diagnostics.explain_account_data(data, "gemini", now=NOW)
    assert report["accounts"][0]["exclusion_reasons"] == ["account_cooldown"]
    assert report["accounts"][2]["routing_eligible"] is True
    assert report["accounts"][5]["exclusion_reasons"] == ["missing_identity"]
    later = diagnostics.explain_account_data(data, "claude", now=NOW + 100)
    assert later["accounts"][0]["routing_eligible"] is True
    assert later["accounts"][2]["routing_eligible"] is True


def test_historical_failure_categories_are_allowlisted_and_not_cooldown_causes():
    data = fixture_data()
    data["accountState"]["counters"] = {
        "fixture-0@example.invalid": {"claude": {"last_failure_class": "transport"}},
        "fixture-1@example.invalid": {"claude": {"last_failure_class": "synthetic-private-error"}},
    }
    rows = diagnostics.explain_account_data(data, "claude", now=NOW)["accounts"]
    assert rows[0]["last_recorded_failure_category"] == "transport"
    assert rows[0]["exclusion_reasons"] == []
    assert rows[1]["last_recorded_failure_category"] == "unknown"
    assert rows[4]["refresh_available"] is False


@pytest.mark.parametrize("encrypted,legacy", [(True, False), (False, False), (False, True)])
def test_repeated_cli_json_and_human_views_preserve_bytes_and_agree(isolated_store, monkeypatch, capsys, encrypted, legacy):
    root, path, write = isolated_store
    data = fixture_data()
    if legacy:
        data["accountState"].pop("schemaVersion")
        data["accountState"]["cooldowns"] = {"fixture-2@example.invalid": NOW + 45}
    write(data, encrypted=encrypted)
    before = tree_snapshot(root)
    for _ in range(2):
        monkeypatch.setattr(sys, "argv", ["codex-antigravity", "accounts", "explain", "--model", "claude", "--json"])
        cli.main()
        report = json.loads(capsys.readouterr().out)
        assert report["ok"] is True
        assert report["namespace"]["account_store"] == "~/.codex/antigravity-accounts.json"
        monkeypatch.setattr(sys, "argv", ["codex-antigravity", "accounts", "explain", "--model", "claude"])
        cli.main()
        human = capsys.readouterr().out
        assert human == "\n".join(diagnostics.account_eligibility_lines(report)) + "\n"
        assert "synthetic-" not in human and "@example" not in human
        assert tree_snapshot(root) == before


@pytest.mark.parametrize("kind", ["future", "bad_version", "corrupt", "wrong_key", "missing"])
def test_store_failures_and_missing_store_are_read_only(isolated_store, monkeypatch, capsys, kind):
    root, path, write = isolated_store
    data = fixture_data()
    if kind != "missing":
        if kind in {"future", "bad_version"}:
            data["accountState"]["schemaVersion"] = 999 if kind == "future" else "synthetic-secret-version"
        write(data)
        if kind == "corrupt":
            path.write_bytes(b"synthetic-secret-corrupt-store")
        elif kind == "wrong_key":
            monkeypatch.setenv("ANTIGRAVITY_STORAGE_KEY", Fernet.generate_key().decode())
    before = tree_snapshot(root)
    monkeypatch.setattr(sys, "argv", ["codex-antigravity", "accounts", "explain", "--model", "gemini", "--json"])
    if kind == "missing":
        cli.main()
    else:
        with pytest.raises(SystemExit) as exc:
            cli.main()
        assert exc.value.code == 1
    text = capsys.readouterr().out
    report = json.loads(text)
    assert "synthetic-" not in text
    if kind in {"future", "bad_version"}:
        assert report["error_class"] == "unsupported_account_state_version"
    elif kind == "missing":
        assert report["ok"] and report["accounts"] == []
    else:
        assert report["error_class"] == "account_store_unavailable"
    assert tree_snapshot(root) == before


def test_byok_route_does_not_inspect_google_store(isolated_store, monkeypatch):
    read = MagicMock(side_effect=AssertionError("wrong account pool"))
    monkeypatch.setattr(storage, "load_accounts_read_only", read)
    report = diagnostics.account_eligibility_report("openrouter:synthetic")
    assert report["error_class"] == "unsupported_route"
    read.assert_not_called()


@pytest.mark.parametrize("model,expected,ok", [
    ("gpt-5.6", {"classic": "openai-disabled", "unified": "openai"}, False),
    ("custom-fixture/deepseek-chat", {"classic": "byok", "unified": "byok"}, False),
    ("openai:sonnet", {"classic": "antigravity", "unified": "antigravity"}, True),
    ("sonnet", {"classic": "antigravity", "unified": "antigravity"}, True),
    ("unlisted-fixture-backend", {"classic": "antigravity", "unified": "unknown"}, True),
])
def test_route_shapes_share_read_only_classifier(isolated_store, monkeypatch, model, expected, ok):
    root, path, write = isolated_store
    write(fixture_data())
    provider_path = path.with_name("providers.json")
    provider_path.write_text(json.dumps({"providers": {"custom-fixture": {
        "baseUrl": "https://example.invalid/v1", "apiKey": "synthetic-provider-secret", "models": ["deepseek-chat"],
    }}}))
    monkeypatch.setattr(byok, "providers_json_path_read_only", lambda: provider_path)
    monkeypatch.setattr(byok, "load_provider_config", MagicMock(side_effect=AssertionError("must not migrate provider store")))
    before = tree_snapshot(root)
    report = diagnostics.account_eligibility_report(model)
    assert report["ok"] is ok
    assert report["route_by_mode"] == expected
    if model in {"sonnet", "openai:sonnet"}:
        assert report["family"] == "claude"
    assert "synthetic-provider-secret" not in json.dumps(report)
    assert tree_snapshot(root) == before


def test_unreadable_slash_provider_configuration_does_not_claim_google_eligibility(isolated_store, monkeypatch):
    monkeypatch.setattr(byok, "all_provider_configs_read_only", MagicMock(side_effect=RuntimeError("synthetic-secret")))
    report = diagnostics.account_eligibility_report("custom-fixture/model")
    assert report["error_class"] == "route_configuration_unavailable"
    assert "synthetic-secret" not in json.dumps(report)


@pytest.mark.parametrize("inside_home", [True, False])
def test_private_namespace_components_are_masked(isolated_store, monkeypatch, inside_home):
    root, path, write = isolated_store
    private = (root if inside_home else root.parent) / "fixture@example.invalid" / "synthetic-private-profile"
    monkeypatch.setattr(storage, "ANTIGRAVITY_ACCOUNTS_FILE", str(private / "accounts.json"))
    monkeypatch.setattr(constants, "CREDENTIALS_FILE", str(private / "oauth.json"))
    before = tree_snapshot(root)
    report = diagnostics.account_eligibility_report("sonnet")
    text = json.dumps(report) + "\n".join(diagnostics.account_eligibility_lines(report))
    assert "fixture@example.invalid" not in text and "synthetic-private-profile" not in text
    assert report["namespace"]["account_store"].startswith("<configured path ")
    assert diagnostics.account_eligibility_report("sonnet")["namespace"] == report["namespace"]
    assert tree_snapshot(root) == before
