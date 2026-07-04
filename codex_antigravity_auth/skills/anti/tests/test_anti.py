from __future__ import annotations

import contextlib
import importlib.util
import io
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
                prompt, paths, caveats = anti.assemble_review_prompt(args)
            finally:
                os.chdir(old_cwd)

        self.assertIn("src/app.py", paths)
        self.assertNotIn("secrets/config.json", paths)
        self.assertNotIn("do-not-send", prompt)
        self.assertTrue(any("secrets/config.json" in caveat for caveat in caveats))


if __name__ == "__main__":
    unittest.main()
