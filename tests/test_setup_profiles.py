"""Synthetic setup state and injected local callbacks; no live login, gateway or keyring."""
import argparse
import json
import os
from pathlib import Path
import stat
import sys
from unittest.mock import Mock

import pytest
import tomlkit

from codex_antigravity_auth import cli, cli_setup, setup_profiles as setup

SECRET = "sk-fixtureabcdefghijklmnopqrstuvwxyz"
ORIGINAL = '# Keep this comment\nmodel = "original-model"\nmodel_provider = "original-provider"\n[projects."/fixture"]\ntrust_level = "trusted"\n'


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    client, state = tmp_path / "client", tmp_path / "state"
    monkeypatch.setenv("CODEX_HOME", str(client))
    monkeypatch.setenv("ANTIGRAVITY_STATE_HOME", str(state))
    monkeypatch.setattr(cli, "resolve_oauth_credentials", lambda **kw: ("synthetic-client", SECRET))
    monkeypatch.setattr(cli, "run_login", Mock())
    monkeypatch.setattr(cli, "start_gateway_background", Mock(return_value={"pid": 123}))
    monkeypatch.setattr(cli, "wait_for_gateway_model_ids", lambda *a, **kw: {"claude-sonnet-4-6"})
    monkeypatch.setattr(cli, "codex_ready_report", lambda **kw: {"ok": True, "checks": [], "next_command": "codex"})
    return client, state


def options(**changes):
    values = dict(name="google", model="claude-sonnet-4-6", provider="antigravity", provider_name="Google Antigravity",
                  base_url="http://localhost:51122/v1", unified_model_picker=False, gateway_token_env=None,
                  write=False, activate=False, install_skill=False, skill_dir="~/.codex/skills", config="~/.codex/config.toml",
                  force=False, repair=False, check=False, json=False, start=False, accounts=1, no_input=True,
                  no_browser=True, host="127.0.0.1", port=51122, allow_remote=False, gateway_timeout=1,
                  verify_skill=False, live=False, plan=False)
    values.update(changes)
    return argparse.Namespace(**values)


def tree(root):
    return {str(path.relative_to(root)): (path.read_bytes(), path.stat().st_mode, path.stat().st_mtime_ns)
            for path in root.rglob("*") if path.is_file()}


def existing_config(client):
    client.mkdir(parents=True, exist_ok=True)
    target = client / "config.toml"
    target.write_text(ORIGINAL)
    return target


def test_setup_plan_is_machine_readable_and_does_not_resolve_credentials_or_write(isolated, monkeypatch, capsys):
    client, state = isolated
    for name in ("resolve_oauth_credentials", "run_login", "start_gateway_background", "codex_ready_report"):
        monkeypatch.setattr(cli, name, Mock(side_effect=AssertionError("planning must not inspect credentials or network")))
    plan = cli_setup.run_setup(options(plan=True, install_skill=True, start=True))
    assert json.loads(capsys.readouterr().out) == plan
    assert plan["readOnly"] is True and plan["activateDefault"] is False
    assert [row["id"] for row in plan["stages"]] == list(setup.STAGES)
    assert plan["credentialsAndServicesRestorable"] is False
    assert not client.exists() and not state.exists()


def test_profiles_store_only_settings_and_reference_names_and_create_defaults_to_dry_run(isolated, monkeypatch):
    client, state = isolated
    monkeypatch.setenv("FIXTURE_GATEWAY_KEY", SECRET)
    plan = setup.create_profile(options(gateway_token_env="FIXTURE_GATEWAY_KEY"))
    assert not state.exists() and not client.exists()
    assert SECRET not in json.dumps(plan)
    saved = setup.create_profile(options(write=True, gateway_token_env="FIXTURE_GATEWAY_KEY"))
    content = Path(saved["path"]).read_text()
    assert "FIXTURE_GATEWAY_KEY" in content and SECRET not in content
    assert setup.load_profile("google")["secretReferences"] == {"gatewayTokenEnv": "FIXTURE_GATEWAY_KEY"}
    with pytest.raises(setup.SetupError, match="already exists"):
        setup.create_profile(options(write=True))


def test_profile_apply_preserves_unrelated_toml_and_requires_explicit_activation(isolated):
    client, state = isolated
    target = existing_config(client)
    setup.create_profile(options(write=True, gateway_token_env="FIXTURE_GATEWAY_KEY"))
    before = tree(state), target.read_bytes()
    plan = setup.apply_profile(options())
    assert plan["write"] is False and (tree(state), target.read_bytes()) == before
    applied = setup.apply_profile(options(write=True))
    document = tomlkit.parse(target.read_text()).unwrap()
    assert document["model"] == "original-model" and document["model_provider"] == "original-provider"
    assert document["projects"]["/fixture"]["trust_level"] == "trusted" and "# Keep this comment" in target.read_text()
    assert document["model_providers"]["antigravity"]["env_key"] == "FIXTURE_GATEWAY_KEY"
    assert applied["runtimeChanged"] is False
    setup.create_profile(options(name="local-byok", model="ollama:gpt-oss:20b", write=True))
    setup.apply_profile(options(name="local-byok", write=True, activate=True))
    document = tomlkit.parse(target.read_text()).unwrap()
    assert document["model"] == "ollama:gpt-oss:20b" and document["model_provider"] == "antigravity"
    assert "env_key" not in document["model_providers"]["antigravity"]
    assert document["projects"]["/fixture"]["trust_level"] == "trusted"


def test_unified_profile_uses_its_own_provider_and_does_not_change_running_services(isolated):
    client, _state = isolated
    existing_config(client)
    created = setup.create_profile(options(name="unified", unified_model_picker=True, model="gpt-5.6", write=True))
    assert created["profile"]["settings"]["provider"] == "antigravity-unified"
    result = setup.apply_profile(options(name="unified", write=True, activate=True))
    assert result["runtimeChanged"] is False
    assert "--unified-model-picker" in result["nextSteps"][0]
    cli.start_gateway_background.assert_not_called()
    cli.run_login.assert_not_called()


@pytest.mark.parametrize("existed", [False, True])
def test_explicit_config_restore_is_dry_first_and_retains_original_backup(isolated, existed):
    client, state = isolated
    target = client / "config.toml"
    if existed:
        existing_config(client)
    setup.create_profile(options(write=True))
    result = setup.apply_profile(options(write=True, activate=True))
    run_id = result["receipt"]["id"]
    before = tree(client), tree(state)
    preview = setup.restore_receipt(run_id, ["config"])
    assert preview["stages"] == [{"stage": "config", "status": "would_restore"}]
    assert (tree(client), tree(state)) == before
    setup.restore_receipt(run_id, ["config"], write=True)
    if existed:
        assert target.read_text() == ORIGINAL
        assert (setup.receipts_root() / run_id / "config-before").read_text() == ORIGINAL
    else:
        assert not target.exists()
    assert (setup.receipts_root() / run_id / "config-before-restore").is_file()
    assert setup.load_receipt(run_id)[1]["stages"][2]["state"] == "restored"


def test_drifted_config_and_changed_backup_refuse_restoration_without_changes(isolated):
    client, state = isolated
    target = existing_config(client)
    setup.create_profile(options(write=True))
    receipt = setup.apply_profile(options(write=True))["receipt"]
    target.write_text(target.read_text() + '\n# user changed this\n')
    before = tree(client), tree(state)
    with pytest.raises(setup.SetupError, match="drifted"):
        setup.restore_receipt(receipt["id"], ["config"], write=True)
    assert target.read_bytes() == before[0]["config.toml"][0]
    # The same guard applies during a read-only restore plan.
    with pytest.raises(setup.SetupError, match="drifted"):
        setup.restore_receipt(receipt["id"], ["config"])


@pytest.mark.parametrize("existed", [False, True])
def test_selective_skill_restore_preserves_previous_and_displaced_trees(isolated, monkeypatch, existed):
    client, _state = isolated
    target = client / "skills/anti"
    if existed:
        target.mkdir(parents=True)
        (target / "custom.txt").write_text("synthetic original skill")
        (target / "empty-dir").mkdir()
    def install(parent, **kwargs):
        destination = parent / "anti"
        destination.mkdir(parents=True, exist_ok=True)
        (destination / "new.txt").write_text("synthetic installed skill")
        if existed:
            (destination / "custom.txt").unlink()
        return "installed", destination, None
    monkeypatch.setattr(cli, "install_codex_skill", install)
    setup.create_profile(options(write=True))
    result = setup.apply_profile(options(write=True, install_skill=True, force=True))
    config_after = (client / "config.toml").read_bytes()
    run_id = result["receipt"]["id"]
    setup.restore_receipt(run_id, ["skill"], write=True)
    assert (client / "config.toml").read_bytes() == config_after
    if existed:
        assert (target / "custom.txt").read_text() == "synthetic original skill"
        assert (target / "empty-dir").is_dir() and not (target / "new.txt").exists()
    else:
        assert not target.exists()
    row = setup.load_receipt(run_id)[1]["stages"][3]
    assert (Path(row["retainedAfterRestore"]) / "new.txt").read_text() == "synthetic installed skill"


@pytest.mark.parametrize("failed", setup.STAGES)
def test_each_setup_stage_failure_records_completed_failed_pending_without_secrets(isolated, monkeypatch, capsys, failed):
    client, state = isolated
    existing_config(client)
    target = client / "skills/anti"
    target.mkdir(parents=True)
    (target / "old.txt").write_text("old skill")
    def fault():
        raise RuntimeError(SECRET)
    if failed == "credentials": monkeypatch.setattr(cli, "resolve_oauth_credentials", lambda **kw: fault())
    elif failed == "login": monkeypatch.setattr(cli, "run_login", lambda _: fault())
    elif failed == "gateway": monkeypatch.setattr(cli, "start_gateway_background", lambda _: fault())
    elif failed == "readiness": monkeypatch.setattr(cli, "codex_ready_report", lambda **kw: fault())
    configure = cli.run_configure_codex
    def config(args):
        configure(args)
        if failed == "config": fault()
    monkeypatch.setattr(cli, "run_configure_codex", config)
    def skill(args):
        (target / "new.txt").write_text("new skill")
        if failed == "skill": fault()
    monkeypatch.setattr(cli, "run_install_skill", skill)
    with pytest.raises(SystemExit):
        cli_setup.run_setup(options(write=True, install_skill=True, force=True, start=True))
    output = capsys.readouterr()
    assert SECRET not in output.out + output.err
    files = list((state / "setup-receipts").glob("*/receipt.json"))
    assert len(files) == 1 and SECRET not in files[0].read_text()
    receipt = setup.load_receipt(files[0].parent.name)[1]
    assert receipt["state"] == "failed"
    index = setup.STAGES.index(failed)
    assert all(row["state"] == "completed" for row in receipt["stages"][:index])
    assert receipt["stages"][index]["state"] == "failed"
    assert all(row["state"] == "pending" for row in receipt["stages"][index + 1:])
    assert receipt["nextSteps"]


def test_running_unknown_receipt_and_nonowned_stages_cannot_be_restored(isolated):
    client, state = isolated
    existing_config(client)
    journal = setup.SetupJournal(setup.setup_plan(options()))
    before = tree(client), tree(state)
    with pytest.raises(setup.SetupError, match="still running"):
        setup.restore_receipt(journal.id, ["config"])
    with pytest.raises(setup.SetupError, match="cannot be restored"):
        setup.restore_receipt(journal.id, ["credentials"])
    assert (tree(client), tree(state)) == before


@pytest.mark.parametrize("change", ["version", "key_value", "unknown_setting", "secret_name"])
def test_invalid_profiles_never_echo_or_rewrite_credentials(isolated, change, capsys):
    _client, state = isolated
    created = setup.create_profile(options(write=True))
    path = Path(created["path"])
    profile = json.loads(path.read_text())
    if change == "version": profile["schemaVersion"] = 123
    elif change == "key_value": profile["secretReferences"]["gatewayTokenEnv"] = SECRET
    elif change == "unknown_setting": profile["settings"]["api_key"] = SECRET
    else: profile["settings"]["provider_name"] = SECRET
    path.write_text(json.dumps(profile))
    before = tree(state)
    with pytest.raises((setup.SetupError, ValueError)):
        setup.load_profile("google")
    assert tree(state) == before and SECRET not in capsys.readouterr().out


def test_cli_profile_and_history_commands_emit_json_only(isolated, monkeypatch, capsys):
    client, _state = isolated
    for argv in (["profiles", "create", "google"], ["profiles", "create", "google", "--write"], ["profiles", "list"], ["profiles", "show", "google"], ["profiles", "apply", "google"], ["setup-history", "list"]):
        monkeypatch.setattr(sys, "argv", ["codex-antigravity", *argv])
        cli.main()
        value = json.loads(capsys.readouterr().out)
        assert value["schemaVersion"] == 1 and value["ok"] is True
    assert not client.exists()


def test_backup_tampering_and_uncertain_restore_are_preserved(isolated):
    client, state = isolated
    target = existing_config(client)
    setup.create_profile(options(write=True))
    receipt = setup.apply_profile(options(write=True))["receipt"]
    root = setup.receipts_root() / receipt["id"]
    (root / "config-before").write_bytes(b"changed backup")
    current = target.read_bytes()
    with pytest.raises(setup.SetupError, match="backup is missing or changed"):
        setup.restore_receipt(receipt["id"], ["config"], write=True)
    assert target.read_bytes() == current
    (root / "config-before").write_text(ORIGINAL)
    data = json.loads((root / "receipt.json").read_text())
    data["stages"][2]["state"] = "restoring"
    data["stages"][2]["restorePaths"] = {"target": str(target), "originalBackup": str(root / "config-before"), "displaced": str(root / "config-before-restore")}
    (root / "receipt.json").write_text(json.dumps(data))
    before = tree(state), tree(client)
    with pytest.raises(setup.SetupError, match="uncertain"):
        setup.restore_receipt(receipt["id"], ["config"])
    assert (tree(state), tree(client)) == before


def test_malformed_and_future_receipts_are_not_rewritten(isolated):
    client, state = isolated
    existing_config(client)
    journal = setup.SetupJournal(setup.setup_plan(options()))
    journal.finish(ok=False)
    for change in (lambda data: data.update(schemaVersion=99), lambda data: data["stages"].append("bad row"),
                   lambda data: data["stages"][2].update(before={"exists": "yes", "sha256": "bad"})):
        value = json.loads(journal.path.read_text())
        change(value)
        journal.path.write_text(json.dumps(value))
        before = tree(state)
        with pytest.raises(setup.SetupError):
            setup.load_receipt(journal.id)
        assert tree(state) == before
        journal.persist()


def test_repeated_restore_detects_subsequent_user_drift(isolated):
    client, _state = isolated
    target = existing_config(client)
    setup.create_profile(options(write=True))
    run_id = setup.apply_profile(options(write=True))["receipt"]["id"]
    setup.restore_receipt(run_id, ["config"], write=True)
    assert setup.restore_receipt(run_id, ["config"])["stages"][0]["status"] == "unchanged"
    target.write_text(ORIGINAL + "\n# new user edit\n")
    with pytest.raises(setup.SetupError, match="drifted"):
        setup.restore_receipt(run_id, ["config"], write=True)
    assert "new user edit" in target.read_text()


def test_restore_write_failure_leaves_an_uncertain_receipt_with_recovery_paths(isolated, monkeypatch):
    client, _state = isolated
    target = existing_config(client)
    setup.create_profile(options(write=True))
    run_id = setup.apply_profile(options(write=True))["receipt"]["id"]
    real_write = setup.SecureStore.atomic_write_bytes
    def fail(self, path, content, **kwargs):
        if path == target:
            raise OSError("synthetic filesystem failure")
        return real_write(self, path, content, **kwargs)
    monkeypatch.setattr(setup.SecureStore, "atomic_write_bytes", fail)
    with pytest.raises(OSError):
        setup.restore_receipt(run_id, ["config"], write=True)
    row = setup.load_receipt(run_id)[1]["stages"][2]
    assert row["state"] == "restoring"
    assert all(Path(value).exists() for value in row["restorePaths"].values())
    with pytest.raises(setup.SetupError, match="uncertain"):
        setup.restore_receipt(run_id, ["config"])


def test_concurrent_profile_switches_refuse_a_stale_plan(isolated, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    client, _state = isolated
    target = existing_config(client)
    setup.create_profile(options(name="first", write=True))
    setup.create_profile(options(name="second", model="gemini-3.8-flash", write=True))
    real_plan = setup.setup_plan
    barrier = threading.Barrier(2)
    def plan(*args, **kwargs):
        result = real_plan(*args, **kwargs)
        barrier.wait(timeout=5)
        return result
    monkeypatch.setattr(setup, "setup_plan", plan)
    def apply(name):
        try:
            return setup.apply_profile(options(name=name, write=True, activate=True))["ok"]
        except setup.SetupError:
            return False
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(apply, ("first", "second")))
    assert sorted(results) == [False, True]
    assert tomlkit.parse(target.read_text())["model"] in {"claude-sonnet-4-6", "gemini-3.8-flash"}


@pytest.mark.parametrize("message,outcome", list(setup.PUBLIC_OAUTH_EXITS.items()))
def test_fixed_oauth_outcomes_remain_explicit_without_arbitrary_error_text(isolated, message, outcome):
    journal = setup.SetupJournal(setup.setup_plan(options()))
    def deny():
        raise SystemExit(message)
    with pytest.raises(SystemExit) as error:
        journal.run("login", deny)
    assert str(error.value) == message
    assert journal.data["stages"][1]["outcome"] == outcome
    assert "error" not in journal.data["stages"][1]


@pytest.mark.parametrize("name", ["config", "skill"])
@pytest.mark.parametrize("mutation", ["missing_started", "false_started", "pending", "skipped", "missing_before", "missing_after", "missing_target", "missing_backup", "bad_restore", "wrong_ownership"])
def test_incoherent_restore_stage_is_refused_before_any_target_or_receipt_write(isolated, monkeypatch, name, mutation):
    client, state = isolated
    existing_config(client)
    skill = client / "skills/anti"
    skill.mkdir(parents=True)
    (skill / "old.txt").write_text("original fixture")
    def install(parent, **kwargs):
        (parent / "anti/new.txt").write_text("installed fixture")
    monkeypatch.setattr(cli, "install_codex_skill", install)
    setup.create_profile(options(write=True))
    run_id = setup.apply_profile(options(write=True, install_skill=True, force=True))["receipt"]["id"]
    path = setup.receipts_root() / run_id / "receipt.json"
    receipt = json.loads(path.read_text())
    row = next(item for item in receipt["stages"] if item["id"] == name)
    if mutation == "missing_started": row.pop("operationStarted")
    elif mutation == "false_started": row["operationStarted"] = False
    elif mutation in {"pending", "skipped"}: row["state"] = mutation
    elif mutation.startswith("missing_"): row.pop(mutation[len("missing_"):])
    elif mutation == "bad_restore": row["state"] = "restored"
    else: row["restorable"] = False
    path.write_text(json.dumps(receipt))
    before = tree(client), tree(state)
    with pytest.raises(setup.SetupError):
        setup.restore_receipt(run_id, [name], write=True)
    assert (tree(client), tree(state)) == before


def test_gateway_stage_fails_when_start_succeeds_but_models_never_become_ready(isolated, monkeypatch, capsys):
    _client, state = isolated
    monkeypatch.setattr(cli, "wait_for_gateway_model_ids", Mock(side_effect=RuntimeError(SECRET)))
    with pytest.raises(SystemExit, match="Next command"):
        cli_setup.run_setup(options(write=True, start=True))
    cli.start_gateway_background.assert_called_once()
    paths = list((state / "setup-receipts").glob("*/receipt.json"))
    receipt = setup.load_receipt(paths[0].parent.name)[1]
    states = {stage["id"]: stage["state"] for stage in receipt["stages"]}
    assert states["config"] == "completed" and states["gateway"] == "failed" and states["readiness"] == "pending"
    assert receipt["state"] == "failed" and "codex-antigravity status --port 51122" in receipt["nextSteps"]
    output = capsys.readouterr()
    assert SECRET not in output.out + output.err + paths[0].read_text()


def test_uncertain_selected_skill_prevents_config_restore_before_any_mutation(isolated, monkeypatch):
    client, state = isolated
    target = existing_config(client)
    skill = client / "skills/anti"
    skill.mkdir(parents=True)
    (skill / "before.txt").write_text("original")
    monkeypatch.setattr(cli, "install_codex_skill", lambda parent, **kwargs: (parent / "anti/new.txt").write_text("new"))
    setup.create_profile(options(write=True))
    run_id = setup.apply_profile(options(write=True, install_skill=True, force=True))["receipt"]["id"]
    path = setup.receipts_root() / run_id / "receipt.json"
    receipt = json.loads(path.read_text())
    receipt["state"] = "failed"
    receipt["stages"][3]["state"] = "failed"
    receipt["stages"][3]["errorClass"] = "RuntimeError"
    receipt["stages"][3]["after"] = None
    path.write_text(json.dumps(receipt))
    before = target.read_bytes(), tree(skill), path.read_bytes()
    with pytest.raises(setup.SetupError, match="uncertain"):
        setup.restore_receipt(run_id, ["config", "skill"], write=True)
    assert (target.read_bytes(), tree(skill), path.read_bytes()) == before


@pytest.mark.parametrize("overlap", ["config_in_skill", "config_is_receipts", "state_in_skill"])
def test_overlapping_owned_targets_are_refused_by_plan_without_creating_state(isolated, monkeypatch, overlap):
    client, state = isolated
    changes = dict(install_skill=True)
    if overlap == "config_in_skill": changes["config"] = str(client / "skills/anti/config.toml")
    elif overlap == "config_is_receipts": changes["config"] = str(state / "setup-receipts")
    else: monkeypatch.setenv("ANTIGRAVITY_STATE_HOME", str(client / "skills/anti/state"))
    with pytest.raises(setup.SetupError, match="overlap"):
        setup.setup_plan(options(**changes))
    assert not client.exists() and not state.exists()


@pytest.mark.parametrize("mutation", ["version", "missing_identity", "wrong_identity", "changed_request", "pending_complete"])
def test_plan_and_stage_evidence_must_agree_before_restore(isolated, mutation):
    client, state = isolated
    existing_config(client)
    setup.create_profile(options(write=True))
    run_id = setup.apply_profile(options(write=True))["receipt"]["id"]
    path = setup.receipts_root() / run_id / "receipt.json"
    value = json.loads(path.read_text())
    if mutation == "version": value["plan"]["schemaVersion"] = 2
    elif mutation == "missing_identity": value["plan"].pop("configBeforeSha256")
    elif mutation == "wrong_identity": value["plan"]["configBeforeSha256"] = "0" * 64
    elif mutation == "changed_request":
        value["plan"]["stages"][2].update(requested=False, state="skipped")
    else:
        row = value["stages"][2]
        row["state"] = "pending"
        for key in ("operationStarted", "before", "after", "target", "backup"):
            row.pop(key, None)
    path.write_text(json.dumps(value))
    before = tree(client), tree(state)
    with pytest.raises(setup.SetupError):
        setup.restore_receipt(run_id, ["config"], write=True)
    assert (tree(client), tree(state)) == before


@pytest.mark.parametrize("kind", ["file", "parent", "missing_below_parent"])
def test_setup_plan_rejects_raw_symlink_components_without_reading_or_writing(isolated, tmp_path, kind):
    client, state = isolated
    real = tmp_path / "real-config"
    real.mkdir()
    target = real / "config.toml"
    target.write_text(ORIGINAL)
    link = tmp_path / "linked-config"
    try:
        link.symlink_to(target if kind == "file" else real, target_is_directory=kind != "file")
    except OSError:
        pytest.skip("fixture symlink creation unavailable")
    selected = link if kind == "file" else link / ("absent/config.toml" if kind == "missing_below_parent" else "config.toml")
    before = target.read_bytes(), target.stat().st_mode, target.stat().st_mtime_ns
    with pytest.raises((setup.SetupError, ValueError, OSError)):
        setup.setup_plan(options(config=str(selected)))
    assert (target.read_bytes(), target.stat().st_mode, target.stat().st_mtime_ns) == before
    assert not client.exists() and not state.exists()
    assert link.is_symlink()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX directory rename fixture")
def test_config_read_rejects_parent_replacement_before_open(isolated, tmp_path, monkeypatch):
    import os
    client, _state = isolated
    target = existing_config(client)
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    external = foreign / "config.toml"
    external.write_text('model = "external-secret"\n')
    moved = tmp_path / "moved-client"
    native_open = os.open
    swapped = False
    def replace_parent(path, flags, *args, **kwargs):
        nonlocal swapped
        selected = Path(path)
        if not swapped and (selected == target or (selected == Path(target.name) and "dir_fd" in kwargs)):
            client.rename(moved)
            client.symlink_to(foreign, target_is_directory=True)
            swapped = True
        return native_open(path, flags, *args, **kwargs)
    monkeypatch.setattr(setup.os, "open", replace_parent)
    monkeypatch.setattr(setup.os, "supports_dir_fd", {*os.supports_dir_fd, replace_parent})
    with pytest.raises((setup.SetupError, ValueError, OSError)):
        setup._read_file(target)
    assert swapped and external.read_text() == 'model = "external-secret"\n'
    assert (moved / "config.toml").read_text() == ORIGINAL


def test_config_read_rejects_content_change_during_capture(isolated, monkeypatch):
    client, _state = isolated
    target = existing_config(client)
    native_fdopen = setup.os.fdopen
    captured = []
    class ChangingRead:
        def __init__(self, stream): self.stream = stream
        def __enter__(self): return self
        def __exit__(self, *args): self.stream.close()
        def fileno(self): return self.stream.fileno()
        def read(self, count):
            value = self.stream.read(count)
            captured.append(True)
            target.write_bytes(ORIGINAL.encode() + b"# concurrent edit\n")
            return value
    monkeypatch.setattr(setup.os, "fdopen", lambda *a, **kw: ChangingRead(native_fdopen(*a, **kw)))
    with pytest.raises((setup.SetupError, ValueError, OSError)):
        setup._read_file(target)
    assert captured, "The rejection must exercise an actual changing read"


def test_journal_config_writer_never_follows_a_late_leaf_link(isolated, tmp_path, monkeypatch):
    client, _state = isolated
    target = existing_config(client)
    external = tmp_path / "external.toml"
    external.write_text(ORIGINAL)
    native_configure = cli.run_configure_codex
    def replace_then_configure(args):
        target.unlink()
        try:
            target.symlink_to(external)
        except OSError:
            pytest.skip("fixture symlink creation unavailable")
        return native_configure(args)
    monkeypatch.setattr(cli, "run_configure_codex", replace_then_configure)
    with pytest.raises((setup.SetupError, SystemExit)) as failure:
        cli_setup.run_setup(options(write=True))
    assert external.read_text() == ORIGINAL
    assert target.is_symlink(), str(failure.value)


@pytest.mark.parametrize("existing", [False, True])
def test_setup_plan_rejects_linked_ancestors_of_empty_or_missing_skill(isolated, tmp_path, existing):
    client, state = isolated
    actual = tmp_path / "actual-skills"
    actual.mkdir()
    if existing:
        (actual / "skills" / "anti").mkdir(parents=True)
    alias = tmp_path / "alias-skills"
    try:
        alias.symlink_to(actual, target_is_directory=True)
    except OSError:
        pytest.skip("fixture symlink creation unavailable")
    with pytest.raises((setup.SetupError, ValueError, OSError)):
        setup.setup_plan(options(install_skill=True, skill_dir=str(alias / "skills")))
    assert not client.exists() and not state.exists()
    assert not (actual / "skills" / "anti" / "SKILL.md").exists()


@pytest.mark.skipif(sys.platform == "win32", reason="Native held parent handles prohibit this POSIX directory rename")
def test_config_writer_refuses_same_hash_parent_swap_during_temp_fsync(isolated, tmp_path, monkeypatch):
    client, _state = isolated
    target = existing_config(client)
    replacement_parent = tmp_path / "replacement-client"
    replacement_parent.mkdir(mode=0o700)
    replacement_target = replacement_parent / target.name
    setup.SecureStore().atomic_write_bytes(replacement_target, ORIGINAL.encode())
    displaced_parent = tmp_path / "displaced-client"
    intended = (ORIGINAL + "# intended config update\n").encode()
    native_fsync = os.fsync
    swapped = False

    def swap_parent_after_temp_fsync(descriptor):
        nonlocal swapped
        native_fsync(descriptor)
        info = os.fstat(descriptor)
        if not swapped and stat.S_ISREG(info.st_mode):
            assert info.st_size == len(intended), "The hook must reach the intended temporary config write"
            client.rename(displaced_parent)
            replacement_parent.rename(client)
            swapped = True

    monkeypatch.setattr(setup.os, "fsync", swap_parent_after_temp_fsync)
    failure = None
    completed = False
    try:
        setup._write_bound_config(target, intended, expected_sha256=setup._hash(ORIGINAL.encode()))
        completed = True
    except setup.SetupError as exc:
        failure = exc

    assert swapped, "The fixture must replace an ordinary parent during the temporary regular-file fsync"
    assert target.read_bytes() == ORIGINAL.encode(), "The replacement parent's config must be preserved"
    assert (displaced_parent / target.name).read_bytes() == ORIGINAL.encode(), "The displaced original config must be preserved"
    assert isinstance(failure, setup.SetupError), "The writer must refuse the changed parent rather than return normally"
    assert not completed


def test_planned_config_refuses_ordinary_leaf_replacement_after_publication(isolated, tmp_path, monkeypatch):
    client, _state = isolated
    target = existing_config(client)
    args = options()
    plan = setup.setup_plan(args)
    merge_options = dict(model=args.model, provider_id=args.provider, provider_name=args.provider_name,
                         base_url=args.base_url, activate=args.activate)
    intended = cli.merge_codex_config(ORIGINAL, **merge_options).encode()
    assert intended != ORIGINAL.encode()
    replacement = tmp_path / "replacement-config.toml"
    setup.SecureStore().atomic_write_bytes(replacement, ORIGINAL.encode())
    native_replace = os.replace
    swapped = False
    published = None

    def replace_then_swap_leaf(source, destination, *positional, **kwargs):
        nonlocal swapped, published
        result = native_replace(source, destination, *positional, **kwargs)
        selected = Path(destination)
        if not swapped and (selected == target or
                            (selected == Path(target.name) and kwargs.get("dst_dir_fd") is not None)):
            published = target.read_bytes()
            assert published == intended, "The fixture must run after native publication of the intended bytes"
            native_replace(replacement, target)
            swapped = True
        return result

    monkeypatch.setattr(setup.os, "replace", replace_then_swap_leaf)
    failure = None
    result = None
    try:
        result = setup.write_planned_config(plan, **merge_options)
    except setup.SetupError as exc:
        failure = exc

    assert swapped and published == intended
    assert target.read_bytes() == ORIGINAL.encode()
    assert isinstance(failure, setup.SetupError), f"Changed success was returned for different visible bytes: {result}"
    assert result is None
