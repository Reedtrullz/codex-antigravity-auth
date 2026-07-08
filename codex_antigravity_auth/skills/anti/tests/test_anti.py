from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "anti.py"


def load_anti():
    spec = importlib.util.spec_from_file_location("anti_skill_helper", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class AntiHelperTests(unittest.TestCase):
    def test_path_exclusion_balances_secret_safety_with_code_paths(self) -> None:
        anti = load_anti()
        for path in [
            ".env",
            ".ssh/config",
            "secrets/config.json",
            "private/settings.toml",
            "docs/client_credentials.json",
            "config/oauth_token.json",
            "antigravity-providers.json.bak",
            "provider-keys.json",
            "accounts.json",
        ]:
            self.assertTrue(anti.path_is_excluded(path), path)

        for path in [
            "src/tokenizer.py",
            "src/token_utils.py",
            "src/tokenization/vocab.py",
            "tests/test_secret_santa.py",
            "docs/secret-management-design.md",
        ]:
            self.assertFalse(anti.path_is_excluded(path), path)

    def test_setup_google_does_not_forward_missing_base_url_as_none(self) -> None:
        anti = load_anti()
        captured: list[list[str]] = []
        anti.run_cli = lambda args: captured.append(args) or 0

        rc = anti.main(["setup-google", "--accounts", "1", "--skip-codex-config", "--skip-doctor"])

        self.assertEqual(rc, 0)
        self.assertNotIn("--base-url", captured[0])
        self.assertNotIn("None", captured[0])

    def test_start_uses_requested_port_for_default_probe_url(self) -> None:
        anti = load_anti()
        seen_urls: list[str] = []

        def fake_check_gateway(base_url: str, *, timeout: float, token_env: str) -> bool:
            seen_urls.append(base_url)
            return True

        anti.check_gateway = fake_check_gateway

        rc = anti.main(["start", "--port", "51234", "--timeout", "0.01"])

        self.assertEqual(rc, 0)
        self.assertEqual(seen_urls, ["http://127.0.0.1:51234/v1"])

    def test_generation_commands_default_to_longer_timeout(self) -> None:
        anti = load_anti()
        parser = anti.build_parser()

        self.assertEqual(parser.parse_args(["consult", "--prompt", "x"]).timeout, 120.0)
        self.assertEqual(parser.parse_args(["plan", "--prompt", "x"]).timeout, 120.0)
        self.assertEqual(parser.parse_args(["review", "--scope", "files", "--file", "SKILL.md"]).timeout, 120.0)
        self.assertEqual(parser.parse_args(["start"]).timeout, 2.0)

    def test_smoke_explicit_model_does_not_require_default_models(self) -> None:
        anti = load_anti()
        anti.find_cli = lambda: (["codex-antigravity"], None)
        anti.fetch_model_ids = lambda base_url, *, timeout, token_env: {"claude-3.5-sonnet"}
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            rc = anti.main(["smoke", "--skip-doctor", "--model", "sonnet"])

        self.assertEqual(rc, 0, output.getvalue())
        self.assertIn("claude-3.5-sonnet", output.getvalue())
        self.assertNotIn("claude-opus-4-6", output.getvalue())

    def test_smoke_sidecar_mode_does_not_fail_on_doctor_config_mismatch(self) -> None:
        anti = load_anti()
        anti.find_cli = lambda: (["codex-antigravity"], None)
        anti.fetch_model_ids = lambda base_url, *, timeout, token_env: {"claude-3.5-sonnet"}
        anti.run_cli = lambda args: self.fail("doctor should not run in sidecar mode")
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            rc = anti.main(["smoke", "--model", "sonnet"])

        self.assertEqual(rc, 0, output.getvalue())
        self.assertIn("doctor skipped in sidecar mode", output.getvalue())

    def test_smoke_full_mode_fails_when_doctor_fails(self) -> None:
        anti = load_anti()
        anti.find_cli = lambda: (["codex-antigravity"], None)
        anti.fetch_model_ids = lambda base_url, *, timeout, token_env: {"claude-3.5-sonnet"}
        anti.run_cli = lambda args: 1
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            rc = anti.main(["smoke", "--mode", "full", "--model", "sonnet"])

        self.assertEqual(rc, 1)
        self.assertIn("doctor reported hard failures", output.getvalue())

    def test_smoke_json_full_mode_suppresses_doctor_stdout(self) -> None:
        anti = load_anti()
        anti.find_cli = lambda: (["codex-antigravity"], None)
        anti.fetch_model_ids = lambda base_url, *, timeout, token_env: {"claude-3.5-sonnet"}
        anti.run_cli = lambda args: self.fail("json smoke should use quiet doctor")
        anti.run_cli_quiet = lambda args: 0
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            rc = anti.main(["smoke", "--mode", "full", "--model", "sonnet", "--json"])

        self.assertEqual(rc, 0)
        parsed = json.loads(output.getvalue())
        self.assertTrue(parsed["cli_available"])
        self.assertTrue(parsed["models_reachable"])
        self.assertTrue(parsed["codex_backend_ready"])

    def test_consult_truncates_large_prompt_with_caveat(self) -> None:
        anti = load_anti()
        captured: dict[str, str] = {}

        def fake_post_response(**kwargs):
            captured["prompt"] = kwargs["prompt"]
            return "ok"

        anti.post_response = fake_post_response
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            rc = anti.main(["consult", "--prompt", "abcdef", "--max-prompt-chars", "3"])

        self.assertEqual(rc, 0)
        self.assertEqual(captured["prompt"], "abc")
        self.assertIn("Prompt truncated to 3 characters", output.getvalue())

    def test_review_prompt_excludes_staged_secret_paths(self) -> None:
        anti = load_anti()
        with tempfile.TemporaryDirectory(prefix="anti-skill-test-") as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            (root / "src").mkdir()
            (root / "secrets").mkdir()
            (root / "src" / "app.py").write_text("print('ok')\n", encoding="utf-8")
            (root / "secrets" / "config.json").write_text('{"api_key":"do-not-send"}\n', encoding="utf-8")
            subprocess.run(["git", "add", "src/app.py", "secrets/config.json"], cwd=root, check=True)

            old_cwd = Path.cwd()
            try:
                os.chdir(root)
                args = anti.build_parser().parse_args(["review", "--scope", "staged", "--print-prompt"])
                prompt, paths, caveats, _metadata = anti.assemble_review_prompt(args)
            finally:
                os.chdir(old_cwd)

        self.assertIn("src/app.py", paths)
        self.assertNotIn("secrets/config.json", paths)
        self.assertNotIn("do-not-send", prompt)
        self.assertTrue(any("secrets/config.json" in caveat for caveat in caveats))

    def test_review_files_from_supports_nul_delimited_paths_with_spaces(self) -> None:
        anti = load_anti()
        with tempfile.TemporaryDirectory(prefix="anti-skill-test-") as tmp:
            root = Path(tmp)
            (root / "src").mkdir()
            (root / "src" / "with space.py").write_text("print('space')\n", encoding="utf-8")
            (root / "src" / "app.py").write_text("print('app')\n", encoding="utf-8")
            paths_file = root / "paths.txt"
            paths_file.write_bytes(b"src/with space.py\0src/app.py\0")

            old_cwd = Path.cwd()
            try:
                os.chdir(root)
                args = anti.build_parser().parse_args(
                    ["review", "--scope", "files", "--files-from", str(paths_file), "--print-prompt"]
                )
                prompt, paths, _caveats, metadata = anti.assemble_review_prompt(args)
            finally:
                os.chdir(old_cwd)

        self.assertEqual(paths, ["src/with space.py", "src/app.py"])
        self.assertIn("print('space')", prompt)
        self.assertIn("print('app')", prompt)
        self.assertEqual(metadata["status"], "complete")

    def test_review_files_from_rejects_invalid_utf8_path_lists(self) -> None:
        anti = load_anti()
        with tempfile.TemporaryDirectory(prefix="anti-skill-test-") as tmp:
            paths_file = Path(tmp) / "paths.zlist"
            paths_file.write_bytes(b"src/app.py\0src/bad-\xff.py\0")

            with self.assertRaises(anti.AntiError) as raised:
                anti.read_paths_file(str(paths_file))

        self.assertIn("not valid UTF-8", str(raised.exception))

    def test_review_files_from_rejects_secret_like_path_lists(self) -> None:
        anti = load_anti()
        with tempfile.TemporaryDirectory(prefix="anti-skill-test-") as tmp:
            paths_file = Path(tmp) / "paths.txt"
            paths_file.write_text('{"providers":{"deepseek":{"apiKey":"SYNTHETICSECRET1234567890"}}}\n', encoding="utf-8")

            with self.assertRaises(anti.AntiError) as raised:
                anti.read_paths_file(str(paths_file))

        self.assertIn("secret-like content", str(raised.exception))
        self.assertNotIn("SYNTHETICSECRET1234567890", str(raised.exception))

    def test_review_diff_scope_rejects_leading_dash_revision_ranges(self) -> None:
        anti = load_anti()
        parser = anti.build_parser()

        base_args = parser.parse_args(["review", "--scope", "diff", "--base=--output=/tmp/anti-bad"])
        changed_args = parser.parse_args(
            ["review", "--scope", "diff", "--changed-files=--output=/tmp/anti-bad"]
        )

        with self.assertRaises(anti.AntiError):
            anti.review_rev_range(base_args)
        with self.assertRaises(anti.AntiError):
            anti.review_rev_range(changed_args)

    def test_review_diff_scope_uses_base_on_clean_branch(self) -> None:
        anti = load_anti()
        with tempfile.TemporaryDirectory(prefix="anti-skill-test-") as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            (root / "src").mkdir()
            (root / "src" / "app.py").write_text("print('one')\n", encoding="utf-8")
            subprocess.run(["git", "add", "src/app.py"], cwd=root, check=True)
            subprocess.run(
                ["git", "-c", "user.email=a@example.com", "-c", "user.name=A", "commit", "-qm", "initial"],
                cwd=root,
                check=True,
            )
            (root / "src" / "app.py").write_text("print('two')\n", encoding="utf-8")
            subprocess.run(["git", "add", "src/app.py"], cwd=root, check=True)
            subprocess.run(
                ["git", "-c", "user.email=a@example.com", "-c", "user.name=A", "commit", "-qm", "change"],
                cwd=root,
                check=True,
            )

            old_cwd = Path.cwd()
            try:
                os.chdir(root)
                args = anti.build_parser().parse_args(
                    ["review", "--scope", "diff", "--base", "HEAD~1", "--print-prompt"]
                )
                prompt, paths, _caveats, metadata = anti.assemble_review_prompt(args)
            finally:
                os.chdir(old_cwd)

        self.assertEqual(paths, ["src/app.py"])
        self.assertIn("HEAD~1...HEAD", prompt)
        self.assertIn("-print('one')", prompt)
        self.assertIn("+print('two')", prompt)
        self.assertEqual(metadata["status"], "complete")

    def test_review_prompt_omits_whole_files_that_do_not_fit_budget(self) -> None:
        anti = load_anti()
        with tempfile.TemporaryDirectory(prefix="anti-skill-test-") as tmp:
            root = Path(tmp)
            (root / "small.py").write_text("print('small')\n", encoding="utf-8")
            (root / "large.py").write_text("LARGE_MARKER = '" + ("x" * 5000) + "'\n", encoding="utf-8")

            old_cwd = Path.cwd()
            try:
                os.chdir(root)
                args = anti.build_parser().parse_args(
                    [
                        "review",
                        "--scope",
                        "files",
                        "--file",
                        "small.py",
                        "--file",
                        "large.py",
                        "--max-prompt-chars",
                        "2400",
                        "--print-prompt",
                    ]
                )
                prompt, paths, _caveats, metadata = anti.assemble_review_prompt(args)
            finally:
                os.chdir(old_cwd)

        self.assertEqual(paths, ["small.py", "large.py"])
        self.assertIn("print('small')", prompt)
        self.assertNotIn("LARGE_MARKER", prompt)
        self.assertIn("large.py (omitted to keep whole-file prompt under 2400 chars)", prompt)
        self.assertEqual(metadata["status"], "incomplete")

    def test_read_text_file_truncates_large_utf8_files_with_caveat(self) -> None:
        anti = load_anti()
        original_max = anti.MAX_FILE_BYTES
        anti.MAX_FILE_BYTES = 24
        try:
            with tempfile.TemporaryDirectory(prefix="anti-skill-test-") as tmp:
                root = Path(tmp)
                (root / "large.py").write_text("VALUE = '" + ("x" * 200) + "'\n", encoding="utf-8")

                text, note = anti.read_text_file(root, "large.py")
        finally:
            anti.MAX_FILE_BYTES = original_max

        self.assertEqual(len(text.encode("utf-8")), 24)
        self.assertIn("VALUE", text)
        self.assertIsNotNone(note)
        self.assertIn("truncated to 24 bytes", note or "")

    def test_post_response_retries_transient_backend_errors(self) -> None:
        anti = load_anti()
        anti.fetch_model_ids = lambda base_url, *, timeout, token_env: {"claude-opus-4-6"}
        calls = {"count": 0}

        def fake_request_json(method, url, *, payload=None, timeout=10.0, token_env=anti.DEFAULT_TOKEN_ENV):
            calls["count"] += 1
            if calls["count"] == 1:
                return 502, {"detail": "rotation failed"}
            return 200, {"output": [{"content": [{"type": "output_text", "text": "ok"}]}]}

        anti.request_json = fake_request_json

        text = anti.post_response(
            base_url="http://127.0.0.1:51122/v1",
            model="claude-opus-4-6",
            prompt="hello",
            max_output_tokens=10,
            timeout=1,
            token_env=anti.DEFAULT_TOKEN_ENV,
            retries=1,
        )

        self.assertEqual(text, "ok")
        self.assertEqual(calls["count"], 2)

    def test_review_auto_chunking_runs_chunk_calls_and_synthesis(self) -> None:
        anti = load_anti()
        calls: list[str] = []

        def fake_post_response(**kwargs):
            calls.append(kwargs["prompt"])
            return f"result-{len(calls)}"

        anti.post_response = fake_post_response
        with tempfile.TemporaryDirectory(prefix="anti-skill-test-") as tmp:
            root = Path(tmp)
            (root / "small.py").write_text("print('small')\n", encoding="utf-8")
            (root / "large.py").write_text("LARGE_MARKER = '" + ("x" * 5000) + "'\n", encoding="utf-8")

            old_cwd = Path.cwd()
            output = io.StringIO()
            try:
                os.chdir(root)
                with contextlib.redirect_stdout(output):
                    rc = anti.main(
                        [
                            "review",
                            "--scope",
                            "files",
                            "--file",
                            "small.py",
                            "--file",
                            "large.py",
                            "--max-prompt-chars",
                            "2400",
                            "--chunked",
                            "auto",
                            "--max-review-chunks",
                            "6",
                            "--json",
                        ]
                    )
            finally:
                os.chdir(old_cwd)

        self.assertEqual(rc, 0, output.getvalue())
        self.assertGreaterEqual(len(calls), 2)
        self.assertIn("Chunked Review Manifest", calls[-1])
        result = json.loads(output.getvalue())
        self.assertTrue(result["metadata"]["chunked"])
        self.assertGreaterEqual(result["metadata"]["chunk_count"], 1)
        self.assertEqual(result["metadata"]["status"], "complete")
        self.assertEqual(result["metadata"]["omitted_files"], [])
        self.assertTrue(result["metadata"]["single_prompt_omitted_files"])

    def test_review_chunked_synthesis_prompt_is_bounded(self) -> None:
        anti = load_anti()
        calls: list[str] = []

        def fake_post_response(**kwargs):
            calls.append(kwargs["prompt"])
            if "Chunked Review Manifest" in kwargs["prompt"]:
                return "synthesis"
            return "chunk-finding\n" + ("x" * 4000)

        anti.post_response = fake_post_response
        with tempfile.TemporaryDirectory(prefix="anti-skill-test-") as tmp:
            root = Path(tmp)
            (root / "large.py").write_text("LARGE_MARKER = '" + ("x" * 6000) + "'\n", encoding="utf-8")

            old_cwd = Path.cwd()
            output = io.StringIO()
            try:
                os.chdir(root)
                with contextlib.redirect_stdout(output):
                    rc = anti.main(
                        [
                            "review",
                            "--scope",
                            "files",
                            "--file",
                            "large.py",
                            "--max-prompt-chars",
                            "2400",
                            "--chunked",
                            "auto",
                            "--max-review-chunks",
                            "8",
                            "--max-synthesis-chars",
                            "2500",
                            "--json",
                        ]
                    )
            finally:
                os.chdir(old_cwd)

        self.assertEqual(rc, 0, output.getvalue())
        self.assertLessEqual(len(calls[-1]), 2500)
        self.assertIn("chunk-finding", calls[-1])
        result = json.loads(output.getvalue())
        self.assertLessEqual(result["metadata"]["synthesis_prompt_chars"], 2500)
        self.assertTrue(result["metadata"]["synthesis_truncated_outputs"])
        self.assertTrue(any("Synthesis chunk outputs truncated" in caveat for caveat in result["caveats"]))

    def test_review_chunked_off_preserves_single_incomplete_call(self) -> None:
        anti = load_anti()
        calls: list[str] = []

        def fake_post_response(**kwargs):
            calls.append(kwargs["prompt"])
            return "single"

        anti.post_response = fake_post_response
        with tempfile.TemporaryDirectory(prefix="anti-skill-test-") as tmp:
            root = Path(tmp)
            (root / "small.py").write_text("print('small')\n", encoding="utf-8")
            (root / "large.py").write_text("LARGE_MARKER = '" + ("x" * 5000) + "'\n", encoding="utf-8")

            old_cwd = Path.cwd()
            output = io.StringIO()
            try:
                os.chdir(root)
                with contextlib.redirect_stdout(output):
                    rc = anti.main(
                        [
                            "review",
                            "--scope",
                            "files",
                            "--file",
                            "small.py",
                            "--file",
                            "large.py",
                            "--max-prompt-chars",
                            "2400",
                            "--chunked",
                            "off",
                            "--json",
                        ]
                    )
            finally:
                os.chdir(old_cwd)

        self.assertEqual(rc, 0, output.getvalue())
        self.assertEqual(len(calls), 1)
        result = json.loads(output.getvalue())
        self.assertFalse(result["metadata"]["chunked"])
        self.assertEqual(result["metadata"]["status"], "incomplete")

    def test_review_rejects_zero_chunk_count(self) -> None:
        anti = load_anti()
        output = io.StringIO()

        with contextlib.redirect_stderr(output):
            with self.assertRaises(SystemExit) as raised:
                anti.main(["review", "--scope", "files", "--file", "x.py", "--max-review-chunks", "0"])

        self.assertEqual(raised.exception.code, 2)
        self.assertIn("value must be at least 1", output.getvalue())

    def test_panel_parser_exposes_panel_moa_and_fusion_aliases(self) -> None:
        anti = load_anti()
        parser = anti.build_parser()

        for command in ["panel", "moa", "fusion"]:
            args = parser.parse_args([command, "--mode", "ask", "--prompt", "x"])
            self.assertEqual(args.func, anti.command_panel)

    def test_workflow_and_runs_commands_are_exposed(self) -> None:
        anti = load_anti()
        parser = anti.build_parser()

        workflow_args = parser.parse_args(["workflow", "review-ready", "--print-prompt"])
        runs_args = parser.parse_args(["runs", "list"])

        self.assertEqual(workflow_args.func, anti.command_workflow)
        self.assertEqual(runs_args.func, anti.command_runs)
        self.assertEqual(parser.parse_args(["workflow", "security-review", "--print-prompt"]).func, anti.command_workflow)
        self.assertEqual(
            parser.parse_args(["workflow", "debug-consensus", "--prompt", "bug", "--print-prompt"]).func,
            anti.command_workflow,
        )
        self.assertEqual(
            parser.parse_args(["workflow", "claude-grok", "--panel-mode", "ask", "--prompt", "bug", "--print-prompt"]).func,
            anti.command_workflow,
        )

    def test_workflow_presets_choose_expected_default_scopes(self) -> None:
        anti = load_anti()
        parser = anti.build_parser()

        review_args = parser.parse_args(["workflow", "review-ready", "--print-prompt"])
        review_expansion = anti.workflow_expansion(review_args)
        plan_args = parser.parse_args(["workflow", "plan-deep", "--prompt", "plan this", "--print-prompt"])
        plan_expansion = anti.workflow_expansion(plan_args)
        explicit_args = parser.parse_args(["workflow", "plan-deep", "--scope", "none", "--prompt", "plan this", "--print-prompt"])
        explicit_expansion = anti.workflow_expansion(explicit_args)

        self.assertEqual(review_expansion[review_expansion.index("--scope") + 1], "staged")
        self.assertEqual(plan_expansion[plan_expansion.index("--scope") + 1], "working-tree")
        self.assertEqual(explicit_expansion[explicit_expansion.index("--scope") + 1], "none")

    def test_workflow_review_ready_expands_to_role_panel_prompt(self) -> None:
        anti = load_anti()
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            rc = anti.main(["workflow", "review-ready", "--scope", "files", "--file", "SKILL.md", "--print-prompt", "--json"])

        self.assertEqual(rc, 0, output.getvalue())
        parsed = json.loads(output.getvalue())
        self.assertIn("Panel role lenses requested", parsed["prompt"])
        self.assertIn("correctness", parsed["metadata"]["roles"])
        self.assertIn("security", parsed["metadata"]["roles"])
        self.assertEqual(parsed["metadata"]["panel_mode"], "review")

    def test_workflow_ship_gate_review_prompt_is_included(self) -> None:
        anti = load_anti()
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            rc = anti.main(["workflow", "ship-gate", "--scope", "files", "--file", "README.md", "--print-prompt", "--json"])

        self.assertEqual(rc, 0, output.getvalue())
        parsed = json.loads(output.getvalue())
        self.assertIn("Assess merge readiness", parsed["prompt"])
        self.assertIn("Additional review instructions", parsed["prompt"])

    def test_workflow_progress_redacts_prompt_text(self) -> None:
        anti = load_anti()
        stdout = io.StringIO()
        stderr = io.StringIO()
        secret = "api_key=sk-testsecret1234567890"

        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            rc = anti.main(["workflow", "provider-compare", "--prompt", secret, "--progress", "--print-prompt", "--json"])

        self.assertEqual(rc, 0, stdout.getvalue() + stderr.getvalue())
        self.assertNotIn("sk-testsecret1234567890", stderr.getvalue())
        self.assertIn("<redacted>", stderr.getvalue())

    def test_workflow_plan_deep_rejects_review_only_options(self) -> None:
        anti = load_anti()
        parser = anti.build_parser()

        with self.assertRaisesRegex(anti.AntiError, "does not support --base"):
            anti.workflow_expansion(parser.parse_args(["workflow", "plan-deep", "--base", "HEAD", "--prompt", "plan"]))
        with self.assertRaisesRegex(anti.AntiError, "does not support --files-from"):
            anti.workflow_expansion(parser.parse_args(["workflow", "plan-deep", "--files-from", "paths.txt", "--prompt", "plan"]))
        with self.assertRaisesRegex(anti.AntiError, "does not support --scope diff"):
            anti.workflow_expansion(parser.parse_args(["workflow", "plan-deep", "--scope", "diff", "--prompt", "plan"]))
        with self.assertRaisesRegex(anti.AntiError, "does not support --changed-files"):
            anti.workflow_expansion(
                parser.parse_args(["workflow", "plan-deep", "--changed-files", "HEAD~2..HEAD", "--prompt", "plan"])
            )

    def test_workflow_omits_max_output_tokens_unless_set(self) -> None:
        anti = load_anti()
        parser = anti.build_parser()

        default_expansion = anti.workflow_expansion(
            parser.parse_args(["workflow", "plan-deep", "--prompt", "plan this"])
        )
        self.assertNotIn("--max-output-tokens", default_expansion)
        expanded_args = parser.parse_args(default_expansion)
        self.assertEqual(expanded_args.max_output_tokens, 6144)

        explicit_expansion = anti.workflow_expansion(
            parser.parse_args(["workflow", "plan-deep", "--max-output-tokens", "1234", "--prompt", "plan this"])
        )
        self.assertEqual(
            explicit_expansion[explicit_expansion.index("--max-output-tokens") + 1],
            "1234",
        )

    def test_workflow_ship_gate_forwards_changed_files_range(self) -> None:
        anti = load_anti()
        parser = anti.build_parser()

        expansion = anti.workflow_expansion(
            parser.parse_args(["workflow", "ship-gate", "--scope", "diff", "--changed-files", "HEAD~3..HEAD"])
        )
        self.assertEqual(expansion[expansion.index("--changed-files") + 1], "HEAD~3..HEAD")
        expanded_args = parser.parse_args(expansion)
        self.assertEqual(expanded_args.changed_files_range, "HEAD~3..HEAD")

    def test_workflow_security_review_expands_expected_roles_and_output(self) -> None:
        anti = load_anti()
        parser = anti.build_parser()

        expansion = anti.workflow_expansion(
            parser.parse_args(
                ["workflow", "security-review", "--scope", "files", "--file", "README.md", "--output", "findings"]
            )
        )

        self.assertEqual(expansion[:5], ["panel", "--mode", "review", "--scope", "files"])
        self.assertEqual(expansion[expansion.index("--output") + 1], "findings")
        for role in ["injection", "secrets-handling", "authz", "dependency-surface"]:
            self.assertIn(role, expansion)

    def test_workflow_debug_consensus_is_prompt_only(self) -> None:
        anti = load_anti()
        parser = anti.build_parser()

        expansion = anti.workflow_expansion(
            parser.parse_args(["workflow", "debug-consensus", "--prompt", "service times out"])
        )

        self.assertEqual(expansion[:3], ["panel", "--mode", "ask"])
        self.assertIn("ranked hypotheses", " ".join(expansion))
        with self.assertRaises(anti.AntiError):
            anti.workflow_expansion(
                parser.parse_args(
                    ["workflow", "debug-consensus", "--scope", "files", "--file", "README.md", "--prompt", "bug"]
                )
            )

    def test_workflow_claude_grok_expands_to_collaboration_panel(self) -> None:
        anti = load_anti()
        parser = anti.build_parser()

        expansion = anti.workflow_expansion(
            parser.parse_args(["workflow", "claude-grok", "--panel-mode", "ask", "--prompt", "compare"])
        )

        self.assertEqual(expansion[:3], ["panel", "--mode", "ask"])
        self.assertEqual(expansion[expansion.index("--collab") + 1], "claude-grok")
        for model in ["sonnet", "opus", "grok"]:
            self.assertIn(model, expansion)
        self.assertIn("Claude/Grok collaboration", " ".join(expansion))

    def test_failed_workflow_run_record_keeps_workflow_identity(self) -> None:
        anti = load_anti()
        with tempfile.TemporaryDirectory(prefix="anti-runs-") as tmp:
            anti.RUNS_DIR = Path(tmp)
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                rc = anti.main(["workflow", "review-ready", "--scope", "none", "--save-output", "summary"])

            records = list(Path(tmp).glob("*.json"))
            record = json.loads(records[0].read_text(encoding="utf-8")) if records else {}

        self.assertEqual(rc, 1)
        self.assertEqual(len(records), 1)
        self.assertEqual(record["workflow"], "review-ready")
        self.assertEqual(record["run_label"], "review-ready")
        self.assertEqual(record["status"], "error")

    def test_generation_fallback_uses_sonnet_on_retryable_error(self) -> None:
        anti = load_anti()
        calls: list[str] = []

        def fake_post_response(**kwargs):
            calls.append(kwargs["model"])
            if kwargs["model"] == "claude-opus-4-6":
                raise anti.AntiError("HTTP 502: backend failed retryable=true")
            return "fallback-ok"

        anti.post_response = fake_post_response
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            rc = anti.main(
                [
                    "consult",
                    "--model",
                    "opus",
                    "--fallback-model",
                    "sonnet",
                    "--fallback-policy",
                    "on-retryable",
                    "--prompt",
                    "hello",
                    "--json",
                ]
            )

        self.assertEqual(rc, 0, output.getvalue())
        self.assertEqual(calls, ["claude-opus-4-6", "claude-3.5-sonnet"])
        parsed = json.loads(output.getvalue())
        self.assertEqual(parsed["model"], "claude-3.5-sonnet")
        self.assertTrue(parsed["metadata"]["fallback_used"])

    def test_generation_fallback_uses_sonnet_on_non_json_http_502(self) -> None:
        anti = load_anti()
        args = anti.build_parser().parse_args(
            [
                "consult",
                "--model",
                "opus",
                "--fallback-model",
                "sonnet",
                "--fallback-policy",
                "on-retryable",
                "--prompt",
                "hello",
            ]
        )
        calls: list[str] = []

        def fake_request_json(method, url, *, payload=None, timeout=10.0, token_env=anti.DEFAULT_TOKEN_ENV):
            calls.append(payload["model"])
            if payload["model"] == "claude-opus-4-6":
                raise anti.AntiError("request to http://127.0.0.1:51122/v1/responses returned HTTP 502 non-JSON response")
            return 200, {"output_text": "fallback-ok"}

        anti.request_json = fake_request_json
        text, model_used, metadata = anti.generate_with_fallback(
            args,
            model="claude-opus-4-6",
            prompt="hello",
            max_output_tokens=16,
            purpose="consult",
            model_ids={"claude-opus-4-6", "claude-3.5-sonnet"},
        )

        self.assertEqual(text, "fallback-ok")
        self.assertEqual(model_used, "claude-3.5-sonnet")
        self.assertTrue(metadata["fallback_used"])
        self.assertEqual(calls, ["claude-opus-4-6", "claude-opus-4-6", "claude-3.5-sonnet"])

    def test_retryable_generation_failure_reports_wedged_gateway_probe(self) -> None:
        anti = load_anti()
        args = anti.build_parser().parse_args(["plan", "--model", "opus", "--prompt", "hello"])
        probes: list[float] = []

        def fake_post_response(**kwargs):
            raise anti.AntiError(
                "/v1/responses returned HTTP 502: backend failed after 1 attempt(s). "
                "Diagnostics: model=claude-opus-4-6, retryable=true"
            )

        def fake_fetch_model_ids(base_url: str, *, timeout: float, token_env: str):
            probes.append(timeout)
            raise anti.AntiError(f"request to {base_url}/models failed: timed out")

        anti.post_response = fake_post_response
        anti.fetch_model_ids = fake_fetch_model_ids

        with self.assertRaises(anti.AntiError) as raised:
            anti.generate_with_fallback(
                args,
                model="claude-opus-4-6",
                prompt="hello",
                max_output_tokens=16,
                purpose="plan",
            )

        message = str(raised.exception)
        self.assertIn("Gateway health check after this retryable failure also timed out", message)
        self.assertIn("gateway appears wedged; restart recommended", message)
        self.assertIn("--port 51122", message)
        self.assertEqual(probes, [8.0])

    def test_saved_generation_sends_run_id_metadata(self) -> None:
        anti = load_anti()
        args = anti.build_parser().parse_args(["consult", "--prompt", "hello", "--save-output", "summary"])
        args.run_id = "anti-run_123"
        payloads: list[dict] = []

        def fake_request_json(method, url, *, payload=None, timeout=10.0, token_env=anti.DEFAULT_TOKEN_ENV):
            payloads.append(payload or {})
            return 200, {"output_text": "ok", "usage": {"input_tokens": 1, "output_tokens": 2, "total_tokens": 3}}

        anti.request_json = fake_request_json
        text, model_used, metadata = anti.generate_with_fallback(
            args,
            model="claude-3.5-sonnet",
            prompt="hello",
            max_output_tokens=16,
            purpose="consult",
            model_ids={"claude-3.5-sonnet"},
        )

        self.assertEqual(text, "ok")
        self.assertEqual(model_used, "claude-3.5-sonnet")
        self.assertEqual(payloads[0]["metadata"], {"run_id": "anti-run_123"})
        self.assertEqual(metadata["usage"], {"input_tokens": 1, "output_tokens": 2, "total_tokens": 3})

    def test_base_url_rejects_userinfo_without_echoing_secret(self) -> None:
        anti = load_anti()
        stderr = io.StringIO()

        with contextlib.redirect_stderr(stderr):
            rc = anti.main(
                [
                    "consult",
                    "--base-url",
                    "https://user:SYNTHETICPASS1234567890@example.test/v1",
                    "--prompt",
                    "hello",
                ]
            )

        self.assertEqual(rc, 1)
        self.assertIn("must not contain username or password", stderr.getvalue())
        self.assertNotIn("SYNTHETICPASS1234567890", stderr.getvalue())

    def test_run_ledger_redacts_full_prompt_and_output(self) -> None:
        anti = load_anti()
        anti.post_response = lambda **kwargs: "output api_key=sk-testsecret1234567890"
        with tempfile.TemporaryDirectory(prefix="anti-runs-") as tmp:
            anti.RUNS_DIR = Path(tmp)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                rc = anti.main(
                    [
                        "consult",
                        "--prompt",
                        "please inspect api_key=sk-testsecret1234567890",
                        "--save-output",
                        "full",
                    ]
                )

            self.assertEqual(rc, 0, output.getvalue())
            records = list(Path(tmp).glob("*.json"))
            self.assertEqual(len(records), 1)
            stored = records[0].read_text(encoding="utf-8")
            self.assertNotIn("sk-testsecret1234567890", stored)
            self.assertIn("<redacted>", stored)
            if os.name != "nt":
                self.assertEqual(records[0].stat().st_mode & 0o777, 0o600)

    def test_run_ledger_redacts_quoted_secret_shapes(self) -> None:
        anti = load_anti()
        secret_json = '{"clientSecret":"CLIENTSECRET1234567890","refresh_token":"REFRESHSECRET1234567890"}'
        anti.post_response = lambda **kwargs: f"output {secret_json}"
        with tempfile.TemporaryDirectory(prefix="anti-runs-") as tmp:
            anti.RUNS_DIR = Path(tmp)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                rc = anti.main(["consult", "--prompt", secret_json, "--save-output", "full"])

            self.assertEqual(rc, 0, output.getvalue())
            records = list(Path(tmp).glob("*.json"))
            self.assertEqual(len(records), 1)
            stored = records[0].read_text(encoding="utf-8")
            self.assertNotIn("CLIENTSECRET1234567890", stored)
            self.assertNotIn("REFRESHSECRET1234567890", stored)
            self.assertIn("<redacted>", stored)

    def test_runs_list_show_and_clean_use_sanitized_records(self) -> None:
        anti = load_anti()
        with tempfile.TemporaryDirectory(prefix="anti-runs-") as tmp:
            anti.RUNS_DIR = Path(tmp)
            anti.RUNS_DIR.mkdir(exist_ok=True)
            record_path = anti.RUNS_DIR / "run-1.json"
            record_path.write_text(
                json.dumps({"id": "run-1", "created_at": "2026-07-05T00:00:00Z", "mode": "consult", "status": "success", "models": ["m"]}),
                encoding="utf-8",
            )
            list_output = io.StringIO()
            show_output = io.StringIO()
            clean_output = io.StringIO()

            with contextlib.redirect_stdout(list_output):
                list_rc = anti.main(["runs", "list", "--json"])
            with contextlib.redirect_stdout(show_output):
                show_rc = anti.main(["runs", "show", "run-1"])
            old = time.time() - 3 * 86400
            os.utime(record_path, (old, old))
            with contextlib.redirect_stdout(clean_output):
                clean_rc = anti.main(["runs", "clean", "--older-than", "1"])

        self.assertEqual(list_rc, 0)
        self.assertEqual(show_rc, 0)
        self.assertEqual(clean_rc, 0)
        self.assertEqual(json.loads(list_output.getvalue())[0]["id"], "run-1")
        self.assertEqual(json.loads(show_output.getvalue())["id"], "run-1")
        self.assertIn("Removed 1", clean_output.getvalue())

    def test_runs_clean_dry_run_keeps_records(self) -> None:
        anti = load_anti()
        with tempfile.TemporaryDirectory(prefix="anti-runs-") as tmp:
            anti.RUNS_DIR = Path(tmp)
            record_path = anti.RUNS_DIR / "run-1.json"
            record_path.write_text(json.dumps({"id": "run-1"}), encoding="utf-8")
            old = time.time() - 3 * 86400
            os.utime(record_path, (old, old))
            output = io.StringIO()

            with contextlib.redirect_stdout(output):
                rc = anti.main(["runs", "clean", "--older-than", "1", "--dry-run"])

            self.assertEqual(rc, 0)
            self.assertTrue(record_path.exists())
            self.assertIn("Would remove 1", output.getvalue())

    def test_runs_list_skips_symlinked_record_files(self) -> None:
        anti = load_anti()
        with tempfile.TemporaryDirectory(prefix="anti-runs-") as tmp, tempfile.TemporaryDirectory(
            prefix="anti-runs-outside-"
        ) as outside_tmp:
            anti.RUNS_DIR = Path(tmp)
            (anti.RUNS_DIR / "run-1.json").write_text(
                json.dumps({"id": "run-1", "created_at": "2026-07-05T00:00:00Z", "mode": "consult", "status": "success"}),
                encoding="utf-8",
            )
            outside_record = Path(outside_tmp) / "outside.json"
            outside_record.write_text(
                json.dumps({"id": "outside", "output_text": "SYNTHETIC_SECRET_VALUE_1234567890"}),
                encoding="utf-8",
            )
            try:
                (anti.RUNS_DIR / "run-2.json").symlink_to(outside_record)
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"symlink unavailable: {exc}")
            stdout = io.StringIO()
            stderr = io.StringIO()

            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                rc = anti.main(["runs", "list", "--json"])

            self.assertEqual(rc, 0)
            rows = json.loads(stdout.getvalue())
            self.assertEqual([row["id"] for row in rows], ["run-1"])
            self.assertNotIn("SYNTHETIC_SECRET_VALUE_1234567890", stdout.getvalue())
            self.assertIn("skipping non-regular run record", stderr.getvalue())

    def test_write_run_record_rejects_dangling_symlink_runs_dir(self) -> None:
        anti = load_anti()
        with tempfile.TemporaryDirectory(prefix="anti-runs-link-") as link_tmp:
            symlink_path = Path(link_tmp) / "anti-runs"
            try:
                symlink_path.symlink_to(Path(link_tmp) / "missing-target")
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"symlink unavailable: {exc}")
            anti.RUNS_DIR = symlink_path
            args = anti.build_parser().parse_args(["consult", "--prompt", "x", "--save-output", "summary"])

            with self.assertRaisesRegex(anti.AntiError, "symlinked directory"):
                anti.write_run_record(
                    args,
                    mode="consult",
                    status="success",
                    models=["m"],
                    base_url="http://127.0.0.1:51122/v1",
                    output_text="ok",
                )

    def test_sanitize_json_redacts_numeric_secret_values_but_keeps_http_code(self) -> None:
        anti = load_anti()
        sanitized = anti.sanitize_json(
            {
                "code": 429,
                "oauth_code": 123456,
                "key": True,
                "api_key": 123456,
                "client_secret": 987654,
                "access": 1.5,
                "token": "SECRETTOKENVALUE1234567890",
                "detail": {"code": "SECRETOAUTHCODE1234567890"},
                "prompt_text": '{"token":123456,"code":789012,"api_key":345678}',
                "error": "{'client_secret': 987654}",
            }
        )

        self.assertEqual(sanitized["code"], 429)
        self.assertEqual(sanitized["oauth_code"], "<redacted>")
        self.assertEqual(sanitized["key"], True)
        self.assertEqual(sanitized["api_key"], "<redacted>")
        self.assertEqual(sanitized["client_secret"], "<redacted>")
        self.assertEqual(sanitized["access"], "<redacted>")
        self.assertEqual(sanitized["token"], "<redacted>")
        self.assertEqual(sanitized["detail"]["code"], "<redacted>")
        self.assertNotIn("123456", sanitized["prompt_text"])
        self.assertNotIn("789012", sanitized["prompt_text"])
        self.assertNotIn("345678", sanitized["prompt_text"])
        self.assertNotIn("987654", sanitized["error"])

    def test_runs_show_rejects_path_like_ids(self) -> None:
        anti = load_anti()
        with tempfile.TemporaryDirectory(prefix="anti-runs-") as tmp:
            anti.RUNS_DIR = Path(tmp)
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                rc = anti.main(["runs", "show", "../antigravity-credentials"])

        self.assertEqual(rc, 1)
        self.assertIn("run id must contain only", stderr.getvalue())

    def test_runs_show_rejects_symlinked_runs_dir_without_leaking_record(self) -> None:
        anti = load_anti()
        with tempfile.TemporaryDirectory(prefix="anti-runs-target-") as target_tmp, tempfile.TemporaryDirectory(
            prefix="anti-runs-link-"
        ) as link_tmp:
            target = Path(target_tmp)
            symlink_path = Path(link_tmp) / "anti-runs"
            try:
                symlink_path.symlink_to(target, target_is_directory=True)
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"symlink unavailable: {exc}")

            (target / "synthetic-run.json").write_text(
                json.dumps({"id": "synthetic-run", "output_text": "SYNTHETIC_SECRET_VALUE_1234567890"}),
                encoding="utf-8",
            )
            anti.RUNS_DIR = symlink_path
            stdout = io.StringIO()
            stderr = io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                rc = anti.main(["runs", "show", "synthetic-run"])

        rendered = stdout.getvalue() + stderr.getvalue()
        self.assertEqual(rc, 1)
        self.assertIn("symlinked", rendered)
        self.assertNotIn("SYNTHETIC_SECRET_VALUE_1234567890", rendered)

    def test_plan_ledger_records_limited_prompt_for_non_chunked_calls(self) -> None:
        anti = load_anti()
        anti.post_response = lambda **kwargs: "plan-ok"
        with tempfile.TemporaryDirectory(prefix="anti-runs-") as tmp:
            anti.RUNS_DIR = Path(tmp)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                rc = anti.main(
                    [
                        "plan",
                        "--scope",
                        "none",
                        "--prompt",
                        "x" * 5000,
                        "--max-prompt-chars",
                        "1200",
                        "--chunked",
                        "off",
                        "--save-output",
                        "full",
                        "--json",
                    ]
                )

            self.assertEqual(rc, 0, output.getvalue())
            record = json.loads(next(Path(tmp).glob("*.json")).read_text(encoding="utf-8"))
            self.assertEqual(len(record["prompt_text"]), 1200)
            self.assertEqual(record["metadata"]["prompt_chars"], 1200)

    def test_large_plan_prompt_is_split_before_generation(self) -> None:
        anti = load_anti()
        calls: list[str] = []

        def fake_post_response(**kwargs):
            calls.append(kwargs["prompt"])
            if "synthesizing a decision-complete autonomous work plan" in kwargs["prompt"]:
                return "plan-synthesis"
            return "chunk-note"

        anti.post_response = fake_post_response
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            rc = anti.main(
                [
                    "plan",
                    "--prompt",
                    "x" * 5000,
                    "--max-prompt-chars",
                    "1800",
                    "--max-plan-chunks",
                    "5",
                    "--json",
                ]
            )

        self.assertEqual(rc, 0, output.getvalue())
        self.assertGreater(len(calls), 1)
        parsed = json.loads(output.getvalue())
        self.assertTrue(parsed["metadata"]["chunked"])
        self.assertEqual(parsed["output_text"], "plan-synthesis")

    def test_chunked_plan_prompt_chunks_respect_max_prompt_chars(self) -> None:
        anti = load_anti()
        chunk_prompts: list[str] = []

        def fake_post_response(**kwargs):
            prompt = kwargs["prompt"]
            if "You are reviewing one bounded chunk" in prompt:
                chunk_prompts.append(prompt)
                return "chunk-note"
            return "plan-synthesis"

        anti.post_response = fake_post_response
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            rc = anti.main(
                [
                    "plan",
                    "--scope",
                    "none",
                    "--prompt",
                    "x" * 3000,
                    "--max-prompt-chars",
                    "1000",
                    "--max-plan-chunks",
                    "8",
                    "--json",
                ]
            )

        self.assertEqual(rc, 0, output.getvalue())
        self.assertTrue(chunk_prompts)
        self.assertTrue(all(len(prompt) <= 1000 for prompt in chunk_prompts), [len(prompt) for prompt in chunk_prompts])
        parsed = json.loads(output.getvalue())
        self.assertTrue(all(length <= 1000 for length in parsed["metadata"]["sent_chunk_prompt_chars"]))

    def test_default_panel_models_resolve_to_sonnet_and_opus(self) -> None:
        anti = load_anti()
        parser = anti.build_parser()
        args = parser.parse_args(["panel", "--mode", "ask", "--prompt", "x"])

        self.assertEqual(anti.resolve_panel_models(args.model), ["claude-3.5-sonnet", "claude-opus-4-6"])
        self.assertEqual(anti.resolve_model(args.judge, default=anti.DEFAULT_PANEL_JUDGE_MODEL), "claude-opus-4-6")

    def test_grok_aliases_resolve_to_xai_oauth_models(self) -> None:
        anti = load_anti()

        self.assertEqual(anti.resolve_model("grok", default="sonnet"), "xai-oauth:grok-build-0.1")
        self.assertEqual(anti.resolve_model("supergrok", default="sonnet"), "xai-oauth:grok-build-0.1")
        self.assertEqual(anti.resolve_model("grok-4.3", default="sonnet"), "xai-oauth:grok-4.3")

    def test_claude_grok_collab_defaults_models_and_prompt_contract(self) -> None:
        anti = load_anti()
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            rc = anti.main(["panel", "--mode", "ask", "--collab", "claude-grok", "--prompt", "Compare options", "--print-prompt", "--json"])

        self.assertEqual(rc, 0, output.getvalue())
        parsed = json.loads(output.getvalue())
        self.assertEqual(parsed["metadata"]["collaboration_profile"], "claude-grok")
        self.assertEqual(
            parsed["metadata"]["panel_models"],
            ["claude-3.5-sonnet", "claude-opus-4-6", "xai-oauth:grok-build-0.1"],
        )
        self.assertIn("Claude + Grok collaboration", parsed["prompt"])
        self.assertIn("Claude-family lanes", parsed["prompt"])
        self.assertIn("Grok/xAI lanes", parsed["prompt"])

    def test_claude_grok_judge_prompt_requires_cross_lane_synthesis(self) -> None:
        anti = load_anti()
        anti.fetch_model_ids = lambda base_url, *, timeout, token_env: {
            "claude-3.5-sonnet",
            "claude-opus-4-6",
            "xai-oauth:grok-build-0.1",
        }
        judge_prompts: list[str] = []

        def fake_post_response(**kwargs):
            prompt = kwargs["prompt"]
            if "You are synthesizing an Antigravity multi-model advisory panel" in prompt:
                judge_prompts.append(prompt)
                return json.dumps(
                    {
                        "summary": "summary",
                        "disagreements": ["Claude and Grok differ"],
                        "findings": [],
                        "unverifiable": [],
                        "recommended_next_actions": [],
                        "caveats": [],
                    }
                )
            return f"lane-output from {kwargs['model']}"

        anti.post_response = fake_post_response
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            rc = anti.main(["panel", "--mode", "ask", "--collab", "claude-grok", "--prompt", "Compare options", "--json"])

        self.assertEqual(rc, 0, output.getvalue())
        self.assertTrue(judge_prompts)
        self.assertIn("Compare Claude-backed lanes with Grok-backed lanes", judge_prompts[0])
        parsed = json.loads(output.getvalue())
        self.assertEqual(parsed["metadata"]["collaboration_profile"], "claude-grok")
        self.assertIn("xai-oauth:grok-build-0.1", parsed["panel_models"])

    def test_panel_review_prompt_reuses_secret_exclusion(self) -> None:
        anti = load_anti()
        with tempfile.TemporaryDirectory(prefix="anti-skill-test-") as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            (root / "src").mkdir()
            (root / "secrets").mkdir()
            (root / "src" / "app.py").write_text("print('ok')\n", encoding="utf-8")
            (root / "secrets" / "config.json").write_text('{"api_key":"do-not-send"}\n', encoding="utf-8")
            subprocess.run(["git", "add", "src/app.py", "secrets/config.json"], cwd=root, check=True)

            old_cwd = Path.cwd()
            output = io.StringIO()
            try:
                os.chdir(root)
                with contextlib.redirect_stdout(output):
                    rc = anti.main(["panel", "--mode", "review", "--scope", "staged", "--print-prompt", "--json"])
            finally:
                os.chdir(old_cwd)

        self.assertEqual(rc, 0)
        parsed = json.loads(output.getvalue())
        self.assertIn("src/app.py", parsed["prompt"])
        self.assertIn("secrets/config.json", parsed["metadata"]["excluded_paths"])
        self.assertNotIn("do-not-send", parsed["prompt"])

    def test_panel_plan_and_ask_modes_assemble_prompts(self) -> None:
        anti = load_anti()
        plan_output = io.StringIO()
        ask_output = io.StringIO()

        with contextlib.redirect_stdout(plan_output):
            plan_rc = anti.main(["panel", "--mode", "plan", "--scope", "none", "--prompt", "Plan the work", "--print-prompt", "--json"])
        with contextlib.redirect_stdout(ask_output):
            ask_rc = anti.main(["panel", "--mode", "ask", "--prompt", "Compare options", "--print-prompt", "--json"])

        self.assertEqual(plan_rc, 0)
        self.assertEqual(ask_rc, 0)
        self.assertIn("decision-complete plan", json.loads(plan_output.getvalue())["prompt"])
        ask_prompt = json.loads(ask_output.getvalue())["prompt"]
        self.assertIn("GPT-complement lens", ask_prompt)
        self.assertTrue(ask_prompt.endswith("Compare options"))

    def test_panel_print_prompt_does_not_allocate_run_correlation_id(self) -> None:
        anti = load_anti()
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            rc = anti.main(
                [
                    "panel",
                    "--mode",
                    "ask",
                    "--prompt",
                    "preview only",
                    "--save-output",
                    "summary",
                    "--print-prompt",
                    "--json",
                ]
            )

        self.assertEqual(rc, 0, output.getvalue())
        parsed = json.loads(output.getvalue())
        self.assertNotIn("run_id", parsed["metadata"])
        self.assertNotIn("request_log_correlation_id", parsed["metadata"])

    def test_panel_role_prompt_respects_max_prompt_chars(self) -> None:
        anti = load_anti()
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            rc = anti.main(
                [
                    "panel",
                    "--mode",
                    "ask",
                    "--prompt",
                    "A" * 1000,
                    "--role",
                    "security",
                    "--max-prompt-chars",
                    "1000",
                    "--print-prompt",
                    "--json",
                ]
            )

        self.assertEqual(rc, 0, output.getvalue())
        parsed = json.loads(output.getvalue())
        self.assertLessEqual(len(parsed["prompt"]), 1000)
        self.assertTrue(any("Prompt truncated" in caveat for caveat in parsed["caveats"]))

    def test_panel_byok_disclosure_only_for_repo_context(self) -> None:
        anti = load_anti()
        repo_output = io.StringIO()
        ask_output = io.StringIO()

        with contextlib.redirect_stdout(repo_output):
            repo_rc = anti.main(
                [
                    "panel",
                    "--mode",
                    "review",
                    "--scope",
                    "files",
                    "--file",
                    "README.md",
                    "--model",
                    "openrouter:deepseek/deepseek-chat",
                    "--judge",
                    "sonnet",
                    "--print-prompt",
                    "--json",
                ]
            )
        with contextlib.redirect_stdout(ask_output):
            ask_rc = anti.main(
                [
                    "panel",
                    "--mode",
                    "ask",
                    "--prompt",
                    "compare",
                    "--model",
                    "openrouter:deepseek/deepseek-chat",
                    "--judge",
                    "sonnet",
                    "--print-prompt",
                    "--json",
                ]
            )

        self.assertEqual(repo_rc, 0, repo_output.getvalue())
        self.assertEqual(ask_rc, 0, ask_output.getvalue())
        self.assertTrue(any("BYOK disclosure" in caveat for caveat in json.loads(repo_output.getvalue())["caveats"]))
        self.assertFalse(any("BYOK disclosure" in caveat for caveat in json.loads(ask_output.getvalue())["caveats"]))

    def test_panel_successful_two_model_run_calls_judge_once(self) -> None:
        anti = load_anti()
        anti.fetch_model_ids = lambda base_url, *, timeout, token_env: {"claude-3.5-sonnet", "claude-opus-4-6"}
        judge_prompts: list[str] = []

        def fake_post_response(**kwargs):
            if "You are synthesizing an Antigravity multi-model advisory panel" in kwargs["prompt"]:
                judge_prompts.append(kwargs["prompt"])
                return "judge-output"
            return f"panel-output-{kwargs['model']}"

        anti.post_response = fake_post_response
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            rc = anti.main(["panel", "--mode", "ask", "--prompt", "What next?", "--json"])

        self.assertEqual(rc, 0, output.getvalue())
        self.assertEqual(len(judge_prompts), 1)
        self.assertIn("panel-output-claude-3.5-sonnet", judge_prompts[0])
        self.assertIn("panel-output-claude-opus-4-6", judge_prompts[0])
        parsed = json.loads(output.getvalue())
        self.assertEqual(parsed["output_text"], "judge-output")
        self.assertEqual([item["status"] for item in parsed["panel_results"]], ["success", "success"])

    def test_panel_usage_latency_and_findings_are_reported(self) -> None:
        anti = load_anti()
        anti.fetch_model_ids = lambda base_url, *, timeout, token_env: {"claude-3.5-sonnet", "claude-opus-4-6"}
        calls: list[dict] = []
        finding_payload = {
            "summary": "Disagreements first.",
            "disagreements": ["Sonnet worries about tests; Opus worries about authz."],
            "findings": [
                {
                    "id": "F1",
                    "claim": "A branch needs local verification.",
                    "severity": "medium",
                    "lanes": ["claude-3.5-sonnet", "claude-opus-4-6"],
                    "verify": "Run python3 -m pytest -q.",
                }
            ],
            "unverifiable": ["External provider behavior may drift."],
            "recommended_next_actions": ["Verify before editing."],
            "caveats": ["Panel consensus is advisory."],
        }

        def fake_post_response(**kwargs):
            calls.append(kwargs)
            if "You are synthesizing an Antigravity multi-model advisory panel" in kwargs["prompt"]:
                return anti.ResponseText(
                    json.dumps(finding_payload),
                    usage={"input_tokens": 5, "output_tokens": 7, "total_tokens": 12},
                    elapsed_ms=30,
                )
            return anti.ResponseText(
                f"panel-output-{kwargs['model']}",
                usage={"input_tokens": 1, "output_tokens": 2, "total_tokens": 3},
                elapsed_ms=10,
            )

        anti.post_response = fake_post_response
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            rc = anti.main(["panel", "--mode", "ask", "--prompt", "What next?", "--json"])

        self.assertEqual(rc, 0, output.getvalue())
        parsed = json.loads(output.getvalue())
        self.assertEqual(parsed["metadata"]["findings_status"], "parsed")
        self.assertEqual(parsed["findings"]["findings"][0]["verify"], "Run python3 -m pytest -q.")
        self.assertEqual(parsed["metadata"]["usage_totals"], {"input_tokens": 7, "output_tokens": 11, "total_tokens": 18})
        self.assertEqual(parsed["panel_results"][0]["elapsed_ms"], 10)
        self.assertEqual(parsed["metadata"]["judge_generation"]["elapsed_ms"], 30)
        self.assertIn("## Findings", parsed["output_text"])
        self.assertTrue(all("metadata" not in call or call["metadata"] == {} for call in calls))

    def test_panel_output_findings_emits_sanitized_json_contract(self) -> None:
        anti = load_anti()
        anti.fetch_model_ids = lambda base_url, *, timeout, token_env: {"claude-3.5-sonnet", "claude-opus-4-6"}
        secret = "sk-testsecret1234567890"

        def fake_post_response(**kwargs):
            if "You are synthesizing an Antigravity multi-model advisory panel" in kwargs["prompt"]:
                return json.dumps(
                    {
                        "summary": f"token {secret}",
                        "disagreements": [],
                        "findings": [
                            {
                                "id": "secret finding",
                                "claim": f"claim with {secret}",
                                "severity": "high",
                                "lanes": [kwargs["model"]],
                                "verify": f"verify {secret}",
                            }
                        ],
                        "unverifiable": [],
                        "recommended_next_actions": [],
                        "caveats": [],
                    }
                )
            return "panel-output"

        anti.post_response = fake_post_response
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            rc = anti.main(["panel", "--mode", "ask", "--prompt", "x", "--output", "findings"])

        self.assertEqual(rc, 0, output.getvalue())
        parsed = json.loads(output.getvalue())
        rendered = json.dumps(parsed)
        self.assertIn("<redacted>", rendered)
        self.assertNotIn(secret, rendered)
        self.assertEqual(parsed["findings"][0]["severity"], "high")

    def test_panel_malformed_findings_falls_back_to_markdown_with_caveat(self) -> None:
        anti = load_anti()
        anti.fetch_model_ids = lambda base_url, *, timeout, token_env: {"claude-3.5-sonnet", "claude-opus-4-6"}
        anti.post_response = lambda **kwargs: "judge-output" if "You are synthesizing" in kwargs["prompt"] else "lane"
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            rc = anti.main(["panel", "--mode", "ask", "--prompt", "x", "--json"])

        self.assertEqual(rc, 0, output.getvalue())
        parsed = json.loads(output.getvalue())
        self.assertEqual(parsed["metadata"]["findings_status"], "fallback")
        self.assertEqual(parsed["output_text"], "judge-output")
        self.assertTrue(any("structured findings" in caveat for caveat in parsed["caveats"]))

    def test_panel_errors_are_redacted_in_json_output(self) -> None:
        anti = load_anti()
        anti.fetch_model_ids = lambda base_url, *, timeout, token_env: {"claude-3.5-sonnet", "claude-opus-4-6"}

        def fake_post_response(**kwargs):
            if kwargs["model"] == "claude-3.5-sonnet":
                raise anti.AntiError('HTTP 502: {"client_secret":"CLIENTSECRET1234567890"}')
            if "You are synthesizing an Antigravity multi-model advisory panel" in kwargs["prompt"]:
                return "judge-output"
            return "opus-panel-output"

        anti.post_response = fake_post_response
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            rc = anti.main(["panel", "--mode", "ask", "--prompt", "What next?", "--min-successes", "1", "--json"])

        self.assertEqual(rc, 0, output.getvalue())
        parsed = json.loads(output.getvalue())
        rendered = json.dumps(parsed)
        self.assertNotIn("CLIENTSECRET1234567890", rendered)
        self.assertIn("<redacted>", rendered)

    def test_panel_model_lane_uses_configured_fallback(self) -> None:
        anti = load_anti()
        anti.fetch_model_ids = lambda base_url, *, timeout, token_env: {"claude-3.5-sonnet", "claude-opus-4-6"}
        calls: list[str] = []

        def fake_post_response(**kwargs):
            calls.append(kwargs["model"])
            if kwargs["model"] == "claude-opus-4-6" and "You are synthesizing" not in kwargs["prompt"]:
                raise anti.AntiError("HTTP 502: backend failed retryable=true")
            if "You are synthesizing an Antigravity multi-model advisory panel" in kwargs["prompt"]:
                return "judge-output"
            return "fallback-panel-output"

        anti.post_response = fake_post_response
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            rc = anti.main(
                [
                    "panel",
                    "--mode",
                    "ask",
                    "--model",
                    "opus",
                    "--judge",
                    "sonnet",
                    "--prompt",
                    "What next?",
                    "--fallback-model",
                    "sonnet",
                    "--fallback-policy",
                    "on-retryable",
                    "--json",
                ]
            )

        self.assertEqual(rc, 0, output.getvalue())
        self.assertEqual(calls[:2], ["claude-opus-4-6", "claude-3.5-sonnet"])
        parsed = json.loads(output.getvalue())
        self.assertEqual(parsed["panel_results"][0]["model_used"], "claude-3.5-sonnet")
        self.assertTrue(parsed["panel_results"][0]["generation"]["fallback_used"])

    def test_panel_model_failure_is_metadata_when_min_successes_met(self) -> None:
        anti = load_anti()
        anti.fetch_model_ids = lambda base_url, *, timeout, token_env: {"claude-3.5-sonnet", "claude-opus-4-6"}

        def fake_post_response(**kwargs):
            if kwargs["model"] == "claude-3.5-sonnet":
                raise anti.AntiError("temporary backend failure")
            if "You are synthesizing an Antigravity multi-model advisory panel" in kwargs["prompt"]:
                return "judge-output"
            return "opus-panel-output"

        anti.post_response = fake_post_response
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            rc = anti.main(["panel", "--mode", "ask", "--prompt", "What next?", "--min-successes", "1", "--json"])

        self.assertEqual(rc, 0, output.getvalue())
        parsed = json.loads(output.getvalue())
        self.assertEqual(parsed["panel_results"][0]["status"], "error")
        self.assertEqual(parsed["panel_results"][1]["status"], "success")
        self.assertTrue(any("temporary backend failure" in caveat for caveat in parsed["caveats"]))

    def test_panel_fails_when_successes_below_minimum(self) -> None:
        anti = load_anti()
        anti.fetch_model_ids = lambda base_url, *, timeout, token_env: {"claude-3.5-sonnet", "claude-opus-4-6"}

        def fake_post_response(**kwargs):
            if kwargs["model"] == "claude-3.5-sonnet":
                raise anti.AntiError("temporary backend failure")
            return "opus-panel-output"

        anti.post_response = fake_post_response
        output = io.StringIO()

        with contextlib.redirect_stderr(output):
            rc = anti.main(["panel", "--mode", "ask", "--prompt", "What next?"])

        self.assertEqual(rc, 1)
        self.assertIn("below --min-successes 2", output.getvalue())

    def test_panel_missing_model_fails_before_generation(self) -> None:
        anti = load_anti()
        anti.fetch_model_ids = lambda base_url, *, timeout, token_env: {"claude-3.5-sonnet"}
        anti.post_response = lambda **kwargs: self.fail("panel should validate models before generation")
        output = io.StringIO()

        with contextlib.redirect_stderr(output):
            rc = anti.main(["panel", "--mode", "ask", "--prompt", "x", "--model", "opus", "--judge", "sonnet"])

        self.assertEqual(rc, 1)
        self.assertIn("not advertised", output.getvalue())

    def test_panel_missing_model_becomes_failed_entry_when_min_successes_met(self) -> None:
        anti = load_anti()
        anti.fetch_model_ids = lambda base_url, *, timeout, token_env: {"claude-3.5-sonnet"}

        def fake_post_response(**kwargs):
            self.assertEqual(kwargs["model"], "claude-3.5-sonnet")
            if "You are synthesizing an Antigravity multi-model advisory panel" in kwargs["prompt"]:
                return "judge-output"
            return "sonnet-panel-output"

        anti.post_response = fake_post_response
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            rc = anti.main(
                [
                    "panel",
                    "--mode",
                    "ask",
                    "--prompt",
                    "What next?",
                    "--model",
                    "sonnet",
                    "--model",
                    "opus",
                    "--judge",
                    "sonnet",
                    "--min-successes",
                    "1",
                    "--json",
                ]
            )

        self.assertEqual(rc, 0, output.getvalue())
        parsed = json.loads(output.getvalue())
        self.assertEqual(parsed["panel_results"][0]["status"], "success")
        self.assertEqual(parsed["panel_results"][1]["status"], "error")
        self.assertIn("not advertised", parsed["panel_results"][1]["error"])
        self.assertEqual(parsed["output_text"], "judge-output")

    def test_panel_missing_judge_model_still_fails_before_generation(self) -> None:
        anti = load_anti()
        anti.fetch_model_ids = lambda base_url, *, timeout, token_env: {"claude-3.5-sonnet"}
        anti.post_response = lambda **kwargs: self.fail("panel should validate the judge before generation")
        output = io.StringIO()

        with contextlib.redirect_stderr(output):
            rc = anti.main(["panel", "--mode", "ask", "--prompt", "x", "--model", "sonnet", "--judge", "opus"])

        self.assertEqual(rc, 1)
        self.assertIn("not advertised", output.getvalue())

    def test_panel_below_min_successes_writes_single_failed_record_with_partial_results(self) -> None:
        anti = load_anti()
        anti.fetch_model_ids = lambda base_url, *, timeout, token_env: {"claude-3.5-sonnet", "claude-opus-4-6"}

        def fake_post_response(**kwargs):
            if kwargs["model"] == "claude-3.5-sonnet":
                raise anti.AntiError("temporary backend failure")
            return "opus-panel-output"

        anti.post_response = fake_post_response
        with tempfile.TemporaryDirectory(prefix="anti-runs-") as tmp:
            anti.RUNS_DIR = Path(tmp)
            stderr = io.StringIO()

            with contextlib.redirect_stderr(stderr):
                rc = anti.main(["panel", "--mode", "ask", "--prompt", "What next?", "--save-output", "summary"])

            self.assertEqual(rc, 1)
            records = list(Path(tmp).glob("*.json"))
            self.assertEqual(len(records), 1)
            record = json.loads(records[0].read_text(encoding="utf-8"))
            self.assertEqual(record["status"], "failed")
            self.assertIn("below --min-successes", record["error"])
            panel_results = record["metadata"]["panel_results"]
            self.assertEqual(len(panel_results), 2)
            statuses = {item["model"]: item["status"] for item in panel_results}
            self.assertEqual(statuses["claude-3.5-sonnet"], "error")
            self.assertEqual(statuses["claude-opus-4-6"], "success")
            success_entry = next(item for item in panel_results if item["status"] == "success")
            self.assertNotIn("output_text", success_entry)
            self.assertIn("opus-panel-output", success_entry["output_preview"])

    def test_panel_synthesis_prompt_is_bounded(self) -> None:
        anti = load_anti()
        anti.fetch_model_ids = lambda base_url, *, timeout, token_env: {"claude-3.5-sonnet", "claude-opus-4-6"}
        judge_prompt_lengths: list[int] = []

        def fake_post_response(**kwargs):
            if "You are synthesizing an Antigravity multi-model advisory panel" in kwargs["prompt"]:
                judge_prompt_lengths.append(len(kwargs["prompt"]))
                return "judge-output"
            return "panel-output\n" + ("x" * 5000)

        anti.post_response = fake_post_response
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            rc = anti.main(
                [
                    "panel",
                    "--mode",
                    "ask",
                    "--prompt",
                    "What next?",
                    "--max-synthesis-chars",
                    "2200",
                    "--json",
                ]
        )

        self.assertEqual(rc, 0, output.getvalue())
        self.assertLessEqual(judge_prompt_lengths[0], 2200)
        parsed = json.loads(output.getvalue())
        self.assertLessEqual(parsed["metadata"]["synthesis_prompt_chars"], 2200)
        self.assertTrue(parsed["metadata"]["synthesis_truncated_models"])

    def test_panel_large_review_summarizes_before_fanout(self) -> None:
        anti = load_anti()
        anti.fetch_model_ids = lambda base_url, *, timeout, token_env: {"claude-3.5-sonnet", "claude-opus-4-6"}
        panel_prompts: list[str] = []

        def fake_post_response(**kwargs):
            prompt = kwargs["prompt"]
            if "Chunked Review Manifest" in prompt:
                return "bounded summary"
            if "You are synthesizing an Antigravity multi-model advisory panel" in prompt:
                return json.dumps(
                    {
                        "summary": "summary",
                        "disagreements": [],
                        "findings": [],
                        "unverifiable": [],
                        "recommended_next_actions": [],
                        "caveats": [],
                    }
                )
            if "This panel review context was summarized" in prompt:
                panel_prompts.append(prompt)
                return "panel from summary"
            return "chunk result"

        anti.post_response = fake_post_response
        with tempfile.TemporaryDirectory(prefix="anti-skill-test-") as tmp:
            root = Path(tmp)
            (root / "large.py").write_text("LARGE = '" + ("x" * 6000) + "'\n", encoding="utf-8")
            old_cwd = Path.cwd()
            output = io.StringIO()
            try:
                os.chdir(root)
                with contextlib.redirect_stdout(output):
                    rc = anti.main(
                        [
                            "panel",
                            "--mode",
                            "review",
                            "--scope",
                            "files",
                            "--file",
                            "large.py",
                            "--max-prompt-chars",
                            "1800",
                            "--json",
                        ]
                    )
            finally:
                os.chdir(old_cwd)

        self.assertEqual(rc, 0, output.getvalue())
        self.assertTrue(panel_prompts)
        parsed = json.loads(output.getvalue())
        self.assertEqual(parsed["metadata"]["panel_review_context"], "chunked-summary")
        self.assertTrue(any("bounded chunked summary" in caveat for caveat in parsed["caveats"]))


if __name__ == "__main__":
    unittest.main()
