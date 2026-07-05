from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import subprocess
import tempfile
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


if __name__ == "__main__":
    unittest.main()
