"""Cleanup runs only against synthetic temporary trees, never real history."""
from contextlib import contextmanager
import importlib.util
import json
import os
from pathlib import Path
import sys
import time
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "codex_antigravity_auth/skills/anti/scripts/anti.py"


@pytest.fixture
def sandbox(monkeypatch, tmp_path):
    script_dir = str(SCRIPT.resolve().parent)
    if script_dir not in sys.path:
        sys.path.insert(0, script_dir)
    spec = importlib.util.spec_from_file_location("anti_cleanup_fixture", SCRIPT)
    anti = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(anti)
    import anti_lib.cleanup as cleanup
    monkeypatch.setattr(anti, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(anti, "helper_identity", lambda: {})
    return anti, cleanup, tmp_path / "runs"


def record(root, run_id="fixture", status="success", old=True, **fields):
    root.mkdir(exist_ok=True)
    path = root / f"{run_id}.json"
    path.write_text(json.dumps({"id": run_id, "status": status, **fields}))
    if old:
        epoch = time.time() - 3 * 86400
        os.utime(path, (epoch, epoch))
    return path


def snapshot(root):
    return {str(p.relative_to(root)): (p.read_bytes(), p.stat().st_mode, p.stat().st_mtime_ns)
            for p in root.rglob("*") if p.is_file() and not p.is_symlink()}


def clean(cleanup, root, **kw):
    return cleanup.clean_runs(root, time.time() - 86400, **kw)


@pytest.mark.parametrize("state", ["running", None, "unknown", "future", {"bad": "shape"}])
@pytest.mark.parametrize("old", [False, True])
def test_running_stale_and_unknown_records_are_preserved(sandbox, state, old):
    _, cleanup, root = sandbox
    path = record(root, status=state, old=old)
    artifact = root / "fixture"
    artifact.mkdir()
    (artifact / "result.json").write_text("synthetic result")
    before = snapshot(root)
    result = clean(cleanup, root)
    assert result["rows"][0]["action"] == "skip"
    assert snapshot(root) == before
    assert not (root / ".deleted").exists()


@pytest.mark.parametrize("state", ["success", "partial", "error", "failed", "interrupted"])
def test_old_terminal_record_and_artifacts_are_removed_with_reservation(sandbox, state):
    anti, cleanup, root = sandbox
    path = record(root, status=state)
    artifact = root / "fixture"
    artifact.mkdir()
    (artifact / "result.json").write_text("synthetic result")
    result = clean(cleanup, root)
    assert result["rows"][0]["action"] == "removed"
    assert not path.exists() and not artifact.exists()
    marker = json.loads((root / ".deleted/fixture.json").read_text())
    assert marker["state"] == "deleted"
    options = SimpleNamespace(run_id="fixture", save_output="never", command="consult")
    with pytest.raises(cleanup.PersistenceError, match="reserved"):
        anti.write_run_record(options, mode="consult", status="running")
    assert not path.exists()


def test_dry_run_and_missing_directory_change_nothing(sandbox):
    _, cleanup, root = sandbox
    assert clean(cleanup, root, dry_run=True)["rows"] == []
    assert not root.exists()
    record(root)
    record(root, "running", "running")
    before = snapshot(root)
    result = clean(cleanup, root, dry_run=True)
    assert {row["id"]: row["action"] for row in result["rows"]} == {"fixture": "remove", "running": "skip"}
    assert snapshot(root) == before
    assert not (root / ".deleted").exists()
    assert not list(root.glob("*.lock"))


def test_corrupt_recent_temporary_and_reflection_history_are_retained(sandbox):
    _, cleanup, root = sandbox
    record(root, "recent", old=False)
    (root / "corrupt.json").write_bytes(b"{synthetic broken")
    (root / "unknown.json.tmp").write_bytes(b"synthetic partial")
    reflections = root / "reflections"
    reflections.mkdir()
    (reflections / "fixture.json").write_bytes(b"synthetic reflection history")
    before = snapshot(root)
    result = clean(cleanup, root)
    assert all(row["action"] == "skip" for row in result["rows"])
    assert result["reflections"] == "retained"
    assert snapshot(root) == before


def test_revalidate_under_writer_lock_before_deleting(sandbox, monkeypatch):
    _, cleanup, root = sandbox
    path = record(root)
    lock = cleanup.file_lock
    @contextmanager
    def changed_before_lock(path_to_lock):
        # Simulate a writer committing after planning but before lock acquisition.
        record(root, status="running")
        with lock(path_to_lock):
            yield
    monkeypatch.setattr(cleanup, "file_lock", changed_before_lock)
    result = clean(cleanup, root)
    assert result["rows"][0]["reason"] == "running_or_uncertain"
    assert json.loads(path.read_text())["status"] == "running"
    assert not (root / ".deleted").exists()


def test_even_terminal_changes_since_plan_are_preserved(sandbox, monkeypatch):
    _, cleanup, root = sandbox
    path = record(root)
    lock = cleanup.file_lock
    @contextmanager
    def changed_before_lock(path_to_lock):
        record(root, status="error")
        with lock(path_to_lock):
            yield
    monkeypatch.setattr(cleanup, "file_lock", changed_before_lock)
    result = clean(cleanup, root)
    assert result["rows"][0]["reason"] == "record_changed_since_plan"
    assert json.loads(path.read_text())["status"] == "error"


@pytest.mark.parametrize("location", ["record", "artifact", "nested"])
def test_symlinks_never_delete_external_targets(sandbox, tmp_path, location):
    _, cleanup, root = sandbox
    path = record(root)
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "keep.json"
    target.write_text("synthetic keep")
    if location == "record":
        path.unlink()
        path.symlink_to(target)
    elif location == "artifact":
        (root / "fixture").symlink_to(outside, target_is_directory=True)
    else:
        (root / "fixture").mkdir()
        (root / "fixture/link").symlink_to(target)
    result = clean(cleanup, root)
    assert result["rows"][0]["action"] == "skip"
    assert target.read_text() == "synthetic keep"
    assert path.exists()


def test_partial_cleanup_reports_paths_and_requires_explicit_resume(sandbox, monkeypatch):
    _, cleanup, root = sandbox
    path = record(root)
    artifact = root / "fixture"
    artifact.mkdir()
    (artifact / "one").write_text("one")
    (artifact / "two").write_text("two")
    def fail(directory):
        (directory / "one").unlink()
        raise PermissionError("synthetic cleanup interruption")
    with monkeypatch.context() as patch:
        patch.setattr(cleanup.shutil, "rmtree", fail)
        result = clean(cleanup, root)
    assert result["errors"] == 1
    assert result["rows"][0]["recordPath"] == str(path)
    assert result["rows"][0]["artifactPath"] == str(artifact)
    assert path.exists() and (artifact / "two").exists()
    assert clean(cleanup, root)["rows"][0]["reason"] == "pending_cleanup"
    before = snapshot(root)
    plan = clean(cleanup, root, dry_run=True, resume=True)
    assert plan["rows"][0]["action"] == "resume"
    assert snapshot(root) == before
    assert clean(cleanup, root, resume=True)["rows"][0]["action"] == "removed"
    assert not path.exists() and not artifact.exists()


def test_resume_does_not_delete_a_record_changed_after_partial_cleanup(sandbox, monkeypatch):
    _, cleanup, root = sandbox
    path = record(root)
    (root / "fixture").mkdir()
    def fail(_):
        raise PermissionError("synthetic")
    with monkeypatch.context() as patch:
        patch.setattr(cleanup.shutil, "rmtree", fail)
        clean(cleanup, root)
    record(root, status="running")
    result = clean(cleanup, root, resume=True)
    assert result["rows"][0]["reason"] == "pending_record_changed"
    assert json.loads(path.read_text())["status"] == "running"


def test_late_original_owner_cannot_resurrect_cleaned_run(sandbox):
    anti, cleanup, root = sandbox
    options = SimpleNamespace(run_id="fixture", save_output="never", command="consult", progress=False)
    path = anti.write_run_record(options, mode="consult", status="running")
    anti.write_run_record(options, mode="consult", status="success")
    epoch = time.time() - 3 * 86400
    os.utime(path, (epoch, epoch))
    assert clean(cleanup, root)["rows"][0]["action"] == "removed"
    with pytest.raises(cleanup.PersistenceError, match="reserved"):
        anti.write_run_record(options, mode="consult", status="running")
    assert not path.exists()


def test_cli_json_plan_and_error_exit_are_explicit(sandbox, monkeypatch, capsys):
    anti, cleanup, root = sandbox
    record(root)
    assert anti.main(["runs", "clean", "--older-than", "1", "--dry-run", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["rows"][0]["action"] == "remove"
    def fail(*a, **kw):
        raise OSError("synthetic marker publication failure")
    monkeypatch.setattr(cleanup, "atomic_write_json", fail)
    assert anti.main(["runs", "clean", "--older-than", "1", "--json"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["errors"] == 1
    assert (root / "fixture.json").exists()


@pytest.mark.parametrize("failure", ["changed", "orphan", "invalid"])
@pytest.mark.parametrize("dry_run", [False, True])
def test_explicit_unsafe_resume_fails_with_paths_and_preserves_bytes(sandbox, monkeypatch, capsys, failure, dry_run):
    anti, cleanup, root = sandbox
    path = record(root)
    artifact = root / "fixture"
    artifact.mkdir()
    (artifact / "retained").write_text("synthetic retained output")
    def fail(_):
        raise PermissionError("synthetic interrupted cleanup")
    with monkeypatch.context() as patch:
        patch.setattr(cleanup.shutil, "rmtree", fail)
        assert clean(cleanup, root)["errors"] == 1
    marker = root / ".deleted/fixture.json"
    if failure == "changed":
        record(root, status="running")
    elif failure == "orphan":
        path.unlink()
    else:
        marker.write_bytes(b"synthetic invalid marker")
    before = snapshot(root)
    argv = ["runs", "clean", "--older-than", "1", "--resume-cleanup", "--json"]
    if dry_run:
        argv.append("--dry-run")
    assert anti.main(argv) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["errors"] == 1
    row = report["rows"][0]
    assert row["action"] == "error"
    assert row["recordPath"] == str(path)
    assert row["artifactPath"] == str(artifact)
    assert row["markerPath"] == str(marker)
    assert "manual recovery" in row["recovery"]
    assert snapshot(root) == before


def test_marker_removed_between_resume_plan_and_lock_is_incomplete(sandbox, monkeypatch):
    _, cleanup, root = sandbox
    path = record(root)
    (root / "fixture").mkdir()
    def fail(_):
        raise PermissionError("synthetic")
    with monkeypatch.context() as patch:
        patch.setattr(cleanup.shutil, "rmtree", fail)
        clean(cleanup, root)
    marker = root / ".deleted/fixture.json"
    lock = cleanup.file_lock
    @contextmanager
    def changed_before_lock(path_to_lock):
        marker.unlink()
        with lock(path_to_lock):
            yield
    monkeypatch.setattr(cleanup, "file_lock", changed_before_lock)
    result = clean(cleanup, root, resume=True)
    assert result["errors"] == 1
    assert result["rows"][0]["reason"] == "pending_marker_missing"
    assert path.exists() and (root / "fixture").exists()
