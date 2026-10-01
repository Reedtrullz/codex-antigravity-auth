"""Synthetic callback/flow fixtures; no real OAuth/browser/provider access."""

import json
import sys
import threading
import time
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import urlopen
from unittest.mock import MagicMock

import pytest

from codex_antigravity_auth import cli, cli_setup, oauth


@pytest.fixture
def callback():
    server = cli.OAuthServer(("127.0.0.1", 0), cli.OAuthCallbackHandler)
    server.expected_state_id = "synthetic-state"
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01))
    thread.start()

    def request(path="/oauth-callback", **query):
        query.setdefault("state", oauth.encode_state({"id": "synthetic-state"}))
        url = f"http://127.0.0.1:{server.server_address[1]}{path}?{urlencode(query, doseq=True)}"
        try:
            response = urlopen(url, timeout=2)
        except HTTPError as exc:
            response = exc
        with response:
            return response.status, response.read().decode()

    try:
        yield server, request
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        assert not thread.is_alive()


@pytest.mark.parametrize("outcome", [{"code": "synthetic-code"}, {"error": "access_denied"}])
def test_validated_callback_is_terminal_once_and_browser_does_not_claim_completion(callback, outcome):
    server, request = callback
    status, body = request(**outcome)
    assert status == 200
    assert '<html lang="en">' in body and 'name="viewport"' in body
    assert "Authentication Successful" not in body
    assert server.auth_code == outcome.get("code")
    assert server.auth_error == outcome.get("error")
    if "code" in outcome:
        assert "Authorization received" in body and "token exchange" in body
    else:
        assert "Authorization declined" in body
    status, _ = request(code="replay-code")
    assert status == 409
    assert server.auth_code == outcome.get("code")
    assert server.auth_error == outcome.get("error")


@pytest.mark.parametrize("path,query", [
    ("/wrong", {"code": "synthetic-code"}),
    ("/oauth-callback", {"code": "synthetic-code", "state": "invalid"}),
    ("/oauth-callback", {"error": "access_denied", "state": "invalid"}),
    ("/oauth-callback", {"code": "synthetic-code", "state": ["a", "b"]}),
    ("/oauth-callback", {"code": ["a", "b"]}),
    ("/oauth-callback", {"code": ""}),
    ("/oauth-callback", {"code": "a", "error": "access_denied"}),
    ("/oauth-callback", {"error": ""}),
])
def test_invalid_callbacks_do_not_terminate_or_change_the_attempt(callback, path, query):
    server, request = callback
    status, _ = request(path, **query)
    assert status in (400, 404)
    assert server.auth_code is server.auth_error is server.auth_state is None
    # An invalid callback does not consume the valid attempt.
    assert request(error="access_denied")[0] == 200


@pytest.fixture
def flow(monkeypatch, tmp_path):
    class Server:
        auth_code = auth_state = auth_error = None
        closed = False
        calls = 0
        timeout = None

        def handle_request(self):
            self.calls += 1
            action(self)

        def server_close(self):
            self.closed = True

    action = lambda server: setattr(server, "auth_error", "access_denied")
    server = Server()
    monkeypatch.setattr(cli, "OAuthServer", lambda *args: server)
    monkeypatch.setattr(cli, "resolve_oauth_credentials", lambda **kwargs: ("synthetic-client", "synthetic-secret"))
    monkeypatch.setattr(cli, "authorize_antigravity", lambda **kwargs: {"url": "https://example.invalid/authorize", "state_id": "synthetic-state"})
    monkeypatch.setattr(oauth, "_pkce_verifier_store", {"synthetic-state": {"verifier": "synthetic-verifier", "createdAt": str(time.time())}})
    browser = MagicMock(return_value=True)
    monkeypatch.setattr(cli_setup.webbrowser, "open", browser)
    exchange = MagicMock(side_effect=RuntimeError("synthetic exchange stop"))
    monkeypatch.setattr(cli, "exchange_antigravity", exchange)
    update = MagicMock(side_effect=AssertionError("must not store a failed login"))
    monkeypatch.setattr(cli, "update_accounts", update)
    monkeypatch.setattr(cli, "run_configure_codex", MagicMock(side_effect=AssertionError("must not reconfigure a failed login")))
    return server, browser, exchange, update


def test_denial_terminates_promptly_and_discards_verifier(flow):
    server, browser, exchange, update = flow
    with pytest.raises(SystemExit, match="consent was denied"):
        cli.run_local_oauth_flow()
    assert server.calls == 1 and server.closed
    assert server.timeout <= 1
    assert "synthetic-state" not in oauth._pkce_verifier_store
    exchange.assert_not_called()
    update.assert_not_called()


@pytest.mark.parametrize("browser_result", [False, RuntimeError("synthetic browser failure")])
def test_browser_failure_prints_manual_guidance_then_handles_denial(flow, browser_result, capsys):
    server, browser, _, _ = flow
    if isinstance(browser_result, Exception):
        browser.side_effect = browser_result
    else:
        browser.return_value = browser_result
    with pytest.raises(SystemExit, match="consent was denied"):
        cli.run_local_oauth_flow()
    output = capsys.readouterr().out
    assert "https://example.invalid/authorize" in output
    assert "Open the URL above manually" in output
    assert server.closed


@pytest.mark.parametrize("command", [["login"], ["setup", "--write", "--no-input"], ["setup-google"]])
def test_no_browser_flag_propagates_without_writing_config_on_denial(monkeypatch, flow, tmp_path, command):
    server, browser, exchange, update = flow
    args = ["codex-antigravity", *command, "--no-browser"]
    config = tmp_path / "untouched.toml"
    if command[0] != "login":
        args += ["--config", str(config)]
    monkeypatch.setattr(sys, "argv", args)
    with pytest.raises(SystemExit, match="consent was denied"):
        cli.main()
    browser.assert_not_called()
    exchange.assert_not_called()
    update.assert_not_called()
    cli.run_configure_codex.assert_not_called()
    assert not config.exists() and server.closed


def test_cancellation_closes_listener_and_drops_pkce_state(flow, monkeypatch):
    server, _, exchange, _ = flow
    monkeypatch.setattr(server, "handle_request", MagicMock(side_effect=KeyboardInterrupt))
    with pytest.raises(SystemExit, match="cancelled"):
        cli.run_local_oauth_flow(no_browser=True)
    assert server.closed
    assert "synthetic-state" not in oauth._pkce_verifier_store
    exchange.assert_not_called()


def test_timeout_uses_monotonic_deadline_without_wall_clock(flow, monkeypatch):
    server, _, exchange, _ = flow
    ticks = iter([10.0, 611.0])
    monkeypatch.setattr(cli_setup, "time", SimpleNamespace(monotonic=lambda: next(ticks), time=lambda: (_ for _ in ()).throw(AssertionError("wall clock used"))))
    with pytest.raises(SystemExit, match="Timed out"):
        cli.run_local_oauth_flow(no_browser=True)
    assert server.closed and server.calls == 0
    assert "synthetic-state" not in oauth._pkce_verifier_store
    exchange.assert_not_called()


def test_code_callback_still_validates_pkce_and_closes_on_exchange_failure(flow, monkeypatch):
    server, _, exchange, _ = flow
    def receive():
        server.auth_code = "synthetic-code"
        server.auth_state = oauth.encode_state({"id": "synthetic-state"})
    monkeypatch.setattr(server, "handle_request", receive)
    with pytest.raises(RuntimeError, match="synthetic exchange stop"):
        cli.run_local_oauth_flow(no_browser=True)
    exchange.assert_called_once_with("synthetic-code", "synthetic-verifier")
    assert server.closed and "synthetic-state" not in oauth._pkce_verifier_store
