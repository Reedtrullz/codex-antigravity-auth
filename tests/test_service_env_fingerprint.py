"""Environment-reference drift checks using owned synthetic files only."""
import hashlib
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


@pytest.fixture
def service_settings(env_reference, tmp_path):
    return {
        "port": 51122, "host": "127.0.0.1", "unified": False,
        "clientHome": str(tmp_path / "client"), "stateHome": str(tmp_path / "state"),
        "executable": sys.executable, "module": manifest.MODULE,
        "packageVersion": "fixture-version", "opEnvFile": str(env_reference),
        "opEnvironment": None,
        "wrapper": [sys.executable, "run", "--env-file", str(env_reference), "--"],
    }


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
    env_reference, tmp_path, monkeypatch, service_settings
):
    value = service_settings
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
    monkeypatch.setattr(manifest.os, "supports_dir_fd", {*os.supports_dir_fd, swap_before_open})
    with pytest.raises((OSError, ValueError)):
        manifest.reference_stat({"opEnvFile": str(env_reference)})
    assert replaced, "The rejection must exercise the path replacement during open"


def test_reference_fingerprint_rejects_content_change_during_capture(env_reference, monkeypatch):
    native_fdopen = os.fdopen
    captured = []

    class ChangingRead:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.stream.close()

        def fileno(self):
            return self.stream.fileno()

        def read(self, count):
            raw = self.stream.read(count)
            captured.append(True)
            env_reference.write_bytes(FIRST + b"# concurrent edit\n")
            return raw

    monkeypatch.setattr(manifest.os, "fdopen", lambda *args, **kwargs: ChangingRead(native_fdopen(*args, **kwargs)))
    with pytest.raises((OSError, ValueError)):
        manifest.reference_stat({"opEnvFile": str(env_reference)})
    assert captured, "The rejection must exercise a changing read"


def test_reference_fingerprint_is_stable_and_does_not_copy_environment_contents(env_reference):
    value = {"opEnvFile": str(env_reference)}
    receipt = manifest.reference_stat(value)
    assert receipt == {
        "size": len(FIRST), "mtimeNs": env_reference.stat().st_mtime_ns,
        "sha256": hashlib.sha256(FIRST).hexdigest(),
    }
    assert receipt == manifest.reference_stat(value)
    assert "API_KEY" not in json.dumps(receipt)
    assert "fixture-secret-one" not in json.dumps(receipt)


def test_missing_environment_reference_is_unavailable(tmp_path):
    with pytest.raises((OSError, ValueError)):
        manifest.reference_stat({"opEnvFile": str(tmp_path / "missing.env")})


def test_environment_id_without_file_has_no_file_fingerprint():
    assert manifest.reference_stat({"opEnvFile": None, "opEnvironment": "fixture-environment"}) is None


def test_manifest_round_trip_preserves_environment_byte_fingerprint(service_settings, tmp_path):
    record = manifest.new_manifest(service_settings, "macos", "a" * 64, "b" * 32)
    path = tmp_path / "intent.json"
    manifest.write_json(path, record)
    loaded = manifest.load(path, port=51122, platform="macos")
    assert loaded == record
    assert loaded["referenceStat"]["sha256"] == hashlib.sha256(FIRST).hexdigest()


@pytest.mark.parametrize("reference", [
    None, [],
    {"size": True, "mtimeNs": 0},
    {"size": -1, "mtimeNs": 0},
    {"size": manifest.MAX_BYTES + 1, "mtimeNs": 0},
    {"size": 0, "mtimeNs": -1},
    {"size": 0, "mtimeNs": 0.0},
    {"size": 0, "mtimeNs": 0, "sha256": None},
    {"size": 0, "mtimeNs": 0, "sha256": "A" * 64},
    {"size": 0, "mtimeNs": 0, "sha256": "a" * 63},
    {"size": 0, "sha256": "a" * 64},
    {"size": 0, "mtimeNs": 0, "sha256": "a" * 64, "contents": "fixture-secret-one"},
])
def test_manifest_rejects_malformed_environment_fingerprint(service_settings, reference):
    record = manifest.new_manifest(service_settings, "macos", "a" * 64, "b" * 32)
    record["referenceStat"] = reference
    with pytest.raises(ValueError):
        manifest.validate(record)


def test_legacy_reference_metadata_is_readable_but_reports_unverified_drift(
    service_settings, tmp_path, monkeypatch
):
    definition = b"fixture service definition\n"
    record = manifest.new_manifest(service_settings, "macos", manifest.digest(definition.decode()), "a" * 32)
    record["referenceStat"].pop("sha256")
    record["state"] = "applied"
    path = tmp_path / "legacy-intent.json"
    manifest.write_json(path, record)
    assert manifest.load(path, port=51122, platform="macos") == record
    monkeypatch.setattr(drift, "manifest_path", lambda port: path)
    monkeypatch.setattr(drift, "desired_settings", lambda *args, **kwargs: dict(service_settings))
    monkeypatch.setattr(drift, "definition", lambda *args: definition)
    result = drift.inspect(51122, "macos", installed=False)
    assert result["drift"] == ["secret_reference_file_changed"]
    assert result["owned_ready"] is False
