"""Test wheel and rebuilt sdist in clean venvs outside the source checkout.

Installation may download public Python dependencies. Application verification
runs under the offline test guard, with no personal configuration or credentials.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist", type=Path, default=ROOT / "dist")
    args = parser.parse_args()
    wheels = list(args.dist.glob("*.whl"))
    sdists = list(args.dist.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        raise SystemExit("expected exactly one wheel and one sdist")
    for artifact in wheels + sdists:
        with tempfile.TemporaryDirectory(prefix="antigravity-installed-") as temporary:
            root = Path(temporary)
            env = {key: value for key, value in os.environ.items() if key.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "TMPDIR", "TMP", "TEMP", "LANG"}}
            home = root / "home"
            home.mkdir()
            env.update({"ANTIGRAVITY_TEST_ORIGINAL_HOME": os.environ.get("ANTIGRAVITY_TEST_ORIGINAL_HOME") or str(Path.home()), "HOME": str(home), "USERPROFILE": str(home), "APPDATA": str(home), "LOCALAPPDATA": str(home), "XDG_CONFIG_HOME": str(home), "XDG_DATA_HOME": str(home), "PIP_CONFIG_FILE": os.devnull, "PIP_DISABLE_PIP_VERSION_CHECK": "1"})
            environment = root / "venv"
            # Run venv creation with the same scrubbed environment as installation.
            subprocess.run([sys.executable, "-m", "venv", str(environment)], cwd=root, env=env, check=True)
            python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            subprocess.run([str(python), "-m", "pip", "install", str(artifact.resolve()) + "[dev]"], cwd=root, env=env, check=True)
            subprocess.run([str(python), "-m", "pip", "check"], cwd=root, env=env, check=True)
            suite = root / "checks"
            suite.mkdir()
            shutil.copy2(ROOT / "conftest.py", suite)
            shutil.copytree(ROOT / "test_support", suite / "test_support", ignore=shutil.ignore_patterns("__pycache__"))
            (suite / "scripts").mkdir()
            shutil.copy2(ROOT / "scripts/run_tests.py", suite / "scripts/run_tests.py")
            (suite / "tests").mkdir()
            for test in ("conftest.py", "test_hermetic_replay.py", "test_installed_contract.py", "test_service_manager.py", "test_model_observations.py", "test_request_shapes.py", "test_anti_inventory.py", "test_final_tool_calls.py", "test_anti_diff_provenance.py"):
                shutil.copy2(ROOT / "tests" / test, suite / "tests" / test)
            # An explicit external root and cwd prevent pytest from finding the
            # checkout's conftest, pythonpath config, or editable source imports.
            subprocess.run([str(python), str(suite / "scripts/run_tests.py"), "-q", "--import-mode=importlib", "--rootdir", str(suite), str(suite)], cwd=suite, env=env, check=True)
            print(f"Installed contract passed: {artifact.name}")


if __name__ == "__main__":
    main()
