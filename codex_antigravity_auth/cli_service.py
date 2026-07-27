"""Gateway process, service, status, and log commands (split from cli.py)."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import cli as _cli


def _codex_home_read_only() -> Path:
    if _cli.get_codex_home is not _cli._DEFAULT_GET_CODEX_HOME:
        return _cli.get_codex_home()
    return Path(os.path.expanduser("~/.codex"))


def gateway_model_ids(
    base_url: str,
    *,
    timeout: float = 2.0,
    token_env: str = "ANTIGRAVITY_GATEWAY_TOKEN",
) -> set[str]:
    url = base_url.rstrip("/") + "/models"
    headers = {"Accept": "application/json"}
    token = os.environ.get(token_env, "").strip() if token_env else ""
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        hint = ""
        if exc.code in (401, 403) and not token:
            hint = f" (remote gateways require a bearer token; export {token_env})"
        raise RuntimeError(f"{url} returned HTTP {exc.code}{hint}") from exc
    except Exception as exc:
        raise RuntimeError(f"{url} is not reachable ({exc})") from exc
    try:
        payload = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise RuntimeError(f"{url} returned non-JSON data") from exc
    entries = payload.get("data")
    if not isinstance(entries, list):
        entries = payload.get("models")
    ids = {
        entry.get("id")
        for entry in entries or []
        if isinstance(entry, dict) and isinstance(entry.get("id"), str)
    }
    if not ids:
        raise RuntimeError(f"{url} returned no model ids")
    return ids


def gateway_base_url_for_port(port: int) -> str:
    try:
        parsed_port = int(port)
    except (TypeError, ValueError) as exc:
        raise ValueError("Gateway port must be an integer") from exc
    if parsed_port < 1 or parsed_port > 65535:
        raise ValueError("Gateway port must be between 1 and 65535")
    return f"http://localhost:{parsed_port}/v1"


def wait_for_gateway_model_ids(
    base_url: str,
    *,
    timeout: float = 2.0,
    token_env: str = "ANTIGRAVITY_GATEWAY_TOKEN",
    wait_seconds: float = _cli.GATEWAY_READY_TIMEOUT_SECONDS,
    interval: float = _cli.GATEWAY_READY_RETRY_INTERVAL_SECONDS,
) -> set[str]:
    deadline = time.monotonic() + max(0.0, float(wait_seconds))
    attempts = 0
    while True:
        attempts += 1
        try:
            return _cli.gateway_model_ids(base_url, timeout=timeout, token_env=token_env)
        except RuntimeError as exc:
            if time.monotonic() >= deadline:
                message = _cli.redact_secret_text(str(exc))
                raise RuntimeError(
                    f"{base_url.rstrip('/')}/models did not become ready within {wait_seconds:g}s "
                    f"after {attempts} attempt(s): {message}"
                ) from exc
            time.sleep(max(0.0, min(interval, deadline - time.monotonic())))


def gateway_runtime_paths(port: int) -> tuple[Path, Path]:
    codex_home = _cli._codex_home_read_only()
    return codex_home / _cli.GATEWAY_PID_TEMPLATE.format(port=port), codex_home / _cli.GATEWAY_LOG_TEMPLATE.format(port=port)


def local_gateway_base_url(host: str, port: int) -> str:
    return f"http://{host}:{port}/v1"


def add_gateway_reachability(info: dict, *, host: str = "127.0.0.1", timeout: float = 5.0) -> dict:
    base_url = _cli.local_gateway_base_url(host, int(info["port"]))
    try:
        model_ids = _cli.gateway_model_ids(base_url, timeout=timeout)
    except RuntimeError as exc:
        info["reachable"] = False
        info["reachable_base_url"] = base_url
        info["reachability_error"] = _cli.redact_secret_text(str(exc))
    else:
        info["reachable"] = True
        info["reachable_base_url"] = base_url
        info["reachable_model_count"] = len(model_ids)
        if not info.get("running") and info.get("status") in {"stopped", "stale"}:
            info["status"] = "unmanaged"
    return info


def process_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        try:
            proc = subprocess.run(
                ["tasklist", "/FI", f"PID eq {int(pid)}", "/FO", "CSV", "/NH"],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=2.0,
                check=False,
            )
        except Exception:
            return False
        if proc.returncode != 0:
            return False
        output = proc.stdout.strip()
        return bool(output and "no tasks" not in output.lower())
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def gateway_process_command(pid: int) -> str | None:
    if sys.platform == "win32":
        commands = [
            ["wmic", "process", "where", f"ProcessId={int(pid)}", "get", "CommandLine", "/value"],
            [
                "powershell",
                "-NoProfile",
                "-Command",
                f"(Get-CimInstance Win32_Process -Filter \"ProcessId={int(pid)}\").CommandLine",
            ],
        ]
        for command in commands:
            try:
                proc = subprocess.run(
                    command,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=2.0,
                    check=False,
                )
            except Exception:
                continue
            if proc.returncode == 0:
                output = proc.stdout.strip()
                if output.startswith("CommandLine="):
                    output = output.split("=", 1)[1].strip()
                return output
        return None
    try:
        proc = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=1.0,
            check=False,
        )
    except Exception:
        return None
    if proc.returncode != 0:
        return ""
    return proc.stdout.strip()


def gateway_pid_matches(pid: int) -> bool | None:
    command = _cli.gateway_process_command(pid)
    if command is None:
        return None
    if not command:
        return False
    return "codex_antigravity_auth.server:app" in command and "uvicorn" in command


def read_pid_file(path: Path) -> int | None:
    try:
        raw = path.read_text(encoding="utf-8").strip()
        return int(raw)
    except Exception:
        return None


def gateway_status_info(port: int) -> dict:
    pid_path, log_path = _cli.gateway_runtime_paths(port)
    pid = _cli.read_pid_file(pid_path) if pid_path.exists() else None
    process_running = bool(pid and _cli.process_is_running(pid))
    process_matches = _cli.gateway_pid_matches(pid) if pid and process_running else None
    if process_running and process_matches is True:
        status = "running"
        running = True
    elif process_running and process_matches is False:
        status = "foreign"
        running = False
    elif process_running:
        status = "unknown"
        running = False
    else:
        status = "stale" if pid_path.exists() else "stopped"
        running = False
    return {
        "port": port,
        "status": status,
        "running": running,
        "pid": pid,
        "pid_file": str(pid_path),
        "log_file": str(log_path),
        "process_running": process_running,
        "process_matches": process_matches,
    }


def run_gateway_status(args) -> dict:
    info = _cli.reachable_gateway_status_info(args.port, wait=True, timeout=5.0)
    info["service"] = _cli.service_status(args.port)
    info["request_log"] = _cli.request_log_info()
    if getattr(args, "json", False):
        print(json.dumps(info, indent=2))
    else:
        print(f"Gateway status: {info['status']} (port {info['port']})")
        if info["pid"]:
            print(f"  pid: {info['pid']}")
        print(f"  pid_file: {info['pid_file']}")
        print(f"  log_file: {info['log_file']}")
        if info.get("reachable"):
            print(f"  reachable: yes ({info.get('reachable_model_count', 0)} model(s) at {info['reachable_base_url']})")
        else:
            print(f"  reachable: no ({info.get('reachability_error', 'not checked')})")
        service_info = info["service"]
        print(
            "  service: "
            f"{'installed' if service_info.get('installed') else 'not installed'}"
            f", {'active' if service_info.get('active') else 'inactive'}"
        )
        service_path = service_info.get("path") or service_info.get("task_name")
        if service_path:
            print(f"  service_ref: {service_path}")
        print(f"  request_log: {info['request_log']['path']}")
    return info


def reachable_gateway_status_info(port: int, *, wait: bool = False, timeout: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout
    while True:
        gateway = _cli.gateway_status_info(port)
        _cli.add_gateway_reachability(gateway)
        if not wait or gateway.get("reachable") or time.monotonic() >= deadline:
            return gateway
        time.sleep(0.25)


def run_service_command(args) -> dict:
    try:
        if args.service_command == "install":
            _cli.require_safe_gateway_host(args.host, allow_remote=False)
            info = _cli.install_service(
                args.port,
                args.host,
                op_env_file=getattr(args, "op_env_file", None),
                op_environment=getattr(args, "op_environment", None),
            )
            action = "installed"
        elif args.service_command == "uninstall":
            info = _cli.uninstall_service(args.port)
            action = "uninstalled"
        elif args.service_command == "status":
            info = _cli.service_status(args.port)
            action = "status"
        else:
            raise SystemExit("service requires install, uninstall, or status")
    except (RuntimeError, ValueError) as exc:
        raise SystemExit(_cli.redact_secret_text(str(exc))) from exc
    if action == "installed" and (
        info.get("state") == "failed"
        or not bool(info.get("installed"))
        or not bool(info.get("active"))
    ):
        detail = info.get("error") or "service installation was not observed as installed and active"
        raise SystemExit(_cli.redact_secret_text(str(detail)))
    if action == "uninstalled" and bool(info.get("installed")):
        detail = info.get("error") or "service uninstall was not observed"
        raise SystemExit(_cli.redact_secret_text(str(detail)))
    gateway = _cli.reachable_gateway_status_info(
        args.port,
        wait=action == "installed" and bool(info.get("installed")) and bool(info.get("active")),
    )
    result_action = {"installed": "install", "uninstalled": "uninstall"}.get(action, "status")
    observed = _cli.observed_service_result(
        action=result_action,
        installed=bool(info.get("installed")),
        active=bool(info.get("active")),
        reachable=bool(gateway.get("reachable")),
        changed=bool(info.get("changed", action != "status")),
        commands=tuple(info.get("commands", ())) if isinstance(info.get("commands", ()), (list, tuple)) else (),
        error=info.get("error"),
    ).to_dict()
    info = {**info, **observed}
    result = {"service": info, "gateway": gateway}
    if getattr(args, "json", False):
        print(json.dumps(result, indent=2))
    else:
        if action == "status":
            print(
                f"Service status: {'installed' if info.get('installed') else 'not installed'}, "
                f"{'active' if info.get('active') else 'inactive'}"
            )
        else:
            print(f"[+] Gateway service {action} for port {args.port}")
            if action == "installed":
                try:
                    onepassword_description = _cli.onepassword_runtime_description(
                        op_env_file=getattr(args, "op_env_file", None),
                        op_environment=getattr(args, "op_environment", None),
                    )
                except ValueError as exc:
                    raise SystemExit(_cli.redact_secret_text(str(exc))) from exc
                if onepassword_description:
                    print(f"    Secrets: {onepassword_description}")
        if info.get("path"):
            print(f"    Service file: {info['path']}")
        if info.get("task_name"):
            print(f"    Task name: {info['task_name']}")
        if gateway.get("reachable"):
            print(
                "    Gateway process: "
                f"reachable ({gateway.get('reachable_model_count', 0)} model(s) at {gateway.get('reachable_base_url')})"
            )
        else:
            print(f"    Gateway process: {gateway['status']}")
    return result


def run_logs_command(args) -> None:
    if getattr(args, "logs_action", None) == "clean":
        removed = _cli.clean_request_logs()
        if getattr(args, "json", False):
            print(json.dumps({"removed": removed}, indent=2))
        else:
            print(f"[+] Removed {len(removed)} request log file(s).")
        return
    if getattr(args, "logs_action", None) == "summary":
        try:
            summary = _cli.request_log_summary(since=getattr(args, "since", "24h"))
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
        if getattr(args, "json", False):
            print(json.dumps(summary, indent=2))
            return
        if not summary["groups"]:
            print(f"[*] No request log entries matched the {summary['since']} window at {summary['path']}")
        else:
            print(f"[*] Request log summary ({summary['since']})")
            for group in summary["groups"].values():
                success_pct = group["success_rate"] * 100
                p50 = group["p50_latency_ms"] if group["p50_latency_ms"] is not None else "n/a"
                p95 = group["p95_latency_ms"] if group["p95_latency_ms"] is not None else "n/a"
                print(
                    f"- {group['route']}/{group['family']}: {group['request_count']} request(s), "
                    f"{success_pct:.1f}% success, p50={p50}ms, p95={p95}ms, "
                    f"429s={group['rate_limit_count']}, rotations={group['rotation_attempted_count']}"
                )
                if group["top_error_classes"]:
                    errors = ", ".join(
                        f"{item['error_class']} ({item['count']})" for item in group["top_error_classes"]
                    )
                    print(f"  errors: {errors}")
        if summary["malformed_records"]:
            print(f"[WARN] Ignored {summary['malformed_records']} malformed request-log entry/entries.")
        return
    tail = getattr(args, "tail", None)
    if getattr(args, "json", False):
        print(json.dumps(list(_cli.iter_request_records(tail=tail)), indent=2))
        return
    path = Path(_cli.request_log_info()["path"])
    records = list(_cli.iter_request_records(tail=tail))
    if not records:
        print(f"[*] No request log entries found at {path}")
    for record in records:
        print(json.dumps(record, sort_keys=True))
    if getattr(args, "follow", False):
        last_size = path.stat().st_size if path.exists() else 0
        try:
            while True:
                time.sleep(1.0)
                if not path.exists():
                    continue
                size = path.stat().st_size
                if size < last_size:
                    last_size = 0
                if size == last_size:
                    continue
                with path.open("r", encoding="utf-8") as handle:
                    handle.seek(last_size)
                    for line in handle:
                        try:
                            parsed = json.loads(line)
                        except json.JSONDecodeError:
                            parsed = {"status": "malformed", "error": "malformed JSONL request-log entry"}
                        print(json.dumps(parsed, sort_keys=True), flush=True)
                    last_size = handle.tell()
        except KeyboardInterrupt:
            return


def start_gateway_background(args) -> dict:
    _cli.require_safe_gateway_host(args.host, args.allow_remote)
    pid_path, log_path = _cli.gateway_runtime_paths(args.port)
    base_url = _cli.local_gateway_base_url(args.host, args.port)
    current = _cli.gateway_status_info(args.port)
    if current["running"]:
        raise SystemExit(f"Gateway already running on port {args.port} (pid {current['pid']}).")
    if current["status"] in {"foreign", "unknown"}:
        raise SystemExit(
            f"Gateway pid file exists for port {args.port}, but pid {current['pid']} "
            "does not look like a codex-antigravity gateway. Refusing stale pid reuse; "
            f"inspect {current['pid_file']} before removing it."
        )
    if pid_path.exists():
        stale_pid = current.get("pid")
        pid_path.unlink(missing_ok=True)
        print(f"[*] Removed stale gateway pid file for pid {stale_pid or 'unknown'}: {pid_path}")
    try:
        _cli.gateway_model_ids(base_url, timeout=0.75)
    except RuntimeError:
        pass
    else:
        raise SystemExit(
            f"Gateway is already reachable at {base_url}. "
            "Stop the existing process before starting another background gateway."
        )
    cmd = [
        sys.executable,
        "-m",
        "uvicorn",
        "codex_antigravity_auth.server:app",
        "--host",
        args.host,
        "--port",
        str(args.port),
        "--log-level",
        "info",
    ]
    try:
        onepassword_description = _cli.onepassword_runtime_description(
            op_env_file=getattr(args, "op_env_file", None),
            op_environment=getattr(args, "op_environment", None),
        )
        cmd = _cli.wrap_with_onepassword(
            cmd,
            op_env_file=getattr(args, "op_env_file", None),
            op_environment=getattr(args, "op_environment", None),
        )
    except ValueError as exc:
        raise SystemExit(_cli.redact_secret_text(str(exc))) from exc
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
    if hasattr(os, "O_NOFOLLOW"):
        log_flags |= os.O_NOFOLLOW
    try:
        log_fd = os.open(log_path, log_flags, 0o600)
    except OSError as exc:
        raise SystemExit(f"Could not open gateway log file {log_path}: {_cli.redact_secret_text(str(exc))}") from exc
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(log_fd, 0o600)
        else:
            os.chmod(log_path, 0o600)
        log_file = os.fdopen(log_fd, "ab")
    except Exception:
        os.close(log_fd)
        raise
    with log_file:
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
        except FileNotFoundError as exc:
            raise SystemExit(
                "Could not start gateway through 1Password because `op` was not found. "
                "Install 1Password CLI or start without --op-env-file/--op-environment."
            ) from exc
    time.sleep(0.25)
    if proc.poll() is not None:
        raise SystemExit(f"Gateway exited during startup with code {proc.returncode}. See log: {log_path}")
    _cli._write_private_text(pid_path, f"{proc.pid}\n")
    try:
        _cli.wait_for_gateway_model_ids(base_url, timeout=0.75)
    except RuntimeError as exc:
        pid_path.unlink(missing_ok=True)
        try:
            proc.terminate()
        except Exception:
            pass
        raise SystemExit(
            f"Gateway process {proc.pid} did not become ready after startup. "
            f"See log: {log_path}. {_cli.redact_secret_text(str(exc))}"
        ) from exc
    info = _cli.gateway_status_info(args.port)
    print(f"[+] Gateway started in background on {args.host}:{args.port} (pid {proc.pid})")
    if onepassword_description:
        print(f"    Secrets: {onepassword_description}")
    print(f"    Log: {log_path}")
    return info


def stop_gateway(args) -> dict:
    info = _cli.gateway_status_info(args.port)
    pid_path = Path(info["pid_file"])
    pid = info.get("pid")
    if not pid:
        if pid_path.exists():
            pid_path.unlink()
        print(f"[*] Gateway is not running on port {args.port}.")
        service_info = _cli.service_status(args.port)
        if service_info.get("installed"):
            print(
                "[*] A durable gateway service is installed. Use "
                f"`codex-antigravity service uninstall --port {args.port}` to remove it."
            )
        return _cli.gateway_status_info(args.port)
    if info["status"] in {"foreign", "unknown"}:
        raise SystemExit(
            f"Pid file {pid_path} points at pid {pid}, but it does not look like a "
            "codex-antigravity gateway. Refusing to stop an unrelated process."
        )
    if not info["running"]:
        pid_path.unlink(missing_ok=True)
        print(f"[*] Removed stale gateway pid file for pid {pid}: {pid_path}")
        return _cli.gateway_status_info(args.port)
    try:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/PID", str(int(pid)), "/T"],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=5.0,
                check=False,
            )
        else:
            os.kill(int(pid), signal.SIGTERM)
    except ProcessLookupError:
        pid_path.unlink(missing_ok=True)
        print(f"[*] Removed stale gateway pid file for pid {pid}: {pid_path}")
        return _cli.gateway_status_info(args.port)
    except PermissionError as exc:
        raise SystemExit(
            f"Gateway pid {pid} could not be stopped because permission was denied. "
            f"Inspect the process manually and remove {pid_path} only if it is stale."
        ) from exc
    except OSError as exc:
        raise SystemExit(f"Gateway pid {pid} could not be stopped: {_cli.redact_secret_text(str(exc))}") from exc
    deadline = time.time() + 5
    while time.time() < deadline and _cli.process_is_running(int(pid)):
        time.sleep(0.1)
    if _cli.process_is_running(int(pid)):
        try:
            if sys.platform == "win32":
                subprocess.run(["taskkill", "/F", "/PID", str(int(pid)), "/T"], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5.0, check=False)
            else:
                os.kill(int(pid), signal.SIGKILL)
            time.sleep(0.5)
        except OSError:
            pass
        if _cli.process_is_running(int(pid)):
            raise SystemExit(f"Gateway pid {pid} did not stop within 5s. Log: {info['log_file']}")
    pid_path.unlink(missing_ok=True)
    print(f"[+] Gateway stopped on port {args.port} (pid {pid})")
    return _cli.gateway_status_info(args.port)

