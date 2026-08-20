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
import unittest.mock
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
        anti.fetch_model_ids = lambda base_url, *, timeout, token_env: {"claude-sonnet-4-6"}
        anti.fetch_gateway_package_version = lambda base_url, *, timeout, token_env: "1.6.3"
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            rc = anti.main(["smoke", "--skip-doctor", "--model", "sonnet"])

        self.assertEqual(rc, 0, output.getvalue())
        self.assertIn("Gateway package version: 1.6.3", output.getvalue())
        self.assertIn("claude-sonnet-4-6", output.getvalue())
        self.assertNotIn("claude-opus-4-6-thinking", output.getvalue())

    def test_smoke_sidecar_mode_does_not_fail_on_doctor_config_mismatch(self) -> None:
        anti = load_anti()
        anti.find_cli = lambda: (["codex-antigravity"], None)
        anti.fetch_model_ids = lambda base_url, *, timeout, token_env: {"claude-sonnet-4-6"}
        anti.fetch_gateway_package_version = lambda base_url, *, timeout, token_env: "1.7.0"
        anti.run_cli = lambda args: self.fail("doctor should not run in sidecar mode")
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            rc = anti.main(["smoke", "--model", "sonnet"])

        self.assertEqual(rc, 0, output.getvalue())
        self.assertIn("doctor skipped in sidecar mode", output.getvalue())

    def test_smoke_full_mode_fails_when_doctor_fails(self) -> None:
        anti = load_anti()
        anti.find_cli = lambda: (["codex-antigravity"], None)
        anti.fetch_model_ids = lambda base_url, *, timeout, token_env: {"claude-sonnet-4-6"}
        anti.fetch_gateway_package_version = lambda base_url, *, timeout, token_env: "1.7.0"
        anti.run_cli = lambda args: 1
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            rc = anti.main(["smoke", "--mode", "full", "--model", "sonnet"])

        self.assertEqual(rc, 1)
        self.assertIn("doctor reported hard failures", output.getvalue())

    def test_smoke_json_full_mode_suppresses_doctor_stdout(self) -> None:
        anti = load_anti()
        anti.find_cli = lambda: (["codex-antigravity"], None)
        anti.fetch_model_ids = lambda base_url, *, timeout, token_env: {"claude-sonnet-4-6"}
        anti.fetch_gateway_package_version = lambda base_url, *, timeout, token_env: "1.6.3"
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
        self.assertEqual(parsed["gateway_package_version"], "1.6.3")

    def test_gateway_package_version_uses_health_root_and_gateway_token_boundary(self) -> None:
        anti = load_anti()
        captured: dict[str, object] = {}

        def fake_request_json(method, url, *, timeout, token_env):
            captured.update(
                {
                    "method": method,
                    "url": url,
                    "timeout": timeout,
                    "token_env": token_env,
                }
            )
            return 200, {"ok": True, "package_version": "1.6.3"}

        anti.request_json = fake_request_json

        version = anti.fetch_gateway_package_version(
            "http://127.0.0.1:51122/v1",
            timeout=2.5,
            token_env="TEST_GATEWAY_TOKEN",
        )

        self.assertEqual(version, "1.6.3")
        self.assertEqual(
            captured,
            {
                "method": "GET",
                "url": "http://127.0.0.1:51122/health",
                "timeout": 2.5,
                "token_env": "TEST_GATEWAY_TOKEN",
            },
        )

    def test_smoke_warns_on_health_failure_without_overriding_models_readiness(self) -> None:
        anti = load_anti()
        anti.find_cli = lambda: (["codex-antigravity"], None)
        anti.fetch_model_ids = lambda base_url, *, timeout, token_env: {"claude-sonnet-4-6"}

        def fail_health(base_url, *, timeout, token_env):
            raise anti.AntiError("/health returned HTTP 503")

        anti.fetch_gateway_package_version = fail_health
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            rc = anti.main(["smoke", "--skip-doctor", "--model", "sonnet"])

        self.assertEqual(rc, 0, output.getvalue())
        self.assertIn("[WARN] Gateway /health: /health returned HTTP 503", output.getvalue())
        self.assertIn("[PASS] Gateway /v1/models", output.getvalue())

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

    def test_review_empty_staged_scope_raises_actionable_error_before_gateway(self) -> None:
        anti = load_anti()
        anti.post_response = lambda **kwargs: self.fail("empty scope must fail before any model call")
        with tempfile.TemporaryDirectory(prefix="anti-skill-test-") as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            (root / "app.py").write_text("print('ok')\n", encoding="utf-8")

            old_cwd = Path.cwd()
            try:
                os.chdir(root)
                args = anti.build_parser().parse_args(["review", "--scope", "staged"])
                with self.assertRaises(anti.AntiError) as raised:
                    anti.command_review(args)
            finally:
                os.chdir(old_cwd)

        self.assertIn("no staged changes", str(raised.exception))
        self.assertIn("git add", str(raised.exception))

    def test_review_clean_working_tree_scope_raises_actionable_error(self) -> None:
        anti = load_anti()
        anti.post_response = lambda **kwargs: self.fail("empty scope must fail before any model call")
        with tempfile.TemporaryDirectory(prefix="anti-skill-test-") as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            (root / "app.py").write_text("print('ok')\n", encoding="utf-8")
            subprocess.run(["git", "add", "app.py"], cwd=root, check=True)
            subprocess.run(
                ["git", "-c", "user.email=a@example.com", "-c", "user.name=A", "commit", "-qm", "initial"],
                cwd=root,
                check=True,
            )

            old_cwd = Path.cwd()
            try:
                os.chdir(root)
                args = anti.build_parser().parse_args(["review", "--scope", "working-tree"])
                with self.assertRaises(anti.AntiError) as raised:
                    anti.command_review(args)
            finally:
                os.chdir(old_cwd)

        self.assertIn("no working-tree changes", str(raised.exception))

    def test_chunked_review_drops_stale_single_prompt_truncation_caveat(self) -> None:
        anti = load_anti()

        def fake_generate_with_fallback(args, *, model, prompt, purpose, **kwargs):
            return f"result-{purpose}", model, {"usage": {"input_tokens": 1, "output_tokens": 2, "total_tokens": 3}}

        anti.generate_with_fallback = fake_generate_with_fallback
        args = anti.build_parser().parse_args(
            ["review", "--scope", "files", "--file", "SKILL.md", "--chunked", "always"]
        )
        context = {
            "root": Path.cwd(),
            "paths": ["src/app.py"],
            "excluded": [],
            "diff": "--- a/src/app.py\n+++ b/src/app.py\n@@ -1 +1 @@\n-print('one')\n+print('two')\n",
            "file_texts": [("src/app.py", "print('two')\n")],
            "scope_line": "diff (origin/main...HEAD)",
            "caveats": [
                "Git diff truncated to fit max prompt budget (78527 original chars, 28944 included)",
                "Some other caveat.",
            ],
        }
        base_metadata = {
            "status": "incomplete",
            "omitted_files": [],
            "diff_truncated": True,
            "diff_original_chars": 78527,
        }

        text, caveats, metadata = anti.run_chunked_review(
            args=args,
            context=context,
            model="claude-opus-4-6-thinking",
            base_metadata=base_metadata,
            max_prompt_chars=30000,
        )

        self.assertTrue(text)
        self.assertNotIn("Git diff truncated", "\n".join(caveats))
        self.assertIn("Some other caveat.", caveats)
        self.assertEqual(metadata["status"], "complete")
        self.assertEqual(metadata["single_prompt_status"], "incomplete")
        self.assertEqual(metadata["diff_original_chars"], 78527)

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
        anti.fetch_model_ids = lambda base_url, *, timeout, token_env: {"claude-opus-4-6-thinking"}
        calls = {"count": 0}

        def fake_request_json(method, url, *, payload=None, timeout=10.0, token_env=anti.DEFAULT_TOKEN_ENV):
            calls["count"] += 1
            if calls["count"] == 1:
                return 502, {"detail": "rotation failed"}
            return 200, {"output": [{"content": [{"type": "output_text", "text": "ok"}]}]}

        anti.request_json = fake_request_json

        text = anti.post_response(
            base_url="http://127.0.0.1:51122/v1",
            model="claude-opus-4-6-thinking",
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

    def test_review_zero_chunk_count_means_unlimited(self) -> None:
        anti = load_anti()
        output = io.StringIO()

        parser = anti.build_parser()
        args = parser.parse_args(["review", "--scope", "files", "--file", "x.py", "--max-review-chunks", "0"])
        self.assertEqual(args.max_review_chunks, 0)

        with contextlib.redirect_stderr(output):
            with self.assertRaises(SystemExit) as raised:
                parser.parse_args(["review", "--scope", "files", "--file", "x.py", "--max-review-chunks", "-1"])

        self.assertEqual(raised.exception.code, 2)
        self.assertIn("value must be at least 0", output.getvalue())

    def test_generation_numeric_arguments_reject_negative_values(self) -> None:
        anti = load_anti()
        parser = anti.build_parser()

        for argv in (
            ["consult", "--prompt", "x", "--max-prompt-chars", "-1"],
            ["consult", "--prompt", "x", "--retry", "-1"],
            ["review", "--scope", "files", "--file", "x.py", "--max-synthesis-chars", "-1"],
        ):
            with self.assertRaises(SystemExit):
                parser.parse_args(argv)

    def test_prompt_sources_keep_file_inline_and_positional_order(self) -> None:
        anti = load_anti()
        with tempfile.TemporaryDirectory(prefix="anti-prompt-") as tmp:
            prompt_file = Path(tmp) / "prompt.txt"
            prompt_file.write_text("from-file", encoding="utf-8")
            args = anti.build_parser().parse_args(
                ["consult", "--prompt-file", str(prompt_file), "--prompt", "inline", "positional", "tail"]
            )

            prompt = anti.read_prompt(args)

        self.assertEqual(prompt, "from-file\n\ninline\n\npositional tail")

    def test_chunk_cap_manifest_matches_prompts_that_will_be_sent(self) -> None:
        anti = load_anti()
        context = {
            "scope_line": "files",
            "diff": "",
            "file_texts": [("a.py", "a\n" * 2000), ("b.py", "b\n" * 2000)],
            "excluded": [],
            "caveats": [],
        }

        chunks, manifest = anti.build_review_chunk_prompts(context, max_prompt_chars=2200, max_chunks=1)

        self.assertEqual(manifest["chunk_count"], len(chunks))
        self.assertEqual(manifest["included_items"], [chunk["label"] for chunk in chunks])
        self.assertTrue(manifest["included_files"])
        self.assertTrue(manifest["omitted_items"])
        self.assertEqual(manifest["status"], "incomplete")

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

    def test_workflow_claude_grok_requires_both_reviewer_families(self) -> None:
        anti = load_anti()

        def fail_gateway_call(*args, **kwargs):
            self.fail("print-only workflow validation must not contact the gateway")

        anti.fetch_model_ids = fail_gateway_call
        anti.request_json = fail_gateway_call
        cases = [
            (None, ["claude-sonnet-4-6", "claude-opus-4-6-thinking", "xai-oauth:grok-build-0.1"]),
            (["sonnet", "grok"], ["claude-sonnet-4-6", "xai-oauth:grok-build-0.1"]),
            (
                ["sonnet", "opus", "grok-bluesminds"],
                ["claude-sonnet-4-6", "claude-opus-4-6-thinking", "bluesminds:grok-4.5"],
            ),
        ]

        for requested_models, expected_models in cases:
            with self.subTest(requested_models=requested_models):
                stdout = io.StringIO()
                stderr = io.StringIO()
                argv = [
                    "workflow",
                    "claude-grok",
                    "--panel-mode",
                    "ask",
                    "--prompt",
                    "compare",
                    "--print-prompt",
                    "--json",
                ]
                for model in requested_models or []:
                    argv.extend(["--model", model])

                with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    rc = anti.main(argv)

                self.assertEqual(rc, 0, stderr.getvalue())
                parsed = json.loads(stdout.getvalue())
                self.assertEqual(parsed["metadata"]["panel_models"], expected_models)
                self.assertEqual(parsed["metadata"]["judge_model"], "claude-opus-4-6-thinking")

    def test_workflow_claude_grok_rejects_single_family_reviewer_sets(self) -> None:
        anti = load_anti()

        def fail_gateway_call(*args, **kwargs):
            self.fail("invalid workflow validation must not contact the gateway")

        anti.fetch_model_ids = fail_gateway_call
        anti.request_json = fail_gateway_call
        with tempfile.TemporaryDirectory(prefix="anti-runs-") as tmp:
            anti.RUNS_DIR = Path(tmp)
            cases = [
                ["grok-bluesminds"],
                ["sonnet", "opus"],
            ]

            for requested_models in cases:
                with self.subTest(requested_models=requested_models):
                    stdout = io.StringIO()
                    stderr = io.StringIO()
                    argv = [
                        "workflow",
                        "claude-grok",
                        "--panel-mode",
                        "ask",
                        "--prompt",
                        "compare",
                        "--print-prompt",
                        "--json",
                    ]
                    for model in requested_models:
                        argv.extend(["--model", model])

                    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                        rc = anti.main(argv)

                    self.assertEqual(rc, 1)
                    self.assertEqual(stdout.getvalue(), "")
                    self.assertIn("requires at least one Claude reviewer and one Grok reviewer", stderr.getvalue())
                    self.assertIn("--model sonnet --model opus --model grok", stderr.getvalue())

    def test_direct_claude_grok_panel_keeps_custom_single_family_behavior(self) -> None:
        anti = load_anti()
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            rc = anti.main(
                [
                    "panel",
                    "--mode",
                    "ask",
                    "--collab",
                    "claude-grok",
                    "--model",
                    "grok-bluesminds",
                    "--prompt",
                    "compare",
                    "--print-prompt",
                    "--json",
                ]
            )

        self.assertEqual(rc, 0, output.getvalue())
        parsed = json.loads(output.getvalue())
        self.assertEqual(parsed["metadata"]["panel_models"], ["bluesminds:grok-4.5"])
        self.assertEqual(parsed["metadata"]["judge_model"], "claude-opus-4-6-thinking")

    def test_claude_grok_reviewer_family_classifier_is_narrow(self) -> None:
        anti = load_anti()
        cases = {
            "claude-sonnet-4-6": "claude",
            "claude-opus-4-6-thinking": "claude",
            "xai-oauth:grok-build-0.1": "grok",
            "bluesminds:grok-4.5": "grok",
            "openrouter:grok-4": None,
            "xai:grok-4": None,
            "custom-claude-opus": None,
        }

        for model_id, expected_family in cases.items():
            with self.subTest(model_id=model_id):
                self.assertEqual(anti.claude_grok_reviewer_family(model_id), expected_family)

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
            if kwargs["model"] == "claude-opus-4-6-thinking":
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
        self.assertEqual(calls, ["claude-opus-4-6-thinking", "claude-sonnet-4-6"])
        parsed = json.loads(output.getvalue())
        self.assertEqual(parsed["model"], "claude-sonnet-4-6")
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
            if payload["model"] == "claude-opus-4-6-thinking":
                raise anti.AntiError("request to http://127.0.0.1:51122/v1/responses returned HTTP 502 non-JSON response")
            return 200, {"output_text": "fallback-ok"}

        anti.request_json = fake_request_json
        text, model_used, metadata = anti.generate_with_fallback(
            args,
            model="claude-opus-4-6-thinking",
            prompt="hello",
            max_output_tokens=16,
            purpose="consult",
            model_ids={"claude-opus-4-6-thinking", "claude-sonnet-4-6"},
        )

        self.assertEqual(text, "fallback-ok")
        self.assertEqual(model_used, "claude-sonnet-4-6")
        self.assertTrue(metadata["fallback_used"])
        self.assertEqual(calls, ["claude-opus-4-6-thinking", "claude-opus-4-6-thinking", "claude-sonnet-4-6"])

    def test_retryable_generation_failure_reports_wedged_gateway_probe(self) -> None:
        anti = load_anti()
        args = anti.build_parser().parse_args(["plan", "--model", "opus", "--prompt", "hello"])
        probes: list[float] = []

        def fake_post_response(**kwargs):
            raise anti.AntiError(
                "/v1/responses returned HTTP 502: backend failed after 1 attempt(s). "
                "Diagnostics: model=claude-opus-4-6-thinking, retryable=true"
            )

        def fake_fetch_model_ids(base_url: str, *, timeout: float, token_env: str):
            probes.append(timeout)
            raise anti.AntiError(f"request to {base_url}/models failed: timed out")

        anti.post_response = fake_post_response
        anti.fetch_model_ids = fake_fetch_model_ids

        with self.assertRaises(anti.AntiError) as raised:
            anti.generate_with_fallback(
                args,
                model="claude-opus-4-6-thinking",
                prompt="hello",
                max_output_tokens=16,
                purpose="plan",
            )

        message = str(raised.exception)
        self.assertIn("Gateway health check after this retryable failure also timed out", message)
        self.assertIn("gateway appears wedged; restart recommended", message)
        default_port = anti.DEFAULT_BASE_URL.rsplit(":", 1)[-1].split("/", 1)[0]
        self.assertIn(f"--port {default_port}", message)
        self.assertEqual(probes, [8.0])

    def test_retryable_generation_failure_reports_healthy_gateway_probe(self) -> None:
        anti = load_anti()
        args = anti.build_parser().parse_args(["plan", "--model", "opus", "--prompt", "hello"])

        def fake_post_response(**kwargs):
            raise anti.AntiError(
                "/v1/responses returned HTTP 502: backend failed after 1 attempt(s). "
                "Diagnostics: model=claude-opus-4-6-thinking, retryable=true"
            )

        anti.post_response = fake_post_response
        anti.fetch_model_ids = lambda base_url, *, timeout, token_env: {"claude-opus-4-6-thinking", "claude-sonnet-4-6"}

        with self.assertRaises(anti.AntiError) as raised:
            anti.generate_with_fallback(
                args,
                model="claude-opus-4-6-thinking",
                prompt="hello",
                max_output_tokens=16,
                purpose="plan",
            )

        message = str(raised.exception)
        self.assertIn("Gateway /v1/models stayed responsive", message)
        self.assertIn("generation path appears unhealthy", message)
        self.assertIn("not model-list readiness", message)
        self.assertNotIn("gateway appears wedged", message)

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
            model="claude-sonnet-4-6",
            prompt="hello",
            max_output_tokens=16,
            purpose="consult",
            model_ids={"claude-sonnet-4-6"},
        )

        self.assertEqual(text, "ok")
        self.assertEqual(model_used, "claude-sonnet-4-6")
        self.assertEqual(payloads[0]["metadata"], {"run_id": "anti-run_123"})
        self.assertEqual(metadata["usage"], {"input_tokens": 1, "output_tokens": 2, "total_tokens": 3})

    def test_long_generation_sends_backend_timeout_metadata(self) -> None:
        anti = load_anti()
        args = anti.build_parser().parse_args(["plan", "--prompt", "hello", "--timeout", "240"])
        payloads: list[dict] = []

        def fake_request_json(method, url, *, payload=None, timeout=10.0, token_env=anti.DEFAULT_TOKEN_ENV):
            payloads.append(payload or {})
            return 200, {"output_text": "ok"}

        anti.request_json = fake_request_json
        text, model_used, _metadata = anti.generate_with_fallback(
            args,
            model="claude-opus-4-6-thinking",
            prompt="hello",
            max_output_tokens=16,
            purpose="plan",
            model_ids={"claude-opus-4-6-thinking"},
        )

        self.assertEqual(text, "ok")
        self.assertEqual(model_used, "claude-opus-4-6-thinking")
        self.assertEqual(payloads[0]["metadata"], {"antigravity_backend_timeout_seconds": 230.0})

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

    def test_interrupted_saved_run_has_deterministic_correlation_record(self) -> None:
        anti = load_anti()
        anti.post_response = lambda **kwargs: (_ for _ in ()).throw(KeyboardInterrupt())
        with tempfile.TemporaryDirectory(prefix="anti-runs-") as tmp:
            anti.RUNS_DIR = Path(tmp)
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                rc = anti.main(
                    [
                        "consult",
                        "--prompt",
                        "hello",
                        "--save-output",
                        "summary",
                        "--run-id",
                        "deterministic-interrupt-1",
                    ]
                )

            record = json.loads(next(Path(tmp).glob("*.json")).read_text(encoding="utf-8"))

        self.assertEqual(rc, 130)
        self.assertEqual(record["id"], "deterministic-interrupt-1")
        self.assertEqual(record["status"], "interrupted")
        self.assertEqual(record["metadata"]["request_log_correlation_id"], "deterministic-interrupt-1")

    def test_final_model_output_is_redacted_in_text_and_json(self) -> None:
        anti = load_anti()
        sentinel = "sk-antitest-secret-sentinel-1234567890"
        anti.post_response = lambda **kwargs: f"result api_key={sentinel}"

        for extra_args in ([], ["--json"]):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                rc = anti.main(["consult", "--prompt", "hello", *extra_args])

            self.assertEqual(rc, 0, output.getvalue())
            self.assertNotIn(sentinel, output.getvalue())
            self.assertIn("<redacted>", output.getvalue())

    def test_panel_presentation_redacts_lane_output_findings_and_metadata(self) -> None:
        anti = load_anti()
        sentinel = "sk-antitest-panel-secret-1234567890"
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            anti.print_panel_result(
                panel_mode="review",
                base_url="http://127.0.0.1:51122/v1",
                judge_model="opus",
                panel_models=["sonnet"],
                panel_results=[{"model": "sonnet", "status": "success", "output_text": f"api_key={sentinel}"}],
                text=f"api_key={sentinel}",
                caveats=[f"api_key={sentinel}"],
                metadata={"manifest": f"api_key={sentinel}"},
                findings={"summary": f"api_key={sentinel}"},
                output_json=True,
            )

        self.assertNotIn(sentinel, output.getvalue())
        self.assertIn("<redacted>", output.getvalue())

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

    def test_chunked_plan_full_ledger_records_actual_calls_in_order(self) -> None:
        anti = load_anti()
        calls: list[str] = []

        def fake_post_response(**kwargs):
            calls.append(kwargs["prompt"])
            return "plan-synthesis" if "synthesizing a decision-complete" in kwargs["prompt"] else "chunk-note"

        anti.post_response = fake_post_response
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
                        "1800",
                        "--max-plan-chunks",
                        "2",
                        "--save-output",
                        "full",
                        "--run-id",
                        "deterministic-run-7",
                        "--json",
                    ]
                )

            self.assertEqual(rc, 0, output.getvalue())
            record = json.loads(next(Path(tmp).glob("*.json")).read_text(encoding="utf-8"))
            ledger = record["execution_ledger"]
            self.assertEqual([entry["prompt"] for entry in ledger], calls)
            self.assertEqual([entry["stage"] for entry in ledger], ["plan_chunk_1", "plan_chunk_2", "plan_synthesis"])
            self.assertEqual(record["id"], "deterministic-run-7")
            self.assertEqual(record["metadata"]["request_log_correlation_id"], "deterministic-run-7")
            self.assertNotEqual(record["prompt_text"], ("x" * 1800))

    def test_default_claude_plan_auto_chunks_before_large_single_call(self) -> None:
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
                    "x" * 45_000,
                    "--json",
                ]
            )

        self.assertEqual(rc, 0, output.getvalue())
        parsed = json.loads(output.getvalue())
        self.assertTrue(parsed["metadata"]["chunked"])
        self.assertGreaterEqual(parsed["metadata"]["chunk_count"], 2)
        self.assertEqual(parsed["metadata"]["prompt_budget_chars"], anti.CLAUDE_SAFE_PROMPT_CHARS)
        self.assertTrue(parsed["metadata"]["claude_prompt_guardrail"])
        self.assertTrue(all(length <= anti.CLAUDE_SAFE_PROMPT_CHARS for length in parsed["metadata"]["sent_chunk_prompt_chars"]))
        self.assertTrue(any("Claude safety budget" in caveat for caveat in parsed["caveats"]))

    def test_plan_chunk_decision_uses_explicit_claude_budget(self) -> None:
        anti = load_anti()
        parser = anti.build_parser()
        args = parser.parse_args(["plan", "--model", "opus", "--max-prompt-chars", "0", "--prompt", "x"])
        budget = anti.prompt_budget_for_model(args, "claude-opus-4-6-thinking")

        self.assertEqual(budget, anti.CLAUDE_SAFE_PROMPT_CHARS)
        self.assertFalse(hasattr(args, "_effective_prompt_budget"))
        self.assertTrue(anti.should_chunk_plan(args, "x" * 45_000, max_prompt_chars=budget))

    def test_default_claude_review_auto_chunks_before_large_single_call(self) -> None:
        anti = load_anti()
        calls: list[str] = []

        def fake_post_response(**kwargs):
            calls.append(kwargs["prompt"])
            return "review-synthesis" if "synthesizing an Antigravity sidecar code review" in kwargs["prompt"] else "chunk-review"

        anti.post_response = fake_post_response
        with tempfile.TemporaryDirectory(prefix="anti-skill-test-") as tmp:
            root = Path(tmp)
            (root / "big.py").write_text("VALUE = '" + ("x" * 45_000) + "'\n", encoding="utf-8")
            output = io.StringIO()
            old_cwd = Path.cwd()
            try:
                os.chdir(root)
                with contextlib.redirect_stdout(output):
                    rc = anti.main(["review", "--scope", "files", "--file", "big.py", "--json"])
            finally:
                os.chdir(old_cwd)

        self.assertEqual(rc, 0, output.getvalue())
        parsed = json.loads(output.getvalue())
        self.assertTrue(parsed["metadata"]["chunked"])
        self.assertGreaterEqual(parsed["metadata"]["chunk_count"], 2)
        self.assertEqual(parsed["metadata"]["prompt_budget_chars"], anti.CLAUDE_SAFE_PROMPT_CHARS)
        self.assertTrue(parsed["metadata"]["claude_prompt_guardrail"])
        self.assertTrue(all(item["prompt_chars"] <= anti.CLAUDE_SAFE_PROMPT_CHARS for item in parsed["metadata"]["chunk_prompts"]))
        self.assertTrue(any("Claude safety budget" in caveat for caveat in parsed["caveats"]))
        self.assertGreater(len(calls), 1)

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

        self.assertEqual(anti.resolve_panel_models(args.model), ["claude-sonnet-4-6", "claude-opus-4-6-thinking"])
        self.assertEqual(anti.resolve_model(args.judge, default=anti.DEFAULT_PANEL_JUDGE_MODEL), "claude-opus-4-6-thinking")

    def test_provider_aliases_resolve_deterministically_without_changing_oauth_defaults(self) -> None:
        anti = load_anti()

        expected = {
            "grok": "xai-oauth:grok-build-0.1",
            "supergrok": "xai-oauth:grok-build-0.1",
            "xai-grok": "xai-oauth:grok-build-0.1",
            "grok-oauth": "xai-oauth:grok-build-0.1",
            "grok-build": "xai-oauth:grok-build-0.1",
            "grok-build-0.1": "xai-oauth:grok-build-0.1",
            "grok-4": "xai-oauth:grok-4.3",
            "grok-4.3": "xai-oauth:grok-4.3",
            "grok-bluesminds": "bluesminds:grok-4.5",
            "grok-4.5": "bluesminds:grok-4.5",
            "deepseek-v4-pro": "deepseek:deepseek-v4-pro",
            "deepseek-v4-flash": "deepseek:deepseek-v4-flash",
            "glm-5.2": "bluesminds:z-ai/glm-5.2",
            "glm52": "bluesminds:z-ai/glm-5.2",
        }
        for alias, model_id in expected.items():
            with self.subTest(alias=alias):
                self.assertEqual(anti.resolve_model(alias, default="sonnet"), model_id)

    def test_claude_grok_can_explicitly_select_bluesminds_without_changing_default_route(self) -> None:
        anti = load_anti()
        default_models = anti.resolve_panel_models(None, collab_profile="claude-grok")
        explicit_models = anti.resolve_panel_models(
            ["sonnet", "opus", "grok-bluesminds"],
            collab_profile="claude-grok",
        )

        self.assertEqual(default_models[-1], "xai-oauth:grok-build-0.1")
        self.assertEqual(explicit_models[-1], "bluesminds:grok-4.5")
        self.assertEqual(anti.DEFAULT_PANEL_JUDGE_MODEL, "claude-opus-4-6-thinking")

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
            ["claude-sonnet-4-6", "claude-opus-4-6-thinking", "xai-oauth:grok-build-0.1"],
        )
        self.assertIn("Claude + Grok collaboration", parsed["prompt"])
        self.assertIn("Claude-family lanes", parsed["prompt"])
        self.assertIn("Grok/xAI lanes", parsed["prompt"])

    def test_claude_grok_judge_prompt_requires_cross_lane_synthesis(self) -> None:
        anti = load_anti()
        anti.fetch_model_ids = lambda base_url, *, timeout, token_env: {
            "claude-sonnet-4-6",
            "claude-opus-4-6-thinking",
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

    def test_panel_repo_disclosure_names_resolved_bluesminds_and_deepseek_lanes(self) -> None:
        anti = load_anti()
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            rc = anti.main(
                [
                    "panel",
                    "--mode",
                    "review",
                    "--scope",
                    "files",
                    "--file",
                    "README.md",
                    "--model",
                    "grok-bluesminds",
                    "--model",
                    "deepseek-v4-pro",
                    "--judge",
                    "opus",
                    "--print-prompt",
                    "--json",
                ]
            )

        self.assertEqual(rc, 0, output.getvalue())
        parsed = json.loads(output.getvalue())
        disclosure = next(item for item in parsed["caveats"] if "BYOK disclosure" in item)
        self.assertIn("bluesminds:grok-4.5", disclosure)
        self.assertIn("deepseek:deepseek-v4-pro", disclosure)
        self.assertNotIn("BLUESMINDS_API_KEY", disclosure)
        self.assertNotIn("DEEPSEEK_API_KEY", disclosure)

    def test_review_repo_disclosure_names_explicit_deepseek_fallback(self) -> None:
        anti = load_anti()
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            rc = anti.main(
                [
                    "review",
                    "--scope",
                    "files",
                    "--file",
                    "README.md",
                    "--model",
                    "opus",
                    "--fallback-model",
                    "deepseek-v4-flash",
                    "--fallback-policy",
                    "on-retryable",
                    "--print-prompt",
                    "--json",
                ]
            )

        self.assertEqual(rc, 0, output.getvalue())
        parsed = json.loads(output.getvalue())
        disclosure = next(item for item in parsed["caveats"] if "BYOK disclosure" in item)
        self.assertIn("deepseek:deepseek-v4-flash", disclosure)
        self.assertNotIn("claude-opus-4-6-thinking", disclosure)

    def test_chunked_review_preserves_bluesminds_disclosure(self) -> None:
        anti = load_anti()
        anti.post_response = lambda **kwargs: (
            "review-synthesis"
            if "synthesizing an Antigravity sidecar code review" in kwargs["prompt"]
            else "chunk-review"
        )
        with tempfile.TemporaryDirectory(prefix="anti-skill-test-") as tmp:
            root = Path(tmp)
            (root / "large.py").write_text("VALUE = '" + ("x" * 5000) + "'\n", encoding="utf-8")
            output = io.StringIO()
            old_cwd = Path.cwd()
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
                            "--model",
                            "grok-bluesminds",
                            "--max-prompt-chars",
                            "2400",
                            "--max-review-chunks",
                            "2",
                            "--allow-partial",
                            "--json",
                        ]
                    )
            finally:
                os.chdir(old_cwd)

        self.assertEqual(rc, 0, output.getvalue())
        parsed = json.loads(output.getvalue())
        self.assertTrue(parsed["metadata"]["chunked"])
        self.assertEqual(parsed["metadata"]["status"], "incomplete")
        self.assertTrue(parsed["metadata"]["omitted_files"])
        self.assertIn("⚠ INCOMPLETE", parsed["output_text"])
        self.assertEqual(parsed["metadata"]["scopeStatus"], "partial")
        self.assertTrue(
            any("bluesminds:grok-4.5" in item for item in parsed["caveats"]),
            parsed["caveats"],
        )
        self.assertTrue(
            any(
                "bluesminds:grok-4.5" in item
                for item in parsed["metadata"]["privacy_disclosures"]
            )
        )

    def test_plan_repo_disclosure_names_bluesminds_glm_lane(self) -> None:
        anti = load_anti()
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            rc = anti.main(
                [
                    "plan",
                    "--scope",
                    "files",
                    "--file",
                    "README.md",
                    "--model",
                    "glm-5.2",
                    "--prompt",
                    "Plan this change",
                    "--print-prompt",
                    "--json",
                ]
            )

        self.assertEqual(rc, 0, output.getvalue())
        parsed = json.loads(output.getvalue())
        disclosure = next(item for item in parsed["caveats"] if "BYOK disclosure" in item)
        self.assertIn("bluesminds:z-ai/glm-5.2", disclosure)

    def test_panel_successful_two_model_run_calls_judge_once(self) -> None:
        anti = load_anti()
        anti.fetch_model_ids = lambda base_url, *, timeout, token_env: {"claude-sonnet-4-6", "claude-opus-4-6-thinking"}
        judge_prompts: list[str] = []

        def fake_post_response(**kwargs):
            if "You are synthesizing an Antigravity multi-model advisory panel" in kwargs["prompt"]:
                judge_prompts.append(kwargs["prompt"])
                return json.dumps(
                    {
                        "summary": "Judge summary.",
                        "disagreements": [],
                        "findings": [],
                        "unverifiable": [],
                        "recommended_next_actions": [],
                        "caveats": [],
                    }
                )
            return f"panel-output-{kwargs['model']}"

        anti.post_response = fake_post_response
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            rc = anti.main(["panel", "--mode", "ask", "--prompt", "What next?", "--json"])

        self.assertEqual(rc, 0, output.getvalue())
        self.assertEqual(len(judge_prompts), 1)
        self.assertIn("panel-output-claude-sonnet-4-6", judge_prompts[0])
        self.assertIn("panel-output-claude-opus-4-6-thinking", judge_prompts[0])
        parsed = json.loads(output.getvalue())
        self.assertEqual(parsed["metadata"]["findings_status"], "parsed")
        self.assertEqual(parsed["metadata"]["judge_retried"], False)
        self.assertEqual([item["status"] for item in parsed["panel_results"]], ["success", "success"])

    def test_panel_usage_latency_and_findings_are_reported(self) -> None:
        anti = load_anti()
        anti.fetch_model_ids = lambda base_url, *, timeout, token_env: {"claude-sonnet-4-6", "claude-opus-4-6-thinking"}
        calls: list[dict] = []
        finding_payload = {
            "summary": "Disagreements first.",
            "disagreements": ["Sonnet worries about tests; Opus worries about authz."],
            "findings": [
                {
                    "id": "F1",
                    "claim": "A branch needs local verification.",
                    "severity": "medium",
                    "lanes": ["claude-sonnet-4-6", "claude-opus-4-6-thinking"],
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
        anti.fetch_model_ids = lambda base_url, *, timeout, token_env: {"claude-sonnet-4-6", "claude-opus-4-6-thinking"}
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
        anti.fetch_model_ids = lambda base_url, *, timeout, token_env: {"claude-sonnet-4-6", "claude-opus-4-6-thinking"}
        anti.post_response = lambda **kwargs: "judge-output" if "You are synthesizing" in kwargs["prompt"] else "lane"
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            rc = anti.main(["panel", "--mode", "ask", "--prompt", "x", "--json"])

        self.assertEqual(rc, 0, output.getvalue())
        parsed = json.loads(output.getvalue())
        self.assertEqual(parsed["metadata"]["findings_status"], "fallback")
        self.assertEqual(parsed["output_text"], "judge-output")
        self.assertTrue(any("structured findings" in caveat for caveat in parsed["caveats"]))

    def test_panel_truncated_lane_is_retried_and_recorded(self) -> None:
        anti = load_anti()
        anti.fetch_model_ids = lambda base_url, *, timeout, token_env: {"claude-sonnet-4-6", "claude-opus-4-6-thinking"}
        judge_prompts: list[str] = []

        def fake_post_response(**kwargs):
            if "You are synthesizing an Antigravity multi-model advisory panel" in kwargs["prompt"]:
                judge_prompts.append(kwargs["prompt"])
                return json.dumps(
                    {
                        "summary": "ok",
                        "disagreements": [],
                        "findings": [],
                        "unverifiable": [],
                        "recommended_next_actions": [],
                        "caveats": [],
                    }
                )
            if kwargs["model"] == "claude-sonnet-4-6":
                cap = kwargs["max_output_tokens"]
                return anti.ResponseText(
                    f"partial-{cap}",
                    usage={"input_tokens": 1, "output_tokens": cap, "total_tokens": cap + 1},
                    elapsed_ms=5,
                )
            return anti.ResponseText(
                "opus output",
                usage={"input_tokens": 1, "output_tokens": 5, "total_tokens": 6},
                elapsed_ms=5,
            )

        anti.post_response = fake_post_response
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            rc = anti.main(
                ["panel", "--mode", "ask", "--prompt", "What next?", "--max-output-tokens", "10", "--json"]
            )

        self.assertEqual(rc, 0, output.getvalue())
        parsed = json.loads(output.getvalue())
        sonnet = parsed["panel_results"][0]
        self.assertEqual(sonnet["status"], "truncated")
        self.assertEqual(len(sonnet["attempts"]), 2)
        # Truncated lanes are usable and fed to the judge, so they belong in
        # truncated_models, not failed_models (failed = non-usable lanes only).
        self.assertEqual(parsed["metadata"]["failed_models"], [])
        self.assertEqual(parsed["metadata"]["truncated_models"], ["claude-sonnet-4-6"])
        self.assertEqual(parsed["metadata"]["retried_models"], ["claude-sonnet-4-6"])
        self.assertTrue(any("truncated at the token cap" in caveat for caveat in parsed["caveats"]))
        self.assertIn("lane output truncated at the token cap", judge_prompts[0])

    def test_panel_non_answer_lane_retries_with_directive_and_recovers(self) -> None:
        anti = load_anti()
        anti.fetch_model_ids = lambda base_url, *, timeout, token_env: {"claude-sonnet-4-6", "claude-opus-4-6-thinking"}
        judge_prompts: list[str] = []

        def fake_post_response(**kwargs):
            if "You are synthesizing an Antigravity multi-model advisory panel" in kwargs["prompt"]:
                judge_prompts.append(kwargs["prompt"])
                return json.dumps(
                    {
                        "summary": "ok",
                        "disagreements": [],
                        "findings": [],
                        "unverifiable": [],
                        "recommended_next_actions": [],
                        "caveats": [],
                    }
                )
            if kwargs["model"] == "claude-opus-4-6-thinking" and "Produce the requested output directly now" not in kwargs["prompt"]:
                return "What would you like me to do with it?"
            return f"output-{kwargs['model']}"

        anti.post_response = fake_post_response
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            rc = anti.main(["panel", "--mode", "ask", "--prompt", "What next?", "--json"])

        self.assertEqual(rc, 0, output.getvalue())
        parsed = json.loads(output.getvalue())
        self.assertEqual([item["status"] for item in parsed["panel_results"]], ["success", "success"])
        self.assertEqual(parsed["metadata"]["retried_models"], ["claude-opus-4-6-thinking"])
        self.assertEqual(parsed["metadata"]["failed_models"], [])
        self.assertIn("output-claude-opus-4-6-thinking", judge_prompts[0])

    def test_panel_non_answer_lane_counts_as_failure_below_min_successes(self) -> None:
        anti = load_anti()
        anti.fetch_model_ids = lambda base_url, *, timeout, token_env: {"claude-sonnet-4-6", "claude-opus-4-6-thinking"}

        def fake_post_response(**kwargs):
            if kwargs["model"] == "claude-opus-4-6-thinking" and "You are synthesizing" not in kwargs["prompt"]:
                return "What would you like me to do with it?"
            if "You are synthesizing an Antigravity multi-model advisory panel" in kwargs["prompt"]:
                return "judge-output"
            return "sonnet output"

        anti.post_response = fake_post_response
        output = io.StringIO()

        with contextlib.redirect_stderr(output):
            rc = anti.main(["panel", "--mode", "ask", "--prompt", "What next?"])

        self.assertEqual(rc, 1)
        self.assertIn("below --min-successes 2", output.getvalue())

    def test_lane_long_non_answer_text_is_classified(self) -> None:
        anti = load_anti()
        live_shape = (
            "I've read the full bounded review summary. It covers 13 confirmed defects (S1-S13), "
            "5 design-level risks (R1-R5), scope caveats, and a cross-chunk verification checklist.\n\n"
            "**What would you like me to do with this?** The review is detailed but there's no explicit "
            "task attached. Here are the most useful directions I can take:\n"
            "| **A - Triage & fix** | Open scripts/anti.py and tests/test_anti.py, verify each finding, then fix |\n"
            "| **D - Full sweep** | All of the above in sequence: verify -> fix -> test |\n\n"
            "Which direction, or should I just start with **D** and work through everything systematically?"
        )
        self.assertEqual(anti.lane_output_status(live_shape, None, 6144), "non_answer")
        # A long real review that merely poses a question must stay a success.
        review = (
            "The retry logic is sound. One question worth settling locally: what should the fallback cap be "
            "when the primary lane is slow? Otherwise the diff looks good and the tests cover the retry path. "
        ) * 5
        self.assertEqual(anti.lane_output_status(review, None, 6144), "success")

    def test_panel_long_non_answer_lane_is_excluded_and_recorded(self) -> None:
        anti = load_anti()
        anti.fetch_model_ids = lambda base_url, *, timeout, token_env: {"claude-sonnet-4-6", "claude-opus-4-6-thinking"}
        judge_prompts: list[str] = []
        non_answer = (
            "I've read the full bounded review summary. It covers 13 confirmed defects and a cross-chunk checklist.\n\n"
            "**What would you like me to do with this?** The review is detailed but there's no explicit task attached.\n"
            "| **A - Triage & fix** | Verify each finding, implement fixes |\n"
            "| **D - Full sweep** | All of the above in sequence |\n\n"
            "Which direction, or should I just start with **D**?"
        )

        def fake_post_response(**kwargs):
            if "You are synthesizing an Antigravity multi-model advisory panel" in kwargs["prompt"]:
                judge_prompts.append(kwargs["prompt"])
                return json.dumps(
                    {
                        "summary": "ok",
                        "disagreements": [],
                        "findings": [],
                        "unverifiable": [],
                        "recommended_next_actions": [],
                        "caveats": [],
                    }
                )
            if kwargs["model"] == "claude-opus-4-6-thinking":
                return non_answer
            return "sonnet output"

        anti.post_response = fake_post_response
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            rc = anti.main(
                ["panel", "--mode", "ask", "--prompt", "What next?", "--min-successes", "1", "--json"]
            )

        self.assertEqual(rc, 0, output.getvalue())
        parsed = json.loads(output.getvalue())
        opus = parsed["panel_results"][1]
        self.assertEqual(opus["status"], "non_answer")
        self.assertEqual(len(opus["attempts"]), 2)
        self.assertEqual(parsed["metadata"]["failed_models"], ["claude-opus-4-6-thinking"])
        self.assertEqual(parsed["metadata"]["retried_models"], ["claude-opus-4-6-thinking"])
        self.assertNotIn("Which direction", judge_prompts[0])
        self.assertIn("asked for direction", judge_prompts[0])
        self.assertTrue(any("asked for direction" in caveat for caveat in parsed["caveats"]))

    def test_request_json_never_forwards_authorization_on_redirect(self) -> None:
        anti = load_anti()
        real_urlopen = anti.urllib.request.urlopen
        captured: dict[str, dict] = {}

        def fake_urlopen(req, timeout=10.0):
            captured["regular"] = dict(req.headers)
            captured["unredirected"] = dict(req.unredirected_hdrs)

            class FakeResponse:
                status = 200

                def read(self):
                    return b"{}"

                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    return False

            return FakeResponse()

        anti.urllib.request.urlopen = fake_urlopen
        try:
            with unittest.mock.patch.dict(os.environ, {"ANTIGRAVITY_GATEWAY_TOKEN": "redirect-test-token"}):
                status, decoded = anti.request_json(
                    "POST",
                    "http://127.0.0.1:51122/v1/responses",
                    payload={"model": "opus"},
                    timeout=2,
                    token_env="ANTIGRAVITY_GATEWAY_TOKEN",
                )
        finally:
            anti.urllib.request.urlopen = real_urlopen

        self.assertEqual(status, 200)
        self.assertEqual(decoded, {})
        self.assertNotIn("Authorization", captured["regular"])
        self.assertEqual(captured["unredirected"]["Authorization"], "Bearer redirect-test-token")

    def test_panel_judge_truncated_json_is_repaired_without_retry(self) -> None:
        anti = load_anti()
        anti.fetch_model_ids = lambda base_url, *, timeout, token_env: {"claude-sonnet-4-6", "claude-opus-4-6-thinking"}
        calls: list[str] = []
        truncated = (
            '{"summary": "partial", "disagreements": [], "findings": ['
            '{"id": "F1", "claim": "first", "severity": "high", "lanes": ["opus"], "verify": "check a"}, '
            '{"id": "F2", "claim": "second", "severity": "medium", "lanes": ["opus"], "verify": "check b"}'
        )

        def fake_post_response(**kwargs):
            calls.append(kwargs["prompt"])
            if "You are synthesizing an Antigravity multi-model advisory panel" in kwargs["prompt"]:
                return truncated
            return "lane-output"

        anti.post_response = fake_post_response
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            rc = anti.main(["panel", "--mode", "ask", "--prompt", "What next?", "--json"])

        self.assertEqual(rc, 0, output.getvalue())
        self.assertEqual(len(calls), 3, "two lanes plus one judge call; repair avoided the retry")
        parsed = json.loads(output.getvalue())
        self.assertEqual(parsed["metadata"]["findings_status"], "parsed")
        self.assertEqual(parsed["metadata"]["judge_json_repaired"], True)
        self.assertEqual(parsed["metadata"]["judge_retried"], False)
        self.assertEqual(parsed["findings"]["findings_total"], 2)
        self.assertEqual(parsed["findings"]["findings_dropped"], 0)
        self.assertIn("repaired", parsed["findings"]["parse_warning"])

    def test_panel_judge_malformed_json_retries_once_with_strict_instruction(self) -> None:
        anti = load_anti()
        anti.fetch_model_ids = lambda base_url, *, timeout, token_env: {"claude-sonnet-4-6", "claude-opus-4-6-thinking"}
        judge_calls: list[str] = []

        def fake_post_response(**kwargs):
            if "You are synthesizing an Antigravity multi-model advisory panel" in kwargs["prompt"]:
                judge_calls.append(kwargs["prompt"])
                if "Your previous response was discarded" in kwargs["prompt"]:
                    return json.dumps(
                        {
                            "summary": "recovered",
                            "disagreements": [],
                            "findings": [
                                {
                                    "id": "F1",
                                    "claim": "fixed after retry",
                                    "severity": "high",
                                    "lanes": ["claude-opus-4-6-thinking"],
                                    "verify": "run the test",
                                }
                            ],
                            "unverifiable": [],
                            "recommended_next_actions": [],
                            "caveats": [],
                        }
                    )
                return "not json at all; just prose"
            return "lane-output"

        anti.post_response = fake_post_response
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            rc = anti.main(["panel", "--mode", "ask", "--prompt", "What next?", "--json"])

        self.assertEqual(rc, 0, output.getvalue())
        self.assertEqual(len(judge_calls), 2)
        parsed = json.loads(output.getvalue())
        self.assertEqual(parsed["metadata"]["findings_status"], "parsed")
        self.assertEqual(parsed["metadata"]["judge_retried"], True)
        self.assertEqual(parsed["metadata"]["judge_json_repaired"], False)
        self.assertEqual(parsed["findings"]["findings"][0]["id"], "F1")

    def test_panel_judge_fallback_never_embeds_broken_json_in_summary(self) -> None:
        anti = load_anti()
        anti.fetch_model_ids = lambda base_url, *, timeout, token_env: {"claude-sonnet-4-6", "claude-opus-4-6-thinking"}

        def fake_post_response(**kwargs):
            if "You are synthesizing an Antigravity multi-model advisory panel" in kwargs["prompt"]:
                return '```json\n{"summary": "x", "findings": [{"id": "F1", "claim": "trunc'
            return "lane-output"

        anti.post_response = fake_post_response
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            rc = anti.main(["panel", "--mode", "ask", "--prompt", "What next?", "--json"])

        self.assertEqual(rc, 0, output.getvalue())
        parsed = json.loads(output.getvalue())
        self.assertEqual(parsed["metadata"]["findings_status"], "fallback")
        self.assertEqual(parsed["findings"]["findings"], [])
        self.assertTrue(parsed["findings"]["parse_warning"])
        self.assertNotIn('"findings"', parsed["findings"]["summary"])
        self.assertTrue(any("structured findings" in caveat for caveat in parsed["caveats"]))

    def test_repair_truncated_json_handles_more_than_80_closers(self) -> None:
        anti = load_anti()
        items = ",".join('{"i": %d}' % index for index in range(100))
        truncated = '{"summary": "s", "findings": [' + items
        parsed = anti.repair_truncated_json(truncated)
        self.assertIsInstance(parsed, dict)
        self.assertEqual(len(parsed["findings"]), 100)
        self.assertEqual(parsed["findings"][-1], {"i": 99})

    def test_read_prompt_rejects_non_utf8_file_with_actionable_error(self) -> None:
        anti = load_anti()
        with tempfile.TemporaryDirectory(prefix="anti-skill-test-") as tmp:
            path = Path(tmp) / "prompt.txt"
            path.write_bytes(b"Latin-1 prompt: caf\xe9\n")
            args = anti.build_parser().parse_args(["consult", "--prompt-file", str(path)])
            with self.assertRaises(anti.AntiError) as raised:
                anti.read_prompt(args)
        self.assertIn("not valid UTF-8", str(raised.exception))
        self.assertIn("prompt.txt", str(raised.exception))

    def test_panel_output_findings_json_keeps_stable_top_level_schema(self) -> None:
        anti = load_anti()
        anti.fetch_model_ids = lambda base_url, *, timeout, token_env: {"claude-sonnet-4-6", "claude-opus-4-6-thinking"}

        def fake_post_response(**kwargs):
            if "You are synthesizing an Antigravity multi-model advisory panel" in kwargs["prompt"]:
                return json.dumps(
                    {
                        "summary": "ok",
                        "disagreements": [],
                        "findings": [],
                        "unverifiable": [],
                        "recommended_next_actions": [],
                        "caveats": [],
                    }
                )
            return "lane-output"

        anti.post_response = fake_post_response
        full_output = io.StringIO()
        contract_output = io.StringIO()

        with contextlib.redirect_stdout(full_output):
            rc = anti.main(["panel", "--mode", "ask", "--prompt", "x", "--output", "findings", "--json"])
        with contextlib.redirect_stdout(contract_output):
            rc2 = anti.main(["panel", "--mode", "ask", "--prompt", "x", "--output", "findings"])

        self.assertEqual(rc, 0, full_output.getvalue())
        self.assertEqual(rc2, 0, contract_output.getvalue())
        full = json.loads(full_output.getvalue())
        self.assertEqual(
            set(full),
            {"caveats", "findings", "gateway", "judge_model", "metadata", "mode", "output_text", "panel_mode", "panel_models", "panel_results"},
        )
        self.assertIsInstance(full["panel_results"], list)
        self.assertIsInstance(full["findings"], dict)
        contract = json.loads(contract_output.getvalue())
        self.assertEqual(
            set(contract),
            {"caveats", "disagreements", "findings", "findings_dropped", "findings_total", "parse_warning", "recommended_next_actions", "summary", "unverifiable"},
        )

    def test_panel_errors_are_redacted_in_json_output(self) -> None:
        anti = load_anti()
        anti.fetch_model_ids = lambda base_url, *, timeout, token_env: {"claude-sonnet-4-6", "claude-opus-4-6-thinking"}

        def fake_post_response(**kwargs):
            if kwargs["model"] == "claude-sonnet-4-6":
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
        anti.fetch_model_ids = lambda base_url, *, timeout, token_env: {"claude-sonnet-4-6", "claude-opus-4-6-thinking"}
        calls: list[str] = []

        def fake_post_response(**kwargs):
            calls.append(kwargs["model"])
            if kwargs["model"] == "claude-opus-4-6-thinking" and "You are synthesizing" not in kwargs["prompt"]:
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
        self.assertEqual(calls[:2], ["claude-opus-4-6-thinking", "claude-sonnet-4-6"])
        parsed = json.loads(output.getvalue())
        self.assertEqual(parsed["panel_results"][0]["model_used"], "claude-sonnet-4-6")
        self.assertTrue(parsed["panel_results"][0]["generation"]["fallback_used"])

    def test_panel_fallback_keeps_identity_failures_and_marks_collapsed_panel(self) -> None:
        """A fallback result must not masquerade as the requested lane."""
        anti = load_anti()
        fallback = "openrouter:nvidia/nemotron-3-ultra-550b-a55b:free"
        requested = ["claude-opus-4-6-thinking", "gemini-3.7-flash", fallback]
        anti.fetch_model_ids = lambda base_url, *, timeout, token_env: set(requested)
        judge_prompts: list[str] = []

        def fake_post_response(**kwargs):
            prompt = kwargs["prompt"]
            if "You are synthesizing an Antigravity multi-model advisory panel" in prompt:
                judge_prompts.append(prompt)
                return json.dumps(
                    {
                        "summary": "The available evidence is degraded.",
                        "disagreements": [],
                        "findings": [],
                        "unverifiable": [],
                        "recommended_next_actions": [],
                        "caveats": [],
                    }
                )
            if kwargs["model"] in requested[:2]:
                raise anti.AntiError("HTTP 502: requested backend unavailable retryable=true")
            return f"lane-output-{kwargs['model']}"

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
                    "--model",
                    "flash-high",
                    "--model",
                    "nemotron-ultra",
                    "--judge",
                    "opus",
                    "--prompt",
                    "What next?",
                    "--fallback-model",
                    "nemotron-ultra",
                    "--fallback-policy",
                    "on-retryable",
                    "--min-successes",
                    "1",
                    "--json",
                ]
            )

        self.assertEqual(rc, 0, output.getvalue())
        parsed = json.loads(output.getvalue())
        results = {item["model"]: item for item in parsed["panel_results"]}
        for model in requested:
            self.assertIn(model, results)
            self.assertEqual(results[model]["model"], model)
            self.assertIn("generation", results[model])

        for model in requested[:2]:
            result = results[model]
            # `model` remains the requested lane while `model_used` records the
            # model that actually produced the output.
            self.assertEqual(result["model_used"], fallback)
            generation = result["generation"]
            self.assertEqual(generation["primary_model"], model)
            self.assertEqual(generation["model_used"], fallback)
            self.assertEqual(generation["fallback_model"], fallback)
            self.assertTrue(generation["fallback_used"])
            self.assertEqual(generation["generation_failures"][0]["model"], model)
            self.assertIn("HTTP 502", generation["generation_failures"][0]["error"])

        # Two logical lanes completed, but both (and the direct Nemotron lane)
        # were produced by one actual model.  The panel must expose that loss
        # of independence to callers and to the judge.
        metadata = parsed["metadata"]
        self.assertEqual(metadata["status"], "degraded_single_model")
        self.assertEqual(metadata["distinct_actual_models"], [fallback])
        self.assertEqual(metadata["distinct_actual_model_count"], 1)
        self.assertTrue(any("degraded_single_model" in caveat for caveat in parsed["caveats"]))
        self.assertEqual(len(judge_prompts), 1)
        self.assertIn("degraded_single_model", judge_prompts[0])
        self.assertIn("HTTP 502", judge_prompts[0])
        self.assertIn(fallback, judge_prompts[0])
        self.assertIn("independent", judge_prompts[0].lower())

    def test_panel_min_successes_uses_actual_model_diversity(self) -> None:
        """A single fallback model cannot satisfy a two-model panel minimum."""
        anti = load_anti()
        fallback = "openrouter:nvidia/nemotron-3-ultra-550b-a55b:free"
        requested = {"claude-opus-4-6-thinking", "gemini-3.5-flash-high", fallback}
        anti.fetch_model_ids = lambda base_url, *, timeout, token_env: set(requested)
        judge_called = False

        def fake_post_response(**kwargs):
            nonlocal judge_called
            if "You are synthesizing an Antigravity multi-model advisory panel" in kwargs["prompt"]:
                judge_called = True
                return "judge-output"
            if kwargs["model"] in {"claude-opus-4-6-thinking", "gemini-3.5-flash-high"}:
                raise anti.AntiError("HTTP 502: requested backend unavailable retryable=true")
            return "nemotron-output"

        anti.post_response = fake_post_response
        stderr = io.StringIO()
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            rc = anti.main(
                [
                    "panel",
                    "--mode",
                    "ask",
                    "--model",
                    "opus",
                    "--model",
                    "flash-high",
                    "--model",
                    "nemotron-ultra",
                    "--judge",
                    "opus",
                    "--prompt",
                    "What next?",
                    "--fallback-model",
                    "nemotron-ultra",
                    "--fallback-policy",
                    "on-retryable",
                    "--min-successes",
                    "2",
                    "--json",
                ]
            )

        self.assertEqual(rc, 1)
        self.assertFalse(judge_called, "a panel below the distinct-model minimum must not be synthesized")
        self.assertIn("below --min-successes 2", stderr.getvalue())
        self.assertIn("distinct", stderr.getvalue().lower())
        parsed = json.loads(stdout.getvalue())
        self.assertEqual(parsed["metadata"]["status"], "degraded_single_model")
        self.assertIn("below --min-successes 2", parsed["metadata"]["panel_error"])
        self.assertEqual(parsed["panel_results"][0]["actualModel"], fallback)
        self.assertTrue(parsed["panel_results"][0]["fallbackChain"])

    def test_panel_failed_fallback_keeps_both_errors_and_identity(self) -> None:
        anti = load_anti()
        fallback = "gemini-3.7-flash"
        anti.fetch_model_ids = lambda base_url, *, timeout, token_env: {
            "claude-opus-4-6-thinking",
            "claude-sonnet-4-6",
            fallback,
        }
        judge_prompts: list[str] = []

        def fake_post_response(**kwargs):
            prompt = kwargs["prompt"]
            if "You are synthesizing an Antigravity multi-model advisory panel" in prompt:
                judge_prompts.append(prompt)
                return json.dumps(
                    {
                        "summary": "ok",
                        "disagreements": [],
                        "findings": [],
                        "unverifiable": [],
                        "recommended_next_actions": [],
                        "caveats": [],
                    }
                )
            if kwargs["model"] == "claude-opus-4-6-thinking":
                raise anti.AntiError("HTTP 502: opus unavailable retryable=true")
            if kwargs["model"] == fallback:
                raise anti.AntiError("HTTP 503: flash unavailable retryable=true")
            return "sonnet lane output"

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
                    "--model",
                    "sonnet",
                    "--judge",
                    "sonnet",
                    "--fallback-model",
                    "flash-high",
                    "--fallback-policy",
                    "on-retryable",
                    "--min-successes",
                    "1",
                    "--prompt",
                    "What next?",
                    "--json",
                ]
            )

        self.assertEqual(rc, 0, output.getvalue())
        parsed = json.loads(output.getvalue())
        failed = parsed["panel_results"][0]
        self.assertEqual(failed["requestedModel"], "claude-opus-4-6-thinking")
        self.assertIsNone(failed["actualModel"])
        self.assertEqual(failed["fallbackChain"], ["claude-opus-4-6-thinking", fallback])
        self.assertIn("opus unavailable", failed["primaryError"])
        self.assertIn("flash unavailable", failed["fallbackError"])
        self.assertEqual(failed["modelIdentity"]["status"], "failed")
        self.assertIn("flash unavailable", judge_prompts[0])

    def test_panel_judge_requested_and_actual_identity_are_separate(self) -> None:
        anti = load_anti()
        fallback = "openrouter:nvidia/nemotron-3-ultra-550b-a55b:free"
        anti.fetch_model_ids = lambda base_url, *, timeout, token_env: {
            "claude-opus-4-6-thinking",
            "claude-sonnet-4-6",
            fallback,
        }

        def fake_post_response(**kwargs):
            prompt = kwargs["prompt"]
            if "You are synthesizing an Antigravity multi-model advisory panel" in prompt:
                if kwargs["model"] == "claude-opus-4-6-thinking":
                    raise anti.AntiError("HTTP 502: judge unavailable retryable=true")
                return json.dumps(
                    {
                        "summary": "ok",
                        "disagreements": [],
                        "findings": [],
                        "unverifiable": [],
                        "recommended_next_actions": [],
                        "caveats": [],
                    }
                )
            return "sonnet lane output"

        anti.post_response = fake_post_response
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            rc = anti.main(
                [
                    "panel",
                    "--mode",
                    "ask",
                    "--model",
                    "sonnet",
                    "--judge",
                    "opus",
                    "--fallback-model",
                    "nemotron-ultra",
                    "--fallback-policy",
                    "on-retryable",
                    "--min-successes",
                    "1",
                    "--prompt",
                    "What next?",
                    "--json",
                ]
            )

        self.assertEqual(rc, 0, output.getvalue())
        parsed = json.loads(output.getvalue())
        self.assertEqual(parsed["judge_model"], "claude-opus-4-6-thinking")
        metadata = parsed["metadata"]
        self.assertEqual(metadata["judge_requested_model"], "claude-opus-4-6-thinking")
        self.assertEqual(metadata["judge_actual_model"], fallback)
        self.assertEqual(metadata["judge_fallback_chain"], ["claude-opus-4-6-thinking", fallback])
        self.assertIn("judge unavailable", metadata["judge_primary_error"])
        self.assertTrue(any("Judge fallback" in caveat for caveat in parsed["caveats"]))

    def test_panel_model_failure_is_metadata_when_min_successes_met(self) -> None:
        anti = load_anti()
        anti.fetch_model_ids = lambda base_url, *, timeout, token_env: {"claude-sonnet-4-6", "claude-opus-4-6-thinking"}

        def fake_post_response(**kwargs):
            if kwargs["model"] == "claude-sonnet-4-6":
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
        anti.fetch_model_ids = lambda base_url, *, timeout, token_env: {"claude-sonnet-4-6", "claude-opus-4-6-thinking"}

        def fake_post_response(**kwargs):
            if kwargs["model"] == "claude-sonnet-4-6":
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
        anti.fetch_model_ids = lambda base_url, *, timeout, token_env: {"claude-sonnet-4-6"}
        anti.post_response = lambda **kwargs: self.fail("panel should validate models before generation")
        output = io.StringIO()

        with contextlib.redirect_stderr(output):
            rc = anti.main(["panel", "--mode", "ask", "--prompt", "x", "--model", "opus", "--judge", "sonnet"])

        self.assertEqual(rc, 1)
        self.assertIn("not advertised", output.getvalue())

    def test_panel_missing_model_becomes_failed_entry_when_min_successes_met(self) -> None:
        anti = load_anti()
        anti.fetch_model_ids = lambda base_url, *, timeout, token_env: {"claude-sonnet-4-6"}

        def fake_post_response(**kwargs):
            self.assertEqual(kwargs["model"], "claude-sonnet-4-6")
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

    def test_provider_alias_missing_from_catalog_is_explicit_failed_lane(self) -> None:
        anti = load_anti()
        anti.fetch_model_ids = lambda base_url, *, timeout, token_env: {
            "claude-sonnet-4-6",
            "deepseek:deepseek-v4-pro",
        }

        def fake_post_response(**kwargs):
            self.assertIn(kwargs["model"], {"claude-sonnet-4-6", "deepseek:deepseek-v4-pro"})
            if "You are synthesizing an Antigravity multi-model advisory panel" in kwargs["prompt"]:
                return "judge-output"
            return "deepseek-panel-output"

        anti.post_response = fake_post_response
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            rc = anti.main(
                [
                    "panel",
                    "--mode",
                    "ask",
                    "--prompt",
                    "compare",
                    "--model",
                    "deepseek-v4-pro",
                    "--model",
                    "grok-bluesminds",
                    "--judge",
                    "sonnet",
                    "--min-successes",
                    "1",
                    "--json",
                ]
            )

        self.assertEqual(rc, 0, output.getvalue())
        parsed = json.loads(output.getvalue())
        statuses = {item["model"]: item for item in parsed["panel_results"]}
        self.assertEqual(statuses["deepseek:deepseek-v4-pro"]["status"], "success")
        self.assertEqual(statuses["bluesminds:grok-4.5"]["status"], "error")
        self.assertIn("not advertised by /v1/models", statuses["bluesminds:grok-4.5"]["error"])

    def test_run_record_preserves_provider_identity_but_redacts_credentials(self) -> None:
        anti = load_anti()
        secret = "sk-antitest-provider-secret-1234567890"
        with tempfile.TemporaryDirectory(prefix="anti-runs-") as tmp:
            anti.RUNS_DIR = Path(tmp)
            args = anti.build_parser().parse_args(
                ["consult", "--prompt", "x", "--save-output", "summary"]
            )
            anti.write_run_record(
                args,
                mode="consult",
                status="failed",
                models=["bluesminds:grok-4.5", "deepseek:deepseek-v4-pro"],
                metadata={"provider_error": f"api_key={secret}"},
                error=f"Authorization: Bearer {secret}",
            )

            record_text = next(Path(tmp).glob("*.json")).read_text(encoding="utf-8")
            record = json.loads(record_text)

        self.assertEqual(
            record["models"],
            ["bluesminds:grok-4.5", "deepseek:deepseek-v4-pro"],
        )
        self.assertNotIn(secret, record_text)
        self.assertIn("<redacted>", record_text)

    def test_panel_missing_judge_model_still_fails_before_generation(self) -> None:
        anti = load_anti()
        anti.fetch_model_ids = lambda base_url, *, timeout, token_env: {"claude-sonnet-4-6"}
        anti.post_response = lambda **kwargs: self.fail("panel should validate the judge before generation")
        output = io.StringIO()

        with contextlib.redirect_stderr(output):
            rc = anti.main(["panel", "--mode", "ask", "--prompt", "x", "--model", "sonnet", "--judge", "opus"])

        self.assertEqual(rc, 1)
        self.assertIn("not advertised", output.getvalue())

    def test_panel_below_min_successes_writes_single_failed_record_with_partial_results(self) -> None:
        anti = load_anti()
        anti.fetch_model_ids = lambda base_url, *, timeout, token_env: {"claude-sonnet-4-6", "claude-opus-4-6-thinking"}

        def fake_post_response(**kwargs):
            if kwargs["model"] == "claude-sonnet-4-6":
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
            self.assertEqual(statuses["claude-sonnet-4-6"], "error")
            self.assertEqual(statuses["claude-opus-4-6-thinking"], "success")
            success_entry = next(item for item in panel_results if item["status"] == "success")
            self.assertNotIn("output_text", success_entry)
            self.assertIn("opus-panel-output", success_entry["output_preview"])

    def test_panel_synthesis_prompt_is_bounded(self) -> None:
        anti = load_anti()
        anti.fetch_model_ids = lambda base_url, *, timeout, token_env: {"claude-sonnet-4-6", "claude-opus-4-6-thinking"}
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
        anti.fetch_model_ids = lambda base_url, *, timeout, token_env: {"claude-sonnet-4-6", "claude-opus-4-6-thinking"}
        panel_prompts: list[str] = []

        def fake_post_response(**kwargs):
            prompt = kwargs["prompt"]
            if "Chunked Review Manifest" in prompt:
                return "bounded summary " * 5000
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

    def test_default_claude_panel_review_summarizes_before_large_fanout(self) -> None:
        anti = load_anti()
        anti.fetch_model_ids = lambda base_url, *, timeout, token_env: {"claude-sonnet-4-6", "claude-opus-4-6-thinking"}
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
            (root / "large.py").write_text("LARGE = '" + ("x" * 45_000) + "'\n", encoding="utf-8")
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
                            "--json",
                        ]
                    )
            finally:
                os.chdir(old_cwd)

        self.assertEqual(rc, 0, output.getvalue())
        self.assertTrue(panel_prompts)
        parsed = json.loads(output.getvalue())
        self.assertEqual(parsed["metadata"]["panel_review_context"], "chunked-summary")
        self.assertEqual(parsed["metadata"]["prompt_budget_chars"], anti.CLAUDE_SAFE_PROMPT_CHARS)
        self.assertTrue(parsed["metadata"]["claude_prompt_guardrail"])
        self.assertTrue(all(len(prompt) <= anti.CLAUDE_SAFE_PROMPT_CHARS for prompt in panel_prompts))
        self.assertTrue(any("Claude safety budget" in caveat for caveat in parsed["caveats"]))


if __name__ == "__main__":
    unittest.main()


class ConsultFileContextTests(unittest.TestCase):
    """Tests for consult file context pre-reading functionality."""

    def test_extract_file_paths_from_prompt_absolute_paths(self) -> None:
        anti = load_anti()
        prompt = 'Review /Users/reidar/Documents/RSHelper/src/rshelper/ (api.py, models.py)'
        paths = anti.extract_file_paths_from_prompt(prompt)
        self.assertEqual(paths, [
            '/Users/reidar/Documents/RSHelper/src/rshelper/api.py',
            '/Users/reidar/Documents/RSHelper/src/rshelper/models.py',
        ])

    def test_extract_file_paths_from_prompt_no_paths(self) -> None:
        anti = load_anti()
        prompt = 'What is the best way to implement a cache?'
        paths = anti.extract_file_paths_from_prompt(prompt)
        self.assertEqual(paths, [])

    def test_extract_file_paths_from_prompt_extensionless_names(self) -> None:
        anti = load_anti()
        self.assertEqual(anti.extract_file_paths_from_prompt("See Dockerfile for details"), ["Dockerfile"])
        self.assertEqual(anti.extract_file_paths_from_prompt("run Makefile then test"), ["Makefile"])
        self.assertEqual(anti.extract_file_paths_from_prompt("check .gitignore entries"), [".gitignore"])

    def test_extract_file_paths_from_prompt_home_relative(self) -> None:
        anti = load_anti()
        prompt = 'Check ~/project/main.py'
        paths = anti.extract_file_paths_from_prompt(prompt)
        self.assertEqual(paths, ['~/project/main.py'])

    def test_build_consult_file_context_reads_file(self) -> None:
        anti = load_anti()
        test_file = Path.cwd() / "_anti_consult_test_file.py"
        try:
            test_file.write_text("print('hello')\n", encoding="utf-8")
            test_file_rel = "./_anti_consult_test_file.py"
            
            prompt = f'Review {test_file_rel}'
            enhanced, caveats, read_files = anti.build_consult_file_context(prompt, 120_000)
            
            self.assertEqual(read_files, [test_file_rel])
            self.assertEqual(caveats, [])
            self.assertIn("print('hello')", enhanced)
            self.assertIn("## File Contents", enhanced)
            self.assertIn("## User Request", enhanced)
        finally:
            test_file.unlink(missing_ok=True)

    def test_build_consult_file_context_missing_file(self) -> None:
        anti = load_anti()
        prompt = 'Review ./_nonexistent_file.py'
        enhanced, caveats, read_files = anti.build_consult_file_context(prompt, 120_000)
        
        self.assertEqual(read_files, [])
        self.assertEqual(enhanced, prompt)
        self.assertTrue(any("File not found" in c for c in caveats))

    def test_build_consult_file_context_no_files(self) -> None:
        anti = load_anti()
        prompt = 'What is the best way to implement a cache?'
        enhanced, caveats, read_files = anti.build_consult_file_context(prompt, 120_000)
        
        self.assertEqual(read_files, [])
        self.assertEqual(caveats, [])
        self.assertEqual(enhanced, prompt)

    def test_build_consult_file_context_budget_exceeded(self) -> None:
        anti = load_anti()
        test_file = Path.cwd() / "_anti_consult_large.py"
        try:
            test_file.write_text("x = 1\n" * 30_000, encoding="utf-8")
            test_file_rel = "./_anti_consult_large.py"
            
            prompt = f'Review {test_file_rel}'
            enhanced, caveats, read_files = anti.build_consult_file_context(prompt, 100)
            
            self.assertEqual(read_files, [])
            self.assertEqual(enhanced, prompt)
            self.assertTrue(any("exceeds max" in c for c in caveats))
        finally:
            test_file.unlink(missing_ok=True)

    def test_build_consult_file_context_rejects_symlink(self) -> None:
        anti = load_anti()
        with tempfile.TemporaryDirectory(prefix="anti-test-") as tmp:
            root = Path(tmp)
            target = root / "real.py"
            target.write_text("print('real')", encoding="utf-8")
            link = root / "link.py"
            link.symlink_to(target)
            
            prompt = f'Review {link}'
            enhanced, caveats, read_files = anti.build_consult_file_context(prompt, 120_000)
            
            self.assertEqual(read_files, [])
            self.assertTrue(any("Skipped symlink" in c for c in caveats))

    def test_extract_file_paths_from_prompt_backtick_paths(self) -> None:
        anti = load_anti()
        prompt = 'Review `/path/to/file.py` and check `/other/config.toml`'
        paths = anti.extract_file_paths_from_prompt(prompt)
        self.assertIn('/path/to/file.py', paths)
        self.assertIn('/other/config.toml', paths)


class PostResponseGuardTests(unittest.TestCase):
    """Tests for model-level failure detection in post_response (status 'failed' and empty output)."""

    def _make_failed_response(self) -> dict:
        return {
            "id": "resp_test_fail",
            "model": "gemini-3.5-flash-high",
            "status": "failed",
            "error": {"message": "The provider returned no meaningful output."},
            "output": [{"type": "message", "content": [{"type": "output_text", "text": ""}]}],
        }

    def _make_empty_output_response(self) -> dict:
        return {
            "id": "resp_test_empty",
            "model": "gemini-3.5-flash-high",
            "status": "completed",
            "output": [{"type": "message", "content": [
                {"type": "output_text", "text": ""},
                {"type": "output_text", "text": "   "},
            ]}],
        }

    def test_post_response_raises_on_status_failed(self) -> None:
        anti = load_anti()
        model_ids = {"gemini-3.5-flash-high"}
        anti.fetch_model_ids = lambda *a, **kw: model_ids
        anti.request_json = lambda *a, **kw: (200, self._make_failed_response())
        try:
            anti.post_response(
                base_url="http://x", model="gemini-3.5-flash-high",
                prompt="x", max_output_tokens=100, timeout=5, token_env="",
                retries=0, model_ids=model_ids,
            )
            self.fail("should have raised AntiError")
        except anti.AntiError as exc:
            self.assertIn("status 'failed'", str(exc))
            self.assertIn("no meaningful output", str(exc))

    def test_post_response_raises_on_empty_output_content(self) -> None:
        anti = load_anti()
        model_ids = {"gemini-3.5-flash-high"}
        anti.fetch_model_ids = lambda *a, **kw: model_ids
        anti.request_json = lambda *a, **kw: (200, self._make_empty_output_response())
        try:
            anti.post_response(
                base_url="http://x", model="gemini-3.5-flash-high",
                prompt="x", max_output_tokens=100, timeout=5, token_env="",
                retries=0, model_ids=model_ids,
            )
            self.fail("should have raised AntiError for empty output")
        except anti.AntiError as exc:
            self.assertIn("empty output", str(exc))

    def test_post_response_happy_path_unchanged(self) -> None:
        anti = load_anti()
        model_ids = {"gemini-3.5-flash-high"}
        anti.fetch_model_ids = lambda *a, **kw: model_ids
        anti.request_json = lambda *a, **kw: (200, {
            "id": "r", "model": "gemini-3.5-flash-high", "status": "completed",
            "output": [{"type": "message", "content": [{"type": "output_text", "text": "Good review."}]}],
        })
        result = anti.post_response(
            base_url="http://x", model="gemini-3.5-flash-high",
            prompt="x", max_output_tokens=100, timeout=5, token_env="",
            retries=0, model_ids=model_ids,
        )
        self.assertIn("Good review", str(result))


class ErrorRetryableTests(unittest.TestCase):
    """Tests for error_is_retryable covering status 'failed' pattern."""

    def test_error_is_retryable_matches_status_failed(self) -> None:
        anti = load_anti()
        self.assertTrue(anti.error_is_retryable(
            "model gemini-3.5-flash-high returned status 'failed': The provider returned no meaningful output."
        ))
        self.assertTrue(anti.error_is_retryable(
            "model x returned status 'failed': "
        ))

    def test_error_is_retryable_does_not_match_ordinary_errors(self) -> None:
        anti = load_anti()
        self.assertFalse(anti.error_is_retryable("some random error"))


class ConsultFileContextWorkspaceTests(unittest.TestCase):
    """Tests for workspace-boundary enforcement in build_consult_file_context."""

    def test_rejects_file_outside_workspace(self) -> None:
        anti = load_anti()
        import tempfile
        with tempfile.TemporaryDirectory(prefix="anti-outside-") as tmp:
            outside = Path(tmp) / "secret.py"
            outside.write_text("secret stuff", encoding="utf-8")
            prompt = f'Review {outside}'
            enhanced, caveats, read_files = anti.build_consult_file_context(prompt, 120_000)
            self.assertEqual(read_files, [])
            self.assertEqual(enhanced, prompt)
            self.assertTrue(any("outside workspace" in c for c in caveats))

    def test_accepts_file_in_workspace(self) -> None:
        anti = load_anti()
        test_file = Path.cwd() / "_anti_workspace_test.py"
        try:
            test_file.write_text("print('ok')", encoding="utf-8")
            prompt = f'Review ./_anti_workspace_test.py'
            enhanced, caveats, read_files = anti.build_consult_file_context(prompt, 120_000)
            self.assertEqual(read_files, ["./_anti_workspace_test.py"])
            self.assertIn("print('ok')", enhanced)
        finally:
            test_file.unlink(missing_ok=True)


class RunGitTimeoutTests(unittest.TestCase):
    """Tests for git timeout in run_git."""

    def test_run_git_has_timeout_and_reports_errors(self) -> None:
        anti = load_anti()
        import tempfile, subprocess as sp
        with tempfile.TemporaryDirectory(prefix="anti-git-test-") as tmp:
            root = Path(tmp)
            sp.run(["git", "init"], cwd=root, capture_output=True)
            # Normal git operation should complete within 60s
            output = anti.run_git(root, ["rev-parse", "--show-toplevel"])
            self.assertTrue(output.strip())
            # Verify timeout is enforced by checking the function's subprocess.run call
            import inspect
            source = inspect.getsource(anti.run_git)
            self.assertIn("timeout=60", source)


class WorkflowFallbackPolicyTests(unittest.TestCase):
    """Tests for correct fallback policy handling in workflow expansion."""

    def test_plan_deep_respects_never_fallback_policy(self) -> None:
        anti = load_anti()
        parser = anti.build_parser()
        args = parser.parse_args([
            "workflow", "plan-deep", "--fallback-policy", "never",
            "--progress", "--no-progress",
        ])
        expanded = anti.workflow_expansion(args)
        # Plan-deep should NOT have added --fallback-policy on-retryable
        policy_indices = [i for i, v in enumerate(expanded) if v == "--fallback-policy"]
        if policy_indices:
            last_policy = expanded[policy_indices[-1] + 1]
            self.assertEqual(last_policy, "never")
class BugfixRegressionTests(unittest.TestCase):
    """Regression tests for the 2026-08-05 anti bug report (B1-B10)."""

    # --- B1: partial review scope must fail loudly unless --allow-partial ---

    def test_review_partial_scope_fails_preflight_without_allow_partial(self) -> None:
        anti = load_anti()
        anti.generate_with_fallback = lambda **kwargs: self.fail("no model call before the partial guard")
        with tempfile.TemporaryDirectory(prefix="anti-skill-test-") as tmp:
            root = Path(tmp)
            for index in range(12):
                (root / f"file{index:02d}.py").write_text("VALUE = '" + ("x" * 6000) + "'\n", encoding="utf-8")
            old_cwd = Path.cwd()
            try:
                os.chdir(root)
                args = anti.build_parser().parse_args(
                    ["review", "--scope", "files", "--file", "file00.py",
                     "--file", "file01.py", "--file", "file02.py", "--file", "file03.py",
                     "--file", "file04.py", "--file", "file05.py", "--file", "file06.py",
                     "--file", "file07.py", "--file", "file08.py", "--file", "file09.py",
                     "--file", "file10.py", "--file", "file11.py",
                     "--max-prompt-chars", "30000", "--max-review-chunks", "2"]
                )
                with self.assertRaises(anti.AntiError) as raised:
                    anti.command_review(args)
            finally:
                os.chdir(old_cwd)

        message = str(raised.exception)
        self.assertIn("--allow-partial", message)
        self.assertIn("would be omitted", message)

    def test_review_partial_scope_runs_with_allow_partial(self) -> None:
        anti = load_anti()
        calls: list[str] = []

        def fake_generate(args, *, model, prompt, purpose, **kwargs):
            calls.append(purpose)
            return "chunk-or-synthesis", model, {"usage": {"input_tokens": 1, "output_tokens": 2, "total_tokens": 3}}

        anti.generate_with_fallback = fake_generate
        with tempfile.TemporaryDirectory(prefix="anti-skill-test-") as tmp:
            root = Path(tmp)
            for index in range(12):
                (root / f"file{index:02d}.py").write_text("VALUE = '" + ("x" * 6000) + "'\n", encoding="utf-8")
            old_cwd = Path.cwd()
            output = io.StringIO()
            try:
                os.chdir(root)
                with contextlib.redirect_stdout(output):
                    rc = anti.main(
                        ["review", "--scope", "files",
                         *sum((["--file", f"file{i:02d}.py"] for i in range(12)), []),
                         "--max-prompt-chars", "30000", "--max-review-chunks", "2",
                         "--allow-partial", "--json"]
                    )
            finally:
                os.chdir(old_cwd)

        self.assertEqual(rc, 0, output.getvalue())
        self.assertTrue(calls)
        parsed = json.loads(output.getvalue())
        self.assertEqual(parsed["metadata"]["status"], "incomplete")
        self.assertTrue(parsed["metadata"]["omitted_files"])
        self.assertGreater(parsed["metadata"]["omitted_chunk_count"], 0)
        self.assertEqual(parsed["metadata"]["scopeStatus"], "partial")
        self.assertIn("⚠ INCOMPLETE", parsed["output_text"])

    def test_review_zero_max_chunks_reviews_everything(self) -> None:
        anti = load_anti()
        with tempfile.TemporaryDirectory(prefix="anti-skill-test-") as tmp:
            root = Path(tmp)
            for index in range(8):
                (root / f"file{index}.py").write_text("VALUE = '" + ("x" * 30000) + "'\n", encoding="utf-8")
            old_cwd = Path.cwd()
            try:
                os.chdir(root)
                args = anti.build_parser().parse_args(
                    ["review", "--scope", "files",
                     *sum((["--file", f"file{i}.py"] for i in range(8)), []),
                     "--max-prompt-chars", "30000", "--max-review-chunks", "0", "--dry-run"]
                )
                prompt_budget = anti.prompt_budget_for_model(args, anti.resolve_model(args.model, default=anti.DEFAULT_REVIEW_MODEL))
                context = anti.collect_review_context(args)
                chunks, metadata = anti.build_review_chunk_prompts(
                    context, max_prompt_chars=prompt_budget, max_chunks=0
                )
            finally:
                os.chdir(old_cwd)

        self.assertGreaterEqual(len(chunks), 8)
        self.assertEqual(metadata["omitted_items"], [])
        self.assertEqual(metadata["status"], "complete")
        self.assertEqual(metadata["planned_chunk_count"], len(chunks))

    # --- B2: full diff is chunked, never silently truncated ---

    def test_diff_review_chunks_full_diff_without_truncation(self) -> None:
        anti = load_anti()
        diff = "".join(f"@@ -{i} +{i} @@\n- old line {i}\n+ new line {i}\n" for i in range(1500))
        context = {
            "scope_line": "diff (origin/main...HEAD)",
            "diff": diff,
            "file_texts": [],
            "excluded": [],
            "caveats": [],
        }
        chunks, metadata = anti.build_review_chunk_prompts(
            context, max_prompt_chars=30000, max_chunks=8
        )
        self.assertGreaterEqual(len(chunks), 2)
        self.assertEqual(metadata["status"], "complete")
        self.assertEqual(metadata["omitted_items"], [])
        self.assertTrue(all(len(chunk["prompt"]) <= 30000 for chunk in chunks))
        total_prompt_chars = sum(chunk["prompt_chars"] for chunk in chunks)
        self.assertGreaterEqual(total_prompt_chars, len(diff))

    def test_diff_review_marks_incomplete_when_cap_cuts_diff_parts(self) -> None:
        anti = load_anti()
        diff = "".join(f"@@ -{i} +{i} @@\n- old line {i}\n+ new line {i}\n" for i in range(1500))
        context = {
            "scope_line": "diff (origin/main...HEAD)",
            "diff": diff,
            "file_texts": [],
            "excluded": [],
            "caveats": [],
        }
        chunks, metadata = anti.build_review_chunk_prompts(
            context, max_prompt_chars=30000, max_chunks=1
        )
        self.assertEqual(len(chunks), 1)
        self.assertEqual(metadata["status"], "incomplete")
        self.assertTrue(any(str(item).startswith("diff part 2/") for item in metadata["omitted_items"]))
        self.assertGreater(metadata["omitted_chunk_count"], 0)

    # --- B3: catalog alias normalization, suggestions, smoke drift ---

    def test_post_response_fuzzy_matches_double_prefixed_catalog(self) -> None:
        anti = load_anti()
        sent: list[str] = []
        anti.fetch_model_ids = lambda base_url, *, timeout, token_env: {
            "openrouter:openrouter/nvidia/nemotron-3-ultra-550b-a55b:free"
        }

        def fake_request_json(method, url, *, payload=None, timeout=10.0, token_env=anti.DEFAULT_TOKEN_ENV):
            if method == "GET":
                return 200, {"data": [{"id": "openrouter:openrouter/nvidia/nemotron-3-ultra-550b-a55b:free"}]}
            sent.append(payload["model"])
            return 200, {"output": [{"content": [{"type": "output_text", "text": "ok"}]}]}

        anti.request_json = fake_request_json
        text = anti.post_response(
            base_url="http://127.0.0.1:51122/v1",
            model="openrouter:nvidia/nemotron-3-ultra-550b-a55b:free",
            prompt="x",
            max_output_tokens=10,
            timeout=5,
            token_env=anti.DEFAULT_TOKEN_ENV,
        )
        self.assertEqual(text, "ok")
        self.assertEqual(sent, ["openrouter:openrouter/nvidia/nemotron-3-ultra-550b-a55b:free"])

    def test_unadvertised_model_error_suggests_closest_ids(self) -> None:
        anti = load_anti()
        anti.fetch_model_ids = lambda base_url, *, timeout, token_env: {"claude-opus-4-6-thinking", "gemini-3.5-flash-high"}
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            rc = anti.main(["consult", "--prompt", "x", "--model", "deepseek-v4-pro"])
        self.assertEqual(rc, 1)
        message = stderr.getvalue()
        self.assertIn("Closest advertised", message)
        self.assertIn("gemini-3.5-flash-high", message)

    def test_catalog_normalization_mirrors_gateway_repeated_prefix_rule(self) -> None:
        anti = load_anti()
        cases = {
            "openrouter:nvidia/nemotron-3-ultra-550b-a55b:free": "openrouter:nvidia/nemotron-3-ultra-550b-a55b:free",
            "openrouter:openrouter/nvidia/nemotron-3-ultra-550b-a55b:free": "openrouter:nvidia/nemotron-3-ultra-550b-a55b:free",
            "openrouter:openrouter/openrouter/nvidia/nemotron-3-ultra-550b-a55b:free": "openrouter:nvidia/nemotron-3-ultra-550b-a55b:free",
            "openrouter:openrouter/auto": "openrouter:openrouter/auto",
            "openrouter:google/gemma-4-31b-it:free": "openrouter:google/gemma-4-31b-it:free",
            "claude-opus-4-6-thinking": "claude-opus-4-6-thinking",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(anti.normalize_catalog_model_id(raw), expected)
        # Requested and advertised forms must agree for the same lane.
        self.assertTrue(
            anti.catalog_model_matches(
                "openrouter:openrouter/nvidia/nemotron-3-ultra-550b-a55b:free",
                "openrouter:nvidia/nemotron-3-ultra-550b-a55b:free",
            )
        )

    def test_smoke_check_documented_reports_drift(self) -> None:
        anti = load_anti()
        anti.find_cli = lambda: (["codex-antigravity"], None)
        anti.fetch_model_ids = lambda base_url, *, timeout, token_env: {
            "claude-opus-4-6-thinking",
            "claude-sonnet-4-6",
            "openrouter:openrouter/nvidia/nemotron-3-ultra-550b-a55b:free",
            "openrouter:openrouter/auto",
        }
        anti.fetch_gateway_package_version = lambda base_url, *, timeout, token_env: "1.7.0"
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            rc = anti.main(["smoke", "--skip-doctor", "--check-documented", "--json"])

        self.assertEqual(rc, 0, output.getvalue())
        parsed = json.loads(output.getvalue())
        check_names = [check["name"] for check in parsed["checks"]]
        self.assertIn("documented-models", check_names)
        self.assertIn("catalog-prefix", check_names)
        drift = next(check for check in parsed["checks"] if check["name"] == "documented-models")
        self.assertIn("deepseek:deepseek-v4-pro", drift["missing"])
        self.assertIn("deepseek:deepseek-v4-flash", drift["missing"])
        prefix = next(check for check in parsed["checks"] if check["name"] == "catalog-prefix")
        self.assertIn("openrouter:openrouter/nvidia/nemotron-3-ultra-550b-a55b:free", prefix["ids"])
        # openrouter:openrouter/auto is a legitimate OpenRouter id and must not
        # be reported as upstream-rejected drift.
        self.assertNotIn("openrouter:openrouter/auto", prefix["ids"])

    def test_smoke_requested_model_uses_fuzzy_catalog_matching(self) -> None:
        anti = load_anti()
        anti.find_cli = lambda: (["codex-antigravity"], None)
        anti.fetch_model_ids = lambda base_url, *, timeout, token_env: {
            "claude-opus-4-6-thinking",
            "openrouter:openrouter/nvidia/nemotron-3-ultra-550b-a55b:free",
        }
        anti.fetch_gateway_package_version = lambda base_url, *, timeout, token_env: "1.7.0"
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            rc = anti.main(["smoke", "--skip-doctor", "--model", "nemotron-ultra", "--json"])

        self.assertEqual(rc, 0, output.getvalue())
        parsed = json.loads(output.getvalue())
        model_checks = [check for check in parsed["checks"] if check["name"] == "model"]
        self.assertTrue(model_checks)
        self.assertEqual(model_checks[0]["status"], "pass")

    # --- B4: consult truncation detection, retry, full-output save ---

    def test_consult_truncated_output_retries_and_saves_full_output(self) -> None:
        anti = load_anti()
        caps: list[int] = []

        def fake_post_response(**kwargs):
            caps.append(kwargs["max_output_tokens"])
            return anti.ResponseText(
                "answer that ends mid-sentence without terminal punctuation",
                usage={"input_tokens": 5, "output_tokens": kwargs["max_output_tokens"], "total_tokens": 50},
            )

        anti.post_response = fake_post_response
        with tempfile.TemporaryDirectory(prefix="anti-runs-") as tmp:
            anti.RUNS_DIR = Path(tmp)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                rc = anti.main(
                    ["consult", "--prompt", "hello", "--max-output-tokens", "40",
                     "--save-output", "summary", "--json"]
                )
            parsed = json.loads(output.getvalue())
            record = json.loads(next(Path(tmp).glob("*.json")).read_text(encoding="utf-8"))

        self.assertEqual(rc, 0, output.getvalue())
        self.assertEqual(caps, [40, 80])
        self.assertEqual(parsed["metadata"]["status"], "truncated")
        self.assertTrue(any("truncated at the token cap" in caveat for caveat in parsed["caveats"]))
        self.assertEqual(record["status"], "success")
        self.assertEqual(record["runStatus"], "success")
        self.assertIn("output_text", record)
        self.assertIn("answer that ends mid-sentence", record["output_text"])
        self.assertIn("consult_attempts", record["metadata"])
        self.assertEqual(len(record["metadata"]["consult_attempts"]), 2)

    def test_consult_recovers_on_higher_cap_retry(self) -> None:
        anti = load_anti()
        calls = {"count": 0}

        def fake_post_response(**kwargs):
            calls["count"] += 1
            if calls["count"] == 1:
                return anti.ResponseText(
                    "cut off",
                    usage={"input_tokens": 5, "output_tokens": kwargs["max_output_tokens"], "total_tokens": 50},
                )
            return anti.ResponseText(
                "complete answer with full detail.",
                usage={"input_tokens": 5, "output_tokens": 7, "total_tokens": 12},
            )

        anti.post_response = fake_post_response
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            rc = anti.main(["consult", "--prompt", "hello", "--max-output-tokens", "40", "--json"])

        self.assertEqual(rc, 0, output.getvalue())
        parsed = json.loads(output.getvalue())
        self.assertEqual(calls["count"], 2)
        self.assertNotEqual(parsed["metadata"].get("status"), "truncated")
        self.assertIn("complete answer", parsed["output_text"])

    # --- B5: run-record lifecycle ---

    def test_runs_list_flags_zero_byte_records(self) -> None:
        anti = load_anti()
        with tempfile.TemporaryDirectory(prefix="anti-runs-") as tmp:
            anti.RUNS_DIR = Path(tmp)
            (anti.RUNS_DIR / "20260805T191529Z-4a6eef80.json").write_bytes(b"")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                rc = anti.main(["runs", "list", "--json"])

            self.assertEqual(rc, 0)
            rows = json.loads(output.getvalue())
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["id"], "20260805T191529Z-4a6eef80")
            self.assertEqual(rows[0]["status"], "interrupted")
            self.assertTrue(rows[0]["interrupted"])
            self.assertEqual(rows[0]["size"], 0)

    def test_runs_clean_removes_stale_tmp_files(self) -> None:
        anti = load_anti()
        with tempfile.TemporaryDirectory(prefix="anti-runs-") as tmp:
            anti.RUNS_DIR = Path(tmp)
            tmp_path = anti.RUNS_DIR / "run-1.json.tmp"
            tmp_path.write_text("partial", encoding="utf-8")
            old = time.time() - 3 * 86400
            os.utime(tmp_path, (old, old))
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                rc = anti.main(["runs", "clean", "--older-than", "1"])
            self.assertEqual(rc, 0)
            self.assertFalse(tmp_path.exists())
            self.assertIn("Removed 1", output.getvalue())

    # --- B6: provider identifier redaction ---

    def test_redact_sensitive_text_redacts_provider_identifiers(self) -> None:
        anti = load_anti()
        redacted = anti.redact_sensitive_text(
            '{"error": {"message": "invalid model", "user_id": "user_380iAbCd1x", "request_id": "req_98765"}}'
        )
        self.assertNotIn("user_380iAbCd1x", redacted)
        self.assertNotIn("req_98765", redacted)
        self.assertIn("<redacted>", redacted)
        # Plain code identifiers must not be mangled.
        self.assertEqual(anti.redact_sensitive_text("user_models = load()"), "user_models = load()")
        self.assertEqual(anti.redact_sensitive_text("user_abc123 = value"), "user_abc123 = value")
        self.assertEqual(anti.redact_sensitive_text("user_id == 42"), "user_id == 42")
        # Python type annotations must not be eaten as headers.
        self.assertEqual(
            anti.redact_sensitive_text("request_id: str = \"req-abc\""),
            "request_id: str = \"req-abc\"",
        )
        self.assertEqual(
            anti.redact_sensitive_text("user_id: int = 5"),
            "user_id: int = 5",
        )
        # Real provider-id headers still redact.
        self.assertIn("x-request-id: <redacted>", anti.redact_sensitive_text("x-request-id: abc-123-xyz"))
        # Form/query context redacts too, without touching code comparisons.
        self.assertIn("request_id=<redacted>", anti.redact_sensitive_text("request_id=req_999"))
        self.assertIn("user_id=<redacted>", anti.redact_sensitive_text("user_id=12345"))
        self.assertEqual(anti.redact_sensitive_text("user_id == 42"), "user_id == 42")
        # Repr and numeric provider-id forms redact too.
        self.assertNotIn("abc12345", anti.redact_sensitive_text("{'user_id': 'abc12345'}"))
        self.assertNotIn("req-abc123", anti.redact_sensitive_text("{'request_id': 'req-abc123'}"))
        self.assertIn("<redacted>", anti.redact_sensitive_text('{"user_id": 12345}'))
        # HTTP-like status codes under "code" are preserved, other numbers redact.
        self.assertIn('"code": 200', anti.redact_sensitive_text('{"code": 200, "status": "ok"}'))
        self.assertNotIn("123456", anti.redact_sensitive_text('{"code": 123456}'))
        self.assertIn('"code": "200"', anti.redact_sensitive_text('{"code": "200", "status": "ok"}'))
        self.assertIn("code: 200", anti.redact_sensitive_text("error code: 200"))
        self.assertNotIn("98765", anti.redact_sensitive_text("error code: 98765"))

    # --- B7: run record status schema ---

    def test_run_record_splits_run_and_scope_status(self) -> None:
        anti = load_anti()
        with tempfile.TemporaryDirectory(prefix="anti-runs-") as tmp:
            anti.RUNS_DIR = Path(tmp)
            args = anti.build_parser().parse_args(["consult", "--prompt", "x", "--save-output", "summary"])
            anti.write_run_record(
                args,
                mode="review",
                status="success",
                metadata={
                    "status": "incomplete",
                    "omitted_files": ["src/prices.ts", "src/scanner.ts"],
                    "omitted_chunk_count": 4,
                    "omitted_file_count": 2,
                },
            )
            record = json.loads(next(Path(tmp).glob("*.json")).read_text(encoding="utf-8"))

        self.assertEqual(record["status"], "success")
        self.assertEqual(record["runStatus"], "success")
        self.assertEqual(record["scopeStatus"], "partial")
        self.assertEqual(record["omittedFileCount"], 2)
        self.assertEqual(record["omittedChunkCount"], 4)

    # --- B9: priority files lead the chunk plan ---

    def test_priority_files_are_ordered_first(self) -> None:
        anti = load_anti()
        context = {
            "scope_line": "files",
            "diff": "",
            "file_texts": [
                ("analytics.py", "x" * 200),
                ("artifact-manifest.py", "x" * 200),
                ("prices.ts", "y" * 200),
                ("scanner.ts", "y" * 200),
                ("story.ts", "y" * 200),
            ],
            "excluded": [],
            "caveats": [],
        }
        chunks, _metadata = anti.build_review_chunk_prompts(
            context, max_prompt_chars=30000, max_chunks=2, priority_paths=["prices.ts", "scanner.ts"]
        )
        first_labels = " ".join(chunk["label"] for chunk in chunks)
        self.assertLess(first_labels.index("prices.ts"), first_labels.index("analytics.py"))

    def test_dry_run_review_prints_chunk_plan(self) -> None:
        anti = load_anti()
        with tempfile.TemporaryDirectory(prefix="anti-skill-test-") as tmp:
            root = Path(tmp)
            (root / "large.py").write_text("VALUE = '" + ("x" * 9000) + "'\n", encoding="utf-8")
            old_cwd = Path.cwd()
            stderr = io.StringIO()
            try:
                os.chdir(root)
                with contextlib.redirect_stderr(stderr):
                    rc = anti.main(
                        ["review", "--scope", "files", "--file", "large.py",
                         "--max-prompt-chars", "2400", "--max-review-chunks", "2",
                         "--dry-run", "--print-prompt"]
                    )
            finally:
                os.chdir(old_cwd)

        self.assertEqual(rc, 0)
        self.assertIn("dry-run chunk plan", stderr.getvalue())
        self.assertIn("would be omitted", stderr.getvalue())

    def test_dry_run_never_contacts_gateway_for_review_plan_panel(self) -> None:
        anti = load_anti()

        def fail_gateway_call(*args, **kwargs):
            self.fail("--dry-run must not contact the gateway")

        anti.fetch_model_ids = fail_gateway_call
        anti.post_response = fail_gateway_call
        anti.request_json = fail_gateway_call
        with tempfile.TemporaryDirectory(prefix="anti-skill-test-") as tmp:
            root = Path(tmp)
            (root / "app.py").write_text("print('ok')\n", encoding="utf-8")
            old_cwd = Path.cwd()
            stdout = io.StringIO()
            try:
                os.chdir(root)
                with contextlib.redirect_stdout(stdout):
                    review_rc = anti.main(["review", "--scope", "files", "--file", "app.py", "--dry-run"])
                    plan_rc = anti.main(["plan", "--prompt", "Plan this", "--dry-run"])
                    panel_rc = anti.main(["panel", "--mode", "ask", "--prompt", "Compare", "--dry-run"])
            finally:
                os.chdir(old_cwd)

        self.assertEqual(review_rc, 0)
        self.assertEqual(plan_rc, 0)
        self.assertEqual(panel_rc, 0)
        self.assertIn("[dry-run] review", stdout.getvalue())
        self.assertIn("[dry-run] plan", stdout.getvalue())
        self.assertIn("[dry-run] panel ask", stdout.getvalue())

    def test_chunk_prompts_do_not_carry_stale_single_prompt_diff_caveat(self) -> None:
        anti = load_anti()
        anti.generate_with_fallback = lambda args, **kwargs: (
            "synthesis" if "synthesizing" in kwargs["prompt"] else "chunk"
        )
        calls: list[str] = []

        def fake_generate(args, *, model, prompt, purpose, **kwargs):
            calls.append(prompt)
            return "chunk-or-synthesis", model, {"usage": {"input_tokens": 1, "output_tokens": 2, "total_tokens": 3}}

        anti.generate_with_fallback = fake_generate
        with tempfile.TemporaryDirectory(prefix="anti-skill-test-") as tmp:
            root = Path(tmp)
            (root / "app.py").write_text("print('ok')\n", encoding="utf-8")
            (root / "big.py").write_text("VALUE = '" + ("x" * 6000) + "'\n", encoding="utf-8")
            old_cwd = Path.cwd()
            try:
                os.chdir(root)
                rc = anti.main(
                    ["review", "--scope", "files", "--file", "app.py", "--file", "big.py",
                     "--max-prompt-chars", "2400", "--chunked", "auto"]
                )
            finally:
                os.chdir(old_cwd)

        self.assertEqual(rc, 0)
        chunk_prompts = [prompt for prompt in calls if "Chunked Review Manifest" not in prompt]
        self.assertTrue(chunk_prompts)
        for prompt in chunk_prompts:
            self.assertNotIn("Git diff truncated", prompt)

    def test_consult_default_output_tokens_is_raised(self) -> None:
        anti = load_anti()
        parser = anti.build_parser()
        self.assertEqual(parser.parse_args(["consult", "--prompt", "x"]).max_output_tokens, 4096)

    # --- Second-pass audit findings (opus sidecar + native verification) ---

    def test_signal_handler_installs_on_platforms_without_sighup(self) -> None:
        anti = load_anti()
        # delete=True actually removes the attribute, exercising the
        # getattr(signal, "SIGHUP", None) branch (create=False would raise on
        # platforms that genuinely lack SIGHUP).
        with unittest.mock.patch.object(anti.signal, "SIGHUP", create=True, delete=True):
            with unittest.mock.patch.object(anti.signal, "signal", create=True) as mock_signal:
                args = anti.build_parser().parse_args(["consult", "--prompt", "x", "--save-output", "summary"])
                anti._install_run_signal_handlers(args)
        self.assertGreaterEqual(mock_signal.call_count, 1)

    def test_signal_handler_survives_missing_sighup_attribute(self) -> None:
        anti = load_anti()
        # Simulate Windows: no SIGHUP attribute at all (patch to None leaves
        # the attribute present, so delete=True is required to emulate the
        # missing-attribute platform).
        with unittest.mock.patch.object(anti.signal, "SIGHUP", create=True, delete=True):
            args = anti.build_parser().parse_args(["consult", "--prompt", "x", "--save-output", "summary"])
            anti._install_run_signal_handlers(args)  # must not raise AttributeError

    def test_omitted_file_count_zero_is_not_overridden_by_item_fallback(self) -> None:
        anti = load_anti()
        with tempfile.TemporaryDirectory(prefix="anti-runs-") as tmp:
            anti.RUNS_DIR = Path(tmp)
            args = anti.build_parser().parse_args(["consult", "--prompt", "x", "--save-output", "summary"])
            anti.write_run_record(
                args,
                mode="review",
                status="success",
                metadata={
                    "status": "incomplete",
                    "omitted_files": ["src/big.py part 3/8"],
                    "omitted_file_count": 0,
                    "omitted_chunk_count": 6,
                },
            )
            record = json.loads(next(Path(tmp).glob("*.json")).read_text(encoding="utf-8"))

        self.assertEqual(record["omittedFileCount"], 0)
        self.assertEqual(record["omittedChunkCount"], 6)

    def test_panel_review_summary_keeps_existing_caveats(self) -> None:
        anti = load_anti()
        # CI has no live gateway: without this mock the panel preflight would
        # probe http://127.0.0.1:51122/v1/models and fail with connection
        # refused, so the test must be hermetic like the other panel tests.
        anti.fetch_model_ids = lambda base_url, *, timeout, token_env: {
            "claude-sonnet-4-6", "claude-opus-4-6-thinking",
        }
        anti.generate_with_fallback = lambda args, **kwargs: (
            ("summary", "claude-sonnet-4-6", {"usage": {"input_tokens": 1, "output_tokens": 2, "total_tokens": 3}})
            if "synthesizing" in kwargs["prompt"]
            else ("chunk", "claude-sonnet-4-6", {"usage": {"input_tokens": 1, "output_tokens": 2, "total_tokens": 3}})
        )
        with tempfile.TemporaryDirectory(prefix="anti-skill-test-") as tmp:
            root = Path(tmp)
            (root / "app.py").write_text("print('ok')\n", encoding="utf-8")
            (root / "big.py").write_text("VALUE = '" + ("x" * 6000) + "'\n", encoding="utf-8")
            old_cwd = Path.cwd()
            output = io.StringIO()
            try:
                os.chdir(root)
                with contextlib.redirect_stdout(output):
                    rc = anti.main(
                        ["panel", "--mode", "review", "--scope", "files",
                         "--file", "app.py", "--file", "big.py",
                         "--model", "sonnet", "--judge", "sonnet",
                         "--max-prompt-chars", "2400", "--max-review-chunks", "2",
                         "--allow-partial", "--json"]
                    )
            finally:
                os.chdir(old_cwd)

        self.assertEqual(rc, 0, output.getvalue())
        parsed = json.loads(output.getvalue())
        self.assertTrue(parsed["caveats"])
        self.assertTrue(any("bounded chunked summary" in caveat for caveat in parsed["caveats"]))
        self.assertFalse(any("Git diff truncated" in caveat for caveat in parsed["caveats"]))

    def test_dir_paren_file_pattern_finds_all_matches(self) -> None:
        anti = load_anti()
        prompt = "Look at /repo/src/ (alpha.py, beta.ts) and also /repo/lib/ (gamma.py)"
        paths = anti.extract_file_paths_from_prompt(prompt)
        self.assertIn("/repo/src/alpha.py", paths)
        self.assertIn("/repo/src/beta.ts", paths)
        self.assertIn("/repo/lib/gamma.py", paths)

    def test_estimate_cost_does_not_allocate_prompt_sized_string(self) -> None:
        anti = load_anti()
        estimate = anti.estimate_cost(model="claude-opus-4-6-thinking", prompt_chars=4000)
        self.assertEqual(estimate["estimated_input_tokens"], 1000)

    def test_model_metadata_covers_documented_byok_aliases(self) -> None:
        anti = load_anti()
        for model_id in ("deepseek:deepseek-v4-pro", "deepseek:deepseek-v4-flash",
                         "bluesminds:grok-4.5", "bluesminds:z-ai/glm-5.2"):
            self.assertIn(model_id, anti.MODEL_CAPABILITIES, model_id)
            self.assertEqual(anti.model_cost_tier(model_id), "paid", model_id)
            self.assertGreater(anti.MODEL_QUALITY_RANK.get(model_id, 0), 0, model_id)
            self.assertTrue(anti.model_supports(model_id, "tools"), model_id)

    def test_ollama_models_are_text_only(self) -> None:
        anti = load_anti()
        for model_id in ("ollama:gpt-oss:20b", "ollama:qwen3:8b"):
            self.assertFalse(anti.model_supports(model_id, "images"), model_id)
            self.assertTrue(anti.model_supports(model_id, "tools"), model_id)

    def test_cheapest_models_for_task_resolves_aliases(self) -> None:
        anti = load_anti()
        result = anti.cheapest_models_for_task(available=["opus", "sonnet", "grok", "flash-3.6"])
        # Alias ids must resolve to canonical ids before capability/tier lookup.
        self.assertIn("claude-opus-4-6-thinking", result)
        self.assertIn("claude-sonnet-4-6", result)
        self.assertIn("xai-oauth:grok-build-0.1", result)
        self.assertIn("gemini-3.6-flash-high", result)
        # Free tiers sort first, so grok leads over the quota-tier claude models.
        self.assertLess(result.index("xai-oauth:grok-build-0.1"), result.index("claude-opus-4-6-thinking"))

    def test_base_url_rejects_non_http_schemes(self) -> None:
        anti = load_anti()
        for bad in ("file:///etc/passwd", "ftp://example.com/v1", "gopher://x/v1"):
            with self.subTest(bad=bad):
                with self.assertRaises(anti.AntiError) as raised:
                    anti.normalize_base_url(bad)
                self.assertIn("scheme", str(raised.exception))
        self.assertEqual(anti.normalize_base_url("http://127.0.0.1:51122/v1"), "http://127.0.0.1:51122/v1")

    def test_base_url_rejects_empty_host(self) -> None:
        anti = load_anti()
        with self.assertRaises(anti.AntiError) as raised:
            anti.normalize_base_url("http://")
        self.assertIn("host", str(raised.exception))

    def test_workflow_error_record_reuses_inner_run_id(self) -> None:
        anti = load_anti()
        anti.fetch_model_ids = lambda base_url, *, timeout, token_env: (_ for _ in ()).throw(
            anti.AntiError("gateway unreachable")
        )
        with tempfile.TemporaryDirectory(prefix="anti-skill-test-") as tmp, tempfile.TemporaryDirectory(
            prefix="anti-runs-"
        ) as runs_tmp:
            anti.RUNS_DIR = Path(runs_tmp)
            root = Path(tmp)
            (root / "app.py").write_text("print('ok')\n", encoding="utf-8")
            old_cwd = Path.cwd()
            stderr = io.StringIO()
            try:
                os.chdir(root)
                with contextlib.redirect_stderr(stderr):
                    rc = anti.main(
                        ["workflow", "review-ready", "--scope", "files", "--file", "app.py",
                         "--save-output", "summary"]
                    )
            finally:
                os.chdir(old_cwd)

            self.assertEqual(rc, 1)
            records = list(Path(runs_tmp).glob("*.json"))
            self.assertEqual(len(records), 1, [r.name for r in records])
            record = json.loads(records[0].read_text(encoding="utf-8"))
            self.assertEqual(record["status"], "error")

    def test_workflow_forwards_run_id_and_writes_single_record(self) -> None:
        anti = load_anti()
        anti.fetch_model_ids = lambda base_url, *, timeout, token_env: (_ for _ in ()).throw(
            anti.AntiError("gateway unreachable")
        )
        with tempfile.TemporaryDirectory(prefix="anti-skill-test-") as tmp, tempfile.TemporaryDirectory(
            prefix="anti-runs-"
        ) as runs_tmp:
            anti.RUNS_DIR = Path(runs_tmp)
            root = Path(tmp)
            (root / "app.py").write_text("print('ok')\n", encoding="utf-8")
            old_cwd = Path.cwd()
            try:
                os.chdir(root)
                rc = anti.main(
                    ["workflow", "review-ready", "--scope", "files", "--file", "app.py",
                     "--save-output", "summary", "--run-id", "workflow-run-42"]
                )
            finally:
                os.chdir(old_cwd)

            self.assertEqual(rc, 1)
            records = list(Path(runs_tmp).glob("*.json"))
            self.assertEqual(len(records), 1, [r.name for r in records])
            record = json.loads(records[0].read_text(encoding="utf-8"))
            self.assertEqual(record["id"], "workflow-run-42")
            self.assertEqual(record["status"], "error")

    def test_workflow_expansion_forwards_run_id(self) -> None:
        anti = load_anti()
        parser = anti.build_parser()
        args = parser.parse_args(["workflow", "review-ready", "--scope", "files", "--run-id", "wf-1"])
        expanded = anti.workflow_expansion(args)
        self.assertIn("--run-id", expanded)
        self.assertEqual(expanded[expanded.index("--run-id") + 1], "wf-1")

    def test_workflow_max_review_chunks_accepts_zero(self) -> None:
        anti = load_anti()
        parser = anti.build_parser()
        args = parser.parse_args(["workflow", "review-ready", "--scope", "none", "--max-review-chunks", "0"])
        self.assertEqual(args.max_review_chunks, 0)

    def test_workflow_installs_signal_handlers_on_expanded_args(self) -> None:
        anti = load_anti()
        seen: list[str] = []
        original = anti._install_run_signal_handlers

        def spy(args):
            seen.append(getattr(args, "command", "unknown"))
            return original(args)

        anti._install_run_signal_handlers = spy
        with tempfile.TemporaryDirectory(prefix="anti-runs-") as tmp:
            anti.RUNS_DIR = Path(tmp)
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                rc = anti.main(["workflow", "review-ready", "--scope", "none", "--save-output", "summary"])

        self.assertEqual(rc, 1)
        self.assertIn("workflow", seen)
        self.assertIn("panel", seen)

    def test_run_record_id_is_not_mangled_by_redaction(self) -> None:
        anti = load_anti()
        with tempfile.TemporaryDirectory(prefix="anti-runs-") as tmp:
            anti.RUNS_DIR = Path(tmp)
            args = anti.build_parser().parse_args(
                ["consult", "--prompt", "x", "--save-output", "summary", "--run-id", "user_12345678"]
            )
            anti.write_run_record(
                args,
                mode="consult",
                status="success",
                models=["m"],
                output_text="ok",
                metadata={"request_log_correlation_id": "user_12345678"},
            )
            path = Path(tmp) / "user_12345678.json"
            record = json.loads(path.read_text(encoding="utf-8"))
            self.assertTrue(path.exists())
            self.assertEqual(record["id"], "user_12345678")
            self.assertEqual(record["metadata"]["request_log_correlation_id"], "user_12345678")


    def test_tiny_prompt_budget_diff_scope_fails_closed(self) -> None:
        anti = load_anti()
        diff = "".join(f"@@ -{i} +{i} @@\n- old line {i}\n+ new line {i}\n" for i in range(200))
        context = {
            "scope_line": "diff (origin/main...HEAD)",
            "diff": diff,
            "file_texts": [],
            "excluded": [],
            "caveats": [],
        }
        chunks, metadata = anti.build_review_chunk_prompts(
            context, max_prompt_chars=900, max_chunks=0
        )
        # The diff cannot fit next to scaffolding at 900 chars; the helper
        # must record the omission instead of silently truncating a chunk.
        self.assertEqual(metadata["status"], "incomplete")
        self.assertTrue(metadata["omitted_items"])
        self.assertEqual(chunks, [])

    def test_run_id_validated_even_without_save_output(self) -> None:
        anti = load_anti()
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            rc = anti.main(["consult", "--prompt", "x", "--run-id", "bad id with spaces"])
        self.assertEqual(rc, 1)
        self.assertIn("run id must contain only letters", stderr.getvalue())

    def test_explicit_run_id_is_bound_to_args_and_used_for_records(self) -> None:
        anti = load_anti()
        with tempfile.TemporaryDirectory(prefix="anti-runs-") as tmp:
            anti.RUNS_DIR = Path(tmp)
            args = anti.build_parser().parse_args(
                ["consult", "--prompt", "x", "--run-id", "my-stable-run", "--save-output", "summary"]
            )
            self.assertEqual(anti.ensure_run_id(args), "my-stable-run")
            self.assertEqual(args.run_id, "my-stable-run")
            # The final record must carry the user-supplied id as its filename
            # and correlation id, not a random replacement.
            record_path = anti.RUNS_DIR / "my-stable-run.json"
            self.assertTrue(record_path.exists(), "record must use the explicit run id")
            record = json.loads(record_path.read_text(encoding="utf-8"))
            self.assertEqual(record["id"], "my-stable-run")
            self.assertEqual(record["metadata"]["request_log_correlation_id"], "my-stable-run")

    def test_panel_membership_uses_fuzzy_catalog_matching(self) -> None:
        anti = load_anti()
        anti.fetch_model_ids = lambda base_url, *, timeout, token_env: {
            "claude-opus-4-6-thinking",
            "openrouter:openrouter/nvidia/nemotron-3-ultra-550b-a55b:free",
        }
        seen_models: list[str] = []

        def fake_generate(args, *, model, prompt, max_output_tokens, model_ids, purpose, **kwargs):
            seen_models.append(model)
            return "lane-output", model, {"usage": {"input_tokens": 1, "output_tokens": 2, "total_tokens": 3}}

        anti.generate_with_fallback = fake_generate
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            rc = anti.main(
                ["panel", "--mode", "ask", "--prompt", "Compare",
                 "--model", "nemotron-ultra", "--model", "opus",
                 "--judge", "opus", "--max-output-tokens", "10", "--json"]
            )

        self.assertEqual(rc, 0, output.getvalue())
        parsed = json.loads(output.getvalue())
        self.assertEqual([item["status"] for item in parsed["panel_results"]], ["success", "success"])
        self.assertEqual(
            {item["model"] for item in parsed["panel_results"]},
            {"claude-opus-4-6-thinking", "openrouter:nvidia/nemotron-3-ultra-550b-a55b:free"},
        )
