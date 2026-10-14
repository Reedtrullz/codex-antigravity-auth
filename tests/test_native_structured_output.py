"""Owned native wire contracts; no provider or credential access."""
from copy import deepcopy
import asyncio
import json
from unittest.mock import Mock

from fastapi.testclient import TestClient
import httpx
import pytest

from codex_antigravity_auth import server, models
from codex_antigravity_auth.google_transport import AccountLease, GoogleTransport
from codex_antigravity_auth.transform import transform_request


SCHEMA = {
    "type": "object",
    "properties": {"content": {"type": "string", "enum": ["speech", "music", "unknown"]}},
    "required": ["content"],
    "additionalProperties": False,
}


def request(format=None, model="gemini-3.8-flash"):
    result = {"model": model, "input": "Describe only the supplied sound."}
    if format is not None:
        result["text"] = {"format": format}
    return result


def test_native_schema_reaches_owned_upstream_without_weakening():
    seen = []

    async def receive(wire):
        seen.append(json.loads(wire.content))
        return httpx.Response(200, json={"candidates": []})

    transport = GoogleTransport(timeout=2, client_factory=lambda **kw: httpx.AsyncClient(
        transport=httpx.MockTransport(receive), **kw))
    body = request({"type": "json_schema", "name": "observation", "strict": False, "schema": SCHEMA})
    before = deepcopy(body)
    asyncio.run(transport.post(body, AccountLease("fixture@example.invalid", "fixture-project", "synthetic")))
    config = seen[0]["request"]["generationConfig"]
    assert config["responseMimeType"] == "application/json"
    assert config["responseJsonSchema"] == SCHEMA
    assert body == before


def test_native_json_object_requests_json_without_inventing_schema():
    config = transform_request(request({"type": "json_object"}))["request"]["generationConfig"]
    assert config["responseMimeType"] == "application/json"
    assert "responseJsonSchema" not in config


@pytest.mark.parametrize("format,path", [
    ({"type": "bogus"}, "text.format.type"),
    ({"type": "json_schema", "schema": []}, "text.format.schema"),
    ({"type": "json_schema", "schema": SCHEMA, "strict": True}, "text.format.strict"),
    ({"type": "json_schema", "schema": {"type": "string", "pattern": "x"}}, "pattern"),
    ({"type": "json_schema", "schema": {"oneOf": [{"type": "string"}, {"type": "number"}]}}, "oneOf"),
    ({"type": "json_schema", "schema": {"type": "integer", "minimum": "one"}}, "minimum"),
    ({"type": "json_schema", "schema": SCHEMA, "other": True}, "text.format"),
])
def test_unhonored_native_formats_refuse_before_account_acquisition(monkeypatch, format, path):
    acquire = Mock(side_effect=AssertionError("no account acquisition"))
    monkeypatch.setattr(server.account_manager, "acquire_account", acquire)
    response = TestClient(server.app).post("/v1/responses", json=request(format))
    assert response.status_code == 400
    assert path in response.json()["detail"]
    acquire.assert_not_called()


@pytest.mark.parametrize("model", ["claude-sonnet-4-6", "gpt-oss-120b-medium", "gemini-3.1-flash-image"])
def test_unestablished_native_schema_routes_refuse(model):
    with pytest.raises(ValueError, match="structured output"):
        transform_request(request({"type": "json_schema", "schema": SCHEMA}, model))


def test_explicit_plain_text_preserves_existing_request():
    plain = transform_request(request())["request"]
    explicit = transform_request(request({"type": "text"}))["request"]
    assert explicit == plain


def test_unknown_gemini_overlay_cannot_gain_schema_support_from_its_name(monkeypatch):
    overlay = models.NativeModel(id='gemini-overlay-test', backend_id='gemini-unverified',
                                family='gemini', display_name='Unverified fixture', context_window=1000)
    monkeypatch.setattr(models, 'load_model_overlays', lambda **kw: [overlay])
    assert models.native_model_definition(overlay.id) is overlay
    with pytest.raises(ValueError, match='structured output'):
        transform_request(request({'type': 'json_schema', 'schema': SCHEMA}, overlay.id))


def test_overlay_alias_of_supported_backend_keeps_schema_transport(monkeypatch):
    overlay = models.NativeModel(id='gemini-overlay-alias', backend_id='gemini-3.8-flash-tiered',
                                family='gemini', display_name='Alias fixture', context_window=1000)
    monkeypatch.setattr(models, 'load_model_overlays', lambda **kw: [overlay])
    body = transform_request(request({'type': 'json_schema', 'schema': SCHEMA}, overlay.id))
    assert body['model'] == 'gemini-3.8-flash-tiered'
    assert body['request']['generationConfig']['responseJsonSchema'] == SCHEMA
