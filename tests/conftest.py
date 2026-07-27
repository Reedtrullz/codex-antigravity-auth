import base64
import uuid
import time
import uuid
import time

import pytest


_TEST_STORAGE_KEY = base64.urlsafe_b64encode(b"\0" * 32).decode("ascii")


def _reject_system_keyring(*_args, **_kwargs):
    raise AssertionError("tests must not access the system keyring")


@pytest.fixture(autouse=True)
def _isolate_test_storage_from_system_keyring(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ANTIGRAVITY_STORAGE_KEY", _TEST_STORAGE_KEY)
    monkeypatch.setattr("keyring.get_password", _reject_system_keyring)
    monkeypatch.setattr("keyring.set_password", _reject_system_keyring)


@pytest.fixture(autouse=True)
def _disable_gateway_refresh_ahead(monkeypatch):
    monkeypatch.setattr(
        "codex_antigravity_auth.server.schedule_refresh_accounts_ahead",
        lambda *args, **kwargs: False,
        raising=False,
    )

import uuid
import time


def _legacy_transform_response(gemini_resp: dict, model: str) -> dict:
    """Test-only compatibility wrapper for tests that used transform_response."""
    from codex_antigravity_auth.google_transport import GoogleTransport
    from codex_antigravity_auth.response_protocol import response_from_result

    result = GoogleTransport(timeout=0).parse_response(gemini_resp)
    return response_from_result(
        result,
        response_id=result.provider_response_id or f"resp_{uuid.uuid4().hex[:12]}",
        model=model,
        created_at=int(time.time()),
    )


import uuid
import time


def _legacy_transform_response(gemini_resp: dict, model: str) -> dict:
    """Test-only compatibility wrapper for tests that used transform_response."""
    from codex_antigravity_auth.google_transport import GoogleTransport
    from codex_antigravity_auth.response_protocol import response_from_result

    result = GoogleTransport(timeout=0).parse_response(gemini_resp)
    return response_from_result(
        result,
        response_id=result.provider_response_id or f"resp_{uuid.uuid4().hex[:12]}",
        model=model,
        created_at=int(time.time()),
    )

