"""Synthetic persistence failures, competing writers and standalone processes."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "codex_antigravity_auth/skills/anti/scripts/anti.py"


@pytest.fixture
def stores(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location("anti_persistence_fixture", SCRIPT)
    anti = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(anti)
    import anti_lib.reflections as reflections
    import anti_lib.persistence as persistence
    monkeypatch.setattr(anti, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(anti, "helper_identity", lambda: {"version": "fixture"})
    monkeypatch.setattr(reflections, "REFLECTIONS_DIR", tmp_path / "reflections")
    return anti, reflections, persistence, tmp_path


def options(mode="never"):
    return argparse.Namespace(save_output=mode, run_id="shared-fixture", command="consult", progress=False)


def save(anti, args, status="running"):
    return anti.write_run_record(args, mode="consult", status=status, output_text="synthetic answer")


def reflect(reflections, root, **changes):
    values = dict(repo_path=root, findings=[], models=["fixture"], panel_status="complete", mode="review", run_id="fixture")
    values.update(changes)
    return reflections.record_review(**values)


@pytest.mark.parametrize("raw", [b"{broken", b"\xff", b"{}", b"[42]", b'[{"timestamp":"future"}]', b'[{"timestamp":1,"findings":[42]}]'])
@pytest.mark.parametrize("operation", ["append", "verdict", "read", "prune", "clear"])
def test_corrupt_history_survives_every_operation(stores, raw, operation):
    _, reflections, persistence, root = stores
    path = reflections._reflection_path(root)
    path.parent.mkdir()
    path.write_bytes(raw)
    operations = {
        "append": lambda: reflect(reflections, root),
        "verdict": lambda: reflections.update_verdict(root, "fixture", "confirmed"),
        "read": lambda: reflections.list_records(root),
        "prune": lambda: reflections.prune_reflections_older_than(time.time() + 100),
        "clear": lambda: reflections.clear_records(root),
    }
    with pytest.raises(persistence.PersistenceError, match="backup"):
        operations[operation]()
    assert path.read_bytes() == raw


def test_unreadable_history_is_not_treated_as_absent(stores, monkeypatch):
    _, reflections, persistence, root = stores
    path = reflections._reflection_path(root)
    path.parent.mkdir()
    original = b'[{"timestamp":1,"findings":[]}]'
    path.write_bytes(original)
    read = Path.read_text
    def denied(self, *a, **kw):
        if self == path:
            raise PermissionError("synthetic denied read")
        return read(self, *a, **kw)
    monkeypatch.setattr(Path, "read_text", denied)
    with pytest.raises(persistence.PersistenceError, match="Unreadable"):
        reflect(reflections, root)
    assert path.read_bytes() == original


def test_absent_reflections_are_empty_and_valid_history_keeps_unknown_fields(stores):
    _, reflections, _, root = stores
    assert reflections.list_records(root) == []
    path = reflections._reflection_path(root)
    path.parent.mkdir()
    original = {"timestamp": time.time(), "findings": [], "futureField": {"fixture": True}}
    path.write_text(json.dumps([original]))
    reflect(reflections, root)
    assert reflections._load_records(path)[0] == original


def test_competing_run_owners_cannot_replace_identity(stores):
    anti, _, _, root = stores
    def attempt(_):
        try:
            return save(anti, options())
        except anti.AntiError:
            return None
    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(attempt, range(8)))
    assert sum(result is not None for result in results) == 1
    record = json.loads((anti.RUNS_DIR / "shared-fixture.json").read_text())
    assert record["id"] == "shared-fixture"
    assert len(record["writerId"]) == 32


@pytest.mark.parametrize("mode", ["never", "summary", "full"])
def test_terminal_state_survives_late_heartbeat_and_final(stores, mode):
    anti, _, _, root = stores
    args = options(mode)
    path = save(anti, args)
    save(anti, args, "success")
    before = {p: p.read_bytes() for p in root.rglob("*.json")}
    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(lambda i: save(anti, args, "running" if i % 2 else "interrupted"), range(16)))
    assert {p: p.read_bytes() for p in root.rglob("*.json")} == before
    assert json.loads(path.read_text())["status"] == "success"


def test_interrupted_replace_preserves_previous_record_and_foreign_temporary(stores, monkeypatch):
    anti, _, persistence, _ = stores
    args = options()
    path = save(anti, args)
    before = path.read_bytes()
    foreign = path.parent / ("." + path.name + ".foreign.tmp")
    foreign.write_bytes(b"synthetic foreign bytes")
    def fail(*a):
        raise OSError("synthetic replace failure")
    monkeypatch.setattr(persistence.os, "replace", fail)
    with pytest.raises(OSError, match="replace failure"):
        save(anti, args, "success")
    assert path.read_bytes() == before
    assert foreign.read_bytes() == b"synthetic foreign bytes"
    assert list(path.parent.glob("*.tmp")) == [foreign]


def test_signal_during_locked_publication_is_deferred_without_deadlock(stores, monkeypatch):
    anti, _, persistence, _ = stores
    args = options()
    original = anti.atomic_write_json
    delivered = []
    def interrupted(path, value):
        if not delivered:
            delivered.append(True)
            anti._handle_run_signal(args, signal.SIGTERM)
            assert not getattr(args, "run_record_written", False)
        original(path, value)
    monkeypatch.setattr(anti, "atomic_write_json", interrupted)
    with pytest.raises(SystemExit) as error:
        save(anti, args)
    assert error.value.code == 128 + signal.SIGTERM
    record = json.loads((anti.RUNS_DIR / "shared-fixture.json").read_text())
    assert record["status"] == "interrupted"
    assert record["writerId"] == args._anti_writer_id


def test_reflection_append_and_verdict_update_do_not_lose_each_other(stores):
    _, reflections, _, root = stores
    reflect(reflections, root)
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(reflect, reflections, root, run_id=f"fixture-{i}") for i in range(12)]
        futures.append(executor.submit(reflections.update_verdict, root, "fixture", "confirmed"))
        for future in futures:
            future.result()
    rows = reflections.list_records(root, limit=None)
    assert len(rows) == 13
    assert next(row for row in rows if row["run_id"] == "fixture")["verdict"] == "confirmed"


def test_standalone_processes_serialize_run_ownership(stores):
    anti, _, _, root = stores
    code = '''
import argparse, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import anti
anti.RUNS_DIR = Path(sys.argv[2])
anti.helper_identity = lambda: {}
args = argparse.Namespace(save_output="never", run_id="shared-fixture", command="consult", progress=False)
try:
    anti.write_run_record(args, mode="consult", status="running")
except anti.AntiError:
    raise SystemExit(3)
'''
    processes = [subprocess.Popen([sys.executable, "-S", "-c", code, str(SCRIPT.parent), str(anti.RUNS_DIR)],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(4)]
    codes = []
    for process in processes:
        _, stderr = process.communicate(timeout=15)
        assert process.returncode in {0, 3}, stderr
        codes.append(process.returncode)
    assert codes.count(0) == 1
    assert codes.count(3) == 3


def test_reflection_cli_reports_corruption_without_traceback_or_replacement(stores, capsys):
    anti, reflections, _, root = stores
    path = reflections._reflection_path(root)
    path.parent.mkdir()
    path.write_bytes(b"synthetic malformed history")
    assert anti.main(["runs", "reflections", "--repo", str(root)]) == 1
    captured = capsys.readouterr()
    assert "backup" in captured.err
    assert "Traceback" not in captured.err
    assert path.read_bytes() == b"synthetic malformed history"


def test_passive_reflection_failure_warns_without_losing_completed_result(stores, monkeypatch, capsys):
    anti, reflections, persistence, root = stores
    path = reflections._reflection_path(root)
    path.parent.mkdir()
    path.write_bytes(b"synthetic corrupt history")
    monkeypatch.chdir(root)
    monkeypatch.setattr(anti, "fetch_model_ids", lambda *a, **kw: {"claude-sonnet-4-6", "claude-opus-4-6-thinking"})
    monkeypatch.setattr(anti, "post_response", lambda **kw: '{"summary":"synthetic answer","findings":[]}')
    rc = anti.main(["panel", "--mode", "ask", "--prompt", "synthetic question", "--save-output", "summary", "--no-progress", "--json"])
    captured = capsys.readouterr()
    assert rc == 0
    assert "synthetic answer" in captured.out
    assert "Reflection history was not updated" in captured.err
    assert "backup" in captured.err
    assert path.read_bytes() == b"synthetic corrupt history"


@pytest.mark.parametrize("error", [OSError("synthetic fsync failure"), KeyboardInterrupt()])
def test_interrupted_fsync_cleans_only_owned_temporary(stores, monkeypatch, error):
    anti, _, persistence, _ = stores
    args = options()
    path = save(anti, args)
    before = path.read_bytes()
    def fail(*a):
        raise error
    monkeypatch.setattr(persistence.os, "fsync", fail)
    with pytest.raises(type(error)):
        save(anti, args, "success")
    assert path.read_bytes() == before
    assert not list(path.parent.glob("*.tmp"))
