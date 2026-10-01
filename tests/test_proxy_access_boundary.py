"""Synthetic ASGI peers and mocked launchers define the proxy trust boundary."""

import asyncio
import json
import sys
from unittest.mock import MagicMock

import httpx
import pytest
from starlette.requests import Request
from starlette.responses import JSONResponse
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from codex_antigravity_auth import cli, server
from codex_antigravity_auth.service import service_command


TOKEN = "synthetic-gateway-token-01234567890123456789"


@pytest.fixture(autouse=True)
def clear_remote_mode(monkeypatch):
    # Record both keys even when initially absent, so direct startup writes are
    # rolled back by monkeypatch rather than leaking into later test modules.
    monkeypatch.setenv("ANTIGRAVITY_ALLOW_REMOTE", "0")
    monkeypatch.setenv("ANTIGRAVITY_GATEWAY_TOKEN", "")


def request(peer="127.0.0.1", *, method="GET", path="/health", headers=None, scheme="http"):
    fields = {"host": "localhost:51122", **(headers or {})}
    return Request({
        "type": "http", "http_version": "1.1", "method": method, "scheme": scheme,
        "path": path, "root_path": "", "query_string": b"", "server": ("127.0.0.1", 51122),
        "client": (peer, 12345) if peer is not None else None,
        "headers": [(key.lower().encode(), value.encode("latin-1")) for key, value in fields.items()],
    })


def authorize(req):
    calls = []
    async def next_handler(incoming):
        calls.append(incoming)
        return JSONResponse({"ok": True})
    response = asyncio.run(server.require_remote_gateway_token(req, next_handler))
    assert bool(calls) is (response.status_code == 200)
    return response


@pytest.mark.parametrize("peer,host", [("127.0.0.1", "localhost:51122"), ("::1", "[::1]:51122"), ("testclient", "testserver")])
@pytest.mark.parametrize("path", ["/health", "/v1/models"])
def test_direct_loopback_clients_remain_local_without_auth(peer, host, path):
    assert authorize(request(peer, path=path, headers={"host": host})).status_code == 200


@pytest.mark.parametrize("header", ["forwarded", "x-forwarded-for", "x-forwarded-host", "x-forwarded-proto", "x-forwarded-port", "x-real-ip"])
@pytest.mark.parametrize("value", ["127.0.0.1", "203.0.113.7", ""])
def test_unconfigured_proxy_markers_never_receive_the_local_exemption(header, value):
    assert authorize(request(headers={header: value})).status_code == 403


@pytest.mark.parametrize("peer", ["127.0.0.1", "::1", "203.0.113.7", None])
@pytest.mark.parametrize("authorization", [None, "Bearer wrong", "Bearer caf\u00e9", "Bearer " + TOKEN])
def test_authenticated_mode_requires_the_token_for_every_peer(monkeypatch, peer, authorization):
    monkeypatch.setenv("ANTIGRAVITY_ALLOW_REMOTE", "1")
    monkeypatch.setenv("ANTIGRAVITY_GATEWAY_TOKEN", TOKEN)
    headers = {} if authorization is None else {"authorization": authorization}
    assert authorize(request(peer, headers=headers)).status_code == (200 if authorization == "Bearer " + TOKEN else 403)


def test_weak_configured_token_cannot_authorize_loopback_health(monkeypatch):
    monkeypatch.setenv("ANTIGRAVITY_ALLOW_REMOTE", "1")
    monkeypatch.setenv("ANTIGRAVITY_GATEWAY_TOKEN", "short")
    response = authorize(request(headers={"authorization": "Bearer short"}))
    assert response.status_code == 403
    assert "at least 32" in json.loads(response.body)["detail"]


def test_token_without_remote_opt_in_does_not_authorize_proxy_or_remote_peer(monkeypatch):
    monkeypatch.setenv("ANTIGRAVITY_GATEWAY_TOKEN", TOKEN)
    auth = {"authorization": "Bearer " + TOKEN}
    assert authorize(request("203.0.113.7", headers={**auth, "x-forwarded-for": "127.0.0.1"})).status_code == 403
    assert authorize(request(headers={**auth, "forwarded": "for=203.0.113.7"})).status_code == 403


@pytest.mark.parametrize("method,path", [("GET", "/health"), ("GET", "/v1/models"), ("POST", "/v1/responses")])
@pytest.mark.parametrize("host", ["public.example:51122", "testclient:51122", "testserver:51122", "[", "localhost:bad", "[::1]extra", "localhost#"])
def test_local_host_boundary_also_protects_health_and_model_reads(method, path, host):
    response = authorize(request(method=method, path=path, headers={"host": host, "content-type": "application/json"}))
    assert response.status_code == 403


def test_missing_and_duplicate_hosts_cannot_use_the_framework_server_fallback():
    missing = request()
    missing.scope["headers"] = []
    assert authorize(missing).status_code == 403
    duplicate = request()
    duplicate.scope["headers"].append((b"host", b"public.example"))
    assert authorize(duplicate).status_code == 403


@pytest.mark.parametrize("remote", [False, True])
def test_origin_and_browser_guards_still_apply_after_authorization(monkeypatch, remote):
    headers = {"content-type": "application/json", "origin": "https://other.example", "sec-fetch-site": "cross-site"}
    if remote:
        monkeypatch.setenv("ANTIGRAVITY_ALLOW_REMOTE", "1")
        monkeypatch.setenv("ANTIGRAVITY_GATEWAY_TOKEN", TOKEN)
        headers["authorization"] = "Bearer " + TOKEN
    assert authorize(request(method="POST", path="/v1/responses", headers=headers)).status_code == 403
    headers.pop("sec-fetch-site")
    headers["origin"] = "http://testclient:51122"
    assert authorize(request(method="POST", path="/v1/responses", headers=headers)).status_code == 403


def test_configured_proxy_can_relay_authenticated_requests_with_a_public_host(monkeypatch):
    monkeypatch.setenv("ANTIGRAVITY_ALLOW_REMOTE", "1")
    monkeypatch.setenv("ANTIGRAVITY_GATEWAY_TOKEN", TOKEN)
    headers = {"authorization": "Bearer " + TOKEN, "content-type": "application/json",
               "host": "gateway.example", "origin": "https://gateway.example", "x-forwarded-for": "203.0.113.7"}
    assert authorize(request(method="POST", path="/v1/responses", headers=headers, scheme="https")).status_code == 200
    assert authorize(request(path="/health", headers=headers, scheme="https")).status_code == 200


def test_even_external_proxy_rewriting_to_loopback_cannot_bypass_a_marker():
    observed = []
    async def app(scope, receive, send):
        observed.append(scope["client"][0])
        async def accepted(_):
            return JSONResponse({"ok": True})
        response = await server.require_remote_gateway_token(Request(scope, receive), accepted)
        await response(scope, receive, send)
    # Adversarial positive control: deliberately trust a forged forwarded IP
    # outside our launchers, then verify the gateway still refuses exemption.
    wrapped = ProxyHeadersMiddleware(app, trusted_hosts="*")
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=wrapped, client=("203.0.113.7", 12345)), base_url="http://localhost") as client:
            return await client.get("/health", headers={"x-forwarded-for": "127.0.0.1"})
    response = asyncio.run(run())
    assert observed == ["127.0.0.1"]
    assert response.status_code == 403


@pytest.mark.parametrize("remote_source", ["flag", "environment"])
def test_loopback_start_in_authenticated_mode_requires_a_strong_token(monkeypatch, remote_source):
    if remote_source == "environment":
        monkeypatch.setenv("ANTIGRAVITY_ALLOW_REMOTE", "1")
    with pytest.raises(SystemExit, match="GATEWAY_TOKEN"):
        cli.require_safe_gateway_host("127.0.0.1", allow_remote=remote_source == "flag")
    monkeypatch.setenv("ANTIGRAVITY_GATEWAY_TOKEN", TOKEN)
    cli.require_safe_gateway_host("127.0.0.1", allow_remote=remote_source == "flag")
    assert server.os.environ["ANTIGRAVITY_ALLOW_REMOTE"] == "1"


@pytest.mark.parametrize("service", [False, True])
def test_foreground_and_service_entrypoint_disable_proxy_interpretation(monkeypatch, service):
    launch = MagicMock()
    monkeypatch.setattr("uvicorn.run", launch)
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", "*")
    monkeypatch.setenv("UVICORN_PROXY_HEADERS", "true")
    command = service_command(51122, "127.0.0.1")[3:] if service else ["start", "--port", "51122"]
    monkeypatch.setattr(sys, "argv", ["codex-antigravity", *command])
    cli.main()
    launch.assert_called_once()
    assert launch.call_args.kwargs["proxy_headers"] is False
