"""Single source/manifest/archive completeness gate for CI and publishing."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import tarfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "codex_antigravity_auth"


def required_assets(root=ROOT):
    manifest = json.loads((root / PACKAGE / "skill_assets.json").read_text())
    if manifest.get("version") != 1:
        raise ValueError("unsupported skill asset manifest")
    skill = root / PACKAGE / "skills/anti"
    actual = {p.relative_to(skill).as_posix() for p in skill.rglob("*") if p.is_file() and "__pycache__" not in p.parts and p.name != ".DS_Store"}
    declared = set(manifest["files"])
    if actual != declared or len(declared) != len(manifest["files"]):
        raise ValueError(f"skill manifest drift: missing={sorted(actual - declared)}, stale={sorted(declared - actual)}")
    modules = {p.relative_to(root).as_posix() for p in (root / PACKAGE).rglob("*.py")}
    return modules | {f"{PACKAGE}/skills/anti/{name}" for name in declared} | {f"{PACKAGE}/skill_assets.json"}


def check_archive(path, required):
    if path.suffix == ".whl":
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
    else:
        with tarfile.open(path) as archive:
            names = {name.partition("/")[2] for name in archive.getnames()}
    missing = required - names
    if missing:
        raise ValueError(f"{path.name} missing assets: {sorted(missing)}")
    if not any(name == "LICENSE" or name.endswith("/LICENSE") for name in names):
        raise ValueError(f"{path.name} missing LICENSE")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dist", type=Path, default=ROOT / "dist")
    args = parser.parse_args()
    required = required_assets()
    wheels = list(args.dist.glob("*.whl"))
    sdists = list(args.dist.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        raise SystemExit("expected exactly one wheel and one sdist")
    for path in wheels + sdists:
        check_archive(path, required)
        print(f"Complete package assets: {path.name}")


if __name__ == "__main__":
    main()
