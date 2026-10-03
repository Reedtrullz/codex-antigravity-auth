"""Reject malformed model findings and merge duplicates deterministically."""

import itertools
import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "codex_antigravity_auth/skills/anti/scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
import anti


def finding(**fields):
    return {"claim": "Missing validation", "verify": "Run the boundary test", "file": "module.py", "line": 3, **fields}


def parse(items):
    result, warning, diagnostics = anti.parse_panel_findings(json.dumps({"summary": "fixture", "findings": items}))
    assert warning is None
    return result, diagnostics


@pytest.mark.parametrize("field,value", [
    *[("line", value) for value in [float("inf"), float("-inf"), float("nan"), True, False, "3", 3.0, -1, 0, 10**400, [], {}]],
    *[("confidence", value) for value in [float("inf"), float("-inf"), float("nan"), True, "0.7", None, [], {}, 10**400]],
    ("claim", {}), ("verify", []), ("id", 10**400), ("file", True), ("evidence", {}), ("lanes", [False]),
])
def test_invalid_scalars_do_not_crash_and_have_bounded_diagnostics(field, value):
    result, diagnostics = parse([finding(**{field: value}), finding(claim="Valid independent finding")])
    assert result["findings_total"] == 2
    assert result["findings_invalid"] == result["findings_dropped"] == 1
    assert result["findings_merged"] == result["findings_truncated"] == 0
    assert len(result["findings"]) == 1
    assert result["finding_errors"][0]["index"] == 1
    assert field in result["finding_errors"][0]["reason"]
    assert len(result["finding_errors"][0]["reason"]) < 120
    json.dumps(result, allow_nan=False)


def test_invalid_findings_report_bounded_errors_without_echoing_provider_values():
    result, _ = parse([finding(line="api_key=fixture-secret")] * 75 + [None, [], "malformed"])
    assert result["findings_invalid"] == 78
    assert len(result["finding_errors"]) == 20
    assert result["finding_errors_omitted"] == 58
    assert "fixture-secret" not in json.dumps(result)


def test_duplicate_permutations_preserve_evidence_with_true_mean_and_no_loss():
    inputs = [
        finding(confidence=0.1, evidence="test A", verify="check A", lanes=["a"], severity="low"),
        finding(confidence=0.9, evidence="test B", verify="check B", lanes=["b"], severity="high"),
        finding(confidence=0.9, evidence="test C", verify="check C", lanes=["c"], severity="medium"),
    ]
    expected, _ = parse(inputs)
    for permutation in itertools.permutations(inputs):
        actual, _ = parse(list(permutation))
        assert actual == expected
    assert expected["findings_merged"] == 2
    assert expected["findings_invalid"] == expected["findings_dropped"] == expected["findings_truncated"] == 0
    merged = expected["findings"][0]
    assert merged["confidence"] == 0.63
    assert expected["confidence_kind"] == "model_reported"
    assert merged["lanes"] == ["a", "b", "c"]
    assert merged["severity"] == "high"
    assert {row["verify"] for row in merged["corroboration"]} == {"check A", "check B", "check C"}
    assert all(text in merged["evidence"] for text in ["test A", "test B", "test C"])


def test_generated_ids_and_result_order_do_not_depend_on_input_order():
    inputs = [finding(claim="First"), finding(claim="Second"), finding(claim="Third")]
    expected, _ = parse(inputs)
    for permutation in itertools.permutations(inputs):
        actual, _ = parse(list(permutation))
        assert actual == expected


def test_truncation_is_counted_without_false_duplicate_merges_and_raw_source_remains():
    inputs = [finding(claim="x" * 1800 + suffix) for suffix in ("one", "two")]
    result, diagnostics = parse(inputs)
    assert result["findings_truncated"] == 2
    assert result["findings_dropped"] == result["findings_merged"] == 0
    assert len(result["findings"]) == 2
    assert diagnostics["safe_structured"]["findings"] == inputs


def test_judge_input_treats_merging_as_complete_and_actual_loss_as_partial():
    duplicates = [finding(confidence=confidence, lanes=[str(index)]) for index, confidence in enumerate([0.1, 0.9, 0.9])]
    for inputs, expected in [(duplicates, "complete"), ([finding(line=True)], "partial"), ([finding(evidence="x" * 2100)], "partial")]:
        metadata = {}
        anti.build_panel_synthesis_prompt(
            panel_mode="ask", roles=[], max_chars=100000,
            source_prompt="fixture",
            panel_results=[{"status": "success", "model": "fixture", "output_text": json.dumps({"findings": inputs})}],
            metadata=metadata,
            caveats=[],
        )
        assert metadata["judge_input_contract_status"] == expected


def test_duplicate_corroboration_does_not_amplify_judge_prompt_past_default_budget():
    inputs = [finding(evidence=f"evidence-{index}:" + "x" * 1000, verify=f"check {index}") for index in range(45)]
    raw = json.dumps({"findings": inputs})
    assert len(raw) < 60_000
    contract, _ = parse(inputs)
    assert len(contract["findings"][0]["corroboration"]) == 45
    metadata = {}
    prompt, _, _ = anti.build_panel_synthesis_prompt(
        panel_mode="ask", source_prompt="fixture", roles=[], max_chars=120_000,
        panel_results=[{"status": "success", "model": "fixture", "output_text": raw}],
        metadata=metadata, caveats=[],
    )
    assert len(prompt) < 120_000
    assert metadata["judge_input_contract_status"] == "complete"
    for item in inputs:
        assert prompt.count(item["evidence"]) == 1
