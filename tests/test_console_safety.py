"""Captured output tests; control fixtures are never sent to a real terminal."""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from standalone import without_installed_packages

from codex_antigravity_auth import cli
from codex_antigravity_auth.console import console_print, safe_terminal_text

SCRIPTS = Path(__file__).resolve().parents[1] / "codex_antigravity_auth/skills/anti/scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
import anti

TEXT = "café\n\t**markdown** \x1b]52;c;fixture\x07 \x1b[31m red \x9b31m \r end"


def assert_safe(text):
    assert all(ord(char) not in {*range(32), *range(0x7F, 0xA0)} - {9, 10} for char in text)


def test_escape_all_terminal_controls_preserves_normal_text():
    controls = "".join(chr(code) for code in (*range(32), *range(0x7F, 0xA0)))
    rendered = safe_terminal_text(controls + "café\n\t**markdown**")
    assert_safe(rendered)
    assert "café\n\t**markdown**" in rendered
    assert "\\x1b" in rendered and "\\x07" in rendered


@pytest.mark.parametrize("stderr", [False, True])
def test_console_print_covers_values_separators_and_end(capsys, stderr):
    console_print(TEXT, TEXT, sep="\x1b", end="\x07", file=sys.stderr if stderr else sys.stdout)
    output = capsys.readouterr()
    rendered = output.err if stderr else output.out
    assert_safe(rendered)
    assert "café\n\t**markdown**" in rendered


@pytest.mark.parametrize("panel", [False, True])
@pytest.mark.parametrize("output_json", [False, True])
def test_anti_output_and_labels_are_safe_but_json_roundtrips(capsys, panel, output_json):
    if panel:
        anti.print_panel_result(
            panel_mode="ask", base_url="http://fixture.invalid", judge_model=TEXT,
            panel_models=[TEXT], panel_results=[], text=TEXT, caveats=[TEXT],
            metadata={"panel_status": "complete_multi_model"}, output_json=output_json,
        )
    else:
        anti.print_result(mode="consult", model=TEXT, base_url="http://fixture.invalid", text=TEXT, caveats=[TEXT], output_json=output_json)
    rendered = capsys.readouterr().out
    assert_safe(rendered)
    if output_json:
        result = json.loads(rendered)
        assert result["output_text"] == TEXT
        assert result["caveats"] == [TEXT]
    else:
        assert "café\n\t**markdown**" in rendered
        assert "\\x1b" in rendered
    # Cleaning model data does not perform display-only rewriting.
    assert anti.clean_string(TEXT) == TEXT


def test_anti_errors_and_argument_labels_are_escaped(capsys):
    anti.eprint(TEXT)
    assert_safe(capsys.readouterr().err)
    with pytest.raises(SystemExit) as exc:
        anti.main(["--invalid-" + TEXT])
    assert exc.value.code == 2
    assert_safe(capsys.readouterr().err)


def test_gateway_raw_error_output_is_escaped(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(cli, "_diagnostic_all_provider_configs", lambda: (_ for _ in ()).throw(RuntimeError(TEXT)))
    monkeypatch.setattr(cli, "version_check_result", lambda: {"status": "skip", "detail": "disabled"})
    assert not cli.run_doctor(byok_only=True, config=str(tmp_path / "missing.toml"))
    rendered = capsys.readouterr().out
    assert_safe(rendered)
    assert "\\x1b" in rendered


def test_gateway_system_exit_error_is_escaped(monkeypatch):
    def fail(*args, **kwargs):
        raise SystemExit(TEXT)
    monkeypatch.setattr(cli, "run_doctor", fail)
    monkeypatch.setattr(sys, "argv", ["codex-antigravity", "doctor"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert_safe(exc.value.code)
    assert "\\x1b" in exc.value.code


def test_gateway_argument_error_is_escaped(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["codex-antigravity", "--invalid-" + TEXT])
    with pytest.raises(SystemExit):
        cli.main()
    assert_safe(capsys.readouterr().err)


def test_standalone_anti_uses_same_console_protection(tmp_path):
    # No site packages or repository path: exercise the installed helper layout.
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    shutil.copy2(SCRIPTS / "anti.py", scripts / "anti.py")
    shutil.copytree(SCRIPTS / "anti_lib", scripts / "anti_lib", ignore=shutil.ignore_patterns("__pycache__"))
    code = "import sys; sys.path.insert(0, sys.argv[1]); import anti; anti.print_result(mode='consult', model='fixture', base_url='http://fixture.invalid', text='safe\\x1b]fixture\\x07end')"
    completed = subprocess.run([sys.executable, "-c", without_installed_packages(code), str(scripts)], cwd=tmp_path, capture_output=True, text=True, timeout=10)
    assert completed.returncode == 0, completed.stderr
    assert_safe(completed.stdout)
    assert "safe\\x1b]fixture\\x07end" in completed.stdout


@pytest.mark.parametrize("outcome", ["busy", "empty", "failure", "success"])
def test_refresh_snapshot_logs_safely_without_changing_account_data(monkeypatch, outcome):
    import io
    import logging
    from unittest.mock import MagicMock
    from codex_antigravity_auth import accounts, oauth

    captured = io.StringIO()
    logger = logging.Logger("synthetic-account-log", level=logging.DEBUG)
    logger.addHandler(logging.StreamHandler(captured))
    logger.propagate = False
    monkeypatch.setattr(accounts, "_log", logger)
    lock = MagicMock()
    lock.acquire.return_value = outcome != "busy"
    monkeypatch.setattr(accounts, "_get_refresh_lock", lambda email: lock)
    refresh = MagicMock(return_value={"access_token": "synthetic-access", "expires_in": 3600})
    monkeypatch.setattr(accounts, "refresh_access_token", refresh)
    discover = MagicMock(return_value=TEXT if outcome == "success" else None)
    if outcome == "failure":
        discover.side_effect = RuntimeError(TEXT)
    monkeypatch.setattr(oauth, "discover_project_id", discover)
    account = {"email": TEXT, "refreshToken": "synthetic-refresh", "accessToken": "expired", "expiresAt": 0}
    monkeypatch.setattr(accounts, "update_accounts", lambda mutator: mutator({"accounts": [account]}))
    result = accounts.AccountManager()._refresh_snapshot(dict(account))
    assert result == ("busy" if outcome == "busy" else "refreshed")
    rendered = captured.getvalue()
    assert_safe(rendered)
    if outcome == "failure":
        assert "Project discovery failed" in rendered
    assert account["email"] == TEXT
    if outcome == "success":
        assert account["projectId"] == TEXT
    if outcome == "busy":
        refresh.assert_not_called()


def test_account_confirmation_prompt_is_escaped_before_input(monkeypatch):
    prompts = []
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: prompts.append(prompt) or "n")
    assert not cli._confirm_account_mutation(TEXT, yes=False, non_interactive_error="fixture")
    assert len(prompts) == 1
    assert_safe(prompts[0])
    assert "\\x1b" in prompts[0]
