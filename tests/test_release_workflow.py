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


if __name__ == "__main__":
    unittest.main()
