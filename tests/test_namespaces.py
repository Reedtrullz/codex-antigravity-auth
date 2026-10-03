"""Namespace boundary and copy tests use temporary roots and synthetic secrets."""
import json
import os
from pathlib import Path
import plistlib
import shlex
import subprocess
import sys
from types import SimpleNamespace

import pytest
from standalone import without_installed_packages

from codex_antigravity_auth import byok, cli, constants, models, observability, service, storage, unified
from codex_antigravity_auth import namespace_migration as migration
from codex_antigravity_auth.namespaces import (
    client_home, client_config_path, client_skills_path, gateway_home, namespace_diagnostics,
)


@pytest.fixture
def roots(monkeypatch, tmp_path):
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.delenv("ANTIGRAVITY_STATE_HOME", raising=False)
    home = tmp_path / "synthetic-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    return home / ".codex", tmp_path / "client", tmp_path / "state"


def test_defaults_are_compatible_and_diagnostics_make_no_directories(roots):
    default, client, state = roots
    assert client_home() == gateway_home() == default
    assert client_config_path() == default / "config.toml"
    assert client_skills_path() == default / "skills"
    assert namespace_diagnostics() == {"client_root_source": "default", "gateway_root_source": "default",
                                       "roots_shared": True, "client_auth_fallback": False, "gateway_shared_default": False}
    assert not any(path.exists() for path in (default, client, state))


def test_explicit_client_keeps_named_shared_gateway_default(roots, monkeypatch):
    default, client, _state = roots
    monkeypatch.setenv("CODEX_HOME", str(client))
    assert client_home() == client and gateway_home() == default
    assert namespace_diagnostics()["gateway_shared_default"] is True
    assert namespace_diagnostics()["roots_shared"] is False


@pytest.mark.parametrize("value", ["", " ", "relative/path", "\n/tmp/fixture", "/tmp/fixture\x1b"])
@pytest.mark.parametrize("name", ["CODEX_HOME", "ANTIGRAVITY_STATE_HOME"])
def test_explicit_invalid_namespace_never_falls_back(roots, monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError):
        namespace_diagnostics()
    assert not roots[0].exists()


@pytest.mark.parametrize("alternate", [None, "missing", "malformed", "empty", "missing_token"])
def test_client_auth_never_borrows_default_credentials(roots, monkeypatch, alternate):
    default, client, state = roots
    default.mkdir()
    (default / "auth.json").write_text(json.dumps({"tokens": {"access_token": "fixture-default-secret"}}))
    monkeypatch.setenv("CODEX_HOME", str(client))
    monkeypatch.setenv("ANTIGRAVITY_STATE_HOME", str(state))
    if alternate not in (None, "missing"):
        client.mkdir()
        text = {"malformed": "{", "empty": "{}", "missing_token": '{"tokens":{"account_id":"fixture-id"}}'}[alternate]
        (client / "auth.json").write_text(text)
    before = (default / "auth.json").read_bytes()
    with pytest.raises(unified.OpenAIUpstreamAuthError) as exc:
        unified._resolve_codex_oauth_auth()
    assert "fixture-default-secret" not in str(exc.value)
    assert (default / "auth.json").read_bytes() == before
    assert not state.exists()


def test_client_auth_uses_selected_token_and_preserves_file(roots, monkeypatch):
    _default, client, state = roots
    monkeypatch.setenv("CODEX_HOME", str(client))
    monkeypatch.setenv("ANTIGRAVITY_STATE_HOME", str(state))
    client.mkdir()
    path = client / "auth.json"
    path.write_text(json.dumps({"tokens": {"access_token": "fixture-selected", "account_id": "fixture-account"}}))
    before = path.read_bytes()
    result = unified._resolve_codex_oauth_auth()
    assert result.access_token == "fixture-selected"
    assert path.read_bytes() == before
    assert not state.exists()


def test_gateway_paths_and_writes_stay_in_selected_state(roots, monkeypatch):
    default, client, state = roots
    monkeypatch.setenv("CODEX_HOME", str(client))
    monkeypatch.setenv("ANTIGRAVITY_STATE_HOME", str(state))
    assert storage.accounts_json_path_read_only() == state / "antigravity-accounts.json"
    assert byok.providers_json_path_read_only() == state / "antigravity-providers.json"
    assert models.model_overlay_path() == state / "antigravity-models.toml"
    assert observability.request_log_path().parent == state
    assert cli.gateway_runtime_paths(51122)[0].parent == state
    assert service.service_log_paths(51122)[0].parent == state
    assert not state.exists()
    storage.save_accounts({"accounts": [{"email": "fixture@example.invalid", "accessToken": "fixture-token"}]})
    byok.set_provider_config("custom", api_key="fixture-key", base_url="https://example.invalid/v1", models=["fixture-model"])
    constants.save_oauth_credentials("fixture-client", "fixture-secret")
    assert constants.resolve_oauth_credentials() == ("fixture-client", "fixture-secret")
    assert len(storage.load_accounts()["accounts"]) == 1
    assert byok.load_provider_config()["providers"]["custom"]["apiKey"] == "fixture-key"
    assert not default.exists() and not client.exists()


def test_cli_config_and_skill_defaults_follow_client_root(roots, monkeypatch):
    default, client, state = roots
    monkeypatch.setenv("CODEX_HOME", str(client))
    monkeypatch.setenv("ANTIGRAVITY_STATE_HOME", str(state))
    args = SimpleNamespace(config="~/.codex/config.toml", model="gemini-3.8-flash", provider="antigravity",
                           base_url="http://localhost:51122/v1", write=True, activate=False)
    cli.run_configure_codex(args)
    assert (client / "config.toml").is_file()
    assert not default.exists() and not state.exists()
    assert cli.client_skills_path("~/.codex/skills") == client / "skills"
    explicit = default / "config.toml"
    assert client_config_path(explicit) == explicit


def test_service_definitions_freeze_both_selected_roots(roots, monkeypatch):
    _default, client, state = roots
    monkeypatch.setenv("CODEX_HOME", str(client))
    monkeypatch.setenv("ANTIGRAVITY_STATE_HOME", str(state))
    command = service.service_command(51122, "127.0.0.1")
    assert command[command.index("--client-home") + 1] == str(client)
    assert command[command.index("--state-home") + 1] == str(state)
    agent = plistlib.loads(service.render_macos_launch_agent(51122, "127.0.0.1").encode())
    assert agent["ProgramArguments"] == command
    assert agent["StandardOutPath"] == "/dev/null"
    from codex_antigravity_auth.process_logs import log_path
    assert command[command.index("--process-log") + 1] == str(log_path(state, 51122))

    assert Path(command[command.index("--process-log") + 1]).parent.parent == state
    unit = service.render_linux_systemd_unit(51122, "127.0.0.1")
    exec_start = unit.split("ExecStart=", 1)[1].splitlines()[0].replace("%%", "%")
    assert shlex.split(exec_start) == command
    assert not client.exists() and not state.exists()


def test_service_defaults_use_target_users_home(roots, monkeypatch, tmp_path):
    target_home = tmp_path / "service-user"
    monkeypatch.setattr(service, "_service_home", lambda: target_home)
    command = service.service_command(51122, "127.0.0.1")
    assert command[command.index("--state-home") + 1] == str(target_home / ".codex")
    assert command[command.index("--client-home") + 1] == str(target_home / ".codex")


def test_standalone_anti_namespace_paths_without_site_packages(roots, monkeypatch):
    default, client, state = roots
    monkeypatch.setenv("CODEX_HOME", str(client))
    monkeypatch.setenv("ANTIGRAVITY_STATE_HOME", str(state))
    script_dir = Path(cli.__file__).parent / "skills/anti/scripts"
    code = '''import json, os, runpy, sys
os.environ['CODEX_HOME'] = sys.argv[2]
os.environ['ANTIGRAVITY_STATE_HOME'] = sys.argv[3]
sys.path.insert(0, sys.argv[1])
anti = runpy.run_path(sys.argv[1] + '/anti.py', run_name='synthetic_namespace_import')
from anti_lib import reflections
print(json.dumps([str(anti['PID_FILE']), str(anti['LOG_FILE']), str(anti['RUNS_DIR']), str(reflections.REFLECTIONS_DIR)]))
'''
    result = subprocess.run([sys.executable, "-c", without_installed_packages(code), str(script_dir), str(client), str(state)], cwd=client.parent, text=True, capture_output=True, timeout=10, check=True)
    assert json.loads(result.stdout) == [str(state / name) for name in ("anti-gateway.pid", "anti-gateway.log", "anti-runs", "anti-runs/reflections")]
    assert not any(path.exists() for path in roots)


def test_copy_dry_run_reads_only_and_write_preserves_originals(roots, tmp_path):
    source, _client, destination = roots
    source.mkdir()
    payloads = {"antigravity-accounts.json": b"fixture-ciphertext", "antigravity-storage.key": b"fixture-key"}
    for name, data in payloads.items():
        (source / name).write_bytes(data)
    (source / "auth.json").write_bytes(b"fixture-client-auth-not-copied")
    (source / "anti-gateway.pid").write_text("123")
    before = {p.name: p.read_bytes() for p in source.iterdir()}
    plan = migration.copy_gateway_state(str(source), str(destination))
    assert plan["mode"] == "dry_run" and plan["files"] == sorted(payloads)
    assert not destination.exists()
    assert {p.name: p.read_bytes() for p in source.iterdir()} == before
    result = migration.copy_gateway_state(str(source), str(destination), write=True)
    assert result["mode"] == "copied"
    for name, data in before.items():
        assert (source / name).read_bytes() == data
    for name, data in payloads.items():
        assert (destination / name).read_bytes() == data
        if os.name != "nt":
            assert (destination / name).stat().st_mode & 0o777 == 0o600
    assert not (destination / "auth.json").exists()
    assert not (destination / "anti-gateway.pid").exists()
    assert json.loads((destination / "antigravity-namespace-migration.json").read_text())["schemaVersion"] == 1


@pytest.mark.parametrize("failure", ["write", "source_change", "destination_appears", "rename"])
def test_copy_failure_does_not_publish_partial_or_destroy_source(roots, monkeypatch, failure):
    source, _client, destination = roots
    source.mkdir()
    (source / "antigravity-accounts.json").write_bytes(b"fixture-original")
    real_write = migration.SecureStore._atomic_write_bytes_unlocked
    calls = []
    def fail_during_stage(self, path, content, **kwargs):
        calls.append(path)
        real_write(self, path, content, **kwargs)
        if len(calls) == 1:
            if failure == "write":
                raise OSError("synthetic interrupted copy")
            if failure == "source_change":
                (source / "antigravity-accounts.json").write_bytes(b"fixture-newer")
            if failure == "destination_appears":
                destination.mkdir()
                (destination / "sentinel").write_bytes(b"fixture-existing")
    monkeypatch.setattr(migration.SecureStore, "_atomic_write_bytes_unlocked", fail_during_stage)
    if failure == "rename":
        monkeypatch.setattr(migration.os, "rename", lambda *_: (_ for _ in ()).throw(OSError("synthetic rename failure")))
    with pytest.raises((OSError, RuntimeError)):
        migration.copy_gateway_state(str(source), str(destination), write=True)
    assert (source / "antigravity-accounts.json").read_bytes() == (b"fixture-newer" if failure == "source_change" else b"fixture-original")
    if failure == "destination_appears":
        assert (destination / "sentinel").read_bytes() == b"fixture-existing"
    else:
        assert not destination.exists()
    assert not list(destination.parent.glob(f".{destination.name}-*"))


def test_copy_rejects_existing_destination_and_symlink_input(roots, tmp_path):
    source, _client, destination = roots
    source.mkdir()
    destination.mkdir()
    with pytest.raises(ValueError, match="must not exist"):
        migration.copy_gateway_state(str(source), str(destination), write=True)
    destination.rmdir()
    target = tmp_path / "fixture-target"
    target.write_bytes(b"fixture-preserve")
    (source / "antigravity-storage.key").symlink_to(target)
    with pytest.raises(ValueError, match="symlink"):
        migration.copy_gateway_state(str(source), str(destination), write=True)
    assert target.read_bytes() == b"fixture-preserve" and not destination.exists()


def test_default_client_auth_stays_compatible(roots):
    default, _client, _state = roots
    default.mkdir()
    (default / "auth.json").write_text('{"tokens":{"access_token":"fixture-default"}}')
    assert unified._resolve_codex_oauth_auth().access_token == "fixture-default"


def test_namespace_show_does_not_read_keys_or_files(roots, monkeypatch, capsys):
    _default, client, state = roots
    monkeypatch.setenv("CODEX_HOME", str(client))
    monkeypatch.setenv("ANTIGRAVITY_STATE_HOME", str(state))
    monkeypatch.setattr(sys, "argv", ["codex-antigravity", "namespace", "show"])
    monkeypatch.setattr(storage, "_peek_encryption_key", lambda: pytest.fail("namespace diagnostics must not read a key"))
    cli.main()
    result = json.loads(capsys.readouterr().out)
    assert result["client_root_source"] == "CODEX_HOME"
    assert result["gateway_root_source"] == "ANTIGRAVITY_STATE_HOME"
    assert str(client) not in json.dumps(result) and str(state) not in json.dumps(result)
    assert not any(path.exists() for path in roots)


def test_readiness_config_path_and_client_override_are_consistent(roots, monkeypatch):
    default, client, state = roots
    monkeypatch.setenv("CODEX_HOME", str(client))
    monkeypatch.setenv("ANTIGRAVITY_STATE_HOME", str(state))
    path, text, error = cli._read_codex_config_for_readiness("~/.codex/config.toml")
    assert path == client / "config.toml" and text is None and error
    client.mkdir()
    (client / "config.toml").write_text('model="fixture-model"')
    assert cli._read_codex_config_for_readiness("~/.codex/config.toml")[1] == 'model="fixture-model"'
    assert not default.exists() and not state.exists()


def test_start_arguments_capture_service_namespace_without_starting_server(roots, monkeypatch):
    _default, client, state = roots
    # Track restoration of environment fields changed by the CLI itself.
    monkeypatch.setenv("CODEX_HOME", str(client))
    monkeypatch.setenv("ANTIGRAVITY_STATE_HOME", str(state))
    monkeypatch.setattr(sys, "argv", ["codex-antigravity", "start", "--client-home", str(client), "--state-home", str(state)])
    calls = []
    monkeypatch.setattr("codex_antigravity_auth.process_logs.run_gateway", lambda *args, **kwargs: calls.append((client_home(), gateway_home())))
    cli.main()
    assert calls == [(client, state)]
    assert not client.exists() and not state.exists()


@pytest.mark.parametrize("name", ["CODEX_HOME", "ANTIGRAVITY_STATE_HOME"])
@pytest.mark.parametrize("kind", ["file", "file_parent", "broken_symlink"])
def test_existing_non_directory_root_is_rejected_without_writes(roots, monkeypatch, tmp_path, name, kind):
    value = tmp_path / "invalid-root"
    if kind == "broken_symlink":
        value.symlink_to(tmp_path / "missing-target")
    else:
        value.write_bytes(b"fixture-preserved")
    selected = value / "child" if kind == "file_parent" else value
    monkeypatch.setenv(name, str(selected))
    with pytest.raises(ValueError, match="directory"):
        namespace_diagnostics()
    if kind != "broken_symlink":
        assert value.read_bytes() == b"fixture-preserved"
    assert not any(path.exists() for path in roots)


def test_default_root_regular_file_is_rejected(roots):
    default, _client, _state = roots
    default.write_bytes(b"fixture-preserved")
    with pytest.raises(ValueError, match="directory"):
        namespace_diagnostics()
    assert default.read_bytes() == b"fixture-preserved"


def test_namespace_show_and_start_reject_file_roots_before_dispatch(roots, monkeypatch, tmp_path):
    value = tmp_path / "file-root"
    value.write_bytes(b"fixture-preserved")
    monkeypatch.setenv("CODEX_HOME", str(value))
    monkeypatch.setattr(sys, "argv", ["codex-antigravity", "namespace", "show"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 1
    monkeypatch.delenv("CODEX_HOME")
    monkeypatch.setattr(sys, "argv", ["codex-antigravity", "start", "--state-home", str(value)])
    monkeypatch.setattr("uvicorn.run", lambda *a, **k: pytest.fail("invalid root started a server"))
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2
    assert value.read_bytes() == b"fixture-preserved"


def test_nonexistent_and_directory_symlink_roots_remain_supported(roots, monkeypatch, tmp_path):
    _default, client, state = roots
    monkeypatch.setenv("CODEX_HOME", str(client / "future"))
    state.mkdir()
    alias = tmp_path / "state-link"
    alias.symlink_to(state, target_is_directory=True)
    monkeypatch.setenv("ANTIGRAVITY_STATE_HOME", str(alias))
    assert client_home() == client / "future"
    assert gateway_home() == alias
    assert not client.exists()
