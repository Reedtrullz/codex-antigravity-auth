from __future__ import annotations

import os
import platform
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

from .constants import get_codex_home
from .redaction import redact_secret_text


def service_platform() -> str:
    if sys.platform == "darwin":
        return "macos"
    if sys.platform.startswith("linux"):
        return "linux"
    if sys.platform == "win32":
        return "windows"
    return "unsupported"


def service_label(port: int) -> str:
    return f"com.codex-antigravity.gateway.{int(port)}"


def service_task_name(port: int) -> str:
    return f"CodexAntigravityGateway{int(port)}"


def service_log_paths(port: int) -> tuple[Path, Path]:
    home = get_codex_home()
    return home / f"antigravity-service-{port}.out.log", home / f"antigravity-service-{port}.err.log"


def service_command(port: int, host: str) -> list[str]:
    return [
        sys.executable,
        "-m",
        "codex_antigravity_auth.cli",
        "start",
        "--port",
        str(int(port)),
        "--host",
        str(host),
    ]


def macos_launch_agent_path(port: int) -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{service_label(port)}.plist"


def linux_systemd_unit_path(port: int) -> Path:
    return Path.home() / ".config" / "systemd" / "user" / f"codex-antigravity-gateway-{int(port)}.service"


def render_macos_launch_agent(port: int, host: str) -> str:
    stdout, stderr = service_log_paths(port)
    args = "\n".join(f"    <string>{_xml_escape(arg)}</string>" for arg in service_command(port, host))
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>{service_label(port)}</string>
  <key>ProgramArguments</key>
  <array>
{args}
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>{_xml_escape(str(stdout))}</string>
  <key>StandardErrorPath</key>
  <string>{_xml_escape(str(stderr))}</string>
</dict>
</plist>
"""


def render_linux_systemd_unit(port: int, host: str) -> str:
    stdout, stderr = service_log_paths(port)
    command = " ".join(shlex.quote(part) for part in service_command(port, host))
    return f"""[Unit]
Description=Codex Antigravity Gateway ({port})
After=network-online.target

[Service]
Type=simple
ExecStart={command}
Restart=on-failure
RestartSec=2
StandardOutput=append:{stdout}
StandardError=append:{stderr}

[Install]
WantedBy=default.target
"""


def install_service(port: int, host: str, *, platform_name: str | None = None) -> dict[str, Any]:
    platform_name = platform_name or service_platform()
    if platform_name == "macos":
        path = macos_launch_agent_path(port)
        if path.is_symlink():
            raise RuntimeError(f"Refusing to overwrite symlinked service file: {path}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render_macos_launch_agent(port, host), encoding="utf-8")
        os.chmod(path, 0o600)
        _run(["launchctl", "bootout", f"gui/{os.getuid()}", str(path)], allow_failure=True)
        _run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(path)], allow_failure=True)
        _run(["launchctl", "enable", f"gui/{os.getuid()}/{service_label(port)}"], allow_failure=True)
        return service_status(port, platform_name=platform_name)
    if platform_name == "linux":
        path = linux_systemd_unit_path(port)
        if path.is_symlink():
            raise RuntimeError(f"Refusing to overwrite symlinked service file: {path}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render_linux_systemd_unit(port, host), encoding="utf-8")
        os.chmod(path, 0o600)
        _run(["systemctl", "--user", "daemon-reload"], allow_failure=True)
        _run(["systemctl", "--user", "enable", "--now", path.name], allow_failure=True)
        return service_status(port, platform_name=platform_name)
    if platform_name == "windows":
        command = " ".join(_windows_quote(part) for part in service_command(port, host))
        _run(
            [
                "schtasks",
                "/Create",
                "/F",
                "/SC",
                "ONLOGON",
                "/TN",
                service_task_name(port),
                "/TR",
                command,
            ],
            allow_failure=False,
        )
        return service_status(port, platform_name=platform_name)
    raise RuntimeError(f"Unsupported service platform: {platform_name or platform.system()}")


def uninstall_service(port: int, *, platform_name: str | None = None) -> dict[str, Any]:
    platform_name = platform_name or service_platform()
    if platform_name == "macos":
        path = macos_launch_agent_path(port)
        _run(["launchctl", "bootout", f"gui/{os.getuid()}", str(path)], allow_failure=True)
        if path.exists() and not path.is_symlink():
            path.unlink()
        return service_status(port, platform_name=platform_name)
    if platform_name == "linux":
        path = linux_systemd_unit_path(port)
        _run(["systemctl", "--user", "disable", "--now", path.name], allow_failure=True)
        if path.exists() and not path.is_symlink():
            path.unlink()
        _run(["systemctl", "--user", "daemon-reload"], allow_failure=True)
        return service_status(port, platform_name=platform_name)
    if platform_name == "windows":
        _run(["schtasks", "/Delete", "/F", "/TN", service_task_name(port)], allow_failure=True)
        return service_status(port, platform_name=platform_name)
    raise RuntimeError(f"Unsupported service platform: {platform_name or platform.system()}")


def service_status(port: int, *, platform_name: str | None = None) -> dict[str, Any]:
    platform_name = platform_name or service_platform()
    if platform_name == "macos":
        path = macos_launch_agent_path(port)
        loaded = _run(["launchctl", "print", f"gui/{os.getuid()}/{service_label(port)}"], allow_failure=True).returncode == 0
        return {"platform": platform_name, "installed": path.is_file(), "active": loaded, "path": str(path)}
    if platform_name == "linux":
        path = linux_systemd_unit_path(port)
        active = _run(["systemctl", "--user", "is-active", "--quiet", path.name], allow_failure=True).returncode == 0
        enabled = _run(["systemctl", "--user", "is-enabled", "--quiet", path.name], allow_failure=True).returncode == 0
        return {"platform": platform_name, "installed": path.is_file() or enabled, "active": active, "path": str(path)}
    if platform_name == "windows":
        query = _run(["schtasks", "/Query", "/TN", service_task_name(port)], allow_failure=True)
        installed = query.returncode == 0
        return {"platform": platform_name, "installed": installed, "active": installed, "task_name": service_task_name(port)}
    return {"platform": platform_name, "installed": False, "active": False, "error": f"Unsupported platform: {platform_name}"}


def _run(cmd: list[str], *, allow_failure: bool) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5.0, check=False)
    except FileNotFoundError as exc:
        if allow_failure:
            return subprocess.CompletedProcess(cmd, 127, "", redact_secret_text(str(exc)))
        raise RuntimeError(redact_secret_text(str(exc))) from exc
    except subprocess.TimeoutExpired as exc:
        detail = str(exc.stderr or exc.stdout or exc)
        if allow_failure:
            return subprocess.CompletedProcess(cmd, 124, exc.stdout or "", redact_secret_text(detail))
        raise RuntimeError(redact_secret_text(detail)) from exc
    if result.returncode != 0 and not allow_failure:
        detail = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(redact_secret_text(detail or f"{cmd[0]} exited {result.returncode}"))
    return result


def _xml_escape(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _windows_quote(value: str) -> str:
    escaped = value.replace('"', r'\"')
    return f'"{escaped}"'
