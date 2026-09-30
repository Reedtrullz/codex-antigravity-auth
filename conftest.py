"""Apply isolation to collection and every test tree, including packaged Anti."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "test_support"))
from _test_isolation import install, assert_no_violations
install()

import pytest


def _reject_system_keyring(*args, **kwargs):
    raise AssertionError("tests must not access the system keyring")


@pytest.fixture(autouse=True)
def isolated_test_state(monkeypatch, tmp_path):
    for name in ("HOME", "USERPROFILE", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "APPDATA", "LOCALAPPDATA"):
        monkeypatch.setenv(name, str(tmp_path))
    monkeypatch.setattr("keyring.get_password", _reject_system_keyring)
    monkeypatch.setattr("keyring.set_password", _reject_system_keyring)
    monkeypatch.setattr("keyring.delete_password", _reject_system_keyring)


def pytest_collection_finish(session):
    assert_no_violations()


@pytest.fixture(autouse=True)
def report_swallowed_isolation_errors():
    yield
    assert_no_violations()
