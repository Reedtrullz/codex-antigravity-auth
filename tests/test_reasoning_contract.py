"""Advertised effort settings must match the exact payload sent upstream."""
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from codex_antigravity_auth import server
from codex_antigravity_auth.byok import provider_capabilities
from codex_antigravity_auth.response_protocol import validate_capabilities
from fake_upstream import upstream


def provider(base="http://127.0.0.1:1"):
    return {"id": "fixture", "kind": "openai_chat", "baseUrl": base, "apiKey": "synthetic-key", "models": [
        {"id": "verified", "capabilities": {"reasoning_effort": {"parameter": "reasoning.effort", "levels": ["low", "high"]}}},
        {"id": "unverified", "capabilities": {"reasoning": True}},
        {"id": "replay-only", "capabilities": {"reasoning_replay": True}},
    ]}


@pytest.mark.parametrize("effort", ["low", "high"])
def test_declared_reasoning_mapping_reaches_real_http(monkeypatch, effort):
    with upstream((200, {"Content-Type": "application/json"}, b'{"choices":[{"finish_reason":"stop","message":{"content":"answer"}}]}')) as (base, requests):
        config = provider(base)
        monkeypatch.setattr(server, "all_provider_configs", lambda: {"fixture": config})
        response = TestClient(server.app).post("/v1/responses", json={"model": "fixture:verified", "input": "hello", "reasoning": {"effort": effort}})
    assert response.status_code == 200
    assert requests[0]["body"]["reasoning"] == {"effort": effort}
    assert "reasoning_effort" not in requests[0]["body"]


@pytest.mark.parametrize("model,reasoning", [
    ("unverified", {"effort": "high"}), ("unknown", {"effort": "high"}),
    ("verified", {"effort": "medium"}), ("verified", {"summary": "auto"}),
    ("verified", {}), ("verified", {"max_tokens": 100}), ("verified", {"effort": []}),
])
def test_unmapped_reasoning_is_rejected_before_http_and_key_resolution(monkeypatch, model, reasoning):
    from codex_antigravity_auth import openai_transport
    config = provider()
    monkeypatch.setattr(server, "all_provider_configs", lambda: {"fixture": config})
    key = Mock(side_effect=AssertionError("key resolution must not happen"))
    monkeypatch.setattr(openai_transport, "resolve_api_key", key)
    response = TestClient(server.app).post("/v1/responses", json={"model": "fixture:" + model, "input": "hello", "reasoning": reasoning})
    assert response.status_code == 400
    assert "reasoning" in response.json()["detail"]
    key.assert_not_called()


def test_picker_levels_match_route_validation(monkeypatch):
    config = provider()
    monkeypatch.setattr(server, "all_provider_configs_read_only", lambda: {"fixture": config})
    models = {m["id"]: m for m in TestClient(server.app).get("/v1/models").json()["data"]}
    supported = [item["effort"] for item in models["fixture:verified"]["supported_reasoning_levels"]]
    assert supported == ["low", "high"]
    assert models["fixture:unverified"]["supported_reasoning_levels"] == []
    assert models["fixture:unverified"]["default_reasoning_level"] is None
    for effort in supported:
        validate_capabilities({"input": "hello", "reasoning": {"effort": effort}}, provider_capabilities(config, "verified"))


def test_replay_and_effort_are_independent_and_opaque_replay_is_rejected():
    config = provider()
    replay = {"input": [{"type": "reasoning", "step_by_step_summary": "synthetic summary"}]}
    with pytest.raises(ValueError, match="reasoning replay"):
        validate_capabilities(replay, provider_capabilities(config, "verified"))
    caps = provider_capabilities(config, "replay-only")
    validate_capabilities(replay, caps)
    assert caps.reasoning_replay and not caps.reasoning
    with pytest.raises(ValueError, match="opaque reasoning"):
        validate_capabilities({"input": [{"type": "reasoning", "encrypted_content": "synthetic"}]}, caps)


def test_plain_legacy_request_does_not_gain_reasoning_fields(monkeypatch):
    with upstream((200, {"Content-Type": "application/json"}, b'{"choices":[{"finish_reason":"stop","message":{"content":"answer"}}]}')) as (base, requests):
        monkeypatch.setattr(server, "all_provider_configs", lambda: {"fixture": provider(base)})
        response = TestClient(server.app).post("/v1/responses", json={"model": "fixture:unverified", "input": "hello"})
    assert response.status_code == 200
    assert "reasoning" not in requests[0]["body"]
    assert "reasoning_effort" not in requests[0]["body"]


def test_google_replay_is_rejected_and_generated_summaries_are_not_fake_ciphertext(monkeypatch):
    from codex_antigravity_auth.models import native_model_capabilities
    from codex_antigravity_auth.google_transport import GoogleTransport
    assert not native_model_capabilities("sonnet").reasoning_replay
    acquire = Mock()
    monkeypatch.setattr(server.account_manager, "acquire_account", acquire)
    response = TestClient(server.app).post("/v1/responses", json={"model": "sonnet", "input": [{"type": "reasoning", "step_by_step_summary": "prior summary"}]})
    assert response.status_code == 400
    acquire.assert_not_called()
    result = GoogleTransport(timeout=1).parse_response({"candidates": [{"finishReason": "STOP", "content": {"parts": [{"thought": True, "text": "summary"}, {"thoughtSignature": "opaque", "text": "answer"}]}}]})
    assert "encrypted_content" not in result.output[0]
    assert result.output[1]["content"][0]["text"] == "answer"
