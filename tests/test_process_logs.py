"""Synthetic runtime messages and temporary files; no gateway or credentials used."""
from concurrent.futures import ThreadPoolExecutor
import io
import logging
import os
from pathlib import Path
import sys
from unittest.mock import Mock

import pytest

from codex_antigravity_auth import accounts, byok, cli, process_logs as logs, service


def record(message, *args, **kwargs):
    return logging.LogRecord("fixture", logging.WARNING, __file__, 1, message, args, kwargs.get("exc_info"))


def test_runtime_record_redacts_credentials_email_controls_and_exception_values(tmp_path):
    console = io.StringIO()
    path = logs.log_path(tmp_path, 51122)
    handler = logs.BoundedProcessHandler(path, console=console)
    try:
        raise ValueError("unlabelled-fixture-secret person@example.invalid")
    except ValueError:
        handler.handle(record('person@example.invalid password="fixture first\nsecond" api_key=sk-fixtureabcdefghijklmnop\x1b[31m', exc_info=sys.exc_info()))
    output = path.read_text()
    assert output == console.getvalue()
    for sentinel in ("person@example.invalid", "fixture first", "second", "sk-fixture", "unlabelled-fixture-secret", "\x1b"):
        assert sentinel not in output
    assert "ValueError" in output and "test_process_logs.py" in output
    assert len(output.splitlines()) == 1


def test_opaque_account_references_are_stable_only_within_process(monkeypatch):
    first = logs.account_ref("person@example.invalid")
    assert first == logs.account_ref("person@example.invalid")
    assert first != logs.account_ref("another@example.invalid")
    assert "person" not in first
    monkeypatch.setattr(logs, "_SALT", b"synthetic-different-process-salt")
    assert logs.account_ref("person@example.invalid") != first


def test_account_refresh_and_cooldown_runtime_messages_never_include_identity_or_project(tmp_path, monkeypatch):
    path = logs.log_path(tmp_path, 51122)
    account = {"email": "person@example.invalid", "refreshToken": "fixture-refresh"}
    monkeypatch.setattr(accounts, "refresh_access_token", lambda _: {"access_token": "fixture-access", "expires_in": 3600})
    monkeypatch.setattr("codex_antigravity_auth.oauth.discover_project_id", lambda _: "private-project-sentinel")
    manager = accounts.AccountManager()
    monkeypatch.setattr(manager, "_mutate_state", lambda f: f(manager._state_owner))
    with logs.runtime_logging(path, console=False):
        assert accounts._apply_token_refresh(account, "fixture-refresh")
        manager.mark_failure(account["email"], "fixture-unlabelled-secret person@example.invalid", status_code=429)
        del account["projectId"]
        monkeypatch.setattr("codex_antigravity_auth.oauth.discover_project_id", Mock(side_effect=ValueError("fixture-unlabelled-secret")))
        assert accounts._apply_token_refresh(account, "fixture-refresh")
    output = path.read_text()
    assert "acct_" in output and "ValueError" in output and "Discovered project" in output
    for sentinel in ("person@example.invalid", "private-project-sentinel", "fixture-access", "fixture-refresh", "fixture-unlabelled-secret"):
        assert sentinel not in output
    assert account["email"] == "person@example.invalid" and account["accessToken"] == "fixture-access"


def test_invalid_provider_warning_omits_arbitrary_display_name(tmp_path):
    path = logs.log_path(tmp_path, 51122)
    with logs.runtime_logging(path, console=False):
        provider = byok.normalize_provider_entry({"displayName": "person@example.invalid fixture-private-label", "apiKey": "fixture\nbad"})
    assert "apiKey" not in provider
    output = path.read_text()
    assert "failed validation" in output
    assert "person@example.invalid" not in output and "fixture-private-label" not in output


def test_bounded_rotation_truncates_after_redaction_and_preserves_legacy_logs(tmp_path):
    legacy = tmp_path / "antigravity-gateway-51122.log"
    legacy.write_bytes(b"legacy bytes must remain untouched")
    old = legacy.stat()
    path = logs.log_path(tmp_path, 51122)
    handler = logs.BoundedProcessHandler(path, max_bytes=512, backups=2)
    for index in range(40):
        handler.handle(record("record %d %s person@example.invalid password=fixture-long-secret", index, "🦊" * 8000))
    assert handler.dropped == 0
    files = [path, Path(f"{path}.1"), Path(f"{path}.2")]
    assert all(file.exists() and file.stat().st_size <= 512 for file in files)
    assert len(list(path.parent.glob("*.log*"))) == 4  # Three logs and the cross-process lock.
    assert "record 39" in path.read_text() and "[truncated]" in path.read_text()
    assert all("fixture-long-secret" not in file.read_text() for file in files)
    assert legacy.read_bytes() == b"legacy bytes must remain untouched" and legacy.stat().st_mtime_ns == old.st_mtime_ns
    if os.name != "nt":
        assert all(file.stat().st_mode & 0o777 == 0o600 for file in files)


def test_multiple_handlers_serialize_rotation_with_bounded_files(tmp_path):
    path = logs.log_path(tmp_path, 51122)
    handlers = [logs.BoundedProcessHandler(path, max_bytes=512) for _ in range(4)]
    def write(index):
        for item in range(100):
            handlers[index].handle(record("writer=%d item=%d payload=%s", index, item, "🦊" * 70))
    with ThreadPoolExecutor(4) as pool:
        list(pool.map(write, range(4)))
    assert sum(handler.dropped for handler in handlers) == 0
    assert all(file.stat().st_size <= 512 for file in path.parent.glob("*.log*"))
    for file in path.parent.glob("*.log*"):
        file.read_text()  # No torn UTF-8 records.


def test_rotation_failure_drops_log_without_interrupting_gateway_work(tmp_path, monkeypatch):
    path = logs.log_path(tmp_path, 51122)
    handler = logs.BoundedProcessHandler(path, max_bytes=128)
    handler.handle(record("first " + "x" * 90))
    before = path.read_bytes()
    monkeypatch.setattr(logs.os, "replace", Mock(side_effect=OSError("synthetic failure")))
    handler.handle(record("second " + "y" * 90))
    assert handler.dropped == 1 and path.read_bytes() == before
    assert path.stat().st_size <= 128


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "unowned_directory", "bad_marker"])
def test_unknown_or_unsafe_log_files_are_preserved(tmp_path, kind):
    path = logs.log_path(tmp_path, 51122)
    original = tmp_path / "original"
    original.write_bytes(b"preserve original")
    if kind == "unowned_directory":
        path.parent.mkdir()
        path.write_bytes(b"preexisting unmanaged data")
    else:
        logs.prepare_log(path)
        if kind == "bad_marker":
            (path.parent / ".policy-v1").write_bytes(b"unknown version")
        else:
            path.unlink()
            if kind == "symlink":
                try:
                    path.symlink_to(original)
                except OSError:
                    pytest.skip("symlink privilege unavailable")
            else:
                os.link(original, path)
    with pytest.raises(OSError):
        logs.prepare_log(path)
    assert original.read_bytes() == b"preserve original"
    if kind == "unowned_directory":
        assert path.read_bytes() == b"preexisting unmanaged data"


def test_runtime_streams_suppress_fragmented_secrets_and_access_urls(tmp_path):
    path = logs.log_path(tmp_path, 51122)
    stdout, stderr = sys.stdout, sys.stderr
    root_handlers = list(logging.getLogger().handlers)
    with logs.runtime_logging(path, console=False):
        sys.stdout.write('password="fixture ')
        sys.stdout.write('secret\n tail"\n')
        sys.stderr.write("person@example.invalid\n")
        logging.getLogger("uvicorn.access").info("GET /?token=fixture-bearer person@example.invalid")
    output = path.read_text()
    assert "Unstructured stdout output suppressed" in output
    assert "Unstructured stderr output suppressed" in output
    assert "request access record omitted" in output
    assert "fixture" not in output and "person@example.invalid" not in output
    assert sys.stdout is stdout and sys.stderr is stderr
    assert logging.getLogger().handlers == root_handlers


def test_gateway_entrypoint_installs_policy_before_uvicorn_and_suppresses_raw_failures(tmp_path, monkeypatch):
    path = logs.log_path(tmp_path, 51122)
    import uvicorn
    def fail(*args, **kwargs):
        assert kwargs["log_config"] is None and kwargs["access_log"] is False
        logging.getLogger("uvicorn.error").warning("email person@example.invalid api_key=fixture-key")
        raise RuntimeError("raw-fixture-secret")
    monkeypatch.setattr(uvicorn, "run", fail)
    with pytest.raises(SystemExit) as error:
        logs.run_gateway("127.0.0.1", 51122, path=path, console=False)
    assert error.value.code == 1
    output = path.read_text()
    assert "RuntimeError" in output
    assert "fixture-key" not in output and "raw-fixture-secret" not in output and "person@example.invalid" not in output


def test_service_manifests_use_same_writer_and_disable_append_only_capture():
    mac = service.render_macos_launch_agent(51122, "127.0.0.1")
    linux = service.render_linux_systemd_unit(51122, "127.0.0.1")
    assert "--quiet-runtime-console" in mac and "--quiet-runtime-console" in linux
    assert mac.count("<string>/dev/null</string>") == 2
    assert "StandardOutput=null" in linux and "StandardError=null" in linux
    assert "append:" not in linux and ".out.log" not in mac and ".err.log" not in mac


def test_process_log_diagnostics_do_not_read_contents_or_create_missing_root(tmp_path):
    home = tmp_path / "missing"
    result = logs.process_log_info(home, 51122)
    assert result["max_total_bytes"] == 6 * 1024 * 1024
    assert result["kind"] == "process" and result["contents_included"] is False
    assert len(result["legacy_paths"]) == 3 and not home.exists()


def test_cli_foreground_routes_to_managed_writer_without_starting_gateway(tmp_path, monkeypatch):
    fake = Mock()
    monkeypatch.setattr(logs, "run_gateway", fake)
    monkeypatch.setattr(cli, "get_codex_home", lambda: tmp_path)
    monkeypatch.setattr(sys, "argv", ["codex-antigravity", "start", "--port", "51122"])
    cli.main()
    assert fake.call_args.args == ("127.0.0.1", 51122)
    assert fake.call_args.kwargs["path"] == logs.log_path(tmp_path, 51122)


def _child_log_writer(path, index):
    handler = logs.BoundedProcessHandler(Path(path), max_bytes=512)
    for item in range(80):
        handler.handle(record("process=%d item=%d %s", index, item, "x" * 200))
    if handler.dropped:
        raise RuntimeError("synthetic child lost log writes")


@pytest.mark.skipif(os.name == "nt", reason="Native Windows subprocess locking is not exercised on this platform")
def test_cross_process_rotation_keeps_complete_bounded_records(tmp_path):
    import multiprocessing
    context = multiprocessing.get_context("fork")  # Inherits the credential-free runner's protections.
    path = logs.log_path(tmp_path, 51122)
    logs.prepare_log(path)
    children = [context.Process(target=_child_log_writer, args=(str(path), index)) for index in range(3)]
    try:
        for child in children:
            child.start()
        for child in children:
            child.join(10)
            assert child.exitcode == 0
    finally:
        for child in children:
            if child.is_alive():
                child.terminate()
                child.join(2)
    for file in [path, Path(f"{path}.1"), Path(f"{path}.2")]:
        assert file.stat().st_size <= 512
        assert all("process=" in line and "item=" in line for line in file.read_text().splitlines())


def test_concurrent_port_initialization_publishes_one_owned_policy(tmp_path):
    with ThreadPoolExecutor(4) as pool:
        list(pool.map(lambda port: logs.prepare_log(logs.log_path(tmp_path, port)), range(51122, 51126)))
    directory = tmp_path / logs.DIRECTORY
    assert (directory / ".policy-v1").read_bytes() == logs.POLICY
    assert len(list(directory.glob("gateway-*.log"))) == 4


def test_runtime_stream_binary_writes_and_descriptors_are_discarded_and_closed(tmp_path):
    path = logs.log_path(tmp_path, 51122)
    captured, descriptors = [], []
    with logs.runtime_logging(path, console=False):
        for stream in (sys.stdout, sys.stderr):
            captured.append(stream)
            assert stream.writable() and stream.buffer.writable()
            assert not stream.isatty() and not stream.buffer.isatty()
            payload = b'person@example.invalid password="fixture-binary-secret"\xff\n'
            assert stream.buffer.write(payload) == len(payload)
            assert stream.buffer.write(bytearray(payload)) == len(payload)
            assert stream.buffer.write(memoryview(payload)) == len(payload)
            stream.buffer.writelines([payload, payload])
            stream.buffer.flush()
            descriptor = stream.fileno()
            assert stream.buffer.fileno() == descriptor
            descriptors.append(descriptor)
            assert os.write(descriptor, payload) == len(payload)
            assert stream.write("fixture-text-secret\n") == 20
        logging.getLogger("fixture.runtime").info("request work continues")
    output = path.read_text()
    assert "request work continues" in output
    assert output.count("Unstructured stdout output suppressed") == 1
    assert output.count("Unstructured stderr output suppressed") == 1
    assert all(value not in output for value in ("person@example.invalid", "fixture-binary-secret", "fixture-text-secret"))
    for stream, descriptor in zip(captured, descriptors):
        assert stream.closed and stream.buffer.closed
        with pytest.raises(OSError):
            os.fstat(descriptor)


def test_service_log_paths_bind_target_user_under_synthetic_sudo(tmp_path, monkeypatch, capsys):
    import json
    from types import SimpleNamespace
    from codex_antigravity_auth import cli_service
    invoking = tmp_path / "invoking-root"
    target = tmp_path / "service-owner"
    monkeypatch.setenv("HOME", str(invoking))
    monkeypatch.setenv("USERPROFILE", str(invoking))
    monkeypatch.setenv("SUDO_USER", "synthetic-service-owner")
    lookup = Mock(return_value=SimpleNamespace(pw_dir=str(target)))
    monkeypatch.setitem(sys.modules, "pwd", SimpleNamespace(getpwnam=lookup))
    monkeypatch.setattr(cli, "service_status", lambda _: {"installed": True, "active": True})
    monkeypatch.setattr(cli, "reachable_gateway_status_info", lambda *args, **kwargs: {"reachable": False, "status": "stopped"})
    result = cli_service.run_service_command(SimpleNamespace(service_command="status", port=51122, json=True))
    assert json.loads(capsys.readouterr().out) == result
    expected = logs.log_path(target / ".codex", 51122)
    assert result["process_log"]["path"] == str(expected)
    assert all(str(target) in item for item in result["process_log"]["legacy_paths"])
    assert result["request_log"]["path"] == str(target / ".codex/antigravity-requests.jsonl")
    command = service.service_command(51122, "127.0.0.1")
    assert command[command.index("--process-log") + 1] == str(expected)
    assert str(expected) in service.render_linux_systemd_unit(51122, "127.0.0.1")
    assert str(expected) in service.render_macos_launch_agent(51122, "127.0.0.1")
    assert not invoking.exists() and not target.exists()
    assert all(call.args == ("synthetic-service-owner",) for call in lookup.call_args_list)
