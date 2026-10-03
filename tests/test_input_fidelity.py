"""Attachment fidelity is validated before any account/credential/HTTP work."""
import base64
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from codex_antigravity_auth import server
from codex_antigravity_auth.byok import provider_capabilities
from codex_antigravity_auth.models import NativeModel, native_model_capabilities, save_model_overlays
from codex_antigravity_auth.response_protocol import validate_capabilities
from codex_antigravity_auth.transform import transform_request, transform_request_to_chat
from fake_upstream import upstream


def request_with(part, model="gemini-3.8-flash", mixed=True):
    content = ([{"type": "input_text", "text": "inspect this"}] if mixed else []) + [part]
    return {"model": model, "input": [{"role": "user", "content": content}]}


@pytest.mark.parametrize("mixed", [False, True])
@pytest.mark.parametrize("part", [
    {"type": "input_audio", "input_audio": {"data": "AA==", "format": "wav"}},
    {"type": "input_video", "video_url": "https://example.invalid/video.mp4"},
    {"type": "input_image", "file_id": "unresolved"},
    {"type": "input_file", "file_url": "https://example.invalid/report.pdf"},
    {"type": "input_image", "image_url": "data:image/png;base64,not-base64"},
    {"type": "input_image", "image_url": "data:text/plain;base64,AA=="},
    {"type": "input_image", "image_url": "data:image/png;base64,"},
    {"type": "input_image", "image_url": "file:///not-a-real-file"},
    {"type": "input_image", "image_url": "https://user:synthetic@example.invalid/a.png"},
    {"type": {"bad": "shape"}},
    {"type": "input_image", "image_url": "https://example.invalid/a.png", "detail": []},
])
def test_bad_attachments_fail_before_dispatch(monkeypatch, part, mixed):
    acquire = Mock(side_effect=AssertionError("account selection must not happen"))
    credentials = Mock(side_effect=AssertionError("credential lookup must not happen"))
    monkeypatch.setattr(server.account_manager, "acquire_account", acquire)
    monkeypatch.setattr(server, "resolve_openai_auth", credentials)
    result = TestClient(server.app).post("/v1/responses", json=request_with(part, mixed=mixed))
    assert result.status_code == 400
    detail = result.json()["detail"]
    assert "input[0].content[" in (detail["message"] if isinstance(detail, dict) else detail)
    acquire.assert_not_called()
    credentials.assert_not_called()


@pytest.mark.parametrize("model", ["gpt-oss-120b-medium", "unknown-backend"])
def test_text_only_and_unknown_models_reject_images_before_account(monkeypatch, model):
    acquire = Mock()
    monkeypatch.setattr(server.account_manager, "acquire_account", acquire)
    response = TestClient(server.app).post("/v1/responses", json=request_with({"type": "input_image", "image_url": "https://example.invalid/a.png"}, model))
    assert response.status_code == 400
    acquire.assert_not_called()


def test_valid_images_preserve_bytes_urls_and_alias_contract():
    raw = b"\x89PNG\r\n\x1a\nsynthetic-payload"
    data_url = "data:image/png;base64," + base64.b64encode(raw).decode()
    source_url = "https://example.invalid/image.png?signature=a%2Fb&v=1"
    for source in (data_url, source_url):
        request = request_with({"type": "input_image", "image_url": source}, "sonnet")
        google = transform_request(request)["request"]["contents"][0]["parts"][-1]
        if source == data_url:
            assert google["inlineData"] == {"mimeType": "image/png", "data": data_url.split(",", 1)[1]}
            assert base64.b64decode(google["inlineData"]["data"]) == raw
        else:
            assert google["fileData"]["fileUri"] == source_url
        chat = transform_request_to_chat(request, "vision")
        assert chat["messages"][0]["content"][-1]["image_url"]["url"] == source
    assert native_model_capabilities("sonnet") == native_model_capabilities("claude-sonnet-4-6")


def test_data_url_bound_is_checked_before_decode(monkeypatch):
    from codex_antigravity_auth import input_fidelity
    monkeypatch.setattr(input_fidelity, "MAX_IMAGE_BYTES", 3)
    with pytest.raises(ValueError, match="at most 3"):
        transform_request(request_with({"type": "input_image", "image_url": "data:image/png;base64," + base64.b64encode(b"abcd").decode()}))


def test_explicit_byok_image_contract_and_http_payload(monkeypatch):
    provider = {"id": "vision", "kind": "openai_chat", "apiKey": "synthetic-key", "models": [{"id": "image-model", "capabilities": {"input_modalities": ["text", "image"], "image_forms": ["data_url"]}}]}
    request = request_with({"type": "input_image", "image_url": "data:image/png;base64,AA==", "detail": "low"}, "vision:image-model")
    with upstream((200, {"Content-Type": "application/json"}, b'{"choices":[{"finish_reason":"stop","message":{"content":"accepted"}}]}')) as (base, requests):
        provider["baseUrl"] = base
        monkeypatch.setattr(server, "all_provider_configs", lambda: {"vision": provider})
        response = TestClient(server.app).post("/v1/responses", json=request)
    assert response.status_code == 200
    assert requests[0]["body"]["messages"][0]["content"][-1] == {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA==", "detail": "low"}}
    with pytest.raises(ValueError, match="image input"):
        validate_capabilities(request, provider_capabilities(provider, "unverified-model"))
    with pytest.raises(ValueError, match="url images"):
        validate_capabilities(request_with({"type": "input_image", "image_url": "https://example.invalid/a.png"}), provider_capabilities(provider, "image-model"))


def test_overlay_requires_explicit_image_declaration_and_round_trips():
    model = NativeModel("private-vision", "backend-vision", "Private vision", 10000, "claude", aliases=("my-vision",), input_modalities=("text", "image"))
    save_model_overlays([model])
    assert native_model_capabilities("my-vision").input_modalities == frozenset({"text", "image"})
    assert native_model_capabilities("my-vision") == native_model_capabilities("private-vision")


@pytest.mark.parametrize("role", ["system", "developer"])
@pytest.mark.parametrize("mixed", [False, True])
@pytest.mark.parametrize("model", ["gemini-3.8-flash", "vision:configured"])
def test_roles_that_only_carry_text_reject_images(monkeypatch, role, mixed, model):
    request = request_with({"type": "input_image", "image_url": "https://example.invalid/a.png"}, model, mixed)
    request["input"][0]["role"] = role
    selection = Mock(side_effect=AssertionError("dispatch must not happen"))
    monkeypatch.setattr(server.account_manager, "acquire_account", selection)
    monkeypatch.setattr(server, "all_provider_configs", selection)
    result = TestClient(server.app).post("/v1/responses", json=request)
    assert result.status_code == 400
    assert "system/developer" in result.json()["detail"]
    selection.assert_not_called()


@pytest.mark.parametrize("role", [[], {}])
def test_invalid_role_shape_is_a_field_error(role):
    request = request_with({"type": "input_text", "text": "hello"})
    request["input"][0]["role"] = role
    result = TestClient(server.app, raise_server_exceptions=False).post("/v1/responses", json=request)
    assert result.status_code == 400
    assert "input[0].role" in result.json()["detail"]
