"""The live readiness probe must establish generation, not just HTTP success."""

import json
import sys
from unittest.mock import MagicMock

import pytest

from codex_antigravity_auth import cli


def response_payload(status="completed", text="ready", **extra):
    return {
        "status": status,
        "output": [{"type": "message", "role": "assistant", "status": "completed", "content": [
            {"type": "output_text", "text": text},
        ]}],
        **extra,
    }


CASES = [
    pytest.param(response_payload(), "completed", True, id="completed-text"),
    pytest.param(response_payload(output_text=""), "completed", True, id="empty-convenience-text"),
    pytest.param({"status": "completed", "output_text": "ready"}, "malformed", False, id="direct-text-without-output"),
    pytest.param(response_payload("failed", error={"message": "upstream unavailable"}), "failed", False, id="failed-with-text"),
    pytest.param(response_payload(error={"message": "upstream unavailable"}), "failed", False, id="completed-with-error"),
    pytest.param(response_payload("incomplete", incomplete_details={"reason": "max_output_tokens"}), "incomplete", False, id="token-cap"),
    pytest.param(response_payload("incomplete"), "incomplete", False, id="incomplete-no-reason"),
    pytest.param(response_payload(output=[{"type": "message", "content": [{"type": "refusal", "refusal": "cannot comply"}]}]), "refusal", False, id="refusal"),
    pytest.param(response_payload(output_text="ready", output=[{"type": "message", "content": [{"type": "refusal"}]}]), "refusal", False, id="refusal-with-direct-text"),
    pytest.param(response_payload(text=" \n\t"), "empty", False, id="whitespace"),
    pytest.param(response_payload(output=[]), "empty", False, id="empty"),
    pytest.param(response_payload(output=[{"type": "reasoning", "text": "ready"}]), "empty", False, id="reasoning-only"),
    pytest.param(response_payload(output=[{"type": "message", "role": "assistant", "status": "completed", "content": [{"type": "input_text", "text": "ready"}]}]), "empty", False, id="input-text"),
    pytest.param(response_payload("queued"), "queued", False, id="queued"),
    pytest.param(response_payload("in_progress"), "in_progress", False, id="in-progress"),
    pytest.param(response_payload("cancelled"), "cancelled", False, id="cancelled"),
    pytest.param([], "malformed", False, id="list-envelope"),
    pytest.param(None, "malformed", False, id="null-envelope"),
    pytest.param({"output_text": "ready"}, "malformed", False, id="no-status"),
    pytest.param(response_payload(status=[]), "malformed", False, id="invalid-status"),
    pytest.param(response_payload(output={}), "malformed", False, id="invalid-output"),
    pytest.param(response_payload(output=[None]), "malformed", False, id="invalid-item"),
    pytest.param(response_payload(output=[{"type": "message", "content": [None]}]), "malformed", False, id="invalid-content"),
    pytest.param(response_payload(output=[], output_text="ready"), "empty", False, id="direct-text-with-empty-output"),
    pytest.param(response_payload(text="", output_text="ready"), "empty", False, id="direct-text-with-empty-message"),
    *[
        pytest.param(response_payload(output=[{"type": "message", "role": role, "status": "completed", "content": [{"type": "output_text", "text": "ready"}]}]), "empty", False, id=f"non-assistant-{role}")
        for role in ("user", "system", "tool", None)
    ],
    pytest.param(response_payload(output=[{"type": "message", "role": "assistant", "status": "in_progress", "content": [{"type": "output_text", "text": "ready"}]}]), "incomplete", False, id="unfinished-message"),
]


def mock_response(monkeypatch, payload, *, raw=None):
    response = MagicMock()
    response.status = 200
    response.read.return_value = raw if raw is not None else json.dumps(payload).encode()
    response.__enter__.return_value = response
    urlopen = MagicMock(return_value=response)
    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    return urlopen


@pytest.mark.parametrize("payload,kind,ok", CASES)
def test_probe_classifies_http_200(monkeypatch, payload, kind, ok):
    urlopen = mock_response(monkeypatch, payload)
    probe = cli.gateway_generate_probe("http://localhost/v1", "sonnet", timeout=1, token_env="")
    assert probe["transport_ok"] is True
    assert probe["generation_ok"] is probe["ok"] is ok
    assert probe["terminal_kind"] == kind
    assert probe["terminal_reason"]
    assert (probe["error"] is None) is ok
    assert urlopen.call_count == 1
    assert json.loads(urlopen.call_args.args[0].data)["max_output_tokens"] == 16


def test_probe_rejects_non_json_and_redacts_errors(monkeypatch):
    mock_response(monkeypatch, None, raw=b"not json")
    probe = cli.gateway_generate_probe("http://localhost/v1", "sonnet", timeout=1, token_env="")
    assert probe["transport_ok"] and not probe["ok"]
    assert probe["terminal_reason"] == "invalid_json"
    mock_response(monkeypatch, response_payload("failed", error={"message": "api_key=sk-secret-value"}))
    probe = cli.gateway_generate_probe("http://localhost/v1", "sonnet", timeout=1, token_env="")
    assert "sk-secret-value" not in json.dumps(probe)
    assert "Generation failed" in probe["error"]


@pytest.fixture
def ready_cli(monkeypatch, tmp_path):
    config = tmp_path / "config.toml"
    config.write_text(cli.render_codex_config_snippet(
        model="claude-sonnet-4-6", provider_id="antigravity", provider_name="Google Antigravity",
        base_url="http://localhost:51122/v1", activate=True,
    ))
    monkeypatch.setenv("CODEX_ANTIGRAVITY_NO_UPDATE_CHECK", "1")
    monkeypatch.delenv("ANTIGRAVITY_STORAGE_KEY", raising=False)
    monkeypatch.setattr("keyring.get_password", lambda *args: None)
    monkeypatch.setattr(cli, "resolve_oauth_credentials", lambda **kwargs: ("client", "secret"))
    monkeypatch.setattr(cli, "_diagnostic_load_accounts", lambda: {"accounts": [{"email": "fixture@example.com"}]})
    monkeypatch.setattr(cli, "_diagnostic_all_provider_configs", lambda: {})
    monkeypatch.setattr(cli, "gateway_model_ids", lambda *args, **kwargs: {"claude-sonnet-4-6"})
    monkeypatch.setattr(cli, "readiness_storage_diagnostics", lambda: {})
    monkeypatch.setattr(cli, "service_status", lambda **kwargs: {"installed": False, "active": False})
    monkeypatch.setattr(cli, "gateway_status_info", lambda **kwargs: {"running": False, "status": "stopped"})
    monkeypatch.setattr(cli, "add_gateway_reachability", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli, "vision_sidecar_readiness", lambda: {"ok": False, "checks": []})
    return config


@pytest.mark.parametrize("flags", [[], ["--codex-ready"], ["--codex-ready", "--json"]])
@pytest.mark.parametrize("payload,kind,ok", CASES)
def test_doctor_exit_and_diagnostics(monkeypatch, capsys, ready_cli, flags, payload, kind, ok):
    mock_response(monkeypatch, payload)
    monkeypatch.setattr(sys, "argv", ["codex-antigravity", "doctor", "--live", "--config", str(ready_cli), *flags])
    if ok:
        cli.main()
    else:
        with pytest.raises(SystemExit) as exc:
            cli.main()
        assert exc.value.code == 1
    output = capsys.readouterr().out
    if "--json" in flags:
        report = json.loads(output)
        live = next(check for check in report["checks"] if check["name"] == "live_generation")
        assert live["status"] == ("pass" if ok else "fail")
        assert live["probe"]["terminal_kind"] == kind
        assert live["probe"]["transport_ok"] is True
        assert report["ok"] is ok
        if not ok:
            assert live["probe"]["error"] in live["detail"]
    else:
        live_line = next(line for line in output.splitlines() if "Live Generation Smoke" in line or "live_generation:" in line)
        assert ("[PASS]" if ok else "[FAIL]") in live_line
        if not ok:
            assert "unknown error" not in live_line
            assert "failed" in live_line
