"""Offline tests that seed failures in each new CI gate."""
from datetime import date
import importlib.util
import json
from pathlib import Path
import re
import subprocess
import sys
from types import SimpleNamespace

from packaging.requirements import Requirement
import pytest
import yaml

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("audit_gate", ROOT / "scripts/audit_dependencies.py")
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)
TODAY = date(2026, 10, 1)


def report(*ids):
    return {"dependencies": [{"name": "fixture-package", "version": "1.2.3", "vulns": [{"id": value} for value in ids]}]}


def policy(*entries):
    return {"schemaVersion": 1, "exceptions": list(entries)}


def exception(**overrides):
    return {"id": "SYNTHETIC-ADVISORY", "package": "fixture-package", "version": "1.2.3",
            "owner": "fixture-maintainer", "reason": "Synthetic fixture; no real advisory suppressed",
            "expires": "2026-10-02", **overrides}


def check(data, rules=None):
    return gate.check_report(data, rules or policy(), {"fixture-package": "1.2.3"}, today=TODAY)


def test_clean_report_passes_and_seeded_advisory_fails():
    assert check(report()) == []
    assert check(report("SYNTHETIC-ADVISORY")) == ["fixture-package==1.2.3: SYNTHETIC-ADVISORY"]
    assert check(report("SYNTHETIC-ADVISORY"), policy(exception())) == []


@pytest.mark.parametrize("overrides", [
    {"expires": "2026-10-01"}, {"expires": "not-a-date"}, {"owner": ""}, {"reason": " "},
    {"version": "1.2.4"}, {"package": "different"}, {"id": "DIFFERENT"},
])
def test_invalid_expired_or_unused_exceptions_fail(overrides):
    with pytest.raises(ValueError):
        check(report("SYNTHETIC-ADVISORY"), policy(exception(**overrides)))


@pytest.mark.parametrize("data", [
    {}, [], {"dependencies": []}, {"dependencies": [{"name": "fixture-package", "skip_reason": "unavailable"}]},
    {"dependencies": [{"name": "fixture-package", "version": "1.2.3"}]},
    {"dependencies": [{"name": "fixture-package", "version": "1.2.3", "vulns": [{}]}]},
    {"dependencies": [{"name": "fixture-package", "version": "1.2.4", "vulns": []}]},
])
def test_missing_skipped_and_malformed_audit_fails_closed(data):
    with pytest.raises(ValueError):
        check(data)


def test_duplicate_audit_rows_or_exceptions_fail():
    data = report()
    data["dependencies"] *= 2
    with pytest.raises(ValueError):
        check(data)
    with pytest.raises(ValueError):
        check(report("SYNTHETIC-ADVISORY"), policy(exception(), exception()))


@pytest.mark.parametrize("text", ["", "fixture>=1", "fixture==1.*", "fixture @ https://example.invalid/a.whl", "fixture==1; python_version>'3.0'", "fixture==1\nfixture==2"])
def test_audit_requires_complete_exact_pins(text):
    with pytest.raises(ValueError):
        gate.expected_packages(text)


def test_seeded_advisory_sets_process_failure(monkeypatch, tmp_path):
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("fixture-package==1.2.3\n", encoding="utf-8")
    exceptions = tmp_path / "exceptions.json"
    exceptions.write_text(json.dumps(policy()), encoding="utf-8")

    def fake_audit(command, **kwargs):
        assert "--strict" in command and "--disable-pip" in command and kwargs["timeout"] == 300
        Path(command[-1]).write_text(json.dumps(report("SYNTHETIC-ADVISORY")), encoding="utf-8")
        return SimpleNamespace(returncode=1)

    monkeypatch.setattr(gate.subprocess, "run", fake_audit)
    assert gate.run_audit(requirements, exceptions) == 1


@pytest.mark.parametrize("source,rule", [("print(undefined_fixture_name)\n", "F821"), ("import os\n", "F401")])
def test_seeded_lint_defect_fails_actual_config(source, rule):
    result = subprocess.run(
        [sys.executable, "-m", "ruff", "check", "--config", str(ROOT / "pyproject.toml"),
         "--stdin-filename", "codex_antigravity_auth/account_state.py", "-"],
        input=source, text=True, capture_output=True, cwd=ROOT, check=False, timeout=10,
    )
    assert result.returncode == 1
    assert rule in result.stdout


def test_declared_runtime_minimums_match_compatibility_constraints():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    expected = {}
    for raw in project["project"]["dependencies"]:
        req = Requirement(raw)
        floor = [item.version for item in req.specifier if item.operator == ">="]
        assert len(floor) == 1, f"Document a minimum for {req.name}"
        # Version equality treats 0.28 and 0.28.0 alike.
        expected[req.name] = floor[0]
    from packaging.version import Version
    pinned = gate.expected_packages((ROOT / "requirements/minimum-runtime.txt").read_text())
    assert {name: Version(value) for name, value in pinned.items()} == {name: Version(value) for name, value in expected.items()}


def test_workflow_permissions_pins_and_publish_dependencies():
    for path in (ROOT / ".github/workflows").glob("*.yml"):
        workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert workflow["permissions"] == {"contents": "read"}
        for name, job in workflow["jobs"].items():
            if "permissions" in job:
                assert path.name == "publish.yml" and name == "publish"
                assert job["permissions"] == {"contents": "read", "id-token": "write"}
            for step in job.get("steps", []):
                if "uses" in step and not step["uses"].startswith("./"):
                    assert re.fullmatch(r"[^@]+@[a-f0-9]{40}", step["uses"])
                    if step["uses"].startswith("actions/checkout@"):
                        assert step["with"]["persist-credentials"] is False
    publish = yaml.safe_load((ROOT / ".github/workflows/publish.yml").read_text())
    assert "quality" in publish["jobs"]["publish"]["needs"]
