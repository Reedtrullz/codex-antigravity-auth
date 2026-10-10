"""Operational JSON and support evidence tested only with synthetic local state."""
from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from codex_antigravity_auth import cli, cli_json, observability, support_bundle

EMAIL = "private-fixture@example.invalid"
SECRET = "sk-fixture-secret-12345678901234567890"


@pytest.fixture
def state(monkeypatch, tmp_path):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "client"))
    monkeypatch.setenv("ANTIGRAVITY_STATE_HOME", str(tmp_path / "state"))
    return tmp_path


def invoke(monkeypatch, capsys, argv):
    monkeypatch.setattr(sys, "argv", ["codex-antigravity", *argv])
    with pytest.raises(SystemExit) as caught:
        cli.main()
    captured = capsys.readouterr()
    parsed = json.loads(captured.out)
    assert set(parsed) == {"schemaVersion", "command", "status", "ok", "exitCode", "warnings", "errors", "data"}
    schema = json.loads((Path(cli.__file__).parent / "schemas/cli-result-v1.json").read_text())
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(parsed)
    if parsed.get("data") and "bundle" in parsed["data"]:
        bundle_schema = json.loads((Path(cli.__file__).parent / "schemas/support-bundle-v1.json").read_text())
        Draft202012Validator(bundle_schema, format_checker=FormatChecker()).validate(parsed["data"]["bundle"])
    assert parsed["schemaVersion"] == 1
    assert parsed["exitCode"] == caught.value.code
    return parsed, captured


@pytest.mark.parametrize("argv,handler", [
    (["status", "--json"], "run_gateway_status"),
])
@pytest.mark.parametrize("condition,expected,exit_code", [
    ("ready", "ready", 0), ("warn", "degraded", 0), ("fail", "failed", 1),
])
def test_shared_envelope_missing_degraded_ready_and_single_stdout(monkeypatch, capsys, state, argv, handler, condition, expected, exit_code):
    data = {"ok": condition != "fail", "checks": [], "reachable": True, "gateway": {"reachable": True}}
    if condition != "ready":
        data["checks"] = [{"name": "fixture", "status": condition, "detail": "fixture detail"}]
    def collect(*args, **kwargs):
        print("discarded fixture progress")
        print("discarded fixture stderr", file=sys.stderr)
        return deepcopy(data)
    monkeypatch.setattr(cli, handler, collect)
    result, captured = invoke(monkeypatch, capsys, argv)
    assert result["status"] == expected and result["exitCode"] == exit_code
    assert "discarded" not in captured.out + captured.err
    assert "Collecting" in captured.err
    assert result["data"] == data


@pytest.mark.parametrize("failure", [RuntimeError(SECRET), ValueError(SECRET), SystemExit(SECRET), OSError(EMAIL)])
def test_runtime_failures_are_json_without_exception_values(monkeypatch, capsys, state, failure):
    def collect(*args, **kwargs):
        raise failure
    monkeypatch.setattr(cli, "run_gateway_status", collect)
    result, captured = invoke(monkeypatch, capsys, ["status", "--json"])
    assert result["status"] == "failed" and result["exitCode"] == 1
    assert SECRET not in captured.out + captured.err and EMAIL not in captured.out + captured.err


@pytest.mark.parametrize("argv", [
    ["status", "--json", "--unknown-argument"],
    ["support-bundle", "--write"],
    ["support-bundle", "--since", "nonsense"],
])
def test_invalid_invocations_have_versioned_exit_two(monkeypatch, capsys, state, argv):
    result, _ = invoke(monkeypatch, capsys, argv)
    assert result["exitCode"] == 2 and result["errors"] == ["invalid_arguments"]


def test_collection_interrupt_is_json_exit_130(monkeypatch, capsys, state):
    def collect(*args):
        raise KeyboardInterrupt
    monkeypatch.setattr(cli, "run_gateway_status", collect)
    result, _ = invoke(monkeypatch, capsys, ["status", "--json"])
    assert result["exitCode"] == 130 and result["errors"] == ["cancelled"]


def test_account_and_provider_json_only_project_credential_presence(monkeypatch, capsys, state):
    monkeypatch.setattr(cli, "load_accounts_read_only", lambda: {"accounts": [{"email": EMAIL, "refreshToken": SECRET}]})
    result, captured = invoke(monkeypatch, capsys, ["accounts", "list", "--json"])
    assert result["data"]["accounts"][0]["refreshPresent"] is True
    monkeypatch.setattr(cli, "load_provider_config_read_only", lambda: {"providers": {EMAIL: {
        "displayName": EMAIL, "apiKey": SECRET, "models": [EMAIL], "kind": "openai_chat"}}})
    provider, second = invoke(monkeypatch, capsys, ["provider", "list", "--json"])
    assert provider["data"]["providers"][0]["storedKeyPresent"] is True
    assert SECRET not in captured.out + second.out and EMAIL not in captured.out + second.out


def test_model_json_has_same_catalog_in_versioned_data(monkeypatch, capsys, state):
    monkeypatch.setattr(cli, "native_model_catalog", lambda **kw: [{"id": "fixture-model"}])
    monkeypatch.setattr(cli, "load_model_overlays", lambda **kw: [])
    monkeypatch.setattr(sys, "argv", ["codex-antigravity", "models", "list", "--json"])
    cli.main()
    assert json.loads(capsys.readouterr().out) == {"models": [{"id": "fixture-model"}], "overlays": []}


def test_support_preview_is_offline_no_auth_reads_and_no_namespace_creation(monkeypatch, capsys, state):
    def forbidden(*args, **kwargs):
        raise AssertionError("forbidden collection")
    monkeypatch.setattr(cli, "resolve_oauth_credentials", forbidden)
    monkeypatch.setattr(cli, "load_accounts_read_only", forbidden)
    monkeypatch.setattr(cli, "gateway_model_ids", forbidden)
    monkeypatch.setattr(cli, "service_status", forbidden)
    result, _ = invoke(monkeypatch, capsys, ["support-bundle"])
    assert result["data"]["mode"] == "dry_run"
    bundle = result["data"]["bundle"]
    assert bundle["config"]["state"] == "missing"
    assert bundle["stores"]["accounts"] == {"state": "missing", "decryption": "not_attempted"}
    assert bundle["history"]["requestedWindowIncomplete"] is None
    assert not (state / "client").exists() and not (state / "state").exists()


def populate(state):
    client, root = state / "client", state / "state"
    client.mkdir(); root.mkdir()
    (client / "config.toml").write_text(f'''model = "{EMAIL}"
model_provider = "private-provider"
[model_providers.private-provider]
name = "{EMAIL}"
base_url = "https://{EMAIL}/private/{SECRET}"
wire_api = "responses"
api_key = "{SECRET}"
''')
    for filename in ("antigravity-accounts.json", "antigravity-providers.json"):
        (root / filename).write_text(SECRET + EMAIL)
    now = datetime.now(timezone.utc).isoformat()
    row = {"request_id": EMAIL, "timestamp": now, "lifecycle_phase": "terminal", "status": "completed", "route": "openai",
           "model": SECRET, "provider": EMAIL, "family": EMAIL, "error": SECRET, "account_email": EMAIL,
           "prompt": SECRET, "raw": EMAIL, "http_status": 200, "latency_ms": 4}
    (root / observability.REQUEST_LOG_FILE).write_text(json.dumps(row) + "\n{bad-json}\n")
    return client, root


def test_bundle_allowlist_and_selection_preserve_gaps_without_private_values(monkeypatch, capsys, state):
    client, root = populate(state)
    before = {p: p.read_bytes() for p in [client / "config.toml", root / "antigravity-accounts.json", root / "antigravity-providers.json"]}
    result, captured = invoke(monkeypatch, capsys, ["support-bundle", "--request-id", EMAIL])
    bundle = result["data"]["bundle"]
    assert bundle["config"]["providerTableCount"] == 1
    assert bundle["stores"]["accounts"]["state"] == "readable"
    history = bundle["history"]
    assert history["requestedWindowIncomplete"] is True and history["malformedRecords"] == 1
    assert history["matchedRequestCount"] == 1 and history["records"][0]["selectedInputIndex"] == 0
    assert len(history["records"][0]["requestRef"]) == 24
    assert SECRET not in captured.out + captured.err and EMAIL not in captured.out + captured.err
    assert str(state) not in captured.out
    assert all(path.read_bytes() == value for path, value in before.items())
    second = support_bundle.collect_bundle(request_ids=[EMAIL])
    assert second["history"]["records"][0]["requestRef"] != history["records"][0]["requestRef"]


def test_explicit_export_creates_private_json_and_never_overwrites(monkeypatch, capsys, state):
    output = state / "support.json"
    preview, _ = invoke(monkeypatch, capsys, ["support-bundle", "--output", str(output)])
    assert not output.exists() and not preview["data"]["written"]
    result, _ = invoke(monkeypatch, capsys, ["support-bundle", "--output", str(output), "--write"])
    assert result["data"]["written"] and json.loads(output.read_text())["kind"] == "support_bundle"
    original = output.read_bytes()
    if os.name != "nt":
        assert output.stat().st_mode & 0o777 == 0o600
    failed, _ = invoke(monkeypatch, capsys, ["support-bundle", "--output", str(output), "--write"])
    assert failed["exitCode"] == 1 and output.read_bytes() == original


def test_export_symlink_does_not_touch_target(monkeypatch, capsys, state):
    target = state / "existing"
    target.write_text("fixture original")
    link = state / "link"
    link.symlink_to(target)
    failed, _ = invoke(monkeypatch, capsys, ["support-bundle", "--output", str(link), "--write"])
    assert failed["exitCode"] == 1 and target.read_text() == "fixture original"


@pytest.mark.parametrize("limit", ["bytes", "records"])
def test_bounded_history_omissions_stay_visible(monkeypatch, state, limit):
    root = state / "state"; root.mkdir()
    path = root / observability.REQUEST_LOG_FILE
    path.write_text((json.dumps({"request_id": "fixture", "timestamp": "2026-01-01T00:00:00Z", "status": "completed"}) + "\n") * 10)
    kwargs = {"max_bytes": 20} if limit == "bytes" else {"max_records": 2}
    rows = list(observability.iter_request_records(tail=1, **kwargs))
    assert any(row.get("status") == "log_gap" for row in rows)
    summary = observability.request_log_summary(since="24h", records=rows)
    assert summary["requested_window_incomplete"] is True and summary["omitted_records"] == 1


def test_support_config_oversize_and_symlinks_are_not_read(monkeypatch, state):
    path = state / "oversize.toml"; path.write_bytes(b"x" * 100)
    monkeypatch.setattr(support_bundle, "MAX_CONFIG_BYTES", 16)
    assert support_bundle.config_structure(path)["state"] == "too_large"
    link = state / "config-link"; link.symlink_to(path)
    assert support_bundle.config_structure(link)["state"] == "unavailable"


def test_safe_numeric_projection_never_overflows():
    for value in (10**400, float("inf"), float("nan"), True, SECRET):
        assert support_bundle.number(value) is None


def test_invalid_extreme_window_is_usage_error(monkeypatch, capsys, state):
    result, _ = invoke(monkeypatch, capsys, ["support-bundle", "--since", "9" * 500 + "d"])
    assert result["exitCode"] == 2


def test_bounded_reader_handles_deeply_nested_malformed_json(monkeypatch, state):
    root = state / "state"; root.mkdir()
    (root / observability.REQUEST_LOG_FILE).write_text('{"x":' + '[' * 2000 + '0' + ']' * 2000 + '}\n')
    records = list(observability.iter_request_records(max_bytes=10000, max_records=5))
    assert records[0]["status"] == "malformed"


def test_schema_documents_have_no_unrecognized_properties():
    directory = Path(cli.__file__).parent / "schemas"
    for name in ("cli-result-v1.json", "support-bundle-v1.json"):
        schema = json.loads((directory / name).read_text())
        Draft202012Validator.check_schema(schema)
        assert schema["additionalProperties"] is False


def test_support_numbers_and_unknown_labels_do_not_escape_allowlist(monkeypatch, capsys, state):
    populate(state)
    path = state / "state" / observability.REQUEST_LOG_FILE
    row = json.loads(path.read_text().splitlines()[0])
    row.update(route=[EMAIL], lifecycle_phase={"private": EMAIL}, status=SECRET, http_status=10**400)
    path.write_text(json.dumps(row) + "\n")
    result, captured = invoke(monkeypatch, capsys, ["support-bundle", "--request-id", EMAIL])
    assert EMAIL not in captured.out and SECRET not in captured.out
    # Malformed source data may prevent collection, but never produce non-JSON output.
    assert result["status"] in {"degraded", "failed"}


@pytest.mark.parametrize("phase", ["started", "attempt", "terminal"])
def test_bundle_preserves_gateway_lifecycle_phase(monkeypatch, capsys, state, phase):
    root = state / "state"; root.mkdir()
    observability.write_request_record({"request_id": "fixture-request", "lifecycle_phase": phase,
                                       "status": "completed", "route": "openai"})
    result, _ = invoke(monkeypatch, capsys, ["support-bundle", "--request-id", "fixture-request"])
    assert result["data"]["bundle"]["history"]["records"][0]["phase"] == phase


def test_oversized_archive_does_not_hide_selected_recent_request(monkeypatch, capsys, state):
    root = state / "state"; root.mkdir()
    path = root / observability.REQUEST_LOG_FILE
    path.with_name(path.name + ".1").write_bytes(b"x" * (support_bundle.MAX_HISTORY_BYTES + 100))
    observability.write_request_record({"request_id": "recent-fixture", "lifecycle_phase": "terminal",
                                       "status": "completed", "route": "openai"})
    result, _ = invoke(monkeypatch, capsys, ["support-bundle", "--request-id", "recent-fixture"])
    history = result["data"]["bundle"]["history"]
    assert history["matchedRequestCount"] == 1 and history["requestedWindowIncomplete"] is True
    assert history["records"][0]["phase"] == "terminal"


def test_tail_budget_preserves_chronological_terminal_selection(monkeypatch, state):
    root = state / "state"; root.mkdir()
    path = root / observability.REQUEST_LOG_FILE
    rows = [{"request_id": "same", "lifecycle_phase": "terminal", "status": status,
             "timestamp": datetime.now(timezone.utc).isoformat()} for status in ["failed", "incomplete", "completed"]]
    path.with_name(path.name + ".1").write_text(json.dumps(rows[0]) + "\n")
    path.write_text("\n".join(json.dumps(row) for row in rows[1:]) + "\n")
    limited = list(observability.iter_request_records(max_bytes=10000, max_records=2))
    assert [row["status"] for row in limited] == ["log_gap", "incomplete", "completed"]
    summary = observability.request_log_summary(since="all", records=limited)
    assert next(iter(summary["groups"].values()))["success_count"] == 1


def test_byte_tail_discards_clipped_leading_utf8_record(monkeypatch, state):
    root = state / "state"; root.mkdir()
    path = root / observability.REQUEST_LOG_FILE
    first = json.dumps({"request_id": "old", "text": "é" * 500}, ensure_ascii=False).encode() + b"\n"
    last = json.dumps({"request_id": "last", "status": "completed"}).encode() + b"\n"
    path.write_bytes(first + last)
    rows = list(observability.iter_request_records(max_bytes=len(last) + 3, max_records=10))
    assert [row.get("request_id") for row in rows] == [None, "last"]
    assert rows[0]["status"] == "log_gap"
