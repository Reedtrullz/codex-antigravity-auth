"""Phase 6: Evidence-linked verification for panel findings.

Runs targeted tool checks (typecheck, lint, syntax, secrets scan) on files
referenced by findings and attaches concrete evidence.
"""
from __future__ import annotations

import re
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any


def _run_check(cmd: list[str], timeout: int = 30) -> tuple[bool, str]:
    """Run a check command, return (success, output)."""
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
        )
        output = (result.stdout + result.stderr).strip()
        return result.returncode == 0, output[:2000]
    except FileNotFoundError:
        return False, f"tool not found: {cmd[0]}"
    except subprocess.TimeoutExpired:
        return False, f"timeout after {timeout}s"
    except Exception as e:
        return False, str(e)[:500]


def _check_python_syntax(file_path: Path) -> str | None:
    """Check Python file for syntax errors."""
    ok, output = _run_check(["python3", "-m", "py_compile", str(file_path)])
    if not ok and output and "SyntaxError" in output:
        return f"Python syntax error: {output}"
    return None


def _check_secrets(file_path: Path) -> str | None:
    """Scan for common secret patterns."""
    patterns = [
        r'(?i)(api[_-]?key|secret[_-]?key|password|token)\s*[=:]\s*["\'][^"\']{8,}',
        r'(?i)(AWS_SECRET_ACCESS_KEY|AWS_ACCESS_KEY_ID)\s*[=]',
        r'-----BEGIN\s+(RSA\s+)?PRIVATE\s+KEY-----',
    ]
    try:
        content = file_path.read_text(encoding="utf-8", errors="ignore")[:50000]
        for pattern in patterns:
            match = re.search(pattern, content)
            if match:
                return f"Potential secret found: {match.group()[:80]}..."
    except Exception:
        pass
    return None


def _check_eslint(file_path: Path) -> str | None:
    """Run eslint if available."""
    if not shutil.which("eslint"):
        return None
    ok, output = _run_check(["eslint", "--no-eslintrc", "--rule", "{}", str(file_path)], timeout=15)
    if not ok and output and "error" in output.lower():
        return f"ESLint errors: {output[:500]}"
    return None


CHECKS = [
    ("python_syntax", _check_python_syntax, {".py"}),
    ("secrets_scan", _check_secrets, {".py", ".js", ".ts", ".yaml", ".yml", ".toml", ".json", ".env"}),
    ("eslint", _check_eslint, {".js", ".ts", ".jsx", ".tsx"}),
]


def verify_finding(finding: dict[str, Any], workspace_root: Path) -> dict[str, Any]:
    """Run available checks on a finding's file and attach evidence.
    
    Returns the finding with 'evidence' updated if a check produces results.
    """
    file_path_str = finding.get("file")
    if not file_path_str:
        return finding
    
    file_path = Path(file_path_str)
    if not file_path.is_absolute():
        file_path = workspace_root / file_path
    file_path = file_path.resolve()
    workspace_resolved = workspace_root.resolve()
    if not str(file_path).startswith(str(workspace_resolved) + os.sep) and file_path != workspace_resolved:
        return finding
    
    if not file_path.exists() or not file_path.is_file():
        return finding
    
    suffix = file_path.suffix.lower()
    evidence_parts: list[str] = []
    
    for check_name, check_fn, applicable_suffixes in CHECKS:
        if suffix not in applicable_suffixes:
            continue
        result = check_fn(file_path)
        if result:
            evidence_parts.append(f"[{check_name}] {result}")
    
    if evidence_parts:
        existing_evidence = finding.get("evidence", "unverified")
        new_evidence = "; ".join(evidence_parts)
        if existing_evidence == "unverified":
            finding["evidence"] = new_evidence
        else:
            finding["evidence"] = f"{existing_evidence}; {new_evidence}"
    
    return finding


def verify_findings(findings: list[dict[str, Any]], workspace_root: Path) -> list[dict[str, Any]]:
    """Verify all findings that have a file path."""
    return [verify_finding(f, workspace_root) if f.get("file") else f for f in findings]
