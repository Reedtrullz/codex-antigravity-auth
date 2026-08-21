import json
import sys
import tempfile
import unittest
from pathlib import Path


ANTI_SCRIPTS = Path(__file__).resolve().parents[1] / "codex_antigravity_auth" / "skills" / "anti" / "scripts"
if str(ANTI_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(ANTI_SCRIPTS))

import anti  # noqa: E402
from anti_lib.verifier import verify_finding  # noqa: E402


class NormalizeAndFindingsTests(unittest.TestCase):
    def test_panel_identity_preserves_requested_actual_and_fallback_defaults(self):
        identity = anti.panel_model_identity(requested_model="sonnet", actual_model="deepseek:deepseek-v4-pro", fallback_used=True)
        self.assertEqual(identity["requestedModel"], "sonnet")
        self.assertEqual(identity["actualModel"], "deepseek:deepseek-v4-pro")
        self.assertEqual(identity["requested_provider"], "google-antigravity")
        self.assertEqual(identity["actual_provider"], "deepseek")
        self.assertEqual(identity["fallbackChain"], ["sonnet"])
        self.assertTrue(identity["fallbackUsed"])
        self.assertEqual(identity["execution_status"], "fallback")

    def test_normalize_defaults_clamps_and_fingerprints(self):
        item = anti.normalize_finding_item(
            {"claim": "  Unsafe input  ", "verify": "run tests", "confidence": 9, "severity": "bogus", "line": -1},
            2,
        )
        self.assertIsNotNone(item)
        self.assertEqual(item["claim"], "Unsafe input")
        self.assertEqual(item["severity"], "medium")
        self.assertEqual(item["confidence"], 1.0)
        self.assertEqual(item["id"], "F002")
        self.assertIsNone(item["line"])
        self.assertRegex(item["fingerprint"], r"^sha256:[0-9a-f]{16}$")
        self.assertIsNone(anti.normalize_finding_item({"claim": "missing verify"}, 1))

    def test_parse_dedups_lanes_severity_and_averages_confidence(self):
        payload = {
            "summary": "s",
            "findings": [
                {"claim": "Same issue", "verify": "check", "severity": "low", "confidence": 0.2, "lanes": ["a"], "file": "x.py", "line": 3},
                {"claim": "same issue", "verify": "check", "severity": "high", "confidence": 0.8, "lanes": ["b"], "file": "x.py", "line": 3},
            ],
        }
        result, warning, _ = anti.parse_panel_findings(json.dumps(payload))
        self.assertIsNone(warning)
        self.assertEqual(len(result["findings"]), 1)
        finding = result["findings"][0]
        self.assertEqual(finding["severity"], "high")
        self.assertEqual(finding["confidence"], 0.5)
        self.assertEqual(set(finding["lanes"]), {"a", "b"})
        self.assertEqual(result["findings_total"], 2)
        self.assertEqual(result["findings_dropped"], 1)


class RoutingAndCostTests(unittest.TestCase):
    def test_resolve_auto_model_thresholds_high_risk_and_no_diff(self):
        self.assertEqual(anti.resolve_auto_model(diff_lines=1)[0], "flash-3.6")
        self.assertEqual(anti.resolve_auto_model(diff_lines=200)[0], "flash-3.6")
        self.assertEqual(anti.resolve_auto_model(diff_lines=201)[0], "sonnet")
        self.assertEqual(anti.resolve_auto_model(diff_lines=1001)[0], "opus")
        self.assertEqual(anti.resolve_auto_model(diff_lines=1, file_paths=["src/oauth.py"])[0], "opus")
        self.assertEqual(anti.resolve_auto_model(scope="working-tree", diff_lines=0, default="opus")[0], "opus")

    def test_estimate_cost_reports_tokens_and_tier(self):
        estimate = anti.estimate_cost(model="claude-sonnet-4-6", prompt_chars=4000, estimated_output_tokens=500)
        self.assertEqual(estimate["estimated_input_tokens"], 1000)
        self.assertEqual(estimate["estimated_total_tokens"], 1500)
        self.assertEqual(estimate["cost_tier"], "quota")
        self.assertEqual(estimate["quality_rank"], 85)
        self.assertEqual(anti.estimate_cost(model="openrouter:nvidia/nemotron-3-super-120b-a12b:free", prompt_chars=0)["cost_tier"], "free")

    def test_actual_cost_uses_usage_and_fallback_estimate(self):
        actual = anti.actual_call_cost("claude-sonnet-4-6", {"usage": {"total_tokens": 2000}}, prompt_chars=4000, max_output_tokens=500)
        self.assertAlmostEqual(actual, 0.004)
        fallback = anti.actual_call_cost("claude-sonnet-4-6", None, prompt_chars=4000, max_output_tokens=500)
        self.assertAlmostEqual(fallback, 0.003)
        self.assertEqual(anti.actual_call_cost("openrouter:nvidia/nemotron-3-super-120b-a12b:free", {"usage": {"total_tokens": 999}}, prompt_chars=1, max_output_tokens=1), 0.0)


class VerifierTests(unittest.TestCase):
    def test_verifier_syntax_and_secrets(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bad = root / "bad.py"
            bad.write_text("token = '123456789'\ndef broken(:\n", encoding="utf-8")
            result = verify_finding({"file": "bad.py", "evidence": "unverified"}, root)
            self.assertIn("python_syntax", result["evidence"])
            self.assertIn("secrets_scan", result["evidence"])

    def test_verifier_existing_missing_and_no_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            good = root / "good.py"
            good.write_text("value = 1\n", encoding="utf-8")
            existing = verify_finding({"file": "good.py", "evidence": "unverified"}, root)
            self.assertEqual(existing["evidence"], "unverified")
            missing = {"file": "missing.py", "evidence": "unverified"}
            self.assertEqual(verify_finding(missing, root), missing)
            no_file = {"claim": "x"}
            self.assertEqual(verify_finding(no_file, root), no_file)


if __name__ == "__main__":
    unittest.main()
