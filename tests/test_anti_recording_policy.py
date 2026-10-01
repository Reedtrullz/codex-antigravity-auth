"""Inspect every persisted fixture file; never contact a provider or real store."""
import argparse
import importlib.util
import json
import signal
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "codex_antigravity_auth/skills/anti/scripts/anti.py"
SENTINEL = "synthetic-private-content-"
LONG = SENTINEL + "x" * 4000 + "-private-tail"
SECRET = "sk-syntheticfixture01234567890123456789"


@pytest.fixture
def isolated_anti(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location("anti_recording_fixture", SCRIPT)
    anti = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(anti)
    monkeypatch.setattr(anti, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(anti, "helper_identity", lambda: {"version": "fixture", "path": str(SCRIPT)})
    import anti_lib.reflections as reflections
    monkeypatch.setattr(reflections, "REFLECTIONS_DIR", tmp_path / "reflections")
    monkeypatch.setattr(anti, "post_response", lambda **kw: (_ for _ in ()).throw(AssertionError("no generation")))
    return anti, reflections, tmp_path


def args(mode, run_id="fixture-run"):
    return argparse.Namespace(save_output=mode, run_id=run_id, command="panel", workflow_name=LONG, run_label=LONG)


def write(anti, mode, **changes):
    values = dict(mode="panel", status="error", models=[LONG], base_url=LONG,
                  prompt_text=LONG, output_text=LONG, error=LONG, caveats=[LONG],
                  metadata={"findings": {"findings": [{"claim": LONG, "evidence": LONG, "api_key": SECRET}]},
                            "panel_results": [{"output_text": LONG, "nested": {"error": LONG}}],
                            "output_chars": 4000, "scope_status": "partial", "extra": LONG},
                  execution_ledger=[{"output": LONG}], force_full_output=True)
    values.update(changes)
    return anti.write_run_record(args(mode), **values)


def all_files(root):
    return {p: p.read_text() for p in root.rglob("*") if p.is_file()}


@pytest.mark.parametrize("status", ["running", "success", "partial", "error", "interrupted"])
def test_never_records_only_allowlisted_lifecycle(isolated_anti, status):
    anti, reflections, root = isolated_anti
    path = write(anti, "never", status=status)
    assert reflections.record_review(repo_path=root, findings=[{"claim": LONG}], models=[LONG],
                                     panel_status=LONG, mode=LONG, save_output="never") is None
    files = all_files(root)
    assert [p for p in files if p.suffix == ".json"] == [path]
    assert SENTINEL not in files[path]
    record = json.loads(files[path])
    assert record["status"] == status
    assert record["metadata"] == {"request_log_correlation_id": "fixture-run", "output_chars": 4000, "scope_status": "partial"}
    assert "resultPath" not in record
    if sys.platform != "win32":
        assert path.stat().st_mode & 0o777 == 0o600


def test_summary_bounds_record_result_reflection_and_forced_output(isolated_anti):
    anti, reflections, root = isolated_anti
    path = write(anti, "summary")
    reflections.record_review(repo_path=root, findings=[{"claim": LONG, "evidence": LONG, "file": LONG, "fingerprint": LONG}],
                              models=[LONG], panel_status=LONG, mode=LONG, scope=LONG, save_output="summary")
    files = all_files(root)
    assert not any(p.name.startswith("lane-") for p in files)
    for p, text in files.items():
        assert LONG not in text
        assert "private-tail" not in text
        assert SECRET not in text
        if p.suffix != ".json":
            continue
        data = json.loads(text)
        for payload in data if isinstance(data, list) else [data]:
            assert payload["retention"]["contentComplete"] is False
    record = json.loads(path.read_text())
    artifact = json.loads(Path(record["resultPath"]).read_text())
    assert "output_text" not in record
    assert "output_text" not in artifact
    assert record["output_chars"] == len(LONG)
    assert record["runStatus"] == "failed"
    assert artifact["scopeStatus"] == "partial"


def test_full_retains_intended_output_with_redaction(isolated_anti):
    anti, reflections, root = isolated_anti
    path = write(anti, "full")
    reflections.record_review(repo_path=root, findings=[{"claim": LONG, "evidence": SECRET}],
                              models=["fixture"], panel_status="complete", mode="panel", save_output="full")
    files = all_files(root)
    assert all(SECRET not in text for text in files.values())
    record = json.loads(path.read_text())
    assert record["output_text"] == LONG
    artifact = json.loads(Path(record["resultPath"]).read_text())
    assert artifact["output_text"] == LONG
    assert len(artifact["artifacts"]["rawLanePaths"]) == 1
    assert any(LONG in text for p, text in files.items() if p.parent.name == "reflections")


@pytest.mark.parametrize("mode", ["never", "summary"])
def test_reusing_full_id_cannot_relabel_or_remove_previous_content(isolated_anti, mode):
    anti, _, root = isolated_anti
    write(anti, "full")
    before = all_files(root)
    with pytest.raises(anti.AntiError, match="another retention policy"):
        write(anti, mode)
    assert all_files(root) == before


def test_summary_projection_bounds_adversarial_nested_content(isolated_anti):
    from anti_lib.retention import summary_projection, SUMMARY_TOTAL_CHARS, SUMMARY_STRING_CHARS
    value = {str(i): [{LONG: LONG, "api_key": SECRET}] * 100 for i in range(100)}
    result = summary_projection(value)
    strings = []
    def walk(value):
        if isinstance(value, str):
            strings.append(value)
        elif isinstance(value, dict):
            assert len(value) <= 40
            for key, child in value.items():
                strings.append(key)
                walk(child)
        elif isinstance(value, list):
            assert len(value) <= 40
            for child in value:
                walk(child)
    walk(result)
    assert max(map(len, strings)) <= SUMMARY_STRING_CHARS
    assert sum(map(len, strings)) <= SUMMARY_TOTAL_CHARS
    assert SECRET not in json.dumps(result)


def test_never_signal_handler_retains_lifecycle_without_error_body(isolated_anti, monkeypatch):
    anti, _, root = isolated_anti
    options = args("never")
    callbacks = {}
    monkeypatch.setattr(anti.signal, "signal", lambda number, callback: callbacks.setdefault(number, callback))
    anti.write_start_record(options, run_id=options.run_id)
    anti._install_run_signal_handlers(options)
    with pytest.raises(SystemExit):
        callbacks[signal.SIGTERM](signal.SIGTERM, None)
    record = json.loads((anti.RUNS_DIR / "fixture-run.json").read_text())
    assert record["status"] == "interrupted"
    assert record["error"] == "interrupted"
    assert SENTINEL not in json.dumps(record)


@pytest.mark.parametrize("retention", ["never", "summary", "full"])
def test_panel_command_propagates_mode_to_every_store(isolated_anti, monkeypatch, capsys, retention):
    anti, reflections, root = isolated_anti
    monkeypatch.setattr(anti, "fetch_model_ids", lambda *a, **kw: {"claude-sonnet-4-6", "claude-opus-4-6-thinking"})
    answer = json.dumps({"summary": LONG, "findings": [{"id": "F1", "claim": LONG, "evidence": LONG, "severity": "low"}]})
    monkeypatch.setattr(anti, "post_response", lambda **kw: answer)
    rc = anti.main(["panel", "--mode", "ask", "--prompt", LONG, "--json", "--no-progress", "--save-output", retention])
    assert rc == 0
    assert SENTINEL in capsys.readouterr().out  # Content remains visible even in never mode.
    files = all_files(root)
    if retention == "never":
        assert len([p for p in files if p.suffix == ".json"]) == 1
        assert all(SENTINEL not in text for text in files.values())
        assert not reflections.REFLECTIONS_DIR.exists()
    elif retention == "summary":
        assert all(LONG not in text and "private-tail" not in text for text in files.values())
        assert list(reflections.REFLECTIONS_DIR.glob("*.json"))
    else:
        assert any(LONG in text for text in files.values())
        assert list(anti.RUNS_DIR.rglob("lane-*.json"))


def test_summary_refuses_orphan_lane_artifacts_without_deleting_them(isolated_anti):
    anti, _, root = isolated_anti
    directory = anti.RUNS_DIR / "fixture-run"
    directory.mkdir(parents=True)
    (directory / "lane-0001.json").write_text(LONG)
    before = all_files(root)
    with pytest.raises(anti.AntiError, match="unknown retention policy"):
        write(anti, "summary")
    assert all_files(root) == before


def test_standalone_copy_uses_same_retention_without_installed_package(isolated_anti):
    import shutil
    import subprocess
    _, _, root = isolated_anti
    copied = root / "copy"
    shutil.copytree(SCRIPT.parent, copied, ignore=shutil.ignore_patterns("__pycache__"))
    probe = "from anti_lib.retention import summary_projection; assert len(summary_projection('x'*4000)) == 1600"
    subprocess.run([sys.executable, "-S", "-c", probe], cwd=copied, check=True)


@pytest.mark.parametrize("retention", ["never", "summary", "full"])
def test_interrupted_full_publication_preserves_orphan_artifacts(isolated_anti, monkeypatch, retention):
    anti, _, root = isolated_anti
    replace = anti.os.replace
    def interrupt_record(source, destination):
        if Path(destination) == anti.RUNS_DIR / "fixture-run.json":
            raise OSError("synthetic interrupted publication")
        return replace(source, destination)
    with monkeypatch.context() as patch:
        patch.setattr(anti.os, "replace", interrupt_record)
        with pytest.raises(OSError, match="interrupted publication"):
            write(anti, "full", execution_ledger=None)
    assert list((anti.RUNS_DIR / "fixture-run/revisions").glob("*/result.json"))
    assert not (anti.RUNS_DIR / "fixture-run.json").exists()
    before = all_files(root)
    with pytest.raises(anti.AntiError, match="unknown retention policy"):
        write(anti, retention)
    assert all_files(root) == before


def test_orphan_temporary_record_is_preserved(isolated_anti):
    anti, _, root = isolated_anti
    anti.RUNS_DIR.mkdir()
    (anti.RUNS_DIR / "fixture-run.json.tmp").write_text(LONG)
    before = all_files(root)
    with pytest.raises(anti.AntiError, match="unknown retention policy"):
        write(anti, "never")
    assert all_files(root) == before


def test_exhausted_preview_budget_preserves_result_structure_and_reflection_counts(isolated_anti):
    anti, reflections, root = isolated_anti
    findings = [{"claim": LONG, "evidence": LONG, "file": LONG}] * 80
    metadata = {
        "panel_results": [{"output_text": LONG}] * 80,
        "findings": {"findings": findings},
        "scope_status": "partial",
        "planned_chunk_count": 75, "completed_chunk_count": 3,
        "coverage": [{"path": LONG, "contentStatus": "partial"}] * 80,
        "verification": {"status": "tool_checks", "performedBy": "anti", "evidenceCount": 2, "evidence": [LONG] * 80},
    }
    path = write(anti, "summary", metadata=metadata)
    record = json.loads(path.read_text())
    artifact = json.loads(Path(record["resultPath"]).read_text())
    assert artifact["output_chars"] == len(LONG)
    assert artifact["runId"] == "fixture-run"
    assert artifact["runStatus"] == "failed"
    assert artifact["scopeStatus"] == "partial"
    assert artifact["resultPath"] == record["resultPath"]
    assert artifact["artifacts"]["resultPath"] == record["resultPath"]
    assert artifact["verification"]["status"] == "tool_checks"
    assert artifact["verification"]["evidenceCount"] == 2
    assert artifact["coverage"]["chunksExpected"] == 75
    assert artifact["coverage"]["chunksCompleted"] == 3
    saved = reflections.record_review(repo_path=root, findings=findings, models=[LONG] * 80,
                                      panel_status="complete", mode="panel", save_output="summary")
    assert saved["findings_count"] == 80
    assert saved["panel_status"] == "complete"
    assert saved["save_output"] == "summary"
    assert reflections.get_summary(root)["total_findings"] == 80
    assert all(LONG not in text for text in all_files(root).values())
