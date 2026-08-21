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
        self.assertEqual(project["version"], "1.8.1")
        self.assertIn("Verify tag matches package version", self.workflow_text)
        self.assertIn('expected = f"v{version}"', self.workflow_text)

if __name__ == "__main__":
    unittest.main()


class TestAntiSkillDocumentation(unittest.TestCase):
    """M-3: Verify anti skill SKILL.md contains expected sections and features."""

    def setUp(self) -> None:
        self.skill_path = ROOT / "codex_antigravity_auth" / "skills" / "anti" / "SKILL.md"
        self.skill_text = self.skill_path.read_text(encoding="utf-8") if self.skill_path.exists() else ""

    def test_skill_md_exists(self):
        self.assertTrue(self.skill_path.exists(), f"SKILL.md not found at {self.skill_path}")

    def test_findings_schema_section(self):
        self.assertIn("## Findings Schema", self.skill_text)
        self.assertIn("fingerprint", self.skill_text)
        self.assertIn("confidence", self.skill_text)
        self.assertIn("evidence", self.skill_text)

    def test_anonymized_panel_section(self):
        self.assertIn("## Anonymized Panel Judging", self.skill_text)
        self.assertIn("--no-anonymize", self.skill_text)

    def test_role_specialized_section(self):
        self.assertIn("## Role-Specialized Prompts", self.skill_text)
        self.assertIn("correctness", self.skill_text)
        self.assertIn("security", self.skill_text)

    def test_agent_execution_pattern(self):
        self.assertIn("## Agent Execution Pattern", self.skill_text)
        self.assertIn("exec_command", self.skill_text)
        self.assertIn("yield_time_ms", self.skill_text)
