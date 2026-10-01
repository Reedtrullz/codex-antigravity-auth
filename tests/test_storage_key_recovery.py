"""All keyring data, keys, credential payloads and roots are synthetic fixtures."""
import base64
import json
import os
from pathlib import Path
from unittest.mock import Mock

from cryptography.fernet import Fernet
import pytest

from codex_antigravity_auth import byok, cli, storage, storage_keys as keys, storage_recovery as recovery

A = base64.urlsafe_b64encode(b"\x01" * 32).decode()
B = base64.urlsafe_b64encode(b"\x02" * 32).decode()
C = base64.urlsafe_b64encode(b"\x03" * 32).decode()
D = base64.urlsafe_b64encode(b"\x04" * 32).decode()


@pytest.fixture
def vault(monkeypatch, tmp_path):
    root = tmp_path / "state"
    monkeypatch.setenv("ANTIGRAVITY_STATE_HOME", str(root))
    monkeypatch.delenv("ANTIGRAVITY_STORAGE_KEY", raising=False)
    monkeypatch.setenv("FIXTURE_NEW_KEY", C)
    monkeypatch.setenv("FIXTURE_WRONG_KEY", D)
    monkeypatch.setenv("FIXTURE_OLD_KEY", A)
    state = {"available": True, "entries": {}, "writes": []}
    def get(service, name):
        assert service == storage.KEYRING_SERVICE_NAME
        if not state["available"]:
            raise RuntimeError("synthetic keyring outage")
        return state["entries"].get(name)
    def set_key(service, name, value):
        assert service == storage.KEYRING_SERVICE_NAME
        if not state["available"]:
            raise RuntimeError("synthetic keyring outage")
        state["writes"].append(name)
        state["entries"][name] = value
    monkeypatch.setattr(storage.keyring, "get_password", get)
    monkeypatch.setattr(storage.keyring, "set_password", set_key)
    monkeypatch.setattr(storage.keyring, "delete_password", lambda *a: pytest.fail("old keyring keys must not be deleted"))
    return root, state, tmp_path


def seed(vault, *, split=False, plaintext_provider=False):
    root, ring, _ = vault
    root.mkdir(mode=0o700, exist_ok=True)
    (root / storage.FALLBACK_KEY_FILE).write_text(A)
    ring["entries"][storage.KEYRING_KEY_NAME] = B if split else A
    bodies = {"accounts": b'{"accounts":[{"email":"fixture@example.invalid","accessToken":"fixture-access-token"}],"future":{"schemaVersion":987}}',
              "providers": b'{"providers":{"fixture":{"apiKey":"fixture-provider-secret","futureField":[1,2,3]}}}'}
    raw = {}
    for name, path in keys.managed_paths().items():
        key = B if split and name == "providers" else A
        raw[name] = (bodies[name] if plaintext_provider and name == "providers" else
                     Fernet(key.encode()).encrypt(bodies[name]))
        path.write_bytes(raw[name])
    return bodies, raw


def tree(root):
    return {str(path.relative_to(root)): (path.read_bytes(), path.stat().st_mode, path.stat().st_mtime_ns)
            for path in root.rglob("*") if path.is_file()} if root.exists() else {}


def test_outage_then_recovery_keeps_recorded_fallback_identity(vault, monkeypatch):
    root, ring, _ = vault
    ring["available"] = False
    generated = Mock(return_value=A.encode())
    monkeypatch.setattr(Fernet, "generate_key", generated)
    assert storage._get_encryption_key() == A
    assert keys.selection()["backend"] == "file"
    storage.save_accounts({"accounts": [{"email": "fixture@example.invalid"}]})
    before = keys.managed_paths()["accounts"].read_bytes()
    ring["available"] = True
    ring["entries"][storage.KEYRING_KEY_NAME] = B
    assert storage._get_encryption_key() == storage._peek_encryption_key() == A
    assert keys.managed_paths()["accounts"].read_bytes() == before
    assert keys.key_diagnostics()["conflictingIdentities"] is True
    assert keys.key_diagnostics()["ready"] is True
    generated.assert_called_once()


def test_existing_fallback_is_reused_when_empty_keyring_returns(vault, monkeypatch):
    root, ring, _ = vault
    root.mkdir()
    (root / storage.FALLBACK_KEY_FILE).write_text(A)
    monkeypatch.setattr(Fernet, "generate_key", lambda: pytest.fail("existing key must be reused"))
    assert storage._get_encryption_key() == A
    assert ring["writes"] == []
    assert keys.selection()["backend"] == "file"


def test_unrecorded_conflict_is_diagnosed_without_creating_key_or_mutating_bytes(vault, monkeypatch):
    root, ring, _ = vault
    _bodies, raw = seed(vault, split=True)
    before = tree(root)
    monkeypatch.setattr(Fernet, "generate_key", lambda: pytest.fail("no replacement key"))
    report = keys.key_diagnostics()
    assert not report["ready"] and report["error_class"] == "key_conflict"
    assert report["conflictingIdentities"] is True
    assert tree(root) == before
    assert all(value not in json.dumps(report) for value in (A, B, "fixture-access-token", "fixture-provider-secret"))
    with pytest.raises(keys.StorageKeyError, match="conflict"):
        storage._get_encryption_key()
    assert not (root / keys.SELECTION_FILE).exists()
    assert ring["writes"] == []
    assert (root / storage.FALLBACK_KEY_FILE).read_text() == A
    assert all(path.read_bytes() == raw[name] for name, path in keys.managed_paths().items())


def test_missing_key_never_generates_replacement_for_existing_ciphertext(vault, monkeypatch):
    root, ring, _ = vault
    root.mkdir()
    keys.managed_paths()["accounts"].write_bytes(Fernet(A.encode()).encrypt(b'{"accounts":[]}'))
    ring["available"] = False
    monkeypatch.setattr(Fernet, "generate_key", lambda: pytest.fail("ciphertext requires its original key"))
    with pytest.raises(keys.StorageKeyError, match="decrypt"):
        storage._get_encryption_key()
    assert not (root / storage.FALLBACK_KEY_FILE).exists()
    assert not (root / keys.SELECTION_FILE).exists()


def test_diagnostics_missing_root_is_read_only(vault):
    root, ring, _ = vault
    report = keys.key_diagnostics()
    assert not report["ready"] and not root.exists()
    assert ring["writes"] == []


def test_recorded_key_mismatch_refuses_even_explicit_environment_override(vault, monkeypatch):
    root, _ring, _ = vault
    seed(vault)
    keys.write_selection(A, "file")
    monkeypatch.setenv("ANTIGRAVITY_STORAGE_KEY", B)
    before = tree(root)
    with pytest.raises(keys.StorageKeyError, match="differs"):
        storage._peek_encryption_key()
    assert keys.key_diagnostics()["error_class"] == "key_mismatch"
    assert tree(root) == before


@pytest.mark.parametrize("backend", ["file", "keyring"])
def test_backup_first_reencryption_preserves_raw_payloads_and_old_keys(vault, backend):
    root, ring, tmp = vault
    bodies, old_raw = seed(vault, split=True)
    output = tmp / "before.agbackup"
    before, old_ring = tree(root), dict(ring["entries"])
    plan = recovery.reencrypt("FIXTURE_NEW_KEY", backend, output)
    assert plan["write"] is False and tree(root) == before and not output.exists()
    result = recovery.reencrypt("FIXTURE_NEW_KEY", backend, output, write=True)
    assert result["completed"] and result["cleartextTokensWritten"] is False
    assert not keys.transition_pending()
    assert storage._peek_encryption_key() == C
    for name, path in keys.managed_paths().items():
        assert Fernet(C.encode()).decrypt(path.read_bytes()) == bodies[name]
    payload, backed = recovery._read_backup(output, C)
    assert all(backed[name] == data for name, data in old_raw.items())
    assert payload["keys"] == {keys.key_id(A): A, keys.key_id(B): B}
    assert all(ring["entries"][name] == value for name, value in old_ring.items())
    disk = output.read_bytes()
    assert all(value.encode() not in disk for value in (A, B, C, "fixture-access-token", "fixture-provider-secret"))
    assert json.loads(disk)["version"] == 1


@pytest.mark.parametrize("plaintext_provider", [False, True])
def test_interrupted_transition_blocks_normal_access_and_recovers_from_backup(vault, monkeypatch, plaintext_provider):
    root, _ring, tmp = vault
    bodies, old_raw = seed(vault, split=True, plaintext_provider=plaintext_provider)
    output = tmp / "recovery.agbackup"
    original_write = recovery.SecureStore._atomic_write_bytes_unlocked
    failed = []
    def fail_provider(self, path, value, **kwargs):
        if path == keys.managed_paths()["providers"] and not failed:
            failed.append(True)
            raise OSError("synthetic interruption after account publication")
        return original_write(self, path, value, **kwargs)
    monkeypatch.setattr(recovery.SecureStore, "_atomic_write_bytes_unlocked", fail_provider)
    with pytest.raises(keys.StorageKeyError, match="interrupted"):
        recovery.reencrypt("FIXTURE_NEW_KEY", "file", output, write=True)
    assert keys.transition_pending() and output.is_file()
    _payload, backed = recovery._read_backup(output, C)
    assert all(backed[name] == value for name, value in old_raw.items())
    for operation in (storage.load_accounts, storage.load_accounts_read_only, byok.load_provider_config_read_only,
                      lambda: storage.save_accounts({"accounts": []})):
        with pytest.raises((keys.StorageKeyError, RuntimeError), match="transition"):
            operation()
    before = tree(root)
    plan = recovery.restore(output, "FIXTURE_NEW_KEY", "file", None)
    assert plan["write"] is False and plan["recoveryRequired"] is True and tree(root) == before
    safety = tmp / "safety.agbackup"
    assert recovery.restore(output, "FIXTURE_NEW_KEY", "file", safety, write=True)["completed"]
    assert not keys.transition_pending() and safety.exists() and output.exists()
    for name, path in keys.managed_paths().items():
        assert Fernet(C.encode()).decrypt(path.read_bytes()) == bodies[name]
    assert storage._peek_encryption_key() == C


def test_wrong_backup_key_and_tampering_fail_before_any_restore_mutation(vault):
    root, ring, tmp = vault
    seed(vault)
    output = tmp / "backup.agbackup"
    recovery.backup(output, "FIXTURE_NEW_KEY", write=True)
    before, writes = tree(root), list(ring["writes"])
    safety = tmp / "not-created.agbackup"
    with pytest.raises(keys.StorageKeyError, match="match"):
        recovery.restore(output, "FIXTURE_WRONG_KEY", "file", safety, write=True)
    assert tree(root) == before and ring["writes"] == writes and not safety.exists()
    envelope = json.loads(output.read_bytes())
    envelope["payload"] = envelope["payload"][:-10] + "x" * 10
    output.write_text(json.dumps(envelope))
    with pytest.raises(keys.StorageKeyError, match="authentication"):
        recovery.restore(output, "FIXTURE_NEW_KEY", "file", safety, write=True)
    assert tree(root) == before and not safety.exists()


def test_portable_backup_contains_needed_keys_and_restores_into_new_root(vault, monkeypatch):
    _root, _ring, tmp = vault
    bodies, _raw = seed(vault, split=True)
    output = tmp / "portable.agbackup"
    recovery.backup(output, "FIXTURE_NEW_KEY", write=True)
    new_root = tmp / "new-state"
    monkeypatch.setenv("ANTIGRAVITY_STATE_HOME", str(new_root))
    monkeypatch.setattr(storage.keyring, "get_password", lambda *a: None)
    assert recovery.restore(output, "FIXTURE_NEW_KEY", "file", None)["replacesExistingState"] is False
    assert not new_root.exists()
    recovery.restore(output, "FIXTURE_NEW_KEY", "file", tmp / "empty-safety.agbackup", write=True)
    for name, path in keys.managed_paths().items():
        assert Fernet(C.encode()).decrypt(path.read_bytes()) == bodies[name]
    assert storage._peek_encryption_key() == C


def test_new_keyring_slots_do_not_replace_legacy_key(vault):
    _root, ring, tmp = vault
    seed(vault)
    old = dict(ring["entries"])
    recovery.reencrypt("FIXTURE_NEW_KEY", "keyring", tmp / "backup.agbackup", write=True)
    assert all(ring["entries"][name] == value for name, value in old.items())
    assert keys.ring_name(keys.key_id(C)) in ring["entries"]
    assert keys.selection()["slot"] == "id"


def test_existing_backup_and_reserved_paths_are_never_overwritten(vault):
    root, _ring, tmp = vault
    seed(vault)
    path = tmp / "existing"
    path.write_bytes(b"synthetic preserved")
    with pytest.raises(keys.StorageKeyError, match="exists"):
        recovery.reencrypt("FIXTURE_NEW_KEY", "file", path, write=True)
    assert path.read_bytes() == b"synthetic preserved"
    with pytest.raises(keys.StorageKeyError, match="managed"):
        recovery.backup(root / keys.TRANSITION_FILE, "FIXTURE_NEW_KEY", write=True)
    assert not keys.transition_pending()


def test_cli_diagnostics_and_dry_run_never_emit_credentials(vault, monkeypatch, capsys):
    root, _ring, tmp = vault
    seed(vault)
    before = tree(root)
    monkeypatch.setattr("sys.argv", ["codex-antigravity", "storage", "keys"])
    cli.main()
    report = capsys.readouterr().out
    assert A not in report and "fixture-access-token" not in report
    output = tmp / "dry-run.agbackup"
    monkeypatch.setattr("sys.argv", ["codex-antigravity", "storage", "reencrypt", "--key-env", "FIXTURE_NEW_KEY", "--backup", str(output)])
    cli.main()
    report = capsys.readouterr().out
    assert all(value not in report for value in (A, B, C, "fixture-provider-secret"))
    assert tree(root) == before and not output.exists()


def test_recorded_environment_key_keeps_no_keyring_fast_path(vault, monkeypatch):
    root, _ring, _ = vault
    monkeypatch.setenv("ANTIGRAVITY_STORAGE_KEY", A)
    get = Mock(return_value=None)
    monkeypatch.setattr(storage.keyring, "get_password", get)
    monkeypatch.setattr(Fernet, "generate_key", lambda: pytest.fail("no generated key"))
    assert storage._get_encryption_key() == A
    assert keys.selection()["backend"] == "environment"
    get.assert_called_once_with(storage.KEYRING_SERVICE_NAME, storage.KEYRING_KEY_NAME)
    get.reset_mock()
    get.side_effect = AssertionError("recorded environment selection must not inspect keyring")
    assert storage._get_encryption_key() == storage._peek_encryption_key() == A
    monkeypatch.setenv("ANTIGRAVITY_STORAGE_KEY", B)
    with pytest.raises(keys.StorageKeyError, match="differs"):
        storage._get_encryption_key()
    monkeypatch.delenv("ANTIGRAVITY_STORAGE_KEY")
    with pytest.raises(keys.StorageKeyError, match="environment key"):
        storage._get_encryption_key()
    get.assert_not_called()
    assert not (root / storage.FALLBACK_KEY_FILE).exists()


@pytest.mark.parametrize("recorded", [False, True])
def test_ordinary_file_key_use_repairs_permissions_but_inspection_does_not(vault, recorded):
    root, _ring, _ = vault
    root.mkdir()
    path = root / storage.FALLBACK_KEY_FILE
    path.write_text(A)
    if recorded:
        keys.write_selection(A, "file")
    path.chmod(0o644)
    before = tree(root)
    assert keys.key_diagnostics()["selectedBackend"] == "file"
    assert storage._peek_encryption_key() == A
    assert tree(root) == before
    assert storage._get_encryption_key() == A
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600
    assert path.read_text() == A
    assert keys.selection() == {"schemaVersion": 1, "backend": "file", "keyId": keys.key_id(A), "slot": None}


@pytest.mark.parametrize("change", ["protection_failure", "identity_change"])
def test_file_key_is_not_returned_when_protection_fails_or_identity_changes(vault, monkeypatch, change):
    root, _ring, _ = vault
    root.mkdir()
    path = root / storage.FALLBACK_KEY_FILE
    path.write_text(A)
    def protect(target):
        assert target == path
        if change == "protection_failure":
            raise OSError("synthetic permission failure")
        target.write_text(B)
    monkeypatch.setattr(storage, "_ensure_private_file", protect)
    with pytest.raises(keys.StorageKeyError) as error:
        storage._get_encryption_key()
    assert error.value.code == ("unsafe_key_file" if change == "protection_failure" else "state_changed")
    assert not (root / keys.SELECTION_FILE).exists()


@pytest.mark.parametrize("environment", [A, B, C])
def test_environment_key_cannot_bypass_unrecorded_backend_conflict(vault, monkeypatch, environment):
    root, ring, _ = vault
    _bodies, raw = seed(vault, split=True)
    monkeypatch.setenv("ANTIGRAVITY_STORAGE_KEY", environment)
    before = tree(root)
    for operation in (storage._peek_encryption_key, storage._get_encryption_key):
        with pytest.raises(keys.StorageKeyError) as error:
            operation()
        assert error.value.code == "key_conflict"
    report = keys.key_diagnostics()
    assert report["error_class"] == "key_conflict" and report["ready"] is False
    assert not (root / keys.SELECTION_FILE).exists() and ring["writes"] == []
    # Only the initialization lock may be created by the ordinary-use attempt.
    assert all(tree(root)[name] == snapshot for name, snapshot in before.items())
    assert all(path.read_bytes() == raw[name] for name, path in keys.managed_paths().items())
    assert all(key not in json.dumps(report) for key in (A, B, C))


@pytest.mark.parametrize("selected_state", ["available", "missing", "mismatched", "environment", "uninitialized"])
def test_doctor_reports_selected_backend_without_creating_or_repairing_keys(vault, monkeypatch, capsys, selected_state):
    root, ring, tmp = vault
    config = tmp / "config.toml"
    config.write_text(cli.render_codex_config_snippet(
        model="claude-sonnet-4-6", provider_id="antigravity", provider_name="Fixture",
        base_url="http://localhost:51122/v1", activate=True,
    ))
    if selected_state in {"available", "missing", "mismatched"}:
        seed(vault)
        keys.write_selection(A, "file")
        ring["entries"][storage.KEYRING_KEY_NAME] = B
        key_path = root / storage.FALLBACK_KEY_FILE
        if selected_state == "missing":
            key_path.unlink()
        elif selected_state == "mismatched":
            key_path.write_text(C)
        else:
            key_path.chmod(0o644)
    elif selected_state == "environment":
        monkeypatch.setenv("ANTIGRAVITY_STORAGE_KEY", A)
    monkeypatch.setattr(cli, "_diagnostic_all_provider_configs", lambda: {"fixture": {"baseUrl": "https://example.invalid/v1", "models": []}})
    monkeypatch.setattr(cli, "provider_key_status", lambda *a, **kw: "key OK")
    monkeypatch.setattr(cli, "version_check_result", lambda: {"status": "skip", "detail": "synthetic test"})
    before = tree(root)
    result = cli.run_doctor(byok_only=True, config=str(config))
    output = capsys.readouterr().out
    if selected_state in {"available", "environment"}:
        assert result is True
        backend = "file" if selected_state == "available" else "environment"
        assert f"Token Storage Encryption: AVAILABLE (selected {backend} key)" in output
    elif selected_state == "uninitialized":
        assert "Token Storage Encryption: NOT INITIALIZED" in output
    else:
        assert result is False
        code = "key_unavailable" if selected_state == "missing" else "key_mismatch"
        assert f"Token Storage Encryption: UNAVAILABLE ({code})" in output
    assert "OS Keyring Integrated" not in output and "selected OS keyring" not in output
    assert all(key not in output for key in (A, B, C))
    assert tree(root) == before and ring["writes"] == []


def test_wrong_environment_key_cannot_bootstrap_over_existing_ciphertext(vault, monkeypatch):
    root, _ring, _ = vault
    _bodies, raw = seed(vault)
    monkeypatch.setenv("ANTIGRAVITY_STORAGE_KEY", B)
    before = tree(root)
    assert keys.key_diagnostics()["ready"] is False
    assert tree(root) == before
    with pytest.raises(keys.StorageKeyError, match="decrypt"):
        storage._get_encryption_key()
    assert not (root / keys.SELECTION_FILE).exists()
    assert all(path.read_bytes() == raw[name] for name, path in keys.managed_paths().items())


@pytest.mark.parametrize("kind", ["version", "checksum", "traversal", "key_material"])
def test_authenticated_but_invalid_backup_manifest_is_rejected(vault, kind):
    root, _ring, tmp = vault
    seed(vault)
    output = tmp / "backup.agbackup"
    recovery.backup(output, "FIXTURE_NEW_KEY", write=True)
    envelope = json.loads(output.read_bytes())
    payload = json.loads(Fernet(C.encode()).decrypt(envelope["payload"].encode()))
    if kind == "version":
        payload["schemaVersion"] = 2
    elif kind == "checksum":
        payload["files"]["accounts"]["sha256"] = "0" * 64
    elif kind == "traversal":
        payload["files"]["../../outside"] = payload["files"]["accounts"]
    else:
        payload["keys"][keys.key_id(A)] = B
    envelope["payload"] = Fernet(C.encode()).encrypt(json.dumps(payload).encode()).decode()
    output.write_text(json.dumps(envelope))
    before = tree(root)
    with pytest.raises(keys.StorageKeyError):
        recovery.restore(output, "FIXTURE_NEW_KEY", "file", tmp / "unused-safety", write=True)
    assert tree(root) == before and not (tmp / "unused-safety").exists()


def test_namespace_copy_keeps_selection_and_refuses_pending_transition(vault):
    root, _ring, tmp = vault
    seed(vault)
    keys.write_selection(A, "file")
    from codex_antigravity_auth.namespace_migration import copy_gateway_state
    target = tmp / "copied"
    copy_gateway_state(str(root), str(target), write=True)
    assert (target / keys.SELECTION_FILE).read_bytes() == (root / keys.SELECTION_FILE).read_bytes()
    (root / keys.TRANSITION_FILE).write_text('{"schemaVersion":1}')
    with pytest.raises(ValueError, match="transition"):
        copy_gateway_state(str(root), str(tmp / "not-copied"), write=True)
    assert not (tmp / "not-copied").exists()


def test_keyring_failure_keeps_backup_and_guard_until_file_recovery(vault):
    root, ring, tmp = vault
    bodies, raw = seed(vault)
    ring["available"] = False
    output = tmp / "before-keyring.agbackup"
    with pytest.raises(keys.StorageKeyError, match="interrupted"):
        recovery.reencrypt("FIXTURE_NEW_KEY", "keyring", output, write=True)
    assert keys.transition_pending()
    assert all(path.read_bytes() == raw[name] for name, path in keys.managed_paths().items())
    assert output.exists()
    recovery.restore(output, "FIXTURE_NEW_KEY", "file", tmp / "safety.agbackup", write=True)
    assert not keys.transition_pending()
    assert all(Fernet(C.encode()).decrypt(path.read_bytes()) == bodies[name] for name, path in keys.managed_paths().items())


def test_conflicting_keyring_identity_slot_does_not_create_fallback(vault, monkeypatch):
    root, ring, _ = vault
    ring["entries"][keys.ring_name(keys.key_id(A))] = B
    monkeypatch.setattr(Fernet, "generate_key", lambda: A.encode())
    with pytest.raises(keys.StorageKeyError, match="conflicting"):
        storage._get_encryption_key()
    assert not (root / storage.FALLBACK_KEY_FILE).exists()
    assert not (root / keys.SELECTION_FILE).exists()
    assert ring["entries"][keys.ring_name(keys.key_id(A))] == B


def test_backup_is_singly_linked_before_transition_marker_or_key_mutation(vault, monkeypatch):
    root, _ring, tmp = vault
    seed(vault)
    output = tmp / "before.agbackup"
    original_write = recovery.SecureStore._atomic_write_bytes_unlocked
    observations = []
    def inspect_marker(self, path, value, **kwargs):
        if path == root / keys.TRANSITION_FILE:
            assert output.exists() and output.stat().st_nlink == 1
            observations.append(True)
        return original_write(self, path, value, **kwargs)
    monkeypatch.setattr(recovery.SecureStore, "_atomic_write_bytes_unlocked", inspect_marker)
    recovery.reencrypt("FIXTURE_NEW_KEY", "file", output, write=True)
    assert observations == [True]
    assert not list(tmp.glob(".key-backup-*"))


def test_store_diagnostics_disclose_pending_transition_even_for_plaintext(vault):
    root, _ring, _ = vault
    seed(vault, plaintext_provider=True)
    (root / keys.TRANSITION_FILE).write_text('{"schemaVersion":1}')
    before = tree(root)
    for report in (storage.account_store_diagnostics(), storage.provider_store_diagnostics(keys.managed_paths()["providers"])):
        assert not report["accessible"] and report["error_class"] == "recovery_required"
    assert tree(root) == before


def test_equivalent_key_encoding_is_canonicalized_before_persistence(vault, monkeypatch):
    root, _ring, tmp = vault
    bodies, _raw = seed(vault)
    spaced = C[:20] + (" " * 5000) + C[20:]
    monkeypatch.setenv("FIXTURE_SPACED_KEY", spaced)
    assert keys.named_key("FIXTURE_SPACED_KEY") == C
    recovery.reencrypt("FIXTURE_SPACED_KEY", "file", tmp / "canonical.agbackup", write=True)
    assert (root / storage.FALLBACK_KEY_FILE).read_text() == C + "\n"
    assert not keys.transition_pending()
    assert all(Fernet(C.encode()).decrypt(path.read_bytes()) == bodies[name] for name, path in keys.managed_paths().items())
