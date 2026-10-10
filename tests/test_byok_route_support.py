"""Configured BYOK routes must be dispatchable before being advertised."""

import json
import sys
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from codex_antigravity_auth import byok, cli, server


@pytest.fixture
def providers(monkeypatch):
    entries = {
        "chat": {"kind": "openai_chat"},
        "legacy-chat": {},
        "native": {"kind": "openai_responses"},
        "unknown": {"kind": "future_transport"},
        "invalid": {"kind": []},
    }
    normalized = byok.normalize_provider_config({"providers": {
        name: {"baseUrl": "https://example.invalid/v1", "apiKey": "fixture-key", "models": ["model"], **entry}
        for name, entry in entries.items()
    }})
    configs = {name: byok.merged_provider_config(name, entry) for name, entry in normalized["providers"].items()}
    monkeypatch.setattr(server, "all_provider_configs_read_only", lambda: configs)
    monkeypatch.setattr(server, "all_provider_configs", lambda: configs)
    monkeypatch.setattr(server, "is_unified_mode_enabled", lambda: False)
    monkeypatch.setattr(server, "write_request_record", MagicMock())
    return configs


def test_catalog_excludes_unsupported_kinds_after_normalization(providers):
    response = TestClient(server.app).get("/v1/models")
    assert response.status_code == 200
    for field in ("data", "models"):
        ids = {entry["id"] for entry in response.json()[field]}
        assert {"chat:model", "legacy-chat:model"} <= ids
        assert not {"native:model", "unknown:model", "invalid:model"} & ids
    # Diagnostic reads retain invalid routes; defaults must not reinterpret them.
    assert providers["native"]["kind"] == "openai_responses"
    assert providers["unknown"]["kind"] == "future_transport"
    assert providers["invalid"]["kind"] is None


@pytest.mark.parametrize("name,kind", [("native", "openai_responses"), ("unknown", "future_transport"), ("invalid", "None")])
@pytest.mark.parametrize("stream", [False, True])
def test_unsupported_kind_is_configuration_error_before_any_upstream(monkeypatch, providers, name, kind, stream):
    upstream = MagicMock(side_effect=AssertionError("must not contact upstream"))
    monkeypatch.setattr(server.httpx, "AsyncClient", upstream)
    response = TestClient(server.app).post("/v1/responses", json={"model": f"{name}:model", "input": "hello", "stream": stream})
    assert response.status_code == 400
    assert f"Unsupported BYOK provider kind: {kind}" in response.json()["detail"]
    assert "Chat Completions" in response.json()["detail"]
    upstream.assert_not_called()
    record = server.write_request_record.call_args.args[0]
    assert record["error_class"] == "unsupported_provider_kind"
    assert record["http_status"] == 400


@pytest.fixture
def diagnostic_environment(monkeypatch, tmp_path, providers):
    config = tmp_path / "config.toml"
    config.write_text(cli.render_codex_config_snippet(
        model="native:model", provider_id="antigravity", provider_name="Google Antigravity",
        base_url="http://localhost:51122/v1", activate=True,
    ))
    monkeypatch.setenv("CODEX_ANTIGRAVITY_NO_UPDATE_CHECK", "1")
    monkeypatch.delenv("ANTIGRAVITY_STORAGE_KEY", raising=False)
    monkeypatch.setattr("keyring.get_password", lambda *args: None)
    monkeypatch.setattr(cli, "_diagnostic_all_provider_configs", lambda: providers)
    monkeypatch.setattr(cli, "all_provider_configs", lambda: providers)
    # Even a stale gateway catalog must not make a known unsupported route ready.
    monkeypatch.setattr(cli, "gateway_model_ids", lambda *args, **kwargs: {"native:model"})
    monkeypatch.setattr(cli, "readiness_storage_diagnostics", lambda: {})
    monkeypatch.setattr(cli, "service_status", lambda **kwargs: {"installed": False, "active": False})
    monkeypatch.setattr(cli, "gateway_status_info", lambda **kwargs: {"running": False, "status": "stopped"})
    monkeypatch.setattr(cli, "add_gateway_reachability", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli, "vision_sidecar_readiness", lambda: {"ok": False, "checks": []})
    return config


@pytest.mark.parametrize("flags", [["--byok-only"], ["--codex-ready"], ["--codex-ready", "--json"]])
def test_doctor_reports_exact_unsupported_kind_and_fails(monkeypatch, capsys, diagnostic_environment, flags):
    monkeypatch.setattr(sys, "argv", ["codex-antigravity", "doctor", "--config", str(diagnostic_environment), *flags])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 1
    output = capsys.readouterr().out
    assert "openai_responses" in output
    assert "Chat Completions" in output
    if "--json" in flags:
        report = json.loads(output)
        assert report["schemaVersion"] == 1 and report["command"] == "doctor"
        assert report["status"] == "failed" and report["exitCode"] == 1
        route = next(check for check in report["data"]["checks"] if check["name"] == "model_route")
        assert route["status"] == "fail"
        assert "openai_responses" in route["detail"]
        assert not report["ok"]
        assert not report["data"]["ok"]


def test_setup_preflight_and_capability_diagnostics_reject_unsupported_route(diagnostic_environment):
    status, reason, provider = cli.setup_byok_preflight("native", "model")
    assert status == "fail"
    assert "openai_responses" in reason
    assert "Chat Completions" in reason
    mismatches = cli.provider_capability_mismatches({"native": provider})
    assert len(mismatches) == 1
    assert "openai_responses" in mismatches[0]["reason"]
