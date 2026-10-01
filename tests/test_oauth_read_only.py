"""OAuth diagnostics inspect real credential/store fixtures without repairing them."""

import json
import os
import stat
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from codex_antigravity_auth import byok, cli, constants, storage


FILE_ID = "fixture-client-id"
FILE_SECRET = "fixture-client-secret"


def tree_snapshot(root):
    """Capture directory inventory, bytes, modes, and mtimes without following links."""
    if not root.exists():
        return None
    result = {}
    for path in [root, *sorted(root.rglob("*"))]:
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            contents = os.readlink(path)
        elif stat.S_ISREG(info.st_mode):
            contents = path.read_bytes()
        else:
            contents = None
        result[str(path.relative_to(root))] = (info.st_mode, info.st_mtime_ns, contents)
    return result


@pytest.fixture
def isolated_home(monkeypatch, tmp_path):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("CODEX_ANTIGRAVITY_NO_UPDATE_CHECK", "1")
    for key in ("ANTIGRAVITY_CLIENT_ID", "ANTIGRAVITY_CLIENT_SECRET", "ANTIGRAVITY_STORAGE_KEY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(constants, "DEFAULT_CLIENT_ID", None)
    monkeypatch.setattr(constants, "DEFAULT_CLIENT_SECRET", None)
    monkeypatch.setattr(constants, "CREDENTIALS_FILE", str(home / ".codex/antigravity-credentials.json"))
    monkeypatch.setattr(storage, "ANTIGRAVITY_ACCOUNTS_FILE", str(home / ".codex/antigravity-accounts.json"))
    monkeypatch.setattr(byok, "PROVIDER_PRESETS", {})
    monkeypatch.setattr("keyring.get_password", lambda *args: None)
    monkeypatch.setattr("keyring.set_password", MagicMock(side_effect=AssertionError("must not create a key")))
    monkeypatch.setattr(cli, "service_status", lambda **kwargs: {"installed": False, "active": False})
    monkeypatch.setattr(cli, "gateway_status_info", lambda **kwargs: {"running": False, "status": "stopped"})
    monkeypatch.setattr(cli, "add_gateway_reachability", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli, "vision_sidecar_readiness", lambda: {"ok": False, "checks": []})
    monkeypatch.setattr(cli, "gateway_model_ids", lambda *args, **kwargs: {"claude-sonnet-4-6", "fixture:model"})
    response = MagicMock()
    response.status = 200
    response.__enter__.return_value = response
    monkeypatch.setattr("urllib.request.urlopen", MagicMock(return_value=response))
    return home


def write_credentials(home, kind):
    credentials = home / ".codex/antigravity-credentials.json"
    if kind == "missing":
        return credentials
    credentials.parent.mkdir(parents=True, exist_ok=True)
    if kind in {"symlink", "dangling-symlink"}:
        target = home / "linked-credentials.json"
        if kind == "symlink":
            target.write_text(json.dumps({"client_id": FILE_ID, "client_secret": FILE_SECRET}))
            target.chmod(0o644)
        try:
            credentials.symlink_to(target)
        except (OSError, NotImplementedError) as exc:
            pytest.skip(f"symlinks unavailable: {exc}")
    elif kind == "directory":
        credentials.mkdir()
    else:
        payload = "broken JSON" if kind == "malformed" else json.dumps({"client_id": FILE_ID, "client_secret": FILE_SECRET})
        credentials.write_text(payload)
        credentials.chmod(0o644 if kind == "insecure" else 0o600)
    return credentials


def write_stores(home):
    root = home / ".codex"
    root.mkdir(parents=True, exist_ok=True)
    (root / "antigravity-accounts.json").write_text(json.dumps({"accounts": [{"email": "fixture@example.com", "expiresAt": 9_999_999_999}]}))
    (root / "antigravity-providers.json").write_text(json.dumps({"providers": {"fixture": {
        "baseUrl": "https://example.invalid/v1", "apiKey": "fixture-api-key", "models": ["model"],
    }}}))
    (root / "config.toml").write_text(cli.render_codex_config_snippet(
        model="claude-sonnet-4-6", provider_id="antigravity", provider_name="Google Antigravity",
        base_url="http://localhost:51122/v1", activate=True,
    ))


COMMANDS = [
    ["setup"],
    ["setup", "--check"],
    ["setup", "--json"],
    ["setup-v2", "--check-google", "--check-byok"],
    ["doctor"],
    ["doctor", "--codex-ready"],
    ["doctor", "--codex-ready", "--json"],
    ["setup", "--check", "--model", "fixture:model"],
]


@pytest.mark.parametrize("command", COMMANDS, ids=lambda command: " ".join(command))
@pytest.mark.parametrize("kind", ["private", "insecure", "missing", "symlink", "dangling-symlink", "malformed", "directory"])
def test_diagnostic_commands_preserve_real_files(monkeypatch, capsys, isolated_home, command, kind):
    write_credentials(isolated_home, kind)
    write_stores(isolated_home)
    before = tree_snapshot(isolated_home)
    monkeypatch.setattr(sys, "argv", ["codex-antigravity", *command])
    chmod = MagicMock(side_effect=AssertionError("diagnostics must not chmod"))
    monkeypatch.setattr(os, "chmod", chmod)
    if hasattr(os, "fchmod"):
        monkeypatch.setattr(os, "fchmod", chmod)
    try:
        cli.main()
    except SystemExit as exc:
        assert exc.code == 1  # Missing/unusable credentials can fail readiness.
    assert tree_snapshot(isolated_home) == before
    chmod.assert_not_called()
    output = capsys.readouterr().out
    assert FILE_SECRET not in output
    if "--model" not in command:
        if kind == "insecure" and os.name != "nt":
            assert "Unsafe OAuth credential permissions" in output
            assert "0600" in output and "setup --write" in output
        elif kind in {"symlink", "dangling-symlink"}:
            assert "Refusing symlinked OAuth credentials" in output
        elif kind == "malformed":
            assert "valid JSON" in output
        elif kind == "directory":
            assert "not a regular file" in output
    if "--json" in command:
        assert isinstance(json.loads(output), dict)


@pytest.mark.parametrize("command", COMMANDS)
def test_clean_home_checks_create_no_state(monkeypatch, isolated_home, command):
    assert not isolated_home.exists()
    monkeypatch.setattr(sys, "argv", ["codex-antigravity", *command])
    try:
        cli.main()
    except SystemExit as exc:
        assert exc.code == 1
    assert not isolated_home.exists()


@pytest.mark.parametrize("env", [{}, {"ANTIGRAVITY_CLIENT_ID": "env-id"}, {"ANTIGRAVITY_CLIENT_SECRET": "env-secret"}, {"ANTIGRAVITY_CLIENT_ID": "env-id", "ANTIGRAVITY_CLIENT_SECRET": "env-secret"}])
@pytest.mark.parametrize("kind", ["private", "insecure", "missing", "symlink"])
def test_read_only_resolution_preserves_precedence_and_file_security(monkeypatch, isolated_home, env, kind):
    write_credentials(isolated_home, kind)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    before = tree_snapshot(isolated_home)
    warnings = []
    resolved = constants.resolve_oauth_credentials(read_only=True, warnings=warnings)
    file_usable = kind == "private" or (kind == "insecure" and os.name == "nt")
    assert resolved == (
        env.get("ANTIGRAVITY_CLIENT_ID", FILE_ID if file_usable else None),
        env.get("ANTIGRAVITY_CLIENT_SECRET", FILE_SECRET if file_usable else None),
    )
    assert tree_snapshot(isolated_home) == before
    assert FILE_SECRET not in " ".join(warnings)


def test_read_only_rejects_symlink_without_nofollow(monkeypatch, isolated_home):
    write_credentials(isolated_home, "symlink")
    before = tree_snapshot(isolated_home)
    monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)
    warnings = []
    assert constants.resolve_oauth_credentials(read_only=True, warnings=warnings) == (None, None)
    assert "symlink" in warnings[0]
    assert tree_snapshot(isolated_home) == before


def test_read_only_rejects_replaced_file_before_reading(monkeypatch, isolated_home):
    credentials = write_credentials(isolated_home, "private")
    original_open = os.open

    def replace_then_open(path, flags):
        # Move the original aside to guarantee the replacement has another inode.
        credentials.rename(credentials.with_suffix(".saved"))
        credentials.write_text(json.dumps({"client_id": "replacement-id", "client_secret": "replacement-secret"}))
        credentials.chmod(0o600)
        return original_open(path, flags)

    monkeypatch.setattr(os, "open", replace_then_open)
    warnings = []
    assert constants.resolve_oauth_credentials(read_only=True, warnings=warnings) == (None, None)
    assert "changed during inspection" in warnings[0]


@pytest.mark.parametrize("command", [["setup", "--write", "--no-input"], ["login"]])
def test_explicit_setup_and_login_still_repair_permissions(monkeypatch, isolated_home, command):
    credentials = write_credentials(isolated_home, "insecure")
    original = credentials.read_bytes()
    monkeypatch.setenv("ANTIGRAVITY_CLIENT_ID", "env-id")
    observed = []

    class StopBeforeNetwork(Exception):
        pass

    def capture_credentials(*args, **kwargs):
        observed.append(constants.resolve_oauth_credentials(read_only=True))
        raise StopBeforeNetwork

    # Stop after actual CLI credential preflight; never enter an OAuth flow.
    monkeypatch.setattr(cli, "authorize_antigravity", capture_credentials)
    monkeypatch.setattr(cli, "run_login", lambda args: cli.run_local_oauth_flow())
    monkeypatch.setattr(sys, "argv", ["codex-antigravity", *command])
    with pytest.raises(SystemExit if command[0] == "setup" else StopBeforeNetwork):
        cli.main()
    assert observed == [("env-id", FILE_SECRET)]
    assert credentials.read_bytes() == original
    if os.name != "nt":
        assert stat.S_IMODE(credentials.stat().st_mode) == 0o600


def test_disabled_version_check_does_not_create_cache(monkeypatch, isolated_home):
    monkeypatch.setattr(cli, "latest_pypi_version", MagicMock(side_effect=AssertionError("no version lookup")))
    assert cli.version_check_result()["status"] == "skip"
    assert not isolated_home.exists()


@pytest.mark.parametrize("store_kind", ["malformed", "encrypted-without-key", "symlink"])
def test_setup_v2_warns_without_crashing_or_repairing_unreadable_account_store(
    monkeypatch, capsys, isolated_home, store_kind,
):
    write_credentials(isolated_home, "private")
    write_stores(isolated_home)
    accounts = isolated_home / ".codex/antigravity-accounts.json"
    if store_kind == "symlink":
        target = isolated_home / "accounts-target.json"
        accounts.rename(target)
        try:
            accounts.symlink_to(target)
        except (OSError, NotImplementedError) as exc:
            pytest.skip(f"symlinks unavailable: {exc}")
    elif store_kind == "malformed":
        accounts.write_text('{"accounts": [')
    else:
        from cryptography.fernet import Fernet
        accounts.write_bytes(Fernet(Fernet.generate_key()).encrypt(b'{"accounts": []}'))
    before = tree_snapshot(isolated_home)
    monkeypatch.setattr(sys, "argv", ["codex-antigravity", "setup-v2", "--check-google"])
    cli.main()
    output = capsys.readouterr().out
    assert "[WARN] Google account rotation pool: could not inspect account store" in output
    assert FILE_SECRET not in output
    assert tree_snapshot(isolated_home) == before


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits do not describe Windows ACLs")
@pytest.mark.parametrize("json_output", [False, True])
def test_readiness_warns_about_unsafe_credentials_even_without_codex_config(
    monkeypatch, capsys, isolated_home, json_output,
):
    write_credentials(isolated_home, "insecure")
    before = tree_snapshot(isolated_home)
    command = ["codex-antigravity", "doctor", "--codex-ready"]
    monkeypatch.setattr(sys, "argv", command + (["--json"] if json_output else []))
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 1
    output = capsys.readouterr().out
    assert "Unsafe OAuth credential permissions" in output
    assert "0600" in output and "setup --write" in output
    if json_output:
        warning = next(check for check in json.loads(output)["checks"] if check["name"] == "google_oauth_credentials_file")
        assert warning["status"] == "warn"
    assert tree_snapshot(isolated_home) == before
