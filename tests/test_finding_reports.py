"""Local adjudication/export uses only synthetic records, source and evidence."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import jsonschema
import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "codex_antigravity_auth/skills/anti/scripts/anti.py"


@pytest.fixture
def review(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location("anti_report_fixture", SCRIPT)
    anti = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(anti)
    from anti_lib import finding_verdicts, reflections, reports
    monkeypatch.setattr(reflections, "REFLECTIONS_DIR", tmp_path / "reflections")
    monkeypatch.setattr(anti, "RUNS_DIR", tmp_path / "runs")
    repo = tmp_path / "project"
    repo.mkdir()
    (repo / "before.py").write_text("fixture = 1\n")
    return anti, reflections, reports, finding_verdicts, repo


def capture(review, run="run-1", **kwargs):
    _anti, reflections, _reports, _verdicts, repo = review
    data = {"repo_path": repo, "run_id": run, "mode": "review", "panel_status": "partial_multi_model",
            "models": ["requested-model"], "save_output": "full",
            "context": {"actualModels": ["actual-fallback-model"], "actualProviders": ["fixture-provider"],
                        "requestedModels": ["requested-model"], "scopeStatus": "partial", "omitted_files": ["unreviewed.py"]},
            "findings": [{"id": "F1", "fingerprint": "fp-shared", "file": "before.py", "line": 1,
                          "claim": "Synthetic finding", "evidence": "Original model evidence", "severity": "medium",
                          "claimVerdict": "unverified", "checks": [{"fileHash": hashlib.sha256(b"fixture = 1\n").hexdigest(),
                                                                       "status": "passed", "check": "python_syntax"}]},
                         {"id": "F2", "fingerprint": "fp-second", "file": "before.py", "claim": "Other claim", "evidence": "Other evidence"}]}
    data.update(kwargs)
    return reflections.record_review(**data)


def annotate(review, key, *, run="run-1", status="confirmed", **kwargs):
    _anti, reflections, _reports, _verdicts, repo = review
    values = {"status": status, "author": "fixture-reviewer", "evidence": "Inspected synthetic source and reproduced the claim",
              "source_file": "before.py"}
    values.update(kwargs)
    return reflections.update_finding_verdict(repo, run, key, **values)


def test_per_finding_verdict_does_not_mutate_advisory_or_other_findings(review):
    _, reflections, reports, _, repo = review
    original = capture(review)
    key = original["findings"][0]["findingKey"]
    updated = annotate(review, key)
    assert {k: v for k, v in updated.items() if k != "adjudication"} == original["findings"][0]
    stored = reflections.list_records(repo)[0]
    assert stored["findings"][1] == original["findings"][1]
    report = reports.build_report([stored], repo)
    assert [row["verdict"] for row in report["runs"][0]["findings"]] == ["confirmed", "unresolved"]
    assert all(row["claimVerification"] == "unverified" for row in report["runs"][0]["findings"])
    assert updated["adjudication"]["sourceHash"] == hashlib.sha256((repo / "before.py").read_bytes()).hexdigest()


def test_model_cannot_set_a_local_verdict_and_run_verdict_is_separate(review):
    _, reflections, reports, _, repo = review
    capture(review, verdict="confirmed", findings=[{"id": "F1", "claim": "fixture", "adjudication": {"status": "confirmed"}}])
    report = reports.build_report(reflections.list_records(repo), repo)
    assert report["runs"][0]["runVerdict"] == "confirmed"
    assert report["runs"][0]["findings"][0]["verdict"] == "unresolved"
    assert report["runs"][0]["findings"][0]["adjudication"] is None


def test_mixed_cohorts_preserve_rejected_evidence_and_content_identity_after_rename(review):
    _, reflections, reports, _, repo = review
    first = capture(review)
    annotate(review, first["findings"][0]["findingKey"], status="rejected", evidence="Synthetic counterexample")
    (repo / "before.py").rename(repo / "after.py")
    findings = copy.deepcopy(first["findings"])
    findings[0]["file"] = "after.py"
    second = capture(review, run="run-2", findings=findings)
    annotate(review, second["findings"][0]["findingKey"], run="run-2", status="confirmed", source_file="after.py")
    summary = reflections.get_summary(repo)
    assert summary["verdictCohorts"] == {"confirmed": 1, "rejected": 1, "unresolved": 2}
    group = next(item for item in summary["recurringFindings"] if item["sourceHash"])
    assert group["cohorts"] == {"confirmed": 1, "rejected": 1, "unresolved": 0}
    assert {row["file"] for row in group["occurrences"]} == {"before.py", "after.py"}
    assert any(row["localEvidence"] == "Synthetic counterexample" for row in group["occurrences"])
    report = reports.build_report(reflections.list_records(repo), repo)
    assert sum(len(run["findings"]) for run in report["runs"]) == 4


def test_duplicate_ids_are_addressable_but_ambiguous_run_keys_fail(review):
    _, reflections, _, _, repo = review
    row = capture(review, findings=[{"id": "F1", "claim": "same"}, {"id": "F1", "claim": "same"}])
    keys = [item["findingKey"] for item in row["findings"]]
    assert keys[0] != keys[1]
    annotate(review, keys[1], status="rejected")
    path = reflections._reflection_path(repo)
    before = path.read_bytes()
    with pytest.raises(ValueError, match="exactly one"):
        annotate(review, "F1")
    assert path.read_bytes() == before


@pytest.mark.parametrize("values", [{"author": ""}, {"evidence": ""}, {"evidence": "x" * 4001}, {"status": "maybe"}, {"source_file": "../outside.py"}])
def test_invalid_adjudication_preserves_history(review, values):
    _, reflections, _, _, repo = review
    row = capture(review)
    path = reflections._reflection_path(repo)
    before = path.read_bytes()
    with pytest.raises(ValueError):
        annotate(review, row["findings"][0]["findingKey"], **values)
    assert path.read_bytes() == before


def test_malformed_existing_adjudication_refuses_rewrite(review):
    _, reflections, _, _, repo = review
    row = capture(review)
    row["findings"][0]["adjudication"] = {"status": ["confirmed"]}
    path = reflections._reflection_path(repo)
    path.write_text(json.dumps([row]))
    before = path.read_bytes()
    with pytest.raises(RuntimeError, match="Invalid reflection"):
        annotate(review, row["findings"][0]["findingKey"])
    assert path.read_bytes() == before


def test_json_and_sarif_validate_and_preserve_provenance_scope_checks_and_verdict(review):
    _, reflections, reports, _, repo = review
    row = capture(review)
    annotate(review, row["findings"][0]["findingKey"], status="rejected")
    report = reports.build_report(reflections.list_records(repo), repo)
    schema = json.loads((SCRIPT.parent.parent / "schemas/review-report-v1.json").read_text())
    jsonschema.Draft202012Validator(schema).validate(report)
    sarif = reports.to_sarif(report)
    schema = json.loads((ROOT / "tests/fixtures/sarif-schema-2.1.0.json").read_text())
    jsonschema.Draft7Validator(schema, format_checker=jsonschema.FormatChecker()).validate(sarif)
    run = sarif["runs"][0]
    assert run["properties"]["actualModels"] == ["actual-fallback-model"]
    assert run["properties"]["actualProviders"] == ["fixture-provider"]
    assert run["properties"]["coverage"]["omitted_files"] == ["unreviewed.py"]
    assert all(result["kind"] == "review" and "suppressions" not in result for result in run["results"])
    assert run["results"][0]["properties"]["verdict"] == "rejected"
    assert run["results"][0]["properties"]["advisory"]["checks"][0]["status"] == "passed"
    markdown = reports.to_markdown(report)
    assert "actual-fallback-model" in markdown and "rejected" in markdown and "unreviewed.py" in markdown


def test_all_exports_redact_compound_credentials_and_workspace_root(review):
    _, reflections, reports, _, repo = review
    row = capture(review, findings=[{"id": "F1", "file": str(repo / "before.py"),
                                    "claim": '<script>fixture</script> database_password="fixture secret words"',
                                    "evidence": f"provider_api_key=fixture-key-value at {repo}",
                                    "checks": [{"cwd": str(repo), "file": str(repo / "before.py"), "status": "passed"}]}])
    annotate(review, row["findings"][0]["findingKey"], evidence='password="fixture another secret"')
    report = reports.build_report(reflections.list_records(repo), repo)
    outputs = [json.dumps(report), json.dumps(reports.to_sarif(report)), reports.to_markdown(report)]
    for output in outputs:
        assert "fixture secret words" not in output and "fixture-key-value" not in output and "fixture another secret" not in output
        assert str(repo) not in output
    assert "<script>" not in outputs[-1]


def test_summary_export_never_claims_full_content_or_complete_finding_capture(review):
    _, reflections, reports, _, repo = review
    capture(review, save_output="summary", findings=[{"id": f"F{i}", "claim": "x" * 3000} for i in range(60)])
    report = reports.build_report(reflections.list_records(repo), repo)
    run = report["runs"][0]
    assert run["contentComplete"] is False and run["declaredFindingCount"] == 60
    assert run["retainedFindingCount"] < 60
    assert run["actualModels"] == ["actual-fallback-model"]
    assert run["actualProviders"] == ["fixture-provider"]
    assert run["scopeStatus"] == "partial"


def test_cli_export_and_local_adjudication_never_publish(review, capsys, monkeypatch, tmp_path):
    anti, reflections, _, _, repo = review
    row = capture(review)
    monkeypatch.setattr(anti, "post_response", lambda *a, **k: pytest.fail("no gateway call"))
    assert anti.main(["runs", "finding", "--repo", str(repo), "--run-id", "run-1", "--finding", row["findings"][0]["findingKey"],
                      "--verdict", "confirmed", "--author", "fixture-reviewer", "--source-file", "before.py", "--evidence", "Synthetic local evidence"]) == 0
    capsys.readouterr()
    for kind in ("json", "sarif", "markdown"):
        output = tmp_path / ("report." + kind)
        assert anti.main(["runs", "export", "--repo", str(repo), "--run-id", "run-1", "--format", kind, "--output", str(output)]) == 0
        before = output.read_bytes()
        assert before
        assert anti.main(["runs", "export", "--repo", str(repo), "--format", kind, "--output", str(output)]) != 0
        assert output.read_bytes() == before


def test_export_publish_failure_preserves_destination_and_cleans_owned_temporary(review, monkeypatch, tmp_path):
    _, _, reports, _, _ = review
    target = tmp_path / "report.json"
    target.write_bytes(b"fixture-preserved")
    with pytest.raises(FileExistsError):
        reports.write_export(target, "replacement")
    assert target.read_bytes() == b"fixture-preserved"
    assert not list(tmp_path.glob(".anti-report-*"))
    target.unlink()
    monkeypatch.setattr(reports.os, "link", lambda *a: (_ for _ in ()).throw(OSError("synthetic publish failure")))
    with pytest.raises(OSError):
        reports.write_export(target, "complete staged content")
    assert not target.exists() and not list(tmp_path.glob(".anti-report-*"))


def test_exports_label_legacy_provenance_unknown_and_ignore_run_verdict_for_cohorts(review):
    _, reflections, reports, _, repo = review
    record = capture(review)
    record.pop("context")
    record.pop("save_output")
    record["verdict"] = "rejected"
    for finding in record["findings"]:
        finding.pop("findingKey", None)
    report = reports.build_report([record], repo)
    run = report["runs"][0]
    assert run["actualModels"] == run["actualProviders"] == []
    assert run["scopeStatus"] == run["provenanceStatus"] == "unknown"
    assert not run["contentComplete"]
    assert all(item["verdict"] == "unresolved" for item in run["findings"])


def test_summary_manual_annotation_has_separate_bounded_evidence(review):
    _, reflections, reports, _, repo = review
    row = capture(review, save_output="summary")
    annotate(review, row["findings"][0]["findingKey"], status="unresolved", evidence="x" * 4000)
    report = reports.build_report(reflections.list_records(repo), repo)
    finding = report["runs"][0]["findings"][0]
    assert len(finding["adjudication"]["evidence"]) == 4000
    assert finding["verdict"] == "unresolved"
    assert report["runs"][0]["contentComplete"] is False


@pytest.mark.parametrize("mode", ["full", "summary"])
def test_parser_loss_survives_reflection_and_every_export_format(review, mode):
    anti, reflections, reports, _, repo = review
    parsed, _warning, _diagnostics = anti.parse_panel_findings(json.dumps({
        "findings": [{"id": "F1", "claim": "Synthetic valid claim", "file": "before.py", "line": 1,
                      "severity": "medium", "confidence": 0.5, "evidence": "fixture", "verify": "Inspect synthetic fixture"}, "invalid row", 42],
    }))
    assert parsed["findings_total"] == 3 and parsed["findings_dropped"] == 2
    capture(review, save_output=mode, findings=parsed["findings"],
            context={"scopeStatus": "complete", "findings": parsed})
    stored = reflections.list_records(repo)[0]
    assert stored["parser_findings_total"] == 3 and stored["parser_findings_dropped"] == 2
    report = reports.build_report([stored], repo)
    run = report["runs"][0]
    assert run["declaredFindingCount"] == run["retainedFindingCount"] == 1
    assert run["parserFindingTotal"] == 3 and run["parserFindingsDropped"] == 2 and run["parserLossStatus"] == "loss"
    assert run["contentComplete"] is (mode == "full")
    sarif = reports.to_sarif(report)
    assert sarif["runs"][0]["properties"]["parserFindingsDropped"] == 2
    assert "3 total; 2 dropped; loss status: loss" in reports.to_markdown(report)
    jsonschema.Draft202012Validator(json.loads((SCRIPT.parent.parent / "schemas/review-report-v1.json").read_text())).validate(report)


def test_parser_counts_survive_exhausted_summary_budget_and_legacy_stays_unknown(review):
    _, reflections, reports, _, repo = review
    capture(review, save_output="summary", models=["x" * 10000] * 50,
            context={"findings_total": 80, "findings_dropped": 78}, findings=[{"claim": "a"}, {"claim": "b"}])
    stored = reflections.list_records(repo)[0]
    assert stored["parser_findings_total"] == 80 and stored["parser_findings_dropped"] == 78
    run = reports.build_report([stored], repo)["runs"][0]
    assert run["parserFindingsDropped"] == 78
    stored.pop("parser_findings_total")
    stored.pop("parser_findings_dropped")
    run = reports.build_report([stored], repo)["runs"][0]
    assert run["parserFindingTotal"] is None and run["parserFindingsDropped"] is None
    assert run["parserLossStatus"] == "unknown"


def test_markdown_keeps_record_audit_and_mixed_cohort_occurrences(review):
    _, reflections, reports, _, repo = review
    first = capture(review)
    annotate(review, first["findings"][0]["findingKey"], status="rejected", evidence="Synthetic cohort evidence")
    capture(review, run="run-2")
    report = reports.build_report(reflections.list_records(repo), repo)
    markdown = reports.to_markdown(report)
    for run in report["runs"]:
        assert str(run["timestamp"]) in markdown and run["sourceRecordHash"] in markdown
    assert "Verdict cohorts" in markdown and "rejected" in markdown and "unresolved" in markdown
    group = next(item for item in report["summary"]["recurringFindings"] if item["sourceHash"])
    assert group["identity"] in markdown and group["sourceHash"] in markdown
    assert "Synthetic cohort evidence" in markdown and "Original model evidence" in markdown


@pytest.mark.parametrize("mode", ["full", "summary"])
@pytest.mark.parametrize("text", ['{"findings": [garbage', "Synthetic prose without structured findings"])
def test_failed_parse_exports_unknown_counts_and_retains_warning(review, mode, text):
    anti, reflections, reports, _, repo = review
    parsed, warning, _diagnostics = anti.parse_panel_findings(text)
    assert parsed is None and warning
    fallback = anti.fallback_findings_contract(text, [warning], parse_warning=warning)
    assert fallback["findings_total"] is None and fallback["findings_dropped"] is None
    capture(review, save_output=mode, findings=fallback["findings"],
            context={"findings": fallback, "caveats": [warning]})
    report = reports.build_report(reflections.list_records(repo), repo)
    run = report["runs"][0]
    assert run["parserFindingTotal"] is None and run["parserFindingsDropped"] is None
    assert run["parserLossStatus"] == "unknown" and run["coverage"]["caveats"] == [warning]
    assert reports.to_sarif(report)["runs"][0]["properties"]["parserLossStatus"] == "unknown"
    assert "unknown total; unknown dropped; loss status: unknown" in reports.to_markdown(report)


def test_successfully_parsed_empty_findings_have_known_zero_loss(review):
    anti, reflections, reports, _, repo = review
    parsed, warning, _diagnostics = anti.parse_panel_findings('{"findings": []}')
    assert parsed is not None and warning is None
    capture(review, findings=[], context={"findings": parsed})
    run = reports.build_report(reflections.list_records(repo), repo)["runs"][0]
    assert run["parserFindingTotal"] == run["parserFindingsDropped"] == 0
    assert run["parserLossStatus"] == "none"
