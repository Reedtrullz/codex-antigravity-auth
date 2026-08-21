import argparse
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path


ANTI_SCRIPTS = Path(__file__).resolve().parents[1] / "codex_antigravity_auth" / "skills" / "anti" / "scripts"
if str(ANTI_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(ANTI_SCRIPTS))

import anti  # noqa: E402
import anti_lib.reflections as reflections  # noqa: E402
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
    def test_extract_validation_url_from_403_body(self):
        body = 'HTTP 403: {"error": {"reason": "VALIDATION_REQUIRED", "metadata": {"validation_url": "https://accounts.google.com/signin/continue?sarp=1&plt=abc"}}}'
        url = anti.extract_validation_url(body)
        self.assertIsNotNone(url)
        self.assertIn("accounts.google.com/signin/continue", url)

    def test_enrich_validation_required_adds_action(self):
        body = "HTTP 403: VALIDATION_REQUIRED validation_url https://accounts.google.com/signin/continue?x=1"
        enriched = anti.enrich_validation_required_error(body)
        self.assertIn("[ACTION REQUIRED]", enriched)
        self.assertIn("Verify your Google account", enriched)

    def test_enrich_validation_required_noop_for_other_errors(self):
        body = "HTTP 429: rate limited"
        self.assertEqual(anti.enrich_validation_required_error(body), body)

    def test_detect_repo_profile_python_project(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pyproject.toml").write_text("[project]\nname='x'\n")
            (root / "antigravity_auth").mkdir()
            profile = anti.detect_repo_profile(root)
            self.assertIn("Python project", profile)
            self.assertIn("antigravity_auth", profile)

    def test_detect_repo_profile_empty_dir(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            profile = anti.detect_repo_profile(Path(tmp))
            self.assertEqual(profile, "")

    def test_detect_repo_profile_polyglot_reports_all(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pyproject.toml").write_text("[project]\nname='x'\n")
            (root / "package.json").write_text("{}")
            profile = anti.detect_repo_profile(root)
            self.assertIn("Python project", profile)
            self.assertIn("JavaScript/TypeScript project", profile)

    def test_extract_validation_url_redacted_in_enriched_output(self):
        body = "HTTP 403: VALIDATION_REQUIRED validation_url https://accounts.google.com/signin/continue?plt=SECRET_TOKEN_VALUE"
        enriched = anti.enrich_validation_required_error(body)
        self.assertNotIn("SECRET_TOKEN_VALUE", enriched)

    def test_int_coercion_on_malformed_metadata_does_not_raise(self):
        gen_meta = {"prompt_chars": "unknown", "output_chars": None}
        try:
            prompt_chars = int(gen_meta.get("prompt_chars") or 0)
        except (ValueError, TypeError):
            prompt_chars = 0
        try:
            output_chars = int(gen_meta.get("output_chars") or 0)
        except (ValueError, TypeError):
            output_chars = 0
        self.assertEqual(prompt_chars, 0)
        self.assertEqual(output_chars, 0)

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

    def test_maybe_summarize_raises_on_chunk_overflow(self):
        """When omitted_items exist and allow_partial=False, raise before any model call."""
        from unittest.mock import patch

        args = argparse.Namespace(
            mode="review",
            chunked="always",
            allow_partial=False,
            max_review_chunks=2,
            max_prompt_chars=100000,
            priority_file=None,
        )
        metadata = {
            "_review_context": {"diff": "x", "scope_line": "s", "excluded": [], "caveats": []},
            "status": "incomplete",
        }
        chunk_metadata = {
            "planned_chunk_count": 3,
            "omitted_items": ["item-a", "item-b"],
            "omitted_file_count": 2,
        }
        with (
            patch.object(anti, "build_review_chunk_prompts", return_value=([{"kind": "diff"}], chunk_metadata)),
            patch.object(anti, "generate_with_fallback") as mock_generate,
            patch.object(anti, "run_chunked_review") as mock_run_chunked,
        ):
            with self.assertRaisesRegex(anti.AntiError, r"review scope needs.*--max-review-chunks"):
                anti.maybe_summarize_panel_review(args=args, prompt="p", caveats=[], metadata=metadata, panel_models=["claude-sonnet-4-6"])
            mock_generate.assert_not_called()
            mock_run_chunked.assert_not_called()

    def test_main_error_handler_populates_models_from_generation_metadata(self):
        """AntiError with generation_metadata should populate models/prompt_chars/output_chars."""
        exc = anti.AntiError("boom")
        exc.generation_metadata = {  # type: ignore[attr-defined]
            "fallbackChain": ["sonnet", "flash"],
            "prompt_chars": "123",
            "output_chars": None,
            "generation_failures": [{"model": "sonnet", "error": "rate limited sk-abc123def456ghi789"}],
        }
        args = argparse.Namespace(resolved_panel_models=None)
        extract = anti.main.__globals__["_extract_error_diagnostics"]
        result = extract(exc, args)
        self.assertEqual(result["requested_models"], ["sonnet", "flash"])
        self.assertEqual(result["prompt_chars"], 123)
        self.assertEqual(result["output_chars"], 0)
        self.assertEqual(len(result["failed_lanes"]), 1)
        self.assertNotIn("sk-abc123def456ghi789", result["failed_lanes"][0]["error"])


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


class ReflectionTests(unittest.TestCase):
    def setUp(self):
        self._original_dir = reflections.REFLECTIONS_DIR
        self._temp_dir = tempfile.TemporaryDirectory()
        reflections.REFLECTIONS_DIR = Path(self._temp_dir.name) / "reflections"

    def tearDown(self):
        reflections.REFLECTIONS_DIR = self._original_dir
        self._temp_dir.cleanup()

    def _record(self, repo: Path, fingerprint: str = "fp-1", severity: str = "high"):
        return reflections.record_review(
            repo_path=repo,
            findings=[{"id": "F001", "fingerprint": fingerprint, "severity": severity, "file": "src/app.py", "line": 7}],
            models=["sonnet"],
            panel_status="complete",
            mode="quick",
        )

    def test_record_review_creates_file_with_correct_permissions(self):
        if sys.platform == "win32":
            self.skipTest("POSIX permission bits not enforceable on Windows")
        repo = Path(self._temp_dir.name) / "repo-a"
        record = self._record(repo)

        path = reflections.REFLECTIONS_DIR / f"{reflections._repo_hash(repo)}.json"
        self.assertTrue(path.exists())
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        saved = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(saved, [record])

    def test_directory_created_with_700_permissions(self):
        if sys.platform == "win32":
            self.skipTest("POSIX permission bits not enforceable on Windows")
        repo = Path(self._temp_dir.name) / "repo-b"
        self.assertFalse(reflections.REFLECTIONS_DIR.exists())

        self._record(repo)

        self.assertEqual(reflections.REFLECTIONS_DIR.stat().st_mode & 0o777, 0o700)

    def test_get_summary_counts_findings_and_recurring(self):
        repo = Path(self._temp_dir.name) / "repo-c"
        findings_sets = [
            [
                {"fingerprint": "shared", "severity": "high", "file": "a.py"},
                {"fingerprint": "only-one", "severity": "medium", "file": "b.py"},
            ],
            [
                {"fingerprint": "shared", "severity": "high", "file": "nested/a.py"},
            ],
            [
                {"fingerprint": "final", "severity": "low", "file": "c.py"},
            ],
        ]
        for findings in findings_sets:
            reflections.record_review(
                repo_path=repo,
                findings=findings,
                models=["sonnet", "flash"],
                panel_status="complete",
                mode="deep",
            )

        summary = reflections.get_summary(repo)
        self.assertEqual(summary["records"], 3)
        self.assertEqual(summary["total_findings"], 4)
        self.assertEqual(summary["recurring_fingerprints"], 1)
        self.assertEqual(summary["top_recurring"], [("shared", 2)])
        self.assertEqual(summary["severity_distribution"], {"high": 2, "medium": 1, "low": 1})
        self.assertEqual(summary["models_used"], {"sonnet": 3, "flash": 3})

    def test_prune_old_respects_ttl(self):
        repo = Path(self._temp_dir.name) / "repo-d"
        path = reflections._reflection_path(repo)
        old_timestamp = int(time.time()) - ((reflections.TTL_DAYS + 1) * 86400)
        recent_timestamp = int(time.time()) - 3600
        reflections._save_records(
            path,
            [
                {"timestamp": old_timestamp, "findings_count": 1, "findings": [], "models": []},
                {"timestamp": recent_timestamp, "findings_count": 1, "findings": [], "models": []},
            ],
        )

        self._record(repo)

        records = reflections._load_records(path)
        self.assertEqual(len(records), 2)
        self.assertGreater(records[0]["timestamp"], old_timestamp)

    def test_clear_records_deletes_and_returns_count(self):
        repo = Path(self._temp_dir.name) / "repo-e"
        self._record(repo)
        self._record(repo, fingerprint="fp-2")

        deleted = reflections.clear_records(repo)

        self.assertEqual(deleted, 2)
        self.assertFalse(reflections._reflection_path(repo).exists())
        self.assertEqual(reflections.list_records(repo), [])

    def test_bounded_at_max_entries(self):
        original_limit = reflections.MAX_ENTRIES_PER_REPO
        reflections.MAX_ENTRIES_PER_REPO = 5
        try:
            repo = Path(self._temp_dir.name) / "repo-f"
            for index in range(10):
                self._record(repo, fingerprint=f"fp-{index}")

            path = reflections._reflection_path(repo)
            records = reflections._load_records(path)
            self.assertEqual(len(records), 5)
            self.assertEqual([r["findings"][0]["fingerprint"] for r in records], [f"fp-{i}" for i in range(5, 10)])
        finally:
            reflections.MAX_ENTRIES_PER_REPO = original_limit

    def test_existing_files_migrated_to_600_on_next_write(self):
        if sys.platform == "win32":
            self.skipTest("POSIX permission bits not enforceable on Windows")
        legacy_path = reflections.REFLECTIONS_DIR / "legacy.json"
        legacy_path.parent.mkdir(parents=True, exist_ok=True)
        legacy_path.write_text("[]", encoding="utf-8")
        legacy_path.chmod(0o644)

        self._record(Path(self._temp_dir.name) / "repo-g")

        self.assertEqual(legacy_path.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
