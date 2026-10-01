"""Synthetic handler chains and loopback servers verify credential-safe endpoints."""

import asyncio
import io
import json
import shutil
import socket
import subprocess
import sys
import threading
import urllib.error
import urllib.request
import urllib.response
from contextlib import contextmanager
from email.message import Message
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import MagicMock

import pytest
import httpx

from codex_antigravity_auth import byok, cli, oauth, server, unified
from codex_antigravity_auth.google_transport import AccountLease, GoogleTransport
from codex_antigravity_auth.openai_transport import OpenAICompatibleTransport, PreparedOpenAIRequest
from codex_antigravity_auth.unified import OpenAIAuth
from codex_antigravity_auth.endpoint_policy import open_http_request, validate_endpoint_url
from codex_antigravity_auth.skills.anti.scripts.anti_lib import endpoint_policy as shared
from codex_antigravity_auth.skills.anti.tests.test_anti import load_anti


UNSAFE = [
    "http://remote.example/v1", "http://testclient/v1", "http://localhost.example/v1",
    "http://192.0.2.1/v1", "https://fixture-user:fixture-password@example.invalid/v1",
    "http://@localhost/v1", "http://localhost:bad/v1", "http://localhost:0/v1",
    "http://[::1]extra/v1", "https://example.invalid/\x85bad", "https://example.invalid/a\\b",
    "\thttps://example.invalid/v1", "\nhttps://example.invalid/v1", "\x00https://example.invalid/v1",
    "https://example.invalid/v1\r", "https://example.invalid/v1\x7f", "https://example.invalid/v1\x85",
]
SAFE = ["http://localhost:51122/v1", "http://127.0.0.1:51122/v1", "http://127.0.0.2/v1",
        "http://[::1]:51122/v1", "https://example.invalid/v1"]


@pytest.mark.parametrize("url", UNSAFE)
def test_unsafe_endpoints_are_rejected_before_transport(monkeypatch, url):
    anti = load_anti()
    transport = MagicMock(side_effect=AssertionError("network must not run"))
    monkeypatch.setattr(shared.urllib.request, "build_opener", transport)
    monkeypatch.setattr(cli, "open_http_request", transport)
    monkeypatch.setattr(anti, "open_http_request", transport)
    with pytest.raises(ValueError):
        validate_endpoint_url(url)
    with pytest.raises(ValueError):
        byok.validate_http_base_url(url)
    with pytest.raises(ValueError):
        open_http_request(url)
    with pytest.raises(anti.AntiError):
        anti.normalize_base_url(url)
    with pytest.raises(anti.AntiError) as exc:
        anti.request_json("POST", url, payload={"input": "synthetic-source"})
    assert "fixture-password" not in str(exc.value)
    with pytest.raises(RuntimeError):
        cli.gateway_model_ids(url)
    report = cli.gateway_generate_probe(url, "fixture-model", timeout=1, token_env="")
    assert not report["ok"] and report["http_status"] is None
    assert "fixture-password" not in str(report)
    transport.assert_not_called()


@pytest.mark.parametrize("url", SAFE)
def test_base_url_policy_agrees_in_package_and_standalone(url):
    anti = load_anti()
    assert validate_endpoint_url(url) == byok.validate_http_base_url(url) == anti.normalize_base_url(url) == url


def test_query_policy_and_ipv6_authority_are_explicit():
    with pytest.raises(ValueError, match="query"):
        byok.validate_http_base_url("https://example.invalid/v1?fixture=1")
    assert validate_endpoint_url("https://example.invalid/v1?fixture=1", allow_query=True).endswith("?fixture=1")
    assert cli.local_gateway_base_url("::1", 51122) == "http://[::1]:51122/v1"
    assert shared.httpx_client_options("http://localhost/v1", timeout=1)["trust_env"] is False
    assert shared.httpx_client_options("https://localhost/v1", timeout=1)["trust_env"] is True


@pytest.mark.parametrize("suffix", ["#", "#fragment"])
def test_preconstructed_requests_cannot_hide_fragment_delimiters(monkeypatch, suffix):
    transport = MagicMock(side_effect=AssertionError("network must not run"))
    monkeypatch.setattr(shared.urllib.request, "build_opener", transport)
    request = urllib.request.Request("https://example.invalid/path" + suffix)
    with pytest.raises(ValueError, match="fragment"):
        open_http_request(request, timeout=1)
    transport.assert_not_called()


@pytest.mark.parametrize("url", ["https://example.invalid/v1?", "https://example.invalid/v1#"])
def test_empty_base_delimiters_cannot_capture_an_appended_request_path(monkeypatch, url):
    anti = load_anti()
    transport = MagicMock(side_effect=AssertionError("network must not run"))
    monkeypatch.setattr(cli, "open_http_request", transport)
    with pytest.raises(ValueError):
        byok.validate_http_base_url(url)
    with pytest.raises(anti.AntiError):
        anti.normalize_base_url(url)
    with pytest.raises(RuntimeError):
        cli.gateway_model_ids(url)
    assert not cli.gateway_generate_probe(url, "fixture", timeout=1, token_env="")["ok"]
    with pytest.raises(ValueError):
        GoogleTransport(timeout=1, endpoint=url, client_factory=transport)
    with pytest.raises(unified.OpenAIUpstreamAuthError):
        unified.openai_responses_url(OpenAIAuth(kind="api_key", base_url=url, api_key="synthetic-token"))
    transport.assert_not_called()
    if url.endswith("?"):
        assert validate_endpoint_url(url, allow_query=True) == url
    else:
        with pytest.raises(ValueError):
            validate_endpoint_url(url, allow_query=True)


@pytest.mark.parametrize("url", ["https://example.invalid/v1\r", "https://example.invalid/v1?", "http://remote.example/v1", "https://user:fixture-password@example.invalid/v1", 123])
def test_invalid_stored_provider_endpoint_cannot_fall_back_to_a_preset(monkeypatch, url):
    normalized = byok.normalize_provider_entry({"baseUrl": url, "apiKey": "synthetic-token"})
    assert normalized["baseUrl"] is None
    provider = byok.merged_provider_config("deepseek", normalized)
    transport = MagicMock(side_effect=AssertionError("network must not run"))
    monkeypatch.setattr(server.httpx, "AsyncClient", transport)
    with pytest.raises(server.HTTPException) as exc:
        asyncio.run(server.create_openai_compatible_response({"input": "fixture"}, provider, "deepseek-chat", "fixture"))
    assert exc.value.status_code == 400
    transport.assert_not_called()


@pytest.mark.parametrize("url", ["\thttps://example.invalid/v1", "https://example.invalid/v1\n", "https://example.invalid/v1?", "https://example.invalid/v1#"])
@pytest.mark.parametrize("source", ["environment", "file"])
def test_native_auth_url_resolution_cannot_strip_invalid_input(monkeypatch, source, url):
    if source == "environment":
        monkeypatch.setenv("OPENAI_API_KEY", "synthetic-token")
        monkeypatch.setenv("OPENAI_BASE_URL", url)
    else:
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.setattr(unified, "_read_json_file", lambda _: {"api_key": "synthetic-token", "base_url": url})
    with pytest.raises(unified.OpenAIUpstreamAuthError) as exc:
        unified.resolve_openai_auth()
    assert exc.value.status_code == 400


def fake_chain(monkeypatch, target, *, status=302):
    seen, responses = [], []
    original_builder = urllib.request.build_opener
    def respond(request):
        seen.append({"url": request.full_url, "headers": dict(request.header_items()), "body": request.data})
        headers = Message()
        if len(seen) == 1:
            headers["Location"] = target
            body, code = b"fixture-private-redirect-body", status
        else:
            body, code = b'{}', 200
        response = urllib.response.addinfourl(io.BytesIO(body), headers, request.full_url, code)
        response.msg = "fixture response"
        responses.append(response)
        return response
    class HTTP(urllib.request.HTTPHandler):
        def http_open(self, request):
            return respond(request)
    class HTTPS(urllib.request.HTTPSHandler):
        def https_open(self, request):
            return respond(request)
    def build(*handlers):
        return original_builder(urllib.request.ProxyHandler({}), HTTP(), HTTPS(), *handlers)
    monkeypatch.setattr(shared.urllib.request, "build_opener", build)
    return seen, responses, build


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
@pytest.mark.parametrize("target", [
    "https://other.example/target", "https://origin.example:8443/target",
    "http://origin.example/target", "https://origin.example/target",
])
def test_actual_handler_chain_refuses_host_port_scheme_and_same_origin_redirects(monkeypatch, status, target):
    seen, responses, _ = fake_chain(monkeypatch, target, status=status)
    request = urllib.request.Request("https://origin.example/start", data=b"synthetic-source", headers={"Authorization": "Bearer synthetic-token"})
    with pytest.raises(urllib.error.HTTPError) as exc:
        open_http_request(request, timeout=1)
    assert exc.value.code == status
    assert len(seen) == 1
    assert seen[0]["headers"]["Authorization"] == "Bearer synthetic-token"
    assert seen[0]["body"] == b"synthetic-source"
    assert "Authorization" not in request.headers
    assert request.unredirected_hdrs["Authorization"] == "Bearer synthetic-token"
    assert responses[0].closed
    body = exc.value.read().decode()
    assert "redirect_not_allowed" in body
    assert "fixture-private-redirect-body" not in body and target not in body


def test_fake_chain_positive_control_can_follow_a_redirect(monkeypatch):
    seen, _, build = fake_chain(monkeypatch, "https://other.example/target")
    # This intentionally omits our policy, proving that the synthetic transport
    # drives urllib's redirect machinery rather than short-circuiting responses.
    with build().open("https://origin.example/start", timeout=1) as response:
        assert response.status == 200
    assert [item["url"] for item in seen] == ["https://origin.example/start", "https://other.example/target"]


@pytest.mark.parametrize("caller", ["models", "generation", "anti", "oauth"])
def test_real_callers_use_the_no_redirect_handler_chain(monkeypatch, caller):
    seen, _, _ = fake_chain(monkeypatch, "https://other.example/target")
    monkeypatch.setenv("ANTIGRAVITY_GATEWAY_TOKEN", "synthetic-token")
    if caller == "models":
        with pytest.raises(RuntimeError, match="HTTP 302"):
            cli.gateway_model_ids("https://origin.example/v1")
    elif caller == "generation":
        result = cli.gateway_generate_probe("https://origin.example/v1", "fixture", timeout=1, token_env="ANTIGRAVITY_GATEWAY_TOKEN")
        assert not result["ok"] and result["http_status"] == 302
    elif caller == "anti":
        status, result = load_anti().request_json("POST", "https://origin.example/v1/responses", payload={"input": "synthetic-source"})
        assert status == 302 and result["error"]["code"] == "redirect_not_allowed"
    else:
        with pytest.raises(urllib.error.HTTPError):
            oauth.open_http_request(urllib.request.Request("https://origin.example/token", data=b"client_secret=synthetic-secret"), timeout=1)
    assert len(seen) == 1
    if caller != "oauth":
        assert seen[0]["headers"]["Authorization"] == "Bearer synthetic-token"


@contextmanager
def loopback_server(handler, host="127.0.0.1"):
    class IPv6Server(ThreadingHTTPServer):
        address_family = socket.AF_INET6
    try:
        server = (IPv6Server if ":" in host else ThreadingHTTPServer)((host, 0), handler)
    except OSError as exc:
        if ":" in host:
            pytest.skip(f"IPv6 loopback unavailable: {exc}")
        raise
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        assert not thread.is_alive()


def test_redirect_never_reaches_another_loopback_origin(monkeypatch):
    received, initial = [], []
    class Target(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_GET(self):
            received.append(dict(self.headers))
            self.send_response(200)
            self.end_headers()
        do_POST = do_GET
    with loopback_server(Target) as target:
        class Origin(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_GET(self):
                initial.append(self.headers.get("Authorization"))
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{target.server_port}/target")
                self.end_headers()
        with loopback_server(Origin) as origin:
            # Loopback must bypass even an explicitly configured external proxy.
            monkeypatch.setenv("http_proxy", "http://example.invalid:9")
            request = urllib.request.Request(f"http://127.0.0.1:{origin.server_port}/start", headers={"Authorization": "Bearer synthetic-token"})
            with pytest.raises(urllib.error.HTTPError):
                open_http_request(request, timeout=1)
    assert initial == ["Bearer synthetic-token"]
    assert received == []


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1"])
def test_loopback_gateway_requests_keep_working_with_synthetic_authorization(monkeypatch, host):
    received = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_GET(self):
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            received.append(self.headers.get("Authorization"))
            payload = {"data": [{"id": "fixture-model"}]} if self.path.endswith("/models") else {
                "status": "completed", "output": [{"type": "message", "status": "completed", "role": "assistant",
                "content": [{"type": "output_text", "text": "ready"}]}],
            }
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        do_POST = do_GET
    monkeypatch.setenv("ANTIGRAVITY_GATEWAY_TOKEN", "synthetic-token")
    with loopback_server(Handler, "::1" if host == "::1" else "127.0.0.1") as server:
        base = cli.local_gateway_base_url(host, server.server_port)
        assert cli.gateway_model_ids(base) == {"fixture-model"}
        result = cli.gateway_generate_probe(base, "fixture-model", timeout=1, token_env="ANTIGRAVITY_GATEWAY_TOKEN")
        assert result["generation_ok"] is True
    assert received == ["Bearer synthetic-token", "Bearer synthetic-token"]


def test_copied_standalone_policy_needs_no_installed_package(tmp_path):
    module = tmp_path / "endpoint_policy.py"
    shutil.copyfile(shared.__file__, module)
    code = """
import json,sys
sys.path.insert(0,sys.argv[1])
from endpoint_policy import validate_endpoint_url
output=[]
for url in json.load(sys.stdin):
    try: output.append(validate_endpoint_url(url))
    except ValueError: output.append(None)
print(json.dumps(output))
"""
    result = subprocess.run([sys.executable, "-I", "-S", "-c", code, str(tmp_path)], input=json.dumps(SAFE + UNSAFE), text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == SAFE + [None] * len(UNSAFE)


@pytest.mark.parametrize("route", ["google", "chat", "native", "byok"])
@pytest.mark.parametrize("base", ["https://origin.example", "http://127.0.0.1:51122"])
def test_httpx_provider_clients_never_follow_credentialed_redirects(monkeypatch, route, base):
    seen = []
    original_client = httpx.AsyncClient
    def respond(request):
        seen.append(request)
        return httpx.Response(302, headers={"Location": "https://other.example/target"}, json={"error": "fixture redirect"})
    def client_factory(**kwargs):
        assert kwargs["follow_redirects"] is False
        if base.startswith("http:"):
            assert kwargs["trust_env"] is False
        return original_client(transport=httpx.MockTransport(respond), **kwargs)
    monkeypatch.setattr(server.httpx, "AsyncClient", client_factory)
    async def run():
        if route == "google":
            response = await GoogleTransport(timeout=1, endpoint=base, client_factory=client_factory).post(
                {"model": "gemini-3.8-flash", "input": "fixture"}, AccountLease("fixture@example.invalid", "fixture-project", "synthetic-token"))
            assert response.status_code == 302
        elif route == "chat":
            prepared = PreparedOpenAIRequest({}, base + "/chat/completions", {"Authorization": "Bearer synthetic-token"}, 1)
            events = [event async for event in OpenAICompatibleTransport(timeout=1, client_factory=client_factory).stream_chat_events(prepared, response_id="fixture", display_model="fixture")]
            assert any(isinstance(event, dict) and event.get("type") == "response.failed" for event in events)
        elif route == "native":
            auth = OpenAIAuth(kind="api_key", base_url=base + "/v1", api_key="synthetic-token")
            with pytest.raises(server.OpenAIUpstreamHTTPError) as exc:
                await server._open_openai_upstream_stream({"input": "fixture"}, "gpt-5.6", auth)
            assert exc.value.status_code == 302
        else:
            with pytest.raises(server.HTTPException) as exc:
                await server.create_openai_compatible_response({"input": "fixture"}, {"id": "fixture", "baseUrl": base + "/v1", "apiKey": "synthetic-token"}, "fixture-model", "fixture:fixture-model")
            assert exc.value.status_code == 302
    asyncio.run(run())
    assert len(seen) == 1
    assert str(seen[0].url).startswith(base)
    assert seen[0].headers["Authorization"] == "Bearer synthetic-token"
