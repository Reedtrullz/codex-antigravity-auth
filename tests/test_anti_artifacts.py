"""Saved publication fixtures use synthetic bytes and temporary roots only."""
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "codex_antigravity_auth/skills/anti/scripts/anti.py"


@pytest.fixture
def publications(monkeypatch, tmp_path):
    script_dir = str(SCRIPT.resolve().parent)
    if script_dir not in sys.path:
        sys.path.insert(0, script_dir)
    spec = importlib.util.spec_from_file_location("anti_artifact_fixture", SCRIPT)
    anti = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(anti)
    import anti_lib.artifacts as artifacts
    monkeypatch.setattr(anti, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(anti, "helper_identity", lambda: {"version": "fixture"})
    return anti, artifacts, tmp_path / "runs"


def options(mode="full", run_id="fixture"):
    return SimpleNamespace(save_output=mode, run_id=run_id, command="consult", progress=False)


def write(anti, args, status="success", **kwargs):
    params = dict(mode="consult", status=status, output_text="synthetic output", metadata={"scope_status": "complete"},
                  execution_ledger=[{"stage": "consult", "output": "synthetic lane output"}])
    params.update(kwargs)
    return anti.write_run_record(args, **params)


def stored(path):
    return json.loads(path.read_text())


def update_result(artifacts, path, mutate):
    index = stored(path)
    result_path = Path(index["resultPath"])
    result = stored(result_path)
    mutate(result)
    result_path.write_text(json.dumps(result))
    index["publication"]["result"] = artifacts.file_reference(path.parent, result_path)
    path.write_text(json.dumps(index))


@pytest.mark.parametrize("mode", ["never", "summary", "full"])
@pytest.mark.parametrize("status,scope", [("running", "complete"), ("success", "complete"), ("partial", "partial"), ("error", "partial")])
def test_current_publications_validate_with_truthful_retention_and_scope(publications, mode, status, scope):
    anti, artifacts, _ = publications
    path = write(anti, options(mode), status, metadata={"scope_status": scope})
    record = anti.load_run_record(path)
    assert record["recordSchemaVersion"] == 1
    assert record["publicationStatus"] == ("lifecycle_only" if mode == "never" else "validated")
    assert record["runStatus"] == ("failed" if status == "error" else status)
    if mode != "never":
        result = stored(Path(record["resultPath"]))
        assert result["schemaVersion"] == 2
        assert result["scopeStatus"] == record["scopeStatus"] == scope
        assert "/revisions/" in record["resultPath"]
        assert bool(record["publication"]["lanes"]) is (mode == "full")


def test_partial_coverage_cannot_be_upgraded_by_successful_execution(publications):
    anti, _, _ = publications
    path = write(anti, options(), metadata={"scope_status": "complete", "declared_files": ["fixture.py"], "omitted_files": ["fixture.py"]})
    record = anti.load_run_record(path)
    assert record["status"] == "success"
    assert record["scopeStatus"] == "partial"
    assert stored(Path(record["resultPath"]))["coverage"]["status"] == "partial"


@pytest.mark.parametrize("target", ["result", "lane"])
@pytest.mark.parametrize("failure", ["missing", "checksum"])
def test_missing_or_modified_references_fail_closed(publications, target, failure):
    anti, artifacts, root = publications
    path = write(anti, options())
    index = stored(path)
    reference = index["publication"]["result"] if target == "result" else index["publication"]["lanes"][0]
    file = root / reference["path"]
    if failure == "missing":
        file.unlink()
    else:
        file.write_text('{}')
    with pytest.raises(artifacts.ArtifactError) as exc:
        anti.load_run_record(path)
    assert exc.value.code == ("incomplete_publication" if failure == "missing" else "checksum_mismatch")


@pytest.mark.parametrize("target", ["index", "result", "lane"])
def test_future_schema_versions_are_explicitly_refused(publications, target):
    anti, artifacts, root = publications
    path = write(anti, options())
    index = stored(path)
    if target == "index":
        index["recordSchemaVersion"] = 99
        path.write_text(json.dumps(index))
    elif target == "result":
        update_result(artifacts, path, lambda value: value.update(schemaVersion=99))
    else:
        lane = root / index["publication"]["lanes"][0]["path"]
        data = stored(lane)
        data["laneSchemaVersion"] = 99
        lane.write_text(json.dumps(data))
        index["publication"]["lanes"][0] = artifacts.file_reference(root, lane)
        path.write_text(json.dumps(index))
    with pytest.raises(artifacts.ArtifactError) as exc:
        anti.load_run_record(path)
    assert exc.value.code == "unsupported_version"


@pytest.mark.parametrize("change", [
    {"scopeStatus": "partial"}, {"runStatus": "failed"}, {"runId": "another"},
    {"verification": {"status": True}}, {"lanes": ["bad"]},
])
def test_structural_and_identity_errors_fail_even_with_matching_checksum(publications, change):
    anti, artifacts, _ = publications
    path = write(anti, options())
    update_result(artifacts, path, lambda value: value.update(change))
    with pytest.raises(artifacts.ArtifactError):
        anti.load_run_record(path)


def test_interrupted_publication_keeps_last_committed_revision_readable(publications, monkeypatch):
    anti, _, root = publications
    args = options()
    path = write(anti, args, "running")
    before = path.read_bytes()
    original = stored(path)
    old_result_bytes = Path(original["resultPath"]).read_bytes()
    atomic = anti.atomic_write_json
    def interrupted(destination, value):
        if destination == path:
            raise OSError("synthetic index publication interruption")
        return atomic(destination, value)
    with monkeypatch.context() as patch:
        patch.setattr(anti, "atomic_write_json", interrupted)
        with pytest.raises(OSError, match="publication interruption"):
            write(anti, args)
    assert path.read_bytes() == before
    assert anti.load_run_record(path)["status"] == "running"
    assert Path(original["resultPath"]).read_bytes() == old_result_bytes
    assert len(list((root / "fixture/revisions").glob("*/result.json"))) == 2
    write(anti, args)
    final = anti.load_run_record(path)
    assert final["status"] == "success"
    assert final["resultPath"] != original["resultPath"]
    assert Path(original["resultPath"]).read_bytes() == old_result_bytes


def test_orphan_first_revision_is_an_explicit_incomplete_publication(publications, monkeypatch):
    anti, artifacts, root = publications
    atomic = anti.atomic_write_json
    def interrupted(destination, value):
        if destination == root / "fixture.json":
            raise OSError("synthetic interruption")
        return atomic(destination, value)
    monkeypatch.setattr(anti, "atomic_write_json", interrupted)
    with pytest.raises(OSError):
        write(anti, options())
    with pytest.raises(artifacts.ArtifactError) as exc:
        anti.resolve_run_record_path("fixture")
    assert exc.value.code == "incomplete_publication"


def test_references_cannot_escape_or_follow_symlinks(publications, tmp_path):
    anti, artifacts, root = publications
    path = write(anti, options())
    index = stored(path)
    original = json.dumps(index)
    index["publication"]["result"]["path"] = "../private.json"
    path.write_text(json.dumps(index))
    with pytest.raises(artifacts.ArtifactError, match="invalid_reference"):
        anti.load_run_record(path)
    path.write_text(original)
    result = Path(index["resultPath"])
    outside = tmp_path / "outside.json"
    outside.write_bytes(result.read_bytes())
    result.unlink()
    result.symlink_to(outside)
    with pytest.raises(artifacts.ArtifactError, match="invalid_reference"):
        anti.load_run_record(path)


def test_legacy_index_adapter_does_not_invent_scope_or_integrity(publications):
    anti, _, root = publications
    root.mkdir()
    path = root / "old.json"
    raw = '{"id":"old","status":"partial","scopeStatus":"partial","extension":42}'
    path.write_text(raw)
    record = anti.load_run_record(path)
    assert record["publicationStatus"] == "legacy_unverified"
    assert record["scopeStatus"] == "partial"
    assert record["extension"] == 42
    assert path.read_text() == raw


def test_legacy_result_v1_and_missing_lane_are_distinguished(publications):
    anti, artifacts, root = publications
    path = write(anti, options())
    index = stored(path)
    index.pop("recordSchemaVersion")
    index.pop("publication")
    result_path = Path(index["resultPath"])
    result = stored(result_path)
    result["schemaVersion"] = 1
    result_path.write_text(json.dumps(result))
    path.write_text(json.dumps(index))
    assert anti.load_run_record(path)["publicationStatus"] == "legacy_unverified"
    Path(result["artifacts"]["rawLanePaths"][0]).unlink()
    with pytest.raises(artifacts.ArtifactError, match="incomplete_publication"):
        anti.load_run_record(path)


def test_cli_and_cleanup_do_not_treat_damaged_publication_as_completed(publications, capsys):
    anti, _, root = publications
    path = write(anti, options())
    Path(stored(path)["resultPath"]).unlink()
    assert anti.main(["runs", "show", "fixture"]) == 1
    assert "incomplete_publication" in capsys.readouterr().err
    assert anti.main(["runs", "list", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert rows[0]["publicationStatus"] == "incomplete_publication"
    assert rows[0]["status"] == "corrupt"
    assert anti.main(["runs", "clean", "--older-than", "1", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["rows"][0]["reason"] == "invalid_publication"
    assert path.exists()


def test_known_omission_count_cannot_claim_complete_scope(publications):
    anti, artifacts, _ = publications
    path = write(anti, options())
    index = stored(path)
    index["omittedFileCount"] = 1
    path.write_text(json.dumps(index))
    with pytest.raises(artifacts.ArtifactError, match="conflicting_status"):
        anti.load_run_record(path)


def test_validated_private_shaped_run_id_survives_redaction(publications):
    anti, _, _ = publications
    path = write(anti, options(run_id="user_12345678"))
    record = anti.load_run_record(path)
    assert record["id"] == "user_12345678"
    assert stored(Path(record["resultPath"]))["runId"] == record["id"]


def test_standalone_reader_validates_the_same_publication(publications, tmp_path):
    import shutil
    import subprocess
    import sys
    anti, _, _ = publications
    path = write(anti, options())
    copied = tmp_path / "standalone"
    shutil.copytree(SCRIPT.parent, copied, ignore=shutil.ignore_patterns("__pycache__"))
    code = "from pathlib import Path; import sys; from anti_lib.artifacts import read_record; assert read_record(Path(sys.argv[1]))['publicationStatus'] == 'validated'"
    subprocess.run([sys.executable, "-S", "-c", code, str(path)], cwd=copied, check=True)


@pytest.mark.parametrize("coverage_fields", [
    {"chunksOmitted": 1}, {"chunksNotSent": 1}, {"chunksFailed": 1},
    {"omittedFiles": ["fixture.py"]}, {"truncatedFiles": ["fixture.py"]},
    {"partialFiles": ["fixture.py"]}, {"failedFiles": ["fixture.py"]},
    {"chunksExpected": 2, "chunksCompleted": 1},
    {"files": [{"contentStatus": "partial"}]},
    {"files": [{"bytesDeclared": 4, "bytesSent": 2}]},
    {"chunks": [{"status": "failed"}]},
])
@pytest.mark.parametrize("scope", ["complete", "partial"])
def test_coverage_loss_cannot_validate_as_complete_even_with_new_checksum(publications, coverage_fields, scope):
    anti, artifacts, _ = publications
    path = write(anti, options(), metadata={"scope_status": scope})
    update_result(artifacts, path, lambda value: value["coverage"].update(status="complete", **coverage_fields))
    with pytest.raises(artifacts.ArtifactError, match="conflicting_status"):
        anti.load_run_record(path)


def test_writer_derives_partial_scope_from_failed_chunk_counts(publications):
    anti, _, _ = publications
    path = write(anti, options(), metadata={"scope_status": "complete", "planned_chunk_count": 1,
                                          "completed_chunk_count": 0, "failed_chunk_count": 1})
    record = anti.load_run_record(path)
    assert record["scopeStatus"] == "partial"
    assert stored(Path(record["resultPath"]))["coverage"]["status"] == "partial"


@pytest.mark.parametrize("mode", ["never", "summary", "full"])
def test_current_index_requires_explicit_identity(publications, mode):
    anti, artifacts, _ = publications
    path = write(anti, options(mode))
    index = stored(path)
    index.pop("id")
    path.write_text(json.dumps(index))
    with pytest.raises(artifacts.ArtifactError, match="identity_mismatch"):
        anti.load_run_record(path)


def test_legacy_id_fallback_remains_unverified(publications):
    anti, _, root = publications
    root.mkdir()
    path = root / "legacy.json"
    path.write_text('{"status":"partial"}')
    assert anti.load_run_record(path)["publicationStatus"] == "legacy_unverified"


@pytest.mark.parametrize("content", [
    {"output_text": "synthetic private text"}, {"output_preview": "synthetic private preview"},
    {"prompt_text": "synthetic prompt"}, {"execution_ledger": []}, {"models": ["private-model"]},
    {"metadata": {"request_log_correlation_id": "fixture", "findings": []}},
    {"metadata": {"request_log_correlation_id": "fixture", "output_chars": "synthetic content"}},
    {"command": "synthetic private label"}, {"error": "synthetic private error"},
])
def test_never_mode_cannot_claim_content_bearing_index_is_lifecycle_only(publications, content):
    anti, artifacts, _ = publications
    path = write(anti, options("never"))
    index = stored(path)
    index.update(content)
    path.write_text(json.dumps(index))
    with pytest.raises(artifacts.ArtifactError):
        anti.load_run_record(path)


@pytest.mark.parametrize("source_mode,target_mode", [("summary", "full"), ("full", "summary")])
def test_index_cannot_relabel_result_retention(publications, source_mode, target_mode):
    anti, artifacts, _ = publications
    path = write(anti, options(source_mode), execution_ledger=None)
    index = stored(path)
    index["save_output"] = target_mode
    index["retention"] = {"mode": target_mode, "contentComplete": target_mode == "full"}
    path.write_text(json.dumps(index))
    with pytest.raises(artifacts.ArtifactError, match="retention_mismatch"):
        anti.load_run_record(path)


@pytest.mark.parametrize("mode", ["summary", "full"])
def test_result_retention_must_agree_after_checksum_recalculation(publications, mode):
    anti, artifacts, _ = publications
    path = write(anti, options(mode), execution_ledger=None)
    other = "summary" if mode == "full" else "full"
    update_result(artifacts, path, lambda value: value.update(retention={"mode": other, "contentComplete": other == "full"}))
    with pytest.raises(artifacts.ArtifactError, match="retention_mismatch"):
        anti.load_run_record(path)
