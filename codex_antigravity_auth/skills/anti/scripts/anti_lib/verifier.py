"""Read-only file checks, separate from the truth of a model's finding."""
from __future__ import annotations

import ast
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from typing import Any

from .redaction import redact_sensitive_text

MAX_FILE_BYTES = 512 * 1024
MAX_CAPTURE_BYTES = 16 * 1024
MAX_OUTPUT_CHARS = 2000
CHECK_TIMEOUT_SECONDS = 15.0
JAVASCRIPT_SUFFIXES = {".js", ".ts", ".jsx", ".tsx", ".mjs", ".cjs", ".mts", ".cts"}
TEXT_SUFFIXES = JAVASCRIPT_SUFFIXES | {".py", ".yaml", ".yml", ".toml", ".json", ".env"}
CHECK_PROFILES = {"eslint"}
WINDOWS = sys.platform == "win32"
SECRET_PATTERNS = [
    re.compile(r'''(?i)(api[_-]?key|secret[_-]?key|password|token)\s*[=:]\s*["'][^"']{8,}'''),
    re.compile(r"(?i)(AWS_SECRET_ACCESS_KEY|AWS_ACCESS_KEY_ID)\s*[=]"),
    re.compile(r"-----BEGIN\s+(RSA\s+)?PRIVATE\s+KEY-----"),
]


def _result(check: str, status: str, reason: str, *, path: str | None, digest: str | None,
            cwd: Path, command: list[str], output: str = "", started: float | None = None,
            return_code: int | None = None, **details: Any) -> dict[str, Any]:
    identity_command = list(command)
    if "--cache-location" in identity_command:
        identity_command[identity_command.index("--cache-location") + 1] = "<temporary-cache>"
    identity_context = ({"scope": "invocation", "observationId": uuid.uuid4().hex,
                         "effectiveTool": "unknown", "effectiveConfig": "unknown"}
                        if check == "eslint" else {"scope": "builtin", "effectiveConfig": "not_applicable"})
    identity = json.dumps([check, path, digest, str(cwd), identity_command, identity_context], separators=(",", ":"))
    return {
        "checkId": hashlib.sha256(identity.encode()).hexdigest()[:24], "check": check,
        "status": status, "reason": reason, "file": path, "fileHash": digest,
        "cwd": str(cwd), "command": command, "identityContext": identity_context,
        "comparableAcrossRuns": check != "eslint",
        "output": redact_sensitive_text(output)[:MAX_OUTPUT_CHARS],
        "durationMs": max(0, round((time.monotonic() - started) * 1000)) if started is not None else 0,
        "returnCode": return_code, **details,
    }


def _find_eslint(workspace: Path) -> str | list[str] | None:
    # Prefer the project's installed version; never npx, download or install.
    # Invoke the JS entry with Node on Windows rather than executing a .cmd
    # wrapper through a shell with model-originated filenames in its arguments.
    node = shutil.which("node")
    entry = workspace / "node_modules" / "eslint" / "bin" / "eslint.js"
    if node and entry.is_file():
        return [node, str(entry.absolute())]
    local = workspace / "node_modules" / ".bin" / "eslint"
    if not WINDOWS and local.is_file():
        return str(local.absolute())
    tool = shutil.which("eslint")
    if tool and WINDOWS and Path(tool).suffix.lower() in {".cmd", ".bat"}:
        entry = Path(tool).parent / "node_modules" / "eslint" / "bin" / "eslint.js"
        return [node, str(entry)] if node and entry.is_file() else None
    return tool


_WINDOWS_WRAPPER = r"""
import json, subprocess, sys
payload = sys.stdin.buffer.read(int(sys.argv[2]) + 2)
if not payload.startswith(b"1") or len(payload) > int(sys.argv[2]) + 1:
    raise SystemExit(125)
try:
    completed = subprocess.run(json.loads(sys.argv[1]), input=payload[1:], shell=False)
except OSError:
    raise SystemExit(126)
raise SystemExit(completed.returncode)
"""


def _create_windows_job():
    from .windows_job import WindowsJob
    return WindowsJob()


def _run_check(command: list[str], source: bytes, cwd: Path) -> dict[str, Any]:
    """Drain bounded output; Windows tool code starts only after job assignment."""
    captured = bytearray()
    truncated = False
    read_error = False
    feed_error = False
    started = time.monotonic()
    process = reader = feeder = job = None

    def stop():
        if job is not None:
            job.close()  # Kernel terminates the assigned wrapper and descendants.
        elif process is not None and process.poll() is None:
            try:
                if os.name == "posix":
                    os.killpg(process.pid, signal.SIGKILL)
                else:
                    process.kill()
            except ProcessLookupError:
                pass

    try:
        if WINDOWS:
            try:
                job = _create_windows_job()
            except OSError:
                return {"status": "error", "reason": "process_control_unavailable", "output": "Cannot establish checker process-tree control; checker was not started.", "started": started}
        with tempfile.TemporaryFile() as stdin:
            stdin.write(source)
            stdin.seek(0)
            launch = ([sys.executable, "-I", "-S", "-c", _WINDOWS_WRAPPER, json.dumps(command), str(MAX_FILE_BYTES)]
                      if WINDOWS else command)
            process = subprocess.Popen(launch, stdin=subprocess.PIPE if WINDOWS else stdin,
                                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                       cwd=str(cwd), shell=False, start_new_session=os.name == "posix")
            if job is not None:
                try:
                    job.assign(process)
                except OSError:
                    # The isolated stdlib wrapper is still blocked on input; no
                    # project tool/config has run, so direct termination is safe.
                    process.kill()
                    process.wait(timeout=2)
                    process.stdin.close()
                    process.stdout.close()
                    return {"status": "error", "reason": "process_control_unavailable", "output": "Cannot assign checker process-tree control; checker was not started.", "started": started}

            def drain():
                nonlocal truncated, read_error
                try:
                    while chunk := process.stdout.read(4096):
                        remaining = max(0, MAX_CAPTURE_BYTES - len(captured))
                        captured.extend(chunk[:remaining])
                        truncated |= len(chunk) > remaining
                except OSError:
                    read_error = True
                finally:
                    process.stdout.close()

            reader = threading.Thread(target=drain, daemon=True)
            reader.start()
            if WINDOWS:
                def feed():
                    nonlocal feed_error
                    try:
                        process.stdin.write(b"1" + source)
                    except (BrokenPipeError, OSError):
                        feed_error = True
                    finally:
                        try:
                            process.stdin.close()
                        except OSError:
                            feed_error = True
                # Starting the feeder is the gate release, after assignment.
                feeder = threading.Thread(target=feed, daemon=True)
                feeder.start()
            timed_out = False
            try:
                code = process.wait(timeout=CHECK_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                timed_out = True
                stop()
                code = process.wait(timeout=2)
            finally:
                if job is not None:
                    job.close()  # Also stop stragglers after a normal checker exit.
            reader.join(timeout=1)
            if feeder is not None:
                feeder.join(timeout=1)
            if reader.is_alive() or (feeder is not None and feeder.is_alive()) or read_error:
                return {"status": "error", "reason": "output_capture_incomplete", "output": "Check output could not be fully captured.", "return_code": code, "started": started}
            output = "[output omitted: capture limit exceeded]" if truncated else captured.decode("utf-8", errors="replace")
            if timed_out:
                status, reason = "error", "tool_timeout"
            elif feed_error:
                status, reason = "error", "input_delivery_failed"
            elif truncated:
                status, reason = "error", "output_limit"
            elif code == 0:
                status, reason = "passed", "tool_passed"
            elif code == 1:
                status, reason = "failed", "lint_errors"
            elif code == 126:
                status, reason = "error", "tool_execution_error"
            else:
                status, reason = "error", "configuration_or_tool_error"
            return {"status": status, "reason": reason, "output": output, "return_code": code,
                    "started": started, "outputTruncated": truncated}
    except FileNotFoundError:
        return {"status": "skipped", "reason": "tool_missing", "output": "Installed checker was not found.", "started": started}
    except (OSError, subprocess.SubprocessError) as exc:
        return {"status": "error", "reason": "tool_execution_error", "output": str(exc), "started": started}
    finally:
        stop()
        if process is not None and process.poll() is None:
            process.wait(timeout=2)
        for thread in (feeder, reader):
            if thread is not None:
                thread.join(timeout=1)


def _checks(path: Path, content: bytes, workspace: Path, profiles: frozenset[str]) -> list[dict[str, Any]]:
    relative = path.relative_to(workspace).as_posix()
    digest = hashlib.sha256(content).hexdigest()
    common = {"path": relative, "digest": digest, "cwd": workspace}
    suffix = ".env" if path.name == ".env" else path.suffix.lower()
    results = []
    if suffix == ".py":
        started = time.monotonic()
        status, reason, output = "passed", "syntax_valid", "Python syntax parsed without executing source."
        try:
            compile(content, relative, "exec", flags=ast.PyCF_ONLY_AST, dont_inherit=True)
        except SyntaxError as exc:
            status, reason, output = "failed", "syntax_error", f"Python syntax error at line {exc.lineno}, column {exc.offset}: {exc.msg}"
        except (ValueError, TypeError, MemoryError, RecursionError) as exc:
            status, reason, output = "error", "parser_error", str(exc)
        results.append(_result("python_syntax", status, reason, command=["builtin:python-ast", sys.version.split()[0]],
                               output=output, started=started, **common))
    if suffix in TEXT_SUFFIXES:
        started = time.monotonic()
        text = content.decode("utf-8", errors="replace")
        matched = next((match for pattern in SECRET_PATTERNS if (match := pattern.search(text))), None)
        output = (f"Potential credential pattern at line {text.count(chr(10), 0, matched.start()) + 1}; value omitted."
                  if matched else "No supported credential pattern matched; this is not a complete secret audit.")
        results.append(_result("secrets_scan", "failed" if matched else "passed", "credential_pattern" if matched else "no_pattern",
                               command=["builtin:credential-patterns-v1"], output=output, started=started, **common))
    if suffix in JAVASCRIPT_SUFFIXES or "eslint" in profiles:
        if suffix not in JAVASCRIPT_SUFFIXES:
            results.append(_result("eslint", "skipped", "unsupported_file_type", command=["eslint"], **common))
        elif "eslint" not in profiles:
            results.append(_result("eslint", "skipped", "profile_not_enabled", command=["eslint"], **common))
        elif not (tool := _find_eslint(workspace)):
            results.append(_result("eslint", "skipped", "tool_missing", command=["eslint"], **common))
        else:
            # Without an alternate cache location ESLint may delete the project's
            # .eslintcache even when caching is disabled. Keep it outside the repo.
            with tempfile.TemporaryDirectory(prefix="anti-check-cache-") as cache:
                command = ([tool] if isinstance(tool, str) else tool) + ["--stdin", "--stdin-filename", "./" + relative, "--format", "json",
                           "--no-fix", "--no-cache", "--cache-location", str(Path(cache) / "eslintcache")]
                outcome = _run_check(command, content, workspace)
                if outcome["status"] in {"passed", "failed"}:
                    try:
                        report = json.loads(outcome.get("output", ""))
                        if not isinstance(report, list) or any(not isinstance(row, dict) or not isinstance(row.get("messages"), list) for row in report):
                            raise ValueError("Unexpected ESLint report shape")
                        ignored = any(isinstance(message, dict) and str(message.get("message", "")).startswith("File ignored")
                                      for row in report for message in row["messages"])
                        if not report or ignored:
                            outcome.update(status="skipped", reason="file_ignored" if ignored else "no_lint_result")
                    except (ValueError, TypeError):
                        outcome.update(status="error", reason="invalid_tool_report")
                results.append(_result("eslint", command=command, **common, **outcome,
                                       configSource="project-auto-discovery", effectiveConfigHash=None, effectiveToolFingerprint=None,
                                       profile="eslint", operatorOptIn=True))
    if not results:
        results.append(_result("file_checks", "skipped", "unsupported_file_type", command=[], **common))
    return results


def verify_findings(findings: list[dict[str, Any]], workspace_root: Path, *, profiles: list[str] | None = None) -> list[dict[str, Any]]:
    """Attach snapshot-bound checks once per distinct file/hash/profile in a batch.

    A finding's `verify` text is never parsed or executed. Check outcomes never
    change its model evidence into proof of the claim.
    """
    requested = frozenset(profiles or [])
    if not requested <= CHECK_PROFILES:
        raise ValueError("Unsupported operator check profile")
    workspace = Path(workspace_root).resolve()
    cache: dict[tuple[str, str, frozenset[str]], list[dict[str, Any]]] = {}
    results = []
    for original in findings:
        finding = dict(original)
        finding["claimVerdict"] = "unverified"
        if not isinstance(finding.get("verificationStatus"), str) or finding["verificationStatus"] not in {"unverified", "needs-runtime-check"}:
            finding["verificationStatus"] = "unverified"
        label = finding.get("file")
        path = None
        content = None
        reason = None
        status = "skipped"
        if not isinstance(label, str) or not label:
            reason = "file_not_provided"
        else:
            try:
                candidate = Path(label)
                path = (candidate if candidate.is_absolute() else workspace / candidate).resolve()
                path.relative_to(workspace)
            except (ValueError, OSError, RuntimeError):
                reason, path = "path_outside_workspace", None
            if path is not None:
                try:
                    if not path.is_file():
                        reason = "file_missing_or_not_regular"
                    else:
                        with path.open("rb") as stream:
                            content = stream.read(MAX_FILE_BYTES + 1)
                        if len(content) > MAX_FILE_BYTES:
                            reason, content = "file_size_limit", None
                except OSError:
                    reason, status = "file_read_error", "error"
        if reason:
            finding["checks"] = [_result("file_read", status, reason, path=path.relative_to(workspace).as_posix() if path else None,
                                          digest=None, cwd=workspace, command=[])]
        else:
            digest = hashlib.sha256(content).hexdigest()
            key = (str(path), digest, requested)
            if key not in cache:
                cache[key] = _checks(path, content, workspace, requested)
            finding["checks"] = deepcopy(cache[key])
        results.append(finding)
    return results


def verify_finding(finding: dict[str, Any], workspace_root: Path, *, profiles: list[str] | None = None) -> dict[str, Any]:
    return verify_findings([finding], workspace_root, profiles=profiles)[0]
