from pathlib import Path
import unittest

import yaml

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib


ROOT = Path(__file__).resolve().parents[1]


class TestReleaseWorkflow(unittest.TestCase):
    def setUp(self) -> None:
        self.workflow_text = (ROOT / ".github/workflows/publish.yml").read_text(
            encoding="utf-8"
        )
        self.workflow = yaml.safe_load(self.workflow_text)

    def test_publish_is_gated_by_build_and_full_test_matrix(self):
        jobs = self.workflow["jobs"]
        self.assertIn("test", jobs)
        matrix = jobs["test"]["strategy"]["matrix"]["include"]
        lanes = {(entry["os"], str(entry["python-version"])) for entry in matrix}
        self.assertEqual(
            lanes,
            {
                ("ubuntu-latest", "3.10"),
                ("ubuntu-latest", "3.11"),
                ("ubuntu-latest", "3.12"),
                ("ubuntu-latest", "3.14"),
                ("windows-latest", "3.12"),
            },
        )
        self.assertEqual(set(jobs["publish"]["needs"]), {"build", "test"})

    def test_release_version_and_tag_guard_are_current(self):
        project = tomllib.loads(
            (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        )["project"]
        self.assertEqual(project["version"], "1.7.0")
        self.assertIn("Verify tag matches package version", self.workflow_text)
        self.assertIn('expected = f"v{version}"', self.workflow_text)

    def test_provider_and_anti_docs_cover_explicit_bluesminds_deepseek_and_grok_routes(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        usage = (ROOT / "USAGE.md").read_text(encoding="utf-8")
        skill = (ROOT / "codex_antigravity_auth/skills/anti/SKILL.md").read_text(
            encoding="utf-8"
        )
        combined = "\n".join([readme, usage, skill])

        for required in (
            "bluesminds:grok-4.5",
            "bluesminds:z-ai/glm-5.2",
            "deepseek:deepseek-v4-pro",
            "deepseek:deepseek-v4-flash",
            "grok-oauth",
            "grok-bluesminds",
            "glm-5.2",
            "workflow claude-grok --model sonnet --model opus --model grok-bluesminds",
            "workflow provider-compare",
            "--fallback-model deepseek-v4-flash",
            "Chat Completions adapter",
        ):
            with self.subTest(required=required):
                self.assertIn(required, combined)

        self.assertNotIn("gpt-5.4", combined.lower())

    def test_provider_lane_selection_guidance_is_explicit_and_truthful(self):
        docs = {
            "README.md": (ROOT / "README.md").read_text(encoding="utf-8"),
            "USAGE.md": (ROOT / "USAGE.md").read_text(encoding="utf-8"),
            "bundled Anti skill": (
                ROOT / "codex_antigravity_auth/skills/anti/SKILL.md"
            ).read_text(encoding="utf-8"),
        }
        required_guidance = (
            "fast code second opinion",
            "correctness, security, architecture, and deep code review",
            "unproven until",
            "adversarial assumptions, runtime surprises, and product/UX blind spots",
            "unavailable/degraded until",
            "explicit selection",
            "BYOK disclosure",
            "/v1/models",
            "Opus remains the default judge",
        )
        misleading = "workflow claude-grok --model grok-bluesminds"
        corrected = (
            "workflow claude-grok --model sonnet --model opus "
            "--model grok-bluesminds"
        )

        for name, text in docs.items():
            with self.subTest(document=name):
                for phrase in required_guidance:
                    self.assertIn(phrase, text)
                self.assertNotIn(misleading, text)
                self.assertIn(corrected, text)
                self.assertNotIn("gpt-5.4", text.lower())


if __name__ == "__main__":
    unittest.main()
