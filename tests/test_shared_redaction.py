"""One synthetic credential corpus for the gateway and standalone Anti."""

import json
import logging
import shutil
import subprocess
import sys

import pytest

from codex_antigravity_auth import accounts, observability, redaction, server
from codex_antigravity_auth.skills.anti.scripts.anti_lib import redaction as anti
from codex_antigravity_auth.skills.anti.scripts.anti_lib import secret_redaction as core


CORPUS = [
    ('{"password":"fixture-password"}', ["fixture-password"]),
    ('{"nested":{"credentials":{"private_key":"fixture-private"}}}', ["fixture-private"]),
    (json.dumps({"message": json.dumps({"password": "fixture-stringified"})}), ["fixture-stringified"]),
    ('failure: {"password":"fixture-escaped\\\"tail"}', ["fixture-escaped", "tail"]),
    ('failure: {"password":{"value":"fixture-compound"},"code":400}', ["fixture-compound"]),
    ('failure: {"message":"{\\"password\\":\\"fixture-nested\\"}"}', ["fixture-nested"]),
    ("failure: {'private_key': 'fixture-repr\\'tail'}", ["fixture-repr", "tail"]),
    ('{"pass\\u0077ord":"fixture-unicode-key"}', ["fixture-unicode-key"]),
    ('{"password":987654321,"code":400}', ["987654321"]),
    ("password=fixture-form&status=bad", ["fixture-form"]),
    ('password="fixture first second"', ["fixture", "first", "second"]),
    ('private_key="-----BEGIN PRIVATE KEY-----\nfixture-key-body\n-----END PRIVATE KEY-----"', ["fixture-key-body", "PRIVATE KEY"]),
    ('password="fixture first\\\" second"', ["fixture", "first", "second"]),
    ('password="fixture first\nsecond', ["fixture", "first", "second"]),
    ("database_password=fixture-compound-secret", ["fixture-compound-secret"]),
    ("provider_api_key=fixture-provider-secret", ["fixture-provider-secret"]),
    ("password: fixture-unquoted", ["fixture-unquoted"]),
    ("Authorization: Basic Zml4dHVyZTpwYXNzd29yZA==\nstatus=401", ["Zml4dHVyZTpwYXNzd29yZA=="]),
    ("Authorization: Bearer fixture-token\nstatus=403", ["fixture-token"]),
    ("https://fixture-user:fixture-pass@example.invalid/v1", ["fixture-user", "fixture-pass"]),
    ("https://fixture%40user:fixture%3Apass@example.invalid/v1", ["fixture%40user", "fixture%3Apass"]),
    ("provider error ya29.fixture-google-token", ["ya29.fixture-google-token"]),
    ("provider error sk-or-v1-fixtureabcdefghijklmnop", ["sk-or-v1-fixtureabcdefghijklmnop"]),
    ("prefixsk-fixtureabcdefghijklmnop", ["sk-fixtureabcdefghijklmnop"]),
    ("first line\nX-Private-Token: fixture-header\nlast line", ["fixture-header"]),
    ('broken: {"password":"fixture-unclosed', ["fixture-unclosed"]),
    ("https://example.invalid?api_key=fixture-query", ["fixture-query"]),
]


@pytest.mark.parametrize("text,forbidden", CORPUS)
def test_shared_text_corpus(text, forbidden):
    for sanitizer in (redaction.redact_secret_text, anti.redact_sensitive_text):
        rendered = sanitizer(text)
        assert all(secret not in rendered for secret in forbidden)
        assert "redacted" in rendered.lower()


@pytest.mark.parametrize("text", [
    '{"code": 400, "status": "invalid_request", "password_count": 2}',
    '{"code": "200", "access_token_cached": true}',
    "error code: 200", "user_models = load()", "user_abc123 = value",
    'request_id: str = "req-abc"', "https://example.invalid/a@b/path",
])
def test_benign_metadata_status_and_source_examples_are_preserved(text):
    assert redaction.redact_secret_text(text) == text
    assert anti.redact_sensitive_text(text) == text


def test_structured_policy_preserves_telemetry_ids_and_bounds_private_values():
    value = {"password": "fixture-password", "nested": [{"private-key": "fixture-key"}],
             "code": 400, "accessTokenExpiresAt": 123, "token_count": 7, "request_id": "fixture-request"}
    gateway = redaction.redact_secrets(value)
    standalone = anti.sanitize_json(value)
    assert gateway["request_id"] == "fixture-request"
    assert standalone["request_id"] == "<redacted>"
    for rendered in (gateway, standalone):
        assert rendered["code"] == 400 and rendered["token_count"] == 7
        assert rendered["accessTokenExpiresAt"] == 123
        assert "fixture-password" not in json.dumps(rendered) and "fixture-key" not in json.dumps(rendered)
    assert value["password"] == "fixture-password"


@pytest.mark.parametrize("text", [
    '{"request\\u005fid":"req-fixture-123"}',
    '{"nested":{"user\\u005fid":"req-fixture-123"}}',
    json.dumps({"message": '{"request\\u005fid":"req-fixture-123"}'}),
])
def test_standalone_decodes_private_identifier_keys_without_changing_gateway_ids(text):
    assert "req-fixture-123" not in anti.redact_sensitive_text(text)
    assert "req-fixture-123" in redaction.redact_secret_text(text)


def test_input_limits_replace_oversize_deep_cyclic_and_wide_data():
    oversized = "x" * (core.MAX_TEXT_CHARS + 1) + " fixture-oversized-secret"
    for fn in (redaction.redact_secret_text, anti.redact_sensitive_text):
        assert len(fn(oversized)) < 200
    deep = {"value": "fixture-deep-secret"}
    for _ in range(core.MAX_DEPTH + 1):
        deep = {"nested": deep}
    cycle = []
    cycle.append(cycle)
    for value in (deep, cycle, ["fixture-wide-secret"] * (core.MAX_ITEMS + 1)):
        for fn in (redaction.redact_secrets, anti.sanitize_json):
            rendered = fn(value)
            assert isinstance(rendered, str) and "input limit" in rendered
            assert "fixture-" not in rendered
    assert "fixture-deep-secret" not in redaction.redact_secret_text(json.dumps(deep))


def test_copied_standalone_helper_uses_same_corpus_without_site_packages(tmp_path):
    destination = tmp_path / "anti_lib"
    shutil.copytree(core.__file__.rsplit("/", 1)[0], destination, ignore=shutil.ignore_patterns("__pycache__"))
    code = """
import json, sys
sys.path.insert(0, sys.argv[1])
from anti_lib.redaction import redact_sensitive_text
corpus = json.load(sys.stdin)
print(json.dumps([redact_sensitive_text(text) for text, _ in corpus]))
"""
    result = subprocess.run([sys.executable, "-I", "-S", "-c", code, str(tmp_path)],
                            input=json.dumps(CORPUS), text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
    outputs = json.loads(result.stdout)
    assert outputs == [anti.redact_sensitive_text(text) for text, _ in CORPUS]
    for output, (_, forbidden) in zip(outputs, CORPUS):
        assert all(secret not in output for secret in forbidden)


def test_mocked_errors_and_captured_account_logs_use_shared_policy(monkeypatch, tmp_path, caplog):
    error = "failed: password=fixture-pass https://fixture-user:fixture-urlpass@example.invalid ya29.fixture-token"
    forbidden = ["fixture-pass", "fixture-user", "fixture-urlpass", "ya29.fixture-token"]
    detail = server.openai_failure_detail("gpt-5.6", error)
    assert all(secret not in json.dumps(detail) for secret in forbidden)
    monkeypatch.setattr(observability, "get_codex_home", lambda: tmp_path)
    observability.write_request_record({"status": "failed", "error": error})
    assert all(secret not in observability.request_log_path().read_text() for secret in forbidden)
    monkeypatch.setattr(accounts, "refresh_access_token", lambda _: {"access_token": "synthetic-access", "expires_in": 3600})
    def failed_discovery(_):
        raise RuntimeError(error)
    monkeypatch.setattr("codex_antigravity_auth.oauth.discover_project_id", failed_discovery)
    with caplog.at_level(logging.WARNING, logger=accounts.__name__):
        assert accounts._apply_token_refresh({"email": "fixture@example.invalid"}, "synthetic-refresh")
    assert "Project discovery failed" in caplog.text
    assert all(secret not in caplog.text for secret in forbidden)
