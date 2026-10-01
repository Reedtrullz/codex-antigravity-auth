"""Check profiles run only mocked tools or explicitly synthetic child programs."""
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
from unittest.mock import Mock

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "codex_antigravity_auth/skills/anti/scripts/anti.py"


@pytest.fixture
def checks(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location("anti_check_fixture", SCRIPT)
    anti = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(anti)
    import anti_lib.verifier as verifier
    monkeypatch.setattr(verifier, "_fixture_find_eslint", verifier._find_eslint, raising=False)
    monkeypatch.setattr(verifier, "_find_eslint", lambda _: None)
    monkeypatch.setattr(anti, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(anti, "helper_identity", lambda: {})
    workspace = tmp_path / "project"
    workspace.mkdir()
    return anti, verifier, workspace


def test_python_checks_never_execute_source_or_write_pyc(checks, monkeypatch):
    _, verifier, root = checks
    source = b"raise RuntimeError('must not run')\n"
    path = root / "fixture.py"
    path.write_bytes(source)
    monkeypatch.setattr(verifier.subprocess, "Popen", Mock(side_effect=AssertionError("no process for builtins")))
    finding = {"file": "fixture.py", "verify": "touch should-not-exist", "evidence": "model claim"}
    checked = verifier.verify_finding(finding, root)
    assert checked["evidence"] == "model claim"
    assert checked["claimVerdict"] == checked["verificationStatus"] == "unverified"
    assert all(row["status"] == "passed" for row in checked["checks"])
    assert all(row["fileHash"] == hashlib.sha256(source).hexdigest() for row in checked["checks"])
    assert all(row["cwd"] == str(root) for row in checked["checks"])
    assert path.read_bytes() == source
    assert list(root.rglob("*")) == [path]
    assert "checks" not in finding


def test_syntax_and_secret_failures_are_structured_without_secret_values(checks):
    _, verifier, root = checks
    secret = "synthetic-secret-0123456789"
    (root / "fixture.py").write_text(f"api_key = '{secret}'\ndef broken(:\n")
    checked = verifier.verify_finding({"file": "fixture.py", "evidence": "unverified"}, root)
    assert {row["reason"] for row in checked["checks"]} == {"syntax_error", "credential_pattern"}
    assert all(row["status"] == "failed" for row in checked["checks"])
    assert secret not in json.dumps(checked)
    assert checked["evidence"] == checked["claimVerdict"] == "unverified"


@pytest.mark.parametrize("label,reason", [(None, "file_not_provided"), ("missing.py", "file_missing_or_not_regular"), ("../outside.py", "path_outside_workspace")])
def test_missing_and_escaping_files_are_distinct_skips(checks, label, reason):
    _, verifier, root = checks
    result = verifier.verify_finding({"file": label}, root)["checks"][0]
    assert result["status"] == "skipped" and result["reason"] == reason
    assert result["fileHash"] is None


def test_read_error_and_size_limit_are_distinct(checks, monkeypatch):
    _, verifier, root = checks
    path = root / "fixture.py"
    path.write_bytes(b"x" * (verifier.MAX_FILE_BYTES + 1))
    assert verifier.verify_finding({"file": "fixture.py"}, root)["checks"][0]["reason"] == "file_size_limit"
    original = Path.open
    def denied(self, *a, **kw):
        if self == path:
            raise PermissionError("synthetic denied read")
        return original(self, *a, **kw)
    monkeypatch.setattr(Path, "open", denied)
    check = verifier.verify_finding({"file": "fixture.py"}, root)["checks"][0]
    assert check["status"] == "error" and check["reason"] == "file_read_error"


def test_duplicate_findings_share_one_check_per_file_hash(checks, monkeypatch):
    _, verifier, root = checks
    (root / "fixture.py").write_text("value = 1\n")
    original = verifier._checks
    calls = []
    def spy(*a):
        calls.append(a)
        return original(*a)
    monkeypatch.setattr(verifier, "_checks", spy)
    results = verifier.verify_findings([{"file": "fixture.py"}, {"file": str(root / "fixture.py")}], root)
    assert len(calls) == 1
    assert results[0]["checks"] == results[1]["checks"]
    results[0]["checks"][0]["status"] = "changed"
    assert results[1]["checks"][0]["status"] == "passed"


def test_eslint_is_never_discovered_or_run_without_operator_profile(checks, monkeypatch):
    _, verifier, root = checks
    (root / "fixture.js").write_text("let value = 1;\n")
    monkeypatch.setattr(verifier, "_find_eslint", Mock(side_effect=AssertionError("not opted in")))
    checked = verifier.verify_finding({"file": "fixture.js", "verify": "eslint --fix ."}, root)
    lint = next(row for row in checked["checks"] if row["check"] == "eslint")
    assert lint["status"] == "skipped" and lint["reason"] == "profile_not_enabled"


def test_missing_eslint_is_reported_as_skip(checks):
    _, verifier, root = checks
    (root / "fixture.js").write_text("let value = 1;\n")
    lint = verifier.verify_finding({"file": "fixture.js"}, root, profiles=["eslint"])["checks"][-1]
    assert lint["reason"] == "tool_missing" and lint["status"] == "skipped"


@pytest.mark.parametrize("status,reason,output", [
    ("passed", "tool_passed", '[{"messages":[]}]'),
    ("failed", "lint_errors", '[{"messages":[{"message":"synthetic lint problem"}]}]'),
    ("error", "configuration_or_tool_error", "synthetic bad project configuration"),
    ("error", "tool_timeout", "synthetic timeout"),
])
def test_opted_profile_records_exact_snapshot_cwd_and_preserves_project_cache(checks, monkeypatch, status, reason, output):
    _, verifier, root = checks
    path = root / "fixture.js"
    source = b"let value = 1;\n"
    path.write_bytes(source)
    cache = root / ".eslintcache"
    cache.write_bytes(b"synthetic project cache")
    monkeypatch.setattr(verifier, "_find_eslint", lambda _: "/synthetic/bin/eslint")
    def run(command, contents, cwd):
        assert cwd == root and contents == source
        assert "--no-eslintrc" not in command
        assert "--no-fix" in command and "--no-cache" in command
        assert "./fixture.js" == command[command.index("--stdin-filename") + 1]
        cache_path = Path(command[command.index("--cache-location") + 1])
        assert not cache_path.is_relative_to(root)
        # Simulate the checker deleting only its explicitly configured cache.
        cache_path.write_text("temporary")
        cache_path.unlink()
        path.write_bytes(b"changed after capture")
        return {"status": status, "reason": reason, "output": output}
    monkeypatch.setattr(verifier, "_run_check", run)
    result = verifier.verify_finding({"file": "fixture.js"}, root, profiles=["eslint"])
    lint = result["checks"][-1]
    assert lint["status"] == status and lint["reason"] == reason
    assert lint["fileHash"] == hashlib.sha256(source).hexdigest()
    assert lint["configSource"] == "project-auto-discovery"
    assert lint["effectiveConfigHash"] is None
    assert cache.read_bytes() == b"synthetic project cache"
    assert result["claimVerdict"] == "unverified"


@pytest.mark.parametrize("output,reason", [('[{"messages":[{"message":"File ignored because no matching configuration was supplied."}]}]', "file_ignored"), ("[]", "no_lint_result"), ("not json", "invalid_tool_report")])
def test_ignored_or_invalid_tool_output_is_not_a_pass(checks, monkeypatch, output, reason):
    _, verifier, root = checks
    (root / "fixture.js").write_text("let value = 1;")
    monkeypatch.setattr(verifier, "_find_eslint", lambda _: "/synthetic/eslint")
    monkeypatch.setattr(verifier, "_run_check", lambda *a: {"status": "passed", "reason": "tool_passed", "output": output})
    check = verifier.verify_finding({"file": "fixture.js"}, root, profiles=["eslint"])["checks"][-1]
    assert check["reason"] == reason
    assert check["status"] == ("error" if reason == "invalid_tool_report" else "skipped")


def test_synthetic_process_timeout_and_bounded_output(checks, monkeypatch):
    _, verifier, root = checks
    monkeypatch.setattr(verifier, "CHECK_TIMEOUT_SECONDS", 0.1)
    timeout = verifier._run_check([sys.executable, "-c", "import time; time.sleep(10)"], b"", root)
    assert timeout["status"] == "error" and timeout["reason"] == "tool_timeout"
    monkeypatch.setattr(verifier, "CHECK_TIMEOUT_SECONDS", 5)
    overflow = verifier._run_check([sys.executable, "-c", "print('synthetic-output-'*10000)"], b"", root)
    assert overflow["status"] == "error" and overflow["reason"] == "output_limit"
    assert overflow["outputTruncated"] is True
    assert "synthetic-output-" not in overflow["output"]


def test_check_output_is_redacted_before_preview_clipping(checks):
    _, verifier, root = checks
    secret = "sk-syntheticfixture01234567890123456789"
    check = verifier._result("fixture", "error", "fixture", path="fixture.py", digest="0" * 64,
                             cwd=root, command=["synthetic"], output="api_key=" + secret + " " + "x" * 3000)
    assert secret not in check["output"]
    assert len(check["output"]) <= verifier.MAX_OUTPUT_CHARS


def test_cli_profile_choice_is_explicit_and_no_verify_conflicts(checks):
    anti, _, _ = checks
    args = anti.build_parser().parse_args(["panel", "--check-profile", "eslint", "--prompt", "fixture"])
    assert args.check_profile == ["eslint"]
    with pytest.raises(SystemExit):
        anti.build_parser().parse_args(["panel", "--check-profile", "eslint", "--no-verify"])
    with pytest.raises(SystemExit):
        anti.build_parser().parse_args(["panel", "--check-profile", "model-supplied-command"])


def test_windows_profile_uses_node_entry_without_shell_wrapper(checks, monkeypatch):
    _, verifier, root = checks
    monkeypatch.setattr(verifier, "WINDOWS", True)
    monkeypatch.setattr(verifier.shutil, "which", lambda name: "/synthetic/node" if name == "node" else "/synthetic/eslint.cmd")
    entry = root / "node_modules/eslint/bin/eslint.js"
    entry.parent.mkdir(parents=True)
    entry.write_text("synthetic executable identity; never executed")
    assert verifier._fixture_find_eslint(root) == ["/synthetic/node", str(entry)]


def test_profile_is_forwarded_by_workflow_expansion(checks):
    anti, _, _ = checks
    args = anti.build_parser().parse_args(["workflow", "review-ready", "--check-profile", "eslint"])
    expanded = anti.workflow_expansion(args)
    assert expanded[expanded.index("--check-profile") + 1] == "eslint"


def test_panel_checks_are_deduplicated_and_claims_stay_unverified(checks, monkeypatch, capsys):
    anti, verifier, root = checks
    monkeypatch.chdir(root)
    source = root / "fixture.py"
    source.write_text("value = 1\n")
    monkeypatch.setattr(anti, "fetch_model_ids", lambda *a, **kw: {"claude-sonnet-4-6", "claude-opus-4-6-thinking"})
    response = json.dumps({"summary": "synthetic review", "findings": [
        {"id": "F1", "file": "fixture.py", "line": 1, "severity": "low", "claim": "First semantic claim", "verify": "touch malicious-file"},
        {"id": "F2", "file": "fixture.py", "line": 1, "severity": "medium", "claim": "Second semantic claim", "verify": "run arbitrary command"},
    ]})
    monkeypatch.setattr(anti, "post_response", lambda **kw: response)
    original = verifier._checks
    calls = []
    def spy(*a):
        calls.append(a)
        return original(*a)
    monkeypatch.setattr(verifier, "_checks", spy)
    rc = anti.main(["panel", "--mode", "review", "--scope", "files", "--file", "fixture.py", "--json", "--no-progress", "--save-output", "full"])
    assert rc == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["verification"]["claimVerdict"] == "unverified"
    assert printed["verification"]["checkCounts"] == {"passed": 2, "failed": 0, "skipped": 0, "error": 0}
    assert len(calls) == 1
    record_path = next(anti.RUNS_DIR.glob("*.json"))
    record = anti.load_run_record(record_path)
    artifact = json.loads(Path(record["resultPath"]).read_text())
    assert artifact["verification"]["claimVerdict"] == "unverified"
    assert all(finding["claimVerdict"] == "unverified" for finding in artifact["findings"])
    assert list(root.iterdir()) == [source]


def test_summary_retains_check_counts_without_malformed_clipped_details(checks):
    from types import SimpleNamespace
    anti, verifier, root = checks
    records = [verifier._result("synthetic", "failed", "synthetic", path=f"file-{i}.py", digest="0" * 64,
                                cwd=root, command=["builtin:fixture"], output="diagnostic " * 200)
               for i in range(40)]
    verification = {"status": "tool_checks", "evidenceCount": 40, "checkCounts": {"failed": 40},
                    "checks": records, "claimVerdict": "unverified"}
    path = anti.write_run_record(SimpleNamespace(save_output="summary", run_id="checks", command="panel", progress=False),
                                mode="panel", status="success", metadata={"verification": verification,
                                "findings": {"findings": [{"claim": "fixture", "checks": records}]}}, output_text="fixture")
    record = anti.load_run_record(path)
    result = json.loads(Path(record["resultPath"]).read_text())
    assert result["verification"]["checkCounts"] == {"failed": 40}
    assert result["verification"]["checksRetained"] is False
    assert "checks" not in result["verification"]
    assert all("checks" not in finding for finding in result["findings"])
    assert len(verification["checks"]) == 40  # The live result was not mutated.


def test_eslint_identity_is_invocation_scoped_when_tool_or_config_changes(checks, monkeypatch):
    _, verifier, root = checks
    (root / "fixture.js").write_text("let value = 1;")
    config = root / "eslint.config.js"
    tool = root / "synthetic-eslint"
    config.write_text("synthetic config one")
    tool.write_text("synthetic tool one")
    monkeypatch.setattr(verifier, "_find_eslint", lambda _: str(tool))
    calls = []
    def run(*a):
        calls.append(a)
        return {"status": "passed", "reason": "tool_passed", "output": '[{"messages":[]}]'}
    monkeypatch.setattr(verifier, "_run_check", run)
    first = verifier.verify_findings([{"file": "fixture.js"}, {"file": "fixture.js"}], root, profiles=["eslint"])
    a = first[0]["checks"][-1]
    assert first[1]["checks"][-1]["checkId"] == a["checkId"]
    assert len(calls) == 1
    config.write_text("synthetic config two")
    tool.write_text("synthetic tool two")
    b = verifier.verify_finding({"file": "fixture.js"}, root, profiles=["eslint"])["checks"][-1]
    assert a["fileHash"] == b["fileHash"]
    assert a["checkId"] != b["checkId"]
    for result in (a, b):
        assert result["identityContext"]["scope"] == "invocation"
        assert result["identityContext"]["effectiveTool"] == "unknown"
        assert result["identityContext"]["effectiveConfig"] == "unknown"
        assert result["effectiveToolFingerprint"] is None
        assert result["effectiveConfigHash"] is None
        assert result["comparableAcrossRuns"] is False


def test_windows_job_creation_failure_never_starts_a_process(checks, monkeypatch):
    _, verifier, root = checks
    monkeypatch.setattr(verifier, "WINDOWS", True)
    def unavailable():
        raise OSError("synthetic job unavailable")
    monkeypatch.setattr(verifier, "_create_windows_job", unavailable)
    monkeypatch.setattr(verifier.subprocess, "Popen", Mock(side_effect=AssertionError("must not launch")))
    outcome = verifier._run_check(["synthetic-eslint"], b"fixture", root)
    assert outcome["reason"] == "process_control_unavailable"


def test_windows_job_assignment_failure_keeps_gate_closed(checks, monkeypatch):
    _, verifier, root = checks
    monkeypatch.setattr(verifier, "WINDOWS", True)
    guarded_windows_fixture(monkeypatch, verifier)
    marker = root / "must-not-run"
    state = {"closed": False}
    class Job:
        def assign(self, process):
            assert not marker.exists()
            raise OSError("synthetic assignment failure")
        def close(self):
            state["closed"] = True
    monkeypatch.setattr(verifier, "_create_windows_job", Job)
    command = [sys.executable, "-c", "from pathlib import Path; Path('must-not-run').write_text('bad')"]
    outcome = verifier._run_check(command, b"fixture", root)
    assert outcome["reason"] == "process_control_unavailable"
    assert state["closed"] and not marker.exists()


def test_windows_gated_wrapper_starts_only_after_assignment(checks, monkeypatch):
    _, verifier, root = checks
    monkeypatch.setattr(verifier, "WINDOWS", True)
    guarded_windows_fixture(monkeypatch, verifier)
    state = {"assigned": False, "closed": False}
    marker = root / "checker-started"
    class Job:
        def assign(self, process):
            assert not marker.exists()
            assert process.poll() is None
            state["assigned"] = True
        def close(self):
            state["closed"] = True
    monkeypatch.setattr(verifier, "_create_windows_job", Job)
    command = [sys.executable, "-c", "from pathlib import Path; import sys; Path('checker-started').write_text(sys.stdin.buffer.read().decode()); print('synthetic result')"]
    outcome = verifier._run_check(command, b"synthetic captured bytes", root)
    assert outcome["status"] == "passed"
    assert state == {"assigned": True, "closed": True}
    assert marker.read_text() == "synthetic captured bytes"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX emulation of the Windows job adapter; native Windows uses the job backend")
def test_windows_job_adapter_terminates_synthetic_descendants_on_timeout(checks, monkeypatch):
    import os
    import signal
    _, verifier, root = checks
    monkeypatch.setattr(verifier, "WINDOWS", True)
    guarded_windows_fixture(monkeypatch, verifier)
    monkeypatch.setattr(verifier, "CHECK_TIMEOUT_SECONDS", 0.8)
    state = {"assigned": False, "closed": False}
    class Job:
        pid = None
        def assign(self, process):
            self.pid = process.pid
            state["assigned"] = True
        def close(self):
            if self.pid is not None:
                try:
                    os.killpg(self.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                self.pid = None
            state["closed"] = True
    monkeypatch.setattr(verifier, "_create_windows_job", Job)
    code = "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c','import time; time.sleep(20)']); print('synthetic-child-started',flush=True); time.sleep(20)"
    outcome = verifier._run_check([sys.executable, "-c", code], b"", root)
    assert outcome["reason"] == "tool_timeout"
    assert "synthetic-child-started" in outcome["output"]
    assert state == {"assigned": True, "closed": True}


def guarded_windows_fixture(monkeypatch, verifier):
    # Exercise the actual gated wrapper with synthetic children while retaining
    # startup isolation. Production still requests -I/-S for its own wrapper.
    original = verifier.subprocess.Popen
    def start(argv, **kwargs):
        assert argv[:4] == [sys.executable, '-I', '-S', '-c']
        return original([argv[0], *argv[3:]], **kwargs)
    monkeypatch.setattr(verifier.subprocess, 'Popen', start)


def test_windows_job_uses_kill_on_close_and_required_assignment_rights(monkeypatch):
    import ctypes
    from types import SimpleNamespace
    import anti_lib.windows_job as jobs
    calls = []
    class Function:
        def __init__(self, name, implementation):
            self.name, self.implementation = name, implementation
        def __call__(self, *args):
            calls.append((self.name, args))
            return self.implementation(*args)
    def limits(handle, kind, pointer, size):
        assert handle == 101 and kind == 9
        assert pointer._obj.BasicLimitInformation.LimitFlags == 0x2000
        assert size == ctypes.sizeof(jobs._ExtendedLimits)
        return 1
    api = SimpleNamespace(
        CreateJobObjectW=Function("create", lambda *args: 101),
        SetInformationJobObject=Function("limits", limits),
        OpenProcess=Function("open", lambda *args: 202),
        AssignProcessToJobObject=Function("assign", lambda *args: 1),
        CloseHandle=Function("close", lambda *args: 1),
    )
    monkeypatch.setattr(ctypes, "WinDLL", lambda *a, **kw: api, raising=False)
    job = jobs.WindowsJob()
    job.assign(SimpleNamespace(pid=777))
    job.close()
    job.close()
    assert ("create", (None, None)) in calls
    assert ("open", (0x0101, False, 777)) in calls
    assert ("assign", (101, 202)) in calls
    assert [args for name, args in calls if name == "close"] == [(202,), (101,)]
