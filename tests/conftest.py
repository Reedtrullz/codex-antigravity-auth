import base64

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
