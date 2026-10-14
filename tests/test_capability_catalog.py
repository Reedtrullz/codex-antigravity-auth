"""Identity, routing, picker and standalone consumers share one truthful contract."""
import json
from pathlib import Path
import sys

import pytest
from fastapi.testclient import TestClient

from codex_antigravity_auth import server
from codex_antigravity_auth.accounts import AccountManager
from codex_antigravity_auth.capability_catalog import standalone_snapshot
from codex_antigravity_auth.models import (NATIVE_MODELS, NativeModel, all_native_models, canonical_model_id,
    native_model_capabilities, native_model_definition, save_model_overlays)
from codex_antigravity_auth.response_protocol import AttemptOutcome
from codex_antigravity_auth.unified import classify_route, openai_model_capabilities

SCRIPTS = Path(__file__).resolve().parents[1] / "codex_antigravity_auth/skills/anti/scripts"
sys.path.insert(0, str(SCRIPTS))
from anti_lib.capabilities import CapabilityRegistry


def catalog(monkeypatch):
    monkeypatch.setattr(server, "all_provider_configs_read_only", lambda: {})
    result = TestClient(server.app).get("/v1/models")
    assert result.status_code == 200
    return result.json()


def test_unrelated_claude_overlay_name_owns_claude_cooldown():
    save_model_overlays([NativeModel("private-reviewer", "backend-reviewer", "Reviewer", 12345, "claude", aliases=("my-review",))])
    manager = AccountManager()
    manager.record_attempt("fixture@example.invalid", "my-review", AttemptOutcome(scope="family", category="rate_limit", retry_after_seconds=600))
    assert "claude" in manager._cooldowns["fixture@example.invalid"]
    assert "gemini" not in manager._cooldowns["fixture@example.invalid"]
    assert classify_route("my-review", unified_enabled=True) == "antigravity"


def test_all_declared_aliases_agree_with_dispatch_and_catalog(monkeypatch):
    payload = catalog(monkeypatch)
    consumer = CapabilityRegistry()
    consumer.consume(payload)
    for definition in all_native_models():
        for alias in definition.aliases:
            assert canonical_model_id(alias) == definition.id
            assert native_model_capabilities(alias) == native_model_capabilities(definition.id)
            assert AccountManager._model_family(alias) == definition.family
            assert consumer.canonical(alias) == definition.id
            assert consumer.features(alias) == consumer.features(definition.id)
    assert all("image" not in entry["capabilities"]["transport"]["output_types"] for entry in payload["data"])


def test_native_openai_collision_has_one_explained_identity(monkeypatch):
    monkeypatch.setenv("ANTIGRAVITY_UNIFIED_MODEL_PICKER", "1")
    save_model_overlays([NativeModel("gpt-5.6", "private-backend", "Private overlay", 16384, "claude")])
    payload = catalog(monkeypatch)
    matches = [entry for entry in payload["data"] if entry["id"] == "gpt-5.6"]
    assert len(matches) == 1
    assert matches[0]["capabilities"]["route"] == classify_route("gpt-5.6", unified_enabled=True) == "antigravity"
    assert matches[0]["capabilities"]["family"] == "claude"
    assert matches[0]["shadowed_routes"] == [{"route": "openai", "id": "gpt-5.6", "reason": "native_definition_precedence"}]
    assert matches[0]["capabilities"]["context_limit"] == {"known": True, "tokens": 16384, "basis": "declared"}


def test_canonical_id_cannot_be_stolen_by_an_earlier_alias():
    save_model_overlays([NativeModel("sonnet", "my-own-backend", "Explicit sonnet", 100, "gemini")])
    assert native_model_definition("sonnet").id == "sonnet"
    assert AccountManager._model_family("sonnet") == "gemini"
    assert canonical_model_id("claude-sonnet-4-6") == "claude-sonnet-4-6"


def test_unknown_openai_and_byok_context_is_not_invented(monkeypatch):
    monkeypatch.setenv("ANTIGRAVITY_UNIFIED_MODEL_PICKER", "1")
    monkeypatch.setenv("ANTIGRAVITY_OPENAI_MODELS", "custom-openai")
    provider = {"id": "fixture", "kind": "openai_chat", "apiKey": "synthetic-key", "models": ["unknown"], "baseUrl": "http://127.0.0.1:1"}
    monkeypatch.setattr(server, "all_provider_configs_read_only", lambda: {"fixture": provider})
    payload = TestClient(server.app).get("/v1/models").json()
    entries = {entry["id"]: entry for entry in payload["data"]}
    for name in ("custom-openai", "fixture:unknown"):
        assert entries[name]["context_window"] is None
        assert entries[name]["capabilities"]["context_limit"] == {"known": False, "tokens": None, "basis": "unknown"}
        assert entries[name]["input_modalities"] == ["text"]
        assert entries[name]["capabilities"]["availability"] == "unknown"
    assert entries["fixture:unknown"]["capabilities"]["effective"]["output_types"] == ["text", "refusal"]
    assert "function_call" in entries["fixture:unknown"]["capabilities"]["transport"]["output_types"]
    assert entries["fixture:unknown"]["capabilities"]["effective"]["tools"] is None
    assert entries["fixture:unknown"]["capabilities"]["declared_backend"]["tools"] is None
    assert openai_model_capabilities("custom-openai").input_modalities == frozenset({"text"})
    assert entries["custom-openai"]["supported_reasoning_levels"] == []
    assert not openai_model_capabilities("custom-openai").reasoning
    assert classify_route("unknown-backend", unified_enabled=False) == "antigravity"


def test_snapshot_regeneration_and_safe_version_fallback():
    expected = standalone_snapshot()
    path = SCRIPTS / "anti_lib/capabilities.json"
    assert json.loads(path.read_text()) == expected
    consumer = CapabilityRegistry()
    assert consumer.features("sonnet")["images"] is True
    assert consumer.features("gemini-3.8-flash")["audio"] is False
    assert consumer.features("deepseek:deepseek-v4-pro")["tools"] is False
    consumer.consume({"capability_catalog_version": 999, "data": expected["data"]})
    assert consumer.source == "unsupported_version"
    assert not any(consumer.features("sonnet").values())
    assert consumer.context_limit("sonnet") is None
    consumer.consume({"data": [{"id": "sonnet"}]})
    assert consumer.source == "snapshot"
    assert consumer.features("sonnet")["images"] is True


def test_anti_consumes_gateway_capabilities_and_dynamic_aliases(monkeypatch):
    import anti
    save_model_overlays([NativeModel("custom-vision", "vision-backend", "Custom vision", 12000, "claude", aliases=("custom-eye",), input_modalities=("text", "image"))])
    payload = catalog(monkeypatch)
    monkeypatch.setattr(anti, "CAPABILITY_REGISTRY", CapabilityRegistry())
    monkeypatch.setattr(anti, "request_json", lambda *args, **kwargs: (200, payload))
    ids = anti.fetch_model_ids("http://127.0.0.1:1/v1", timeout=1, token_env="SYNTHETIC")
    assert "custom-vision" in ids
    assert anti.model_supports("custom-eye", "images")
    assert anti.catalog_model_matches("custom-eye", "custom-vision")
    assert anti.CAPABILITY_REGISTRY.context_limit("custom-eye") == 12000


def test_native_picker_efforts_match_validation(monkeypatch):
    from codex_antigravity_auth.response_protocol import validate_capabilities
    entries = {entry["id"]: entry for entry in catalog(monkeypatch)["data"]}
    for model in ("gemini-3.8-flash", "claude-opus-4-6-thinking"):
        caps = native_model_capabilities(model)
        efforts = [level["effort"] for level in entries[model]["supported_reasoning_levels"]]
        assert efforts == list(caps.reasoning_effort_levels)
        for effort in efforts:
            validate_capabilities({"input": "hello", "reasoning": {"effort": effort}}, caps)
    with pytest.raises(ValueError, match="reasoning.effort"):
        validate_capabilities({"input": "hello", "reasoning": {"effort": "xhigh"}}, native_model_capabilities("gemini-3.8-flash"))


def test_every_advertised_native_effort_changes_the_wire_contract():
    from codex_antigravity_auth.google_transport import GoogleTransport, AccountLease
    from codex_antigravity_auth.models import resolve_backend_model
    transport = GoogleTransport(timeout=1)
    budgets = {"low": 4000, "medium": 8000, "high": 16000, "xhigh": 32000}
    for definition in NATIVE_MODELS:
        caps = native_model_capabilities(definition.id)
        for effort in caps.reasoning_effort_levels:
            wire = transport.build_request({"model": definition.id, "input": "fixture", "max_output_tokens": 40000, "reasoning": {"effort": effort}}, AccountLease("fixture", "project", "synthetic"))
            assert wire["model"] == resolve_backend_model(definition.id)
            thinking = wire["request"]["generationConfig"]["thinkingConfig"]
            if definition.reasoning_mapping == "thinking_level":
                assert thinking == {"thinkingLevel": effort}
            else:
                assert thinking == {"thinking_budget": budgets[effort], "include_thoughts": True}


def test_unmapped_native_effort_is_rejected_before_account(monkeypatch):
    from unittest.mock import Mock
    save_model_overlays([NativeModel("unrelated-reviewer", "arbitrary-backend", "Reviewer", 12000, "claude")])
    acquire = Mock()
    monkeypatch.setattr(server.account_manager, "acquire_account", acquire)
    for model in ("gemini-3.1-pro", "gpt-oss-120b-medium", "unrelated-reviewer"):
        assert native_model_capabilities(model).reasoning_effort_levels == ()
        response = TestClient(server.app).post("/v1/responses", json={"model": model, "input": "hello", "reasoning": {"effort": "high"}})
        assert response.status_code == 400
    acquire.assert_not_called()


def test_explicit_overlay_budget_mapping_does_not_depend_on_backend_name():
    from codex_antigravity_auth.transform import transform_request
    save_model_overlays([NativeModel("reviewer", "arbitrary-backend", "Reviewer", 12000, "claude", reasoning_mapping="thinking_budget")])
    wire = transform_request({"model": "reviewer", "input": "hello", "reasoning": {"effort": "high"}})
    assert wire["model"] == "arbitrary-backend"
    assert wire["request"]["generationConfig"]["thinkingConfig"]["thinking_budget"] == 16000
    assert native_model_definition("reviewer").reasoning_mapping == "thinking_budget"


def test_unknown_openai_picker_and_output_types_match_dispatch(monkeypatch):
    from codex_antigravity_auth.response_protocol import validate_capabilities
    monkeypatch.setenv("ANTIGRAVITY_UNIFIED_MODEL_PICKER", "1")
    monkeypatch.setenv("ANTIGRAVITY_OPENAI_MODELS", "custom-openai")
    entry = next(item for item in catalog(monkeypatch)["data"] if item["id"] == "custom-openai")
    assert entry["supports_parallel_tool_calls"] is False
    assert entry["capabilities"]["effective"]["output_types"] == ["text", "refusal"]
    for value in (True, False):
        with pytest.raises(ValueError, match="parallel_tool_calls"):
            validate_capabilities({"input": "hello", "parallel_tool_calls": value}, openai_model_capabilities("custom-openai"))
