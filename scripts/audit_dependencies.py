"""Fail-closed pip-audit gate for a fully pinned, public-index CI snapshot."""
from __future__ import annotations

import argparse
from datetime import date
import json
from pathlib import Path
import subprocess
import sys
import tempfile

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

ROOT = Path(__file__).resolve().parents[1]


def expected_packages(text: str) -> dict[str, str]:
    packages = {}
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        req = Requirement(line)
        specs = list(req.specifier)
        if (req.url or req.extras or req.marker or len(specs) != 1
                or specs[0].operator != "==" or "*" in specs[0].version):
            raise ValueError("Audit input must be a complete exact-version snapshot without URLs or markers")
        name = canonicalize_name(req.name)
        if name in packages:
            raise ValueError("Duplicate package in audit snapshot")
        packages[name] = specs[0].version
    if not packages:
        raise ValueError("Audit snapshot is empty")
    return packages


def check_report(report: object, policy: object, expected: dict[str, str], *, today: date) -> list[str]:
    if (not isinstance(policy, dict) or set(policy) != {"schemaVersion", "exceptions"}
            or type(policy.get("schemaVersion")) is not int or policy["schemaVersion"] != 1
            or not isinstance(policy.get("exceptions"), list)):
        raise ValueError("Invalid audit exception policy")
    allowed = {}
    for entry in policy["exceptions"]:
        fields = {"id", "package", "version", "owner", "reason", "expires"}
        if not isinstance(entry, dict) or set(entry) != fields or any(
            not isinstance(entry[key], str) or not entry[key].strip() for key in fields
        ):
            raise ValueError("Each exception requires id, package, version, owner, reason and expires")
        if date.fromisoformat(entry["expires"]) <= today:
            raise ValueError("Expired audit exception: " + entry["id"])
        key = (canonicalize_name(entry["package"]), entry["version"], entry["id"])
        if key in allowed:
            raise ValueError("Duplicate audit exception")
        allowed[key] = entry
    if not isinstance(report, dict) or not isinstance(report.get("dependencies"), list):
        raise ValueError("Missing pip-audit dependency report")
    observed = {}
    used = set()
    failures = []
    for row in report["dependencies"]:
        if (not isinstance(row, dict) or not isinstance(row.get("name"), str)
                or not isinstance(row.get("version"), str) or "skip_reason" in row
                or not isinstance(row.get("vulns"), list)):
            raise ValueError("Skipped or malformed audit dependency")
        name = canonicalize_name(row["name"])
        if name in observed:
            raise ValueError("Duplicate audited package")
        observed[name] = row["version"]
        for vuln in row["vulns"]:
            if not isinstance(vuln, dict) or not isinstance(vuln.get("id"), str) or not vuln["id"]:
                raise ValueError("Malformed vulnerability report")
            key = (name, row["version"], vuln["id"])
            if key in allowed:
                used.add(key)
            else:
                failures.append(f"{name}=={row['version']}: {vuln['id']}")
    if observed != expected:
        raise ValueError("Audit did not cover the exact dependency snapshot")
    if set(allowed) != used:
        raise ValueError("Unused audit exception; remove or revalidate it")
    return failures


def run_audit(requirements: Path, exceptions: Path) -> int:
    expected = expected_packages(requirements.read_text(encoding="utf-8"))
    policy = json.loads(exceptions.read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory(prefix="dependency-audit-") as directory:
        output = Path(directory) / "audit.json"
        process = subprocess.run(
            [sys.executable, "-m", "pip_audit", "--strict", "--no-deps", "--disable-pip",
             "--progress-spinner", "off", "--desc", "off", "--aliases", "off",
             "-r", str(requirements), "--format", "json", "--output", str(output)],
            check=False, timeout=300,
        )
        if process.returncode not in (0, 1) or not output.is_file():
            raise ValueError("Dependency audit did not complete")
        report = json.loads(output.read_text(encoding="utf-8"))
        failures = check_report(report, policy, expected, today=date.today())
        has_findings = any(row["vulns"] for row in report["dependencies"])
        if bool(process.returncode) != has_findings:
            raise ValueError("Audit exit status disagrees with report")
        if failures:
            print("Unaccepted dependency advisories:\n" + "\n".join(failures), file=sys.stderr)
            return 1
        print(f"Dependency audit passed for {len(expected)} pinned packages; {len(policy['exceptions'])} reviewed exceptions")
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--requirements", type=Path, default=ROOT / "requirements/ci-py310-linux.txt")
    parser.add_argument("--exceptions", type=Path, default=ROOT / "requirements/audit-exceptions.json")
    args = parser.parse_args()
    try:
        return run_audit(args.requirements, args.exceptions)
    except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
        print(f"Dependency audit failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
