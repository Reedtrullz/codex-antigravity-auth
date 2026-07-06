import pytest


@pytest.fixture(autouse=True)
def _disable_gateway_refresh_ahead(monkeypatch):
    monkeypatch.setattr(
        "codex_antigravity_auth.server.schedule_refresh_accounts_ahead",
        lambda *args, **kwargs: False,
        raising=False,
    )
