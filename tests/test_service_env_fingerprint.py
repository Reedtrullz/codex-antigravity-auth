"""Environment-reference drift checks using owned synthetic files only."""
import json
import os
from pathlib import Path
import sys

import pytest

from codex_antigravity_auth import service_drift as drift
from codex_antigravity_auth import service_manifest as manifest
from codex_antigravity_auth.secure_store import SecureStore


FIRST = b"API_KEY=fixture-secret-one\n"
SECOND = b"API_KEY=fixture-secret-two\n"


@pytest.fixture
def env_reference(tmp_path):
    path = tmp_path / "fixture.env"
    SecureStore().atomic_write_bytes(path, FIRST)
    return path


def rewrite_with_restored_metadata(path, content):
    before = path.stat()
    assert len(content) == before.st_size
    path.write_bytes(content)
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    after = path.stat()
    assert (after.st_size, after.st_mtime_ns) == (before.st_size, before.st_mtime_ns)


@pytest.mark.parametrize("at_limit", [False, True], ids=["small", "inspection-limit"])
def test_reference_fingerprint_detects_same_size_restored_mtime_edit(env_reference, at_limit):
    # At the cap, only the final environment value changes: hashing a prefix
    # would still miss the edit even after replacing the metadata-only receipt.
    prefix = b""
    if at_limit:
        prefix = b"#" + b"x" * (manifest.MAX_BYTES - len(FIRST) - 2) + b"\n"
    env_reference.write_bytes(prefix + FIRST)
    before = manifest.reference_stat({"opEnvFile": str(env_reference)})

    rewrite_with_restored_metadata(env_reference, prefix + SECOND)
    after = manifest.reference_stat({"opEnvFile": str(env_reference)})

    assert after != before, "Environment bytes changed despite identical size and mtime"


def test_service_inspection_reports_same_size_restored_mtime_reference_drift(
    env_reference, tmp_path, monkeypatch
):
    value = {
        "port": 51122, "host": "127.0.0.1", "unified": False,
        "clientHome": str(tmp_path / "client"), "stateHome": str(tmp_path / "state"),
        "executable": sys.executable, "module": manifest.MODULE,
        "packageVersion": "fixture-version", "opEnvFile": str(env_reference),
        "opEnvironment": None,
        "wrapper": [sys.executable, "run", "--env-file", str(env_reference), "--"],
    }
    definition = b"fixture service definition\n"
    record = manifest.new_manifest(value, "macos", manifest.digest(definition.decode()), "a" * 32)
    record["state"] = "applied"
    intent = tmp_path / "intent.json"
    SecureStore().atomic_write_text(intent, json.dumps(record))
    # Only the unrelated launch rendering/settings boundaries are substituted.
    # Manifest loading, environment capture and the drift comparison stay real.
    monkeypatch.setattr(drift, "manifest_path", lambda port: intent)
    monkeypatch.setattr(drift, "desired_settings", lambda *args, **kwargs: dict(value))
    monkeypatch.setattr(drift, "definition", lambda *args: definition)
    assert drift.inspect(51122, "macos", installed=False)["drift"] == []

    rewrite_with_restored_metadata(env_reference, SECOND)
    result = drift.inspect(51122, "macos", installed=False)

    assert result["drift"] == ["secret_reference_file_changed"]
    assert result["owned_ready"] is False
    for receipt in (record, result):
        assert "fixture-secret-one" not in json.dumps(receipt)
        assert "fixture-secret-two" not in json.dumps(receipt)


@pytest.mark.parametrize("kind", ["symlink", "symlink-parent", "directory", "hardlink", "oversized"])
def test_reference_fingerprint_rejects_unsafe_files(env_reference, tmp_path, kind):
    reference = tmp_path / "unsafe.env"
    if kind == "symlink":
        try:
            reference.symlink_to(env_reference)
        except (OSError, NotImplementedError) as exc:
            pytest.skip(f"Native symlink creation unavailable: {exc}")
    elif kind == "symlink-parent":
        alias = tmp_path / "alias"
        try:
            alias.symlink_to(tmp_path, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            pytest.skip(f"Native directory symlink creation unavailable: {exc}")
        reference = alias / env_reference.name
    elif kind == "directory":
        reference.mkdir(mode=0o700)
    elif kind == "hardlink":
        try:
            os.link(env_reference, reference)
        except (OSError, NotImplementedError) as exc:
            pytest.skip(f"Native hardlink creation unavailable: {exc}")
    else:
        SecureStore().atomic_write_bytes(reference, b"x" * (manifest.MAX_BYTES + 1))

    with pytest.raises((OSError, ValueError)):
        manifest.reference_stat({"opEnvFile": str(reference)})

    assert env_reference.read_bytes() == FIRST


def test_reference_fingerprint_rejects_regular_replacement_before_open(
    env_reference, tmp_path, monkeypatch
):
    replacement = tmp_path / "replacement.env"
    SecureStore().atomic_write_bytes(replacement, SECOND)
    before = env_reference.stat()
    os.utime(replacement, ns=(before.st_atime_ns, before.st_mtime_ns))
    # A normal reference is acceptable before its path identity changes.
    manifest.reference_stat({"opEnvFile": str(env_reference)})
    native_open = os.open
    replaced = False

    def swap_before_open(path, flags, *args, **kwargs):
        nonlocal replaced
        if not replaced and isinstance(path, (str, bytes, os.PathLike)):
            selected = Path(os.fsdecode(path))
            if selected == env_reference or selected == Path(env_reference.name):
                os.replace(replacement, env_reference)
                replaced = True
        return native_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(manifest.os, "open", swap_before_open)
    with pytest.raises((OSError, ValueError)):
        manifest.reference_stat({"opEnvFile": str(env_reference)})
    assert replaced, "The rejection must exercise the path replacement during open"


def test_reference_fingerprint_is_stable_and_does_not_copy_environment_contents(env_reference):
    value = {"opEnvFile": str(env_reference)}
    receipt = manifest.reference_stat(value)
    assert receipt == manifest.reference_stat(value)
    assert "API_KEY" not in json.dumps(receipt)
    assert "fixture-secret-one" not in json.dumps(receipt)


def test_missing_environment_reference_is_unavailable(tmp_path):
    with pytest.raises((OSError, ValueError)):
        manifest.reference_stat({"opEnvFile": str(tmp_path / "missing.env")})


def test_environment_id_without_file_has_no_file_fingerprint():
    assert manifest.reference_stat({"opEnvFile": None, "opEnvironment": "fixture-environment"}) is None
