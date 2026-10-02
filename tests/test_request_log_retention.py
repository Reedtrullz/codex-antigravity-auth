"""Retained JSONL snapshots and concurrent rotation use synthetic temporary logs."""

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from threading import Event

import pytest

from codex_antigravity_auth import observability as logs

SOURCE_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def log_path(monkeypatch, tmp_path):
    monkeypatch.setattr(logs, "get_codex_home", lambda: tmp_path)
    for name in ("ANTIGRAVITY_REQUEST_LOG_MAX_BYTES", "ANTIGRAVITY_REQUEST_LOG_BACKUP_COUNT"):
        monkeypatch.delenv(name, raising=False)
    return tmp_path / logs.REQUEST_LOG_FILE


def row(identity, **values):
    return {"request_id": identity, "timestamp": "2026-10-01T00:00:00Z", "route": "google", "family": "gemini", "status": "success", **values}


def lines(path, rows):
    path.write_text("".join(json.dumps(record) + "\n" for record in rows))


def test_retained_segments_are_read_oldest_first_with_exact_event_deduplication(log_path):
    started = row("stream", status="stream_started")
    terminal = row("stream", usage={"total_tokens": 5})
    lines(log_path.with_name(log_path.name + ".2"), [row("old"), started])
    lines(log_path.with_name(log_path.name + ".1"), [terminal])
    lines(log_path, [terminal, row("new")])
    records = list(logs.iter_request_records())
    assert [record["request_id"] for record in records] == ["old", "stream", "stream", "new"]
    assert [record["request_id"] for record in logs.iter_request_records(tail=2)] == ["stream", "new"]
    assert list(logs.iter_request_records(tail=0)) == []
    group = logs.request_log_summary(since="all")["groups"]["google/gemini"]
    assert group["request_count"] == group["success_count"] == 3
    assert group["usage"]["total_tokens"] == 5


def test_rotated_only_history_and_legacy_rows_are_kept(log_path):
    legacy = {"status": "success", "timestamp": "2026-10-01T00:00:00Z"}
    lines(log_path.with_name(log_path.name + ".1"), [legacy, legacy])
    assert len(list(logs.iter_request_records())) == 2
    info = logs.request_log_info()
    assert not info["exists"] and len(info["retained_segments"]) == 1


def test_summary_reports_retained_time_bounds_and_incomplete_window(log_path):
    lines(log_path.with_name(log_path.name + ".1"), [row("old", timestamp="2026-09-30T23:00:00Z")])
    lines(log_path, [row("new", timestamp="2026-10-01T00:00:00Z")])
    now = logs._timestamp_epoch("2026-10-01T00:30:00Z")
    report = logs.request_log_summary(since="24h", now=now)
    assert report["earliest_retained_timestamp"] == "2026-09-30T23:00:00Z"
    assert report["latest_retained_timestamp"] == "2026-10-01T00:00:00Z"
    assert report["requested_window_incomplete"] is True
    assert logs.request_log_summary(since="1h", now=now)["requested_window_incomplete"] is False


def test_malformed_lines_do_not_hide_other_segments_or_inflate_failures(log_path):
    lines(log_path.with_name(log_path.name + ".1"), [row("old")])
    log_path.write_bytes(b'{not-json\n\xff\n[]\n' + (json.dumps(row("new")) + '\n').encode())
    report = logs.request_log_summary(since="24h", now=logs._timestamp_epoch("2026-10-01T00:01:00Z"))
    assert report["malformed_records"] == 3
    assert report["groups"]["google/gemini"]["success_count"] == 2
    assert report["groups"]["google/gemini"]["failure_count"] == 0
    assert report["requested_window_incomplete"] is True


def test_concurrent_thread_append_rotation_preserves_all_rows_within_retention(log_path):
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda index: logs.write_request_record(row(f"thread-{index}"), max_bytes=4096, backup_count=5), range(80)))
    records = list(logs.iter_request_records())
    assert {record["request_id"] for record in records} == {f"thread-{index}" for index in range(80)}
    segments = logs._retained_paths(log_path)
    assert 1 < len(segments) <= 6
    assert all(path.stat().st_size <= 4096 for path in segments)
    if os.name != "nt":
        assert all(path.stat().st_mode & 0o777 == 0o600 for path in segments)


def test_cross_process_append_rotation_serializes(log_path):
    code = """
import sys
from pathlib import Path
import _test_isolation
assert _test_isolation._installed
sys.path.insert(0, sys.argv[3])
from codex_antigravity_auth import observability as logs
logs.get_codex_home = lambda: Path(sys.argv[1])
for index in range(25):
    logs.write_request_record({'request_id': f'{sys.argv[2]}-{index}', 'status':'success'}, max_bytes=4096, backup_count=5)
"""
    processes = [subprocess.Popen([sys.executable, "-c", code, str(log_path.parent), str(index), str(SOURCE_ROOT)], cwd=log_path.parent, stdout=subprocess.PIPE, stderr=subprocess.PIPE) for index in range(3)]
    try:
        for process in processes:
            stdout, stderr = process.communicate(timeout=10)
            assert process.returncode == 0, stderr.decode()
        records = list(logs.iter_request_records())
        assert {record["request_id"] for record in records} == {f"{worker}-{index}" for worker in range(3) for index in range(25)}
        assert all(path.stat().st_size <= 4096 for path in logs._retained_paths(log_path))
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=2)


def test_retention_controls_bound_files_and_leave_unrelated_paths(monkeypatch, log_path):
    monkeypatch.setenv("ANTIGRAVITY_REQUEST_LOG_MAX_BYTES", "1024")
    monkeypatch.setenv("ANTIGRAVITY_REQUEST_LOG_BACKUP_COUNT", "2")
    unrelated = log_path.with_name(log_path.name + ".01")
    unrelated.write_text("unrelated")
    for index in range(70):
        logs.write_request_record(row(f"request-{index}"))
    segments = logs._retained_paths(log_path)
    assert len(segments) == 3
    assert all(path.stat().st_size <= 1024 for path in segments)
    assert list(logs.iter_request_records())[-1]["request_id"] == "request-69"
    assert unrelated.read_text() == "unrelated"
    monkeypatch.setenv("ANTIGRAVITY_REQUEST_LOG_BACKUP_COUNT", "0")
    logs.write_request_record(row("last"))
    assert logs._archive_paths(log_path) == []
    assert unrelated.exists()
    info = logs.request_log_info()
    assert info["max_bytes"] == 1024 and info["backup_count"] == 0


def test_oversized_record_is_an_explicit_gap_not_a_provider_failure(log_path):
    logs.write_request_record(row("huge", error="x" * 100000), max_bytes=1024)
    assert log_path.stat().st_size <= 1024
    record = list(logs.iter_request_records())[0]
    assert record["status"] == "log_gap"
    report = logs.request_log_summary()
    assert report["omitted_records"] == 1 and report["groups"] == {}
    assert report["requested_window_incomplete"] is True


def test_invalid_retention_settings_have_safe_defaults_and_diagnostics(monkeypatch, log_path):
    monkeypatch.setenv("ANTIGRAVITY_REQUEST_LOG_MAX_BYTES", "synthetic-secret-not-an-integer")
    monkeypatch.setenv("ANTIGRAVITY_REQUEST_LOG_BACKUP_COUNT", "999")
    info = logs.request_log_info()
    assert info["max_bytes"] == logs.REQUEST_LOG_MAX_BYTES and info["backup_count"] == 1
    assert len(info["configuration_warnings"]) == 2
    assert "synthetic-secret" not in json.dumps(info)


def test_short_writes_are_completed_and_failed_partial_appends_are_rolled_back(monkeypatch, log_path):
    original_write = os.write
    monkeypatch.setattr(os, "write", lambda fd, data: original_write(fd, data[:3]))
    logs.write_request_record(row("complete"))
    assert list(logs.iter_request_records())[0]["request_id"] == "complete"
    previous = log_path.read_bytes()
    calls = []
    def partial_then_fail(fd, data):
        if len(data) == 1:
            return original_write(fd, data)  # Windows lock-byte initialization.
        calls.append(1)
        if len(calls) == 1:
            return original_write(fd, data[:7])
        raise OSError("synthetic disk error")
    monkeypatch.setattr(os, "write", partial_then_fail)
    logs.write_request_record(row("must-not-be-partial"))
    assert log_path.read_bytes() == previous


def test_symlinked_archive_is_not_read_or_modified(log_path, tmp_path):
    target = tmp_path / "unrelated.json"
    target.write_text("unrelated")
    link = log_path.with_name(log_path.name + ".1")
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(str(exc))
    logs.write_request_record(row("ignored"), max_bytes=1024)
    assert target.read_text() == "unrelated"
    assert list(logs.iter_request_records()) == []
    assert link.is_symlink()


def test_clean_and_empty_reads_touch_only_retained_files(log_path):
    assert list(logs.iter_request_records()) == []
    assert logs.request_log_info()["retained_segments"] == []
    assert logs.clean_request_logs() == []
    assert list(log_path.parent.iterdir()) == []
    for suffix in ("", ".1", ".2"):
        lines(log_path.with_name(log_path.name + suffix), [row(suffix)])
    unrelated = log_path.with_name(log_path.name + ".note")
    unrelated.write_text("keep")
    assert len(logs.clean_request_logs()) == 3
    assert unrelated.read_text() == "keep"


@pytest.mark.parametrize("operation", ["read", "clean", "info"])
def test_snapshots_wait_for_zero_backup_rotation(monkeypatch, log_path, operation):
    logs.write_request_record(row("old", error="x" * 700), max_bytes=1024, backup_count=0)
    removed, resume, lock_attempted = Event(), Event(), Event()
    original_unlink, original_lock = Path.unlink, logs.file_lock

    def paused_unlink(path, *args, **kwargs):
        result = original_unlink(path, *args, **kwargs)
        if path == log_path and not removed.is_set():
            removed.set()
            assert resume.wait(5)
        return result

    @contextmanager
    def observed_lock(path):
        if removed.is_set():
            lock_attempted.set()
        with original_lock(path):
            yield

    monkeypatch.setattr(Path, "unlink", paused_unlink)
    monkeypatch.setattr(logs, "file_lock", observed_lock)
    snapshot = {"read": lambda: list(logs.iter_request_records()), "clean": logs.clean_request_logs, "info": logs.request_log_info}[operation]
    with ThreadPoolExecutor(max_workers=2) as pool:
        writer = pool.submit(logs.write_request_record, row("new", error="x" * 700), max_bytes=1024, backup_count=0)
        try:
            assert removed.wait(2)
            reader = pool.submit(snapshot)
            assert lock_attempted.wait(2), "snapshot bypassed the writer's lock"
            assert not reader.done()
        finally:
            resume.set()
        writer.result(timeout=5)
        result = reader.result(timeout=5)
    if operation == "read":
        assert [record["request_id"] for record in result] == ["new"]
    elif operation == "clean":
        assert result == [str(log_path)]
        assert not log_path.exists()
    else:
        assert result["exists"] is True
        assert result["retained_segments"] == [{"path": str(log_path), "size_bytes": log_path.stat().st_size}]


@pytest.mark.parametrize("unreadable", [False, True])
def test_known_history_gaps_without_timestamps_mark_window_incomplete(monkeypatch, log_path, unreadable):
    log_path.write_bytes(b"{not-json\n")
    if unreadable:
        @contextmanager
        def failed_lock(*args, **kwargs):
            raise OSError("synthetic unreadable history")
            yield
        monkeypatch.setattr(logs, "_log_lock", failed_lock)
    report = logs.request_log_summary(since="24h")
    assert report["malformed_records"] == 1
    assert report["earliest_retained_timestamp"] is None
    assert report["requested_window_incomplete"] is True
    assert report["groups"] == {}
