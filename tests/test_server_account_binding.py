import json
import os
from functools import partial
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from fake_upstream import test_client_with_peer as fixture_client_with_peer

from codex_antigravity_auth.account_binding import GATEWAY_INSTANCE, parse_account_binding_header, validate_binding_route
from codex_antigravity_auth.server import app


test_client_with_peer = partial(fixture_client_with_peer, app=app)
test_client_with_peer.__test__ = False


def inventory():
    return {
        "schemaVersion": 1,
        "gatewayInstance": GATEWAY_INSTANCE,
        "family": "gemini",
        "accounts": [{"accountRef": "acct_" + "a" * 12, "eligible": True, "tokenExpiresAt": 4102444800, "inFlight": 0, "exclusionReasons": []}],
        "inventorySha256": "b" * 64,
    }


def test_binding_endpoint_is_redacted_and_supported_route_only():
    with patch("codex_antigravity_auth.server.account_manager.binding_inventory", return_value=inventory()) as bound:
        response = TestClient(app).get("/v1/account-bindings?model=gemini-3.8-flash")
    assert response.status_code == 200
    body = response.json()
    assert "@" not in json.dumps(body)
    assert body["gatewayInstance"] == GATEWAY_INSTANCE
    bound.assert_called_once_with("gemini-3.8-flash")
    with patch("codex_antigravity_auth.server.account_manager.binding_inventory", side_effect=ValueError("account binding requires a Gemini route")):
        response = TestClient(app).get("/v1/account-bindings?model=claude-3.5-sonnet")
    assert response.status_code == 400


def test_binding_endpoint_honors_browser_host_and_remote_auth_boundaries():
    with patch("codex_antigravity_auth.server.account_manager.binding_inventory", return_value=inventory()) as bound:
        cross_site = TestClient(app).get(
            "/v1/account-bindings?model=gemini-3.8-flash",
            headers={"Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site"},
        )
    assert cross_site.status_code == 403
    bound.assert_not_called()

    with patch("codex_antigravity_auth.server.account_manager.binding_inventory", return_value=inventory()) as bound:
        bad_host = TestClient(app).get(
            "/v1/account-bindings?model=gemini-3.8-flash",
            headers={"Host": "attacker.example:51122", "Origin": "http://attacker.example:51122"},
        )
    assert bad_host.status_code == 403
    bound.assert_not_called()

    with patch("codex_antigravity_auth.server.all_provider_configs", return_value={}), patch.dict(
        os.environ, {}, clear=True
    ):
        remote = test_client_with_peer(("203.0.113.10", 50000)).get(
            "/v1/account-bindings?model=gemini-3.8-flash"
        )
    assert remote.status_code == 403


def test_binding_header_is_bounded_and_never_contains_identity():
    valid = {"schemaVersion": 1, "gatewayInstance": "a" * 32, "accountRef": "acct_" + "b" * 12, "inventorySha256": "c" * 64}
    assert parse_account_binding_header(json.dumps(valid)).accountRef == valid["accountRef"]
    with pytest.raises(ValueError):
        parse_account_binding_header(json.dumps({**valid, "email": "primary@example.invalid"}))
    with pytest.raises(ValueError):
        parse_account_binding_header("x" * 2049)


def test_binding_route_rejects_byok_claude_and_openai():
    validate_binding_route("antigravity")
    validate_binding_route("antigravity", "gemini")
    with pytest.raises(ValueError, match="native Antigravity Gemini"):
        validate_binding_route("antigravity", "claude")
    for route in ("byok", "claude", "openai", "unknown"):
        with pytest.raises(ValueError, match="native Antigravity Gemini"):
            validate_binding_route(route)


@pytest.mark.parametrize(
    "model,header,status",
    [
        ("gemini-3.8-flash", "not-json", 400),
        ("gemini-3.8-flash", "x" * 2049, 400),
        ("claude-3.5-sonnet", json.dumps({"schemaVersion": 1, "gatewayInstance": "a" * 32, "accountRef": "acct_" + "b" * 12, "inventorySha256": "c" * 64}), 400),
        ("deepseek:deepseek-chat", json.dumps({"schemaVersion": 1, "gatewayInstance": "a" * 32, "accountRef": "acct_" + "b" * 12, "inventorySha256": "c" * 64}), 400),
    ],
)
def test_bound_request_refuses_before_acquisition_or_refresh(model, header, status):
    body = {"model": model, "input": "fixture", "stream": False}
    with patch("codex_antigravity_auth.server.GoogleTransport") as transport, patch(
        "codex_antigravity_auth.server.schedule_refresh_accounts_ahead"
    ) as refresh, patch(
        "codex_antigravity_auth.server.account_manager.acquire_bound_account"
    ) as acquire, patch("codex_antigravity_auth.server.account_manager.acquire_account") as automatic:
        response = TestClient(app).post(
            "/v1/responses", json=body, headers={"X-Anti-Account-Binding": header}
        )
    assert response.status_code == status
    refresh.assert_not_called()
    acquire.assert_not_called()
    automatic.assert_not_called()
    transport.assert_not_called()
