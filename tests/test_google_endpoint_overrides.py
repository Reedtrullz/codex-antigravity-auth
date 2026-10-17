"""Single Cloud Code Assist endpoint constant plus endpoint/IDE-version overrides."""
import json
import urllib.request

import pytest

from codex_antigravity_auth import constants, google_transport, oauth, route_identity
from codex_antigravity_auth.google_transport import GoogleTransport

DEFAULT = "https://daily-cloudcode-pa.googleapis.com"


@pytest.fixture(autouse=True)
def clean_overrides(monkeypatch):
    monkeypatch.delenv("ANTIGRAVITY_ENDPOINT", raising=False)
    monkeypatch.delenv("ANTIGRAVITY_IDE_VERSION", raising=False)
    monkeypatch.setattr(google_transport, "_IDE_USER_AGENT_CACHE", None)
    monkeypatch.setattr(google_transport, "_IDE_USER_AGENT_CACHE_VERSION", None)


def test_default_endpoint_and_backward_compatible_alias():
    assert constants.antigravity_endpoint() == DEFAULT
    assert constants.ANTIGRAVITY_ENDPOINT == constants.ANTIGRAVITY_ENDPOINT_PROD == DEFAULT
    assert google_transport.ANTIGRAVITY_ENDPOINT_PROD == DEFAULT
    assert GoogleTransport(timeout=1).endpoint == DEFAULT


@pytest.mark.parametrize("value", ["", "   "])
def test_blank_endpoint_override_uses_default(monkeypatch, value):
    monkeypatch.setenv("ANTIGRAVITY_ENDPOINT", value)
    assert constants.antigravity_endpoint() == DEFAULT


@pytest.mark.parametrize("value,expected", [
    ("https://cloudcode-pa.googleapis.com", "https://cloudcode-pa.googleapis.com"),
    (" https://sandbox.example.test/ ", "https://sandbox.example.test"),
    ("http://127.0.0.1:8123", "http://127.0.0.1:8123"),
])
def test_endpoint_override_reaches_transport_and_route_identity(monkeypatch, value, expected):
    monkeypatch.setenv("ANTIGRAVITY_ENDPOINT", value)
    assert constants.antigravity_endpoint() == expected
    assert GoogleTransport(timeout=1).endpoint == expected
    receipt = route_identity.identity("antigravity", "gemini-3.8-flash")
    if receipt is not None:
        assert route_identity.identity("antigravity", "gemini-3.8-flash", endpoint=expected) == receipt


@pytest.mark.parametrize("value", [
    "http://attacker.example.test",          # plaintext to a remote host
    "https://user:pass@example.test",        # embedded credentials
    "https://example.test/?q=1",             # query string
    "ftp://example.test",
    "not a url",
])
def test_invalid_endpoint_override_fails_closed(monkeypatch, value):
    monkeypatch.setenv("ANTIGRAVITY_ENDPOINT", value)
    with pytest.raises(ValueError):
        constants.antigravity_endpoint()
    with pytest.raises(ValueError):
        GoogleTransport(timeout=1)


def test_ide_version_default_and_override(monkeypatch):
    assert google_transport.antigravity_ide_version() == google_transport.ANTIGRAVITY_IDE_VERSION
    assert "antigravity/ide/2.5.5 " in google_transport.ide_user_agent()
    monkeypatch.setenv("ANTIGRAVITY_IDE_VERSION", "2.6.0")
    assert google_transport.antigravity_ide_version() == "2.6.0"
    # The cached User-Agent follows the effective version.
    assert google_transport.ide_user_agent().startswith("antigravity/ide/2.6.0 (")
    headers = GoogleTransport(timeout=1).build_headers(
        google_transport.AccountLease(email="a@example.test", access_token="token", project_id=None)
    )
    assert headers["User-Agent"].startswith("antigravity/ide/2.6.0 (")


@pytest.mark.parametrize("value", ["latest", "2", "2.5.5; injected", "2.5.5 (x)", "v2.5.5"])
def test_invalid_ide_version_override_fails_closed(monkeypatch, value):
    monkeypatch.setenv("ANTIGRAVITY_IDE_VERSION", value)
    with pytest.raises(ValueError):
        google_transport.antigravity_ide_version()
    with pytest.raises(ValueError):
        google_transport.ide_user_agent()


class _Response:
    status = 200

    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_onboarding_uses_endpoint_and_ide_version_overrides(monkeypatch):
    monkeypatch.setenv("ANTIGRAVITY_ENDPOINT", "https://override.example.test")
    monkeypatch.setenv("ANTIGRAVITY_IDE_VERSION", "3.0.1")
    seen = []

    def fake_open(request, timeout):
        assert isinstance(request, urllib.request.Request)
        seen.append(request)
        return _Response({"done": True, "response": {"cloudaicompanionProject": "proj-1"}})

    monkeypatch.setattr(oauth, "open_http_request", fake_open)
    assert oauth.onboard_user("token") == "proj-1"
    assert seen[0].full_url == "https://override.example.test/v1internal:onboardUser"
    body = json.loads(seen[0].data.decode("utf-8"))
    assert body["metadata"]["ide_version"] == "3.0.1"
    assert seen[0].get_header("User-agent").startswith("antigravity/ide/3.0.1 (")


def test_project_discovery_host_is_explicit_and_not_overridden(monkeypatch):
    monkeypatch.setenv("ANTIGRAVITY_ENDPOINT", "https://override.example.test")
    seen = []

    def fake_open(request, timeout):
        seen.append(request)
        return _Response({"cloudaicompanionProject": "proj-2"})

    monkeypatch.setattr(oauth, "open_http_request", fake_open)
    assert oauth.load_code_assist("token") == "proj-2"
    assert seen[0].full_url == constants.CODE_ASSIST_DISCOVERY_ENDPOINT + "/v1internal:loadCodeAssist"
