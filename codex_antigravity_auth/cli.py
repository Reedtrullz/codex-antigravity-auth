import sys
import os
import argparse
import getpass
import http.server
import math
import re
import signal
import shlex
import socketserver
import subprocess
import webbrowser
import time
import json
import tempfile
import secrets
import urllib.error
import urllib.request
from importlib import metadata as importlib_metadata
from importlib.resources import files
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse
from .byok import (
    PROVIDER_PRESETS,
    all_provider_configs,
    all_provider_configs_read_only,
    has_provider_api_key_env,
    get_providers_json_path,
    load_provider_config,
    provider_auth_mode,
    provider_allows_keyless_local_use,
    provider_preset,
    providers_json_path_read_only,
    remove_provider_config,
    resolve_api_key,
    set_provider_config,
    split_provider_model,
    validate_http_base_url,
    validate_provider_api_key,
    validate_provider_id,
)
from .models import (
    DEFAULT_CODEX_MODEL_ID,
    NATIVE_MODELS,
    add_model_overlay,
    canonical_model_id,
    load_model_overlays,
    model_identifier_collisions,
    native_model_definition,
    native_model_family,
    native_model_catalog,
    remove_model_overlay,
    validate_model_id,
    validate_overlay_model,
)
from .observability import clean_request_logs, iter_request_records, request_log_info, request_log_summary
from .onepassword import onepassword_runtime_description, wrap_with_onepassword
from .oauth import (
    OAUTH_HTTP_TIMEOUT_SECONDS,
    authorize_antigravity,
    decode_state,
    encode_state,
    exchange_antigravity,
    generate_pkce,
    token_expires_in_seconds,
)
from .service import install_service, service_status, uninstall_service
from .service_manager import observed_service_result
from .storage import (
    account_store_diagnostics,
    load_accounts,
    load_accounts_read_only,
    provider_store_diagnostics,
    save_accounts,
    update_accounts,
)
from .account_state import scoped_cooldown_expiry
from .constants import (
    get_codex_home,
    is_loopback_host,
    resolve_oauth_credentials,
    save_oauth_credentials,
    validate_gateway_token_strength,
)
from .redaction import redact_secret_text
from .xai_oauth import (
    XAI_OAUTH_REDIRECT_URI,
    build_xai_authorize_url,
    clear_xai_oauth_tokens,
    exchange_xai_authorization_code,
    poll_xai_device_code_token,
    request_xai_device_code,
    resolve_xai_oauth_access_token,
    save_xai_oauth_token_response,
    xai_oauth_status,
)

_DEFAULT_LOAD_ACCOUNTS = load_accounts
_DEFAULT_ALL_PROVIDER_CONFIGS = all_provider_configs


def _diagnostic_load_accounts() -> dict:
    # Preserve test/plugin monkeypatch seams while keeping production diagnostics read-only.
    if load_accounts is not _DEFAULT_LOAD_ACCOUNTS:
        return load_accounts()
    return load_accounts_read_only()


def _diagnostic_all_provider_configs() -> dict[str, dict]:
    if all_provider_configs is not _DEFAULT_ALL_PROVIDER_CONFIGS:
        return all_provider_configs()
    return all_provider_configs_read_only()

DEFAULT_CODEX_PROVIDER_ID = "antigravity"
DEFAULT_CODEX_PROVIDER_NAME = "Google Antigravity"
DEFAULT_CODEX_MODEL = DEFAULT_CODEX_MODEL_ID
DEFAULT_CODEX_BASE_URL = "http://localhost:51122/v1"
DEFAULT_CODEX_SKILLS_DIR = "~/.codex/skills"
BUNDLED_CODEX_SKILL_NAME = "anti"
CODEX_PROVIDER_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
GATEWAY_PID_TEMPLATE = "antigravity-gateway-{port}.pid"
GATEWAY_LOG_TEMPLATE = "antigravity-gateway-{port}.log"
GATEWAY_READY_TIMEOUT_SECONDS = 10.0
GATEWAY_READY_RETRY_INTERVAL_SECONDS = 0.25
VERSION_CACHE_FILE = "antigravity-version-check.json"
VERSION_CHECK_MAX_AGE_SECONDS = 86_400
PYPI_PROJECT_JSON_URL = "https://pypi.org/pypi/codex-antigravity-auth/json"


class OAuthCallbackHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # Suppress logging of HTTP requests to keep CLI clean
        pass

    def _write_html(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)

        if "code" in query:
            code = query["code"][0]
            state = query.get("state", [None])[0]
            expected_state_id = getattr(self.server, "expected_state_id", None)
            if expected_state_id:
                try:
                    returned_state = decode_state(state or "")
                except Exception:
                    returned_state = {}
                if returned_state.get("id") != expected_state_id:
                    self._write_html(400, b"""
                    <html>
                    <head><style>body { font-family: sans-serif; text-align: center; margin-top: 50px; background-color: #f4f7f6; }</style></head>
                    <body>
                        <h1 style="color: #f44336;">Authentication Failed</h1>
                        <p>The OAuth callback state did not match the active login attempt.</p>
                    </body>
                    </html>
                    """)
                    return
            # Store globally on server to be grabbed by parent thread
            self.server.auth_code = code
            self.server.auth_state = state
            self._write_html(200, b"""
            <html>
            <head><style>body { font-family: sans-serif; text-align: center; margin-top: 50px; background-color: #f4f7f6; }</style></head>
            <body>
                <h1 style="color: #4caf50;">Authentication Successful!</h1>
                <p>You can close this tab and return to the terminal.</p>
            </body>
            </html>
            """)
        else:
            self._write_html(400, b"""
            <html>
            <head><style>body { font-family: sans-serif; text-align: center; margin-top: 50px; background-color: #f4f7f6; }</style></head>
            <body>
                <h1 style="color: #f44336;">Authentication Failed</h1>
                <p>Could not retrieve authorization code.</p>
            </body>
            </html>
            """)

class OAuthServer(socketserver.TCPServer):
    allow_reuse_address = True
    auth_code = None
    auth_state = None
    expected_state_id = None

def normalize_epoch_seconds(value):
    try:
        ts = float(value or 0)
    except (TypeError, ValueError):
        return 0
    if not math.isfinite(ts):
        return 0
    # Implausibly-future second epochs are safer treated as millisecond epochs:
    # bad local state should expire/refresh, not pin an account active for centuries.
    if ts > 10_000_000_000:
        ts = ts / 1000
    return ts


def positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as e:
        raise argparse.ArgumentTypeError("must be a positive integer") from e
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def bundled_skill_root():
    root = files("codex_antigravity_auth").joinpath("skills", BUNDLED_CODEX_SKILL_NAME)
    if not root.is_dir():
        raise RuntimeError(f"Bundled Codex skill '{BUNDLED_CODEX_SKILL_NAME}' is missing from this install.")
    return root


def _resource_tree_manifest(root, prefix: str = "") -> dict[str, bytes]:
    manifest: dict[str, bytes] = {}
    for item in root.iterdir():
        if item.name == "__pycache__" or item.name == ".DS_Store":
            continue
        rel = f"{prefix}{item.name}"
        if item.is_dir():
            manifest.update(_resource_tree_manifest(item, f"{rel}/"))
        elif item.is_file():
            manifest[rel] = item.read_bytes()
    return manifest


def _path_tree_manifest(root: Path) -> dict[str, bytes]:
    manifest: dict[str, bytes] = {}
    if not root.exists():
        return manifest
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        if "__pycache__" in path.parts or path.name == ".DS_Store":
            continue
        if path.is_symlink():
            manifest[rel] = f"symlink:{os.readlink(path)}".encode("utf-8", "surrogateescape")
        elif path.is_file():
            manifest[rel] = path.read_bytes()
    return manifest


def _copy_resource_tree(source, target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    for item in source.iterdir():
        if item.name == "__pycache__" or item.name == ".DS_Store":
            continue
        destination = target / item.name
        if item.is_dir():
            _copy_resource_tree(item, destination)
        elif item.is_file():
            destination.write_bytes(item.read_bytes())
            mode = 0o755 if destination.parent.name == "scripts" and destination.suffix == ".py" else 0o644
            os.chmod(destination, mode)


def _skill_backup_root(skill_dir: Path) -> Path:
    return skill_dir.with_name(f"{skill_dir.name}-backups")


def install_codex_skill(
    skill_dir: Path,
    *,
    force: bool = False,
    dry_run: bool = False,
) -> tuple[str, Path, Path | None]:
    skill_root = bundled_skill_root()
    destination = skill_dir.expanduser() / BUNDLED_CODEX_SKILL_NAME
    bundled_manifest = _resource_tree_manifest(skill_root)

    if destination.is_symlink():
        raise RuntimeError(f"Refusing to replace symlinked Codex skill path: {destination}")

    backup_path = None
    if destination.exists():
        if destination.is_dir() and _path_tree_manifest(destination) == bundled_manifest:
            return "unchanged", destination, None
        if not force:
            raise RuntimeError(
                f"Codex skill already exists at {destination}. "
                "Use --force to back it up and replace it with the bundled skill."
            )
        backup_root = _skill_backup_root(skill_dir.expanduser())
        backup_base = backup_root / f"{destination.name}.backup-{time.strftime('%Y%m%d%H%M%S')}"
        backup_path = backup_base
        suffix = 2
        while backup_path.exists():
            backup_path = backup_base.with_name(f"{backup_base.name}-{suffix}")
            suffix += 1
        if not dry_run:
            backup_root.mkdir(parents=True, exist_ok=True)
            destination.rename(backup_path)
            _copy_resource_tree(skill_root, destination)
        return "replaced", destination, backup_path

    if not dry_run:
        destination.parent.mkdir(parents=True, exist_ok=True)
        _copy_resource_tree(skill_root, destination)
    return "installed", destination, None


def codex_skill_short_description(skill_path: Path) -> str | None:
    agent_path = skill_path / "agents" / "openai.yaml"
    if not agent_path.is_file():
        return None
    for line in agent_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("short_description:"):
            return stripped.split(":", 1)[1].strip().strip('"').strip("'")
    return None


def codex_skill_matches_bundled(skill_path: Path) -> bool:
    if not skill_path.is_dir():
        return False
    return _path_tree_manifest(skill_path) == _resource_tree_manifest(bundled_skill_root())


def verify_codex_skill(skill_path: Path) -> bool:
    required = [
        skill_path / "SKILL.md",
        skill_path / "agents" / "openai.yaml",
        skill_path / "scripts" / "anti.py",
        skill_path / "tests" / "test_anti.py",
    ]
    missing = [path for path in required if not path.is_file()]
    if missing:
        for path in missing:
            print(f"[FAIL] Missing skill file: {path}")
        return False
    proc = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", str(skill_path / "tests")],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    if proc.returncode != 0:
        if proc.stdout:
            print(proc.stdout.rstrip())
        print("[FAIL] Installed Anti skill tests failed")
        return False
    print("[PASS] Installed Anti skill tests passed")
    return True


def run_install_skill(args) -> None:
    try:
        action, destination, backup_path = install_codex_skill(
            Path(os.path.expanduser(args.skill_dir)),
            force=args.force,
            dry_run=args.dry_run,
        )
    except RuntimeError as e:
        raise SystemExit(str(e)) from e

    prefix = "[dry-run] " if args.dry_run else ""
    if action == "unchanged":
        print(f"[*] Codex Anti skill is already installed: {destination}")
    elif action == "installed":
        print(f"[+] {prefix}Installed Codex Anti skill: {destination}")
    elif action == "replaced":
        print(f"[+] {prefix}Installed Codex Anti skill: {destination}")
        if backup_path:
            print(f"[+] {prefix}Previous skill backup: {backup_path}")
    description = codex_skill_short_description(destination)
    if description:
        print(f"    Skill chip: Anti — {description}")
    print("    Invoke it in Codex with: $anti review this diff with opus")
    if getattr(args, "verify", False) and not args.dry_run:
        if not verify_codex_skill(destination):
            raise SystemExit(1)


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


def _responses_output_preview(payload: dict) -> str:
    if not isinstance(payload, dict):
        return ""
    direct = payload.get("output_text")
    if isinstance(direct, str):
        return direct.strip()
    fragments: list[str] = []
    output = payload.get("output")
    if isinstance(output, list):
        for item in output:
            if not isinstance(item, dict):
                continue
            content = item.get("content")
            if isinstance(content, list):
                for part in content:
                    if not isinstance(part, dict):
                        continue
                    text = part.get("text") or part.get("output_text")
                    if isinstance(text, str):
                        fragments.append(text)
            text = item.get("text")
            if isinstance(text, str):
                fragments.append(text)
    return "".join(fragments).strip()


def gateway_generate_probe(
    base_url: str,
    model: str,
    *,
    timeout: float,
    token_env: str,
    max_output_tokens: int = 16,
) -> dict:
    url = base_url.rstrip("/") + "/responses"
    body = {
        "model": model,
        "input": "Reply with the single word: ready",
        "max_output_tokens": max_output_tokens,
        "stream": False,
    }
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    token = os.environ.get(token_env, "").strip() if token_env else ""
    if token:
        headers["Authorization"] = f"Bearer {token}"
    started = time.monotonic()
    result = {
        "ok": False,
        "model": model,
        "latency_ms": 0,
        "output_preview": "",
        "http_status": None,
        "error": None,
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            raw = response.read()
            result["http_status"] = getattr(response, "status", 200)
    except urllib.error.HTTPError as exc:
        result["latency_ms"] = int((time.monotonic() - started) * 1000)
        result["http_status"] = exc.code
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace")
        except Exception:
            detail = str(exc)
        hint = ""
        if exc.code in (401, 403) and not token:
            hint = f" (remote gateways require a bearer token; export {token_env})"
        result["error"] = redact_secret_text(f"HTTP {exc.code}: {detail}{hint}")[:500]
        return result
    except Exception as exc:
        result["latency_ms"] = int((time.monotonic() - started) * 1000)
        result["error"] = redact_secret_text(str(exc))[:500]
        return result
    result["latency_ms"] = int((time.monotonic() - started) * 1000)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        result["error"] = f"Gateway returned non-JSON data: {redact_secret_text(str(exc))}"
        return result
    preview = redact_secret_text(_responses_output_preview(payload)).replace("\n", " ").strip()
    result["output_preview"] = preview[:80]
    result["ok"] = 200 <= int(result["http_status"] or 0) < 300
    if not result["ok"] and not result["error"]:
        result["error"] = redact_secret_text(str(payload))[:500]
    return result


def _source_checkout_version() -> str | None:
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    try:
        text = pyproject.read_text(encoding="utf-8")
    except Exception:
        return None
    if 'name = "codex-antigravity-auth"' not in text:
        return None
    match = re.search(r'(?m)^version\s*=\s*"([^"]+)"\s*$', text)
    if not match:
        return None
    return match.group(1)


def _installed_package_version() -> str | None:
    source_version = _source_checkout_version()
    if source_version:
        return source_version
    try:
        return importlib_metadata.version("codex-antigravity-auth")
    except importlib_metadata.PackageNotFoundError:
        try:
            from . import __version__  # type: ignore

            return str(__version__)
        except Exception:
            return None


def _version_tuple(version: str | None) -> tuple[int, ...]:
    if not version:
        return ()
    parts: list[int] = []
    for part in re.split(r"[.\-+]", version):
        if part.isdigit():
            parts.append(int(part))
        else:
            break
    return tuple(parts)


def _version_cache_path() -> Path:
    return get_codex_home() / VERSION_CACHE_FILE


def _read_version_cache(now: float) -> dict | None:
    path = _version_cache_path()
    try:
        if path.is_symlink() or not path.is_file():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    checked_at = data.get("checked_at")
    latest = data.get("latest")
    if not isinstance(checked_at, (int, float)) or not isinstance(latest, str):
        return None
    if now - float(checked_at) > VERSION_CHECK_MAX_AGE_SECONDS:
        return None
    return data


def _write_version_cache(latest: str) -> None:
    payload = json.dumps({"checked_at": time.time(), "latest": latest}, indent=2, sort_keys=True) + "\n"
    _write_private_text(_version_cache_path(), payload)


def latest_pypi_version(timeout: float = 2.0) -> str | None:
    req = urllib.request.Request(PYPI_PROJECT_JSON_URL, headers={"Accept": "application/json"}, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    info = payload.get("info") if isinstance(payload, dict) else None
    latest = info.get("version") if isinstance(info, dict) else None
    return latest if isinstance(latest, str) and latest else None


def version_check_result(*, timeout: float = 2.0) -> dict:
    installed = _installed_package_version()
    result = {
        "status": "skip",
        "installed": installed,
        "latest": None,
        "detail": "version check skipped",
    }
    if os.environ.get("CODEX_ANTIGRAVITY_NO_UPDATE_CHECK") == "1":
        result["detail"] = "version check disabled by CODEX_ANTIGRAVITY_NO_UPDATE_CHECK=1"
        return result
    now = time.time()
    cache = _read_version_cache(now)
    latest = cache.get("latest") if cache else None
    if latest is None:
        try:
            latest = latest_pypi_version(timeout=timeout)
            if latest:
                _write_version_cache(latest)
        except Exception:
            result["detail"] = "version check unavailable"
            return result
    result["latest"] = latest
    if not installed or not latest:
        result["detail"] = "version check unavailable"
        return result
    installed_tuple = _version_tuple(installed)
    latest_tuple = _version_tuple(latest)
    if not installed_tuple or not latest_tuple:
        result["detail"] = "version check unavailable"
        return result
    if latest_tuple > installed_tuple:
        result["status"] = "warn"
        result["detail"] = (
            f"Update available: {installed} -> {latest} "
            "(pip install -U codex-antigravity-auth, or uv tool upgrade codex-antigravity-auth)"
        )
    else:
        result["status"] = "pass"
        result["detail"] = f"codex-antigravity-auth {installed} is current"
    return result


def _validate_google_live_model(model: str) -> tuple[str | None, str | None]:
    try:
        canonical = validate_codex_model_id(model)
    except ValueError as exc:
        return None, f"live model is invalid: {exc}"
    provider_prefix, _provider_model = split_provider_model(canonical)
    if provider_prefix is not None:
        return None, "live generation smoke currently supports Google Antigravity models only"
    return canonical, None


def gateway_base_url_for_port(port: int) -> str:
    try:
        parsed_port = int(port)
    except (TypeError, ValueError) as exc:
        raise ValueError("Gateway port must be an integer") from exc
    if parsed_port < 1 or parsed_port > 65535:
        raise ValueError("Gateway port must be between 1 and 65535")
    return f"http://localhost:{parsed_port}/v1"


def setup_effective_base_url(args) -> str:
    raw_base_url = getattr(args, "base_url", None)
    if not raw_base_url:
        return gateway_base_url_for_port(getattr(args, "port", 51122))
    base_url = validate_http_base_url(raw_base_url, label="gateway base URL")
    if getattr(args, "start", False):
        parsed = urlparse(base_url)
        actual_port = parsed.port
        if actual_port is None:
            actual_port = 443 if parsed.scheme == "https" else 80
        expected_port = int(getattr(args, "port", 51122))
        if actual_port != expected_port:
            raise ValueError(
                f"setup --start got --port {expected_port}, but --base-url points at port {actual_port}; "
                "omit --base-url to derive it from --port, or pass matching values"
            )
    return base_url


def wait_for_gateway_model_ids(
    base_url: str,
    *,
    timeout: float = 2.0,
    token_env: str = "ANTIGRAVITY_GATEWAY_TOKEN",
    wait_seconds: float = GATEWAY_READY_TIMEOUT_SECONDS,
    interval: float = GATEWAY_READY_RETRY_INTERVAL_SECONDS,
) -> set[str]:
    deadline = time.monotonic() + max(0.0, float(wait_seconds))
    attempts = 0
    while True:
        attempts += 1
        try:
            return gateway_model_ids(base_url, timeout=timeout, token_env=token_env)
        except RuntimeError as exc:
            if time.monotonic() >= deadline:
                message = redact_secret_text(str(exc))
                raise RuntimeError(
                    f"{base_url.rstrip('/')}/models did not become ready within {wait_seconds:g}s "
                    f"after {attempts} attempt(s): {message}"
                ) from exc
            time.sleep(max(0.0, min(interval, deadline - time.monotonic())))


def gateway_runtime_paths(port: int) -> tuple[Path, Path]:
    codex_home = get_codex_home()
    return codex_home / GATEWAY_PID_TEMPLATE.format(port=port), codex_home / GATEWAY_LOG_TEMPLATE.format(port=port)


def local_gateway_base_url(host: str, port: int) -> str:
    return f"http://{host}:{port}/v1"


def add_gateway_reachability(info: dict, *, host: str = "127.0.0.1", timeout: float = 5.0) -> dict:
    base_url = local_gateway_base_url(host, int(info["port"]))
    try:
        model_ids = gateway_model_ids(base_url, timeout=timeout)
    except RuntimeError as exc:
        info["reachable"] = False
        info["reachable_base_url"] = base_url
        info["reachability_error"] = redact_secret_text(str(exc))
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
    command = gateway_process_command(pid)
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
    pid_path, log_path = gateway_runtime_paths(port)
    pid = read_pid_file(pid_path) if pid_path.exists() else None
    process_running = bool(pid and process_is_running(pid))
    process_matches = gateway_pid_matches(pid) if pid and process_running else None
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
    info = reachable_gateway_status_info(args.port, wait=True, timeout=5.0)
    info["service"] = service_status(args.port)
    info["request_log"] = request_log_info()
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
        gateway = gateway_status_info(port)
        add_gateway_reachability(gateway)
        if not wait or gateway.get("reachable") or time.monotonic() >= deadline:
            return gateway
        time.sleep(0.25)


def run_service_command(args) -> dict:
    try:
        if args.service_command == "install":
            require_safe_gateway_host(args.host, allow_remote=False)
            info = install_service(
                args.port,
                args.host,
                op_env_file=getattr(args, "op_env_file", None),
                op_environment=getattr(args, "op_environment", None),
            )
            action = "installed"
        elif args.service_command == "uninstall":
            info = uninstall_service(args.port)
            action = "uninstalled"
        elif args.service_command == "status":
            info = service_status(args.port)
            action = "status"
        else:
            raise SystemExit("service requires install, uninstall, or status")
    except (RuntimeError, ValueError) as exc:
        raise SystemExit(redact_secret_text(str(exc))) from exc
    if action == "installed" and (
        info.get("state") == "failed"
        or not bool(info.get("installed"))
        or not bool(info.get("active"))
    ):
        detail = info.get("error") or "service installation was not observed as installed and active"
        raise SystemExit(redact_secret_text(str(detail)))
    if action == "uninstalled" and bool(info.get("installed")):
        detail = info.get("error") or "service uninstall was not observed"
        raise SystemExit(redact_secret_text(str(detail)))
    gateway = reachable_gateway_status_info(
        args.port,
        wait=action == "installed" and bool(info.get("installed")) and bool(info.get("active")),
    )
    result_action = {"installed": "install", "uninstalled": "uninstall"}.get(action, "status")
    observed = observed_service_result(
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
                    onepassword_description = onepassword_runtime_description(
                        op_env_file=getattr(args, "op_env_file", None),
                        op_environment=getattr(args, "op_environment", None),
                    )
                except ValueError as exc:
                    raise SystemExit(redact_secret_text(str(exc))) from exc
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
        removed = clean_request_logs()
        if getattr(args, "json", False):
            print(json.dumps({"removed": removed}, indent=2))
        else:
            print(f"[+] Removed {len(removed)} request log file(s).")
        return
    if getattr(args, "logs_action", None) == "summary":
        try:
            summary = request_log_summary(since=getattr(args, "since", "24h"))
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
        print(json.dumps(list(iter_request_records(tail=tail)), indent=2))
        return
    path = Path(request_log_info()["path"])
    records = list(iter_request_records(tail=tail))
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


def _confirm_account_mutation(prompt: str, *, yes: bool, non_interactive_error: str) -> bool:
    if yes:
        return True
    if not sys.stdin.isatty():
        raise SystemExit(non_interactive_error)
    answer = input(f"{prompt} [y/N] ").strip().lower()
    return answer in {"y", "yes"}


def run_accounts_command(args) -> None:
    action = getattr(args, "accounts_action", None) or "list"
    if action == "list":
        data = load_accounts()
        accounts = data.get("accounts", [])
        if not accounts:
            print("[*] No configured accounts found. Run `codex-antigravity login` first.")
            return
        print("[*] Configured Google Accounts:")
        print_account_rotation_summary(data)
        return

    if action == "remove":
        email = getattr(args, "email", "")
        if not _confirm_account_mutation(
            f"Remove Google account {email} from the encrypted rotation store?",
            yes=getattr(args, "yes", False),
            non_interactive_error="accounts remove requires --yes in non-interactive shells",
        ):
            print("[*] Account removal cancelled.")
            return
        try:
            result = remove_google_account(email)
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
        print(f"[+] Removed Google account {result['email']}; {result['account_count']} account(s) remain.")
        return

    if action == "reset":
        email = getattr(args, "email", None)
        all_accounts = bool(getattr(args, "all_accounts", False))
        if all_accounts and not _confirm_account_mutation(
            "Reset cooldown and failure state for all Google accounts?",
            yes=getattr(args, "yes", False),
            non_interactive_error="accounts reset --all requires --yes in non-interactive shells",
        ):
            print("[*] Account reset cancelled.")
            return
        try:
            result = reset_google_account_state(email, all_accounts=all_accounts)
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
        cleared = result["cleared"]
        target = "all Google accounts" if all_accounts else ", ".join(result["emails"])
        print(
            f"[+] Reset cooldown/failure state for {target}: "
            f"{cleared['cooldowns']} cooldown(s), {cleared['failures']} failure count(s) cleared."
        )
        return

    raise SystemExit(f"Unsupported accounts action: {action}")


def run_models_command(args) -> None:
    if args.models_command == "list":
        try:
            overlays = load_model_overlays(strict=True)
            catalog = native_model_catalog(strict_overlays=True)
        except ValueError as exc:
            raise SystemExit(redact_secret_text(str(exc))) from exc
        if getattr(args, "json", False):
            print(json.dumps({"models": catalog, "overlays": [model.id for model in overlays]}, indent=2))
            return
        for model in catalog:
            aliases = ", ".join(model.get("aliases", []))
            suffix = f" aliases: {aliases}" if aliases else ""
            print(f"- {model['id']}: {model['display_name']} -> {model['backend_id']} [{model['family']}]{suffix}")
        return
    if args.models_command == "add":
        try:
            model = validate_overlay_model(
                {
                    "id": validate_model_id(args.id),
                    "backend_id": args.backend_id,
                    "display_name": args.display_name or args.id,
                    "context_window": args.context_window,
                    "family": args.family,
                    "default_reasoning_level": args.default_reasoning_level,
                    "supports_parallel_tool_calls": not args.no_parallel_tool_calls,
                    "aliases": args.alias or [],
                }
            )
            add_model_overlay(model, force=args.force)
        except ValueError as exc:
            raise SystemExit(redact_secret_text(str(exc))) from exc
        print(f"[+] Added overlay model {model.id}")
        return
    if args.models_command == "remove":
        try:
            removed = remove_model_overlay(args.id)
        except ValueError as exc:
            raise SystemExit(redact_secret_text(str(exc))) from exc
        print(f"[+] Removed overlay model {args.id}" if removed else f"[*] No overlay model named {args.id}")
        return
    if args.models_command == "doctor":
        from .transform import thinking_budget_for_request

        ok = True
        try:
            overlays = load_model_overlays(strict=True)
        except ValueError as exc:
            ok = False
            overlays = []
            print(f"[FAIL] Model overlay: {redact_secret_text(str(exc))}")
        else:
            print(f"[PASS] Model overlay: {len(overlays)} local model(s)")
            seen_models = list(NATIVE_MODELS)
            for overlay in overlays:
                collisions = model_identifier_collisions(
                    overlay,
                    tuple(seen_models),
                    allow_same_id_shadow=any(existing.id == overlay.id for existing in NATIVE_MODELS),
                )
                if overlay.id in {model.id for model in NATIVE_MODELS}:
                    print(f"[WARN] {overlay.id}: overlay shadows a built-in model id")
                if collisions:
                    ok = False
                    formatted = ", ".join(
                        f"{label} -> {owner}" for label, owner in sorted(collisions.items())
                    )
                    print(f"[FAIL] {overlay.id}: identifier shadowing detected ({formatted})")
                seen_models.append(overlay)
        if not ok:
            raise SystemExit(1)
        for model in native_model_catalog(strict_overlays=True):
            definition = native_model_definition(model["id"])
            if not definition:
                ok = False
                print(f"[FAIL] {model['id']}: missing runtime definition")
            else:
                print(
                    f"[PASS] {model['id']}: {definition.backend_id}, "
                    f"reasoning={definition.default_reasoning_level}, context={definition.context_window}"
                )
                if definition.family == "claude":
                    budgets = {
                        effort: thinking_budget_for_request({"model": definition.id, "reasoning": {"effort": effort}}, definition.backend_id)
                        for effort in ("low", "medium", "high", "xhigh")
                    }
                    print(f"        thinking_budget: {budgets}")
        if not ok:
            raise SystemExit(1)


def start_gateway_background(args) -> dict:
    require_safe_gateway_host(args.host, args.allow_remote)
    pid_path, log_path = gateway_runtime_paths(args.port)
    base_url = local_gateway_base_url(args.host, args.port)
    current = gateway_status_info(args.port)
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
        gateway_model_ids(base_url, timeout=0.75)
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
        onepassword_description = onepassword_runtime_description(
            op_env_file=getattr(args, "op_env_file", None),
            op_environment=getattr(args, "op_environment", None),
        )
        cmd = wrap_with_onepassword(
            cmd,
            op_env_file=getattr(args, "op_env_file", None),
            op_environment=getattr(args, "op_environment", None),
        )
    except ValueError as exc:
        raise SystemExit(redact_secret_text(str(exc))) from exc
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
    if hasattr(os, "O_NOFOLLOW"):
        log_flags |= os.O_NOFOLLOW
    try:
        log_fd = os.open(log_path, log_flags, 0o600)
    except OSError as exc:
        raise SystemExit(f"Could not open gateway log file {log_path}: {redact_secret_text(str(exc))}") from exc
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
    _write_private_text(pid_path, f"{proc.pid}\n")
    try:
        wait_for_gateway_model_ids(base_url, timeout=0.75)
    except RuntimeError as exc:
        pid_path.unlink(missing_ok=True)
        try:
            proc.terminate()
        except Exception:
            pass
        raise SystemExit(
            f"Gateway process {proc.pid} did not become ready after startup. "
            f"See log: {log_path}. {redact_secret_text(str(exc))}"
        ) from exc
    info = gateway_status_info(args.port)
    print(f"[+] Gateway started in background on {args.host}:{args.port} (pid {proc.pid})")
    if onepassword_description:
        print(f"    Secrets: {onepassword_description}")
    print(f"    Log: {log_path}")
    return info


def stop_gateway(args) -> dict:
    info = gateway_status_info(args.port)
    pid_path = Path(info["pid_file"])
    pid = info.get("pid")
    if not pid:
        if pid_path.exists():
            pid_path.unlink()
        print(f"[*] Gateway is not running on port {args.port}.")
        service_info = service_status(args.port)
        if service_info.get("installed"):
            print(
                "[*] A durable gateway service is installed. Use "
                f"`codex-antigravity service uninstall --port {args.port}` to remove it."
            )
        return gateway_status_info(args.port)
    if info["status"] in {"foreign", "unknown"}:
        raise SystemExit(
            f"Pid file {pid_path} points at pid {pid}, but it does not look like a "
            "codex-antigravity gateway. Refusing to stop an unrelated process."
        )
    if not info["running"]:
        pid_path.unlink(missing_ok=True)
        print(f"[*] Removed stale gateway pid file for pid {pid}: {pid_path}")
        return gateway_status_info(args.port)
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
        return gateway_status_info(args.port)
    except PermissionError as exc:
        raise SystemExit(
            f"Gateway pid {pid} could not be stopped because permission was denied. "
            f"Inspect the process manually and remove {pid_path} only if it is stale."
        ) from exc
    except OSError as exc:
        raise SystemExit(f"Gateway pid {pid} could not be stopped: {redact_secret_text(str(exc))}") from exc
    deadline = time.time() + 5
    while time.time() < deadline and process_is_running(int(pid)):
        time.sleep(0.1)
    if process_is_running(int(pid)):
        raise SystemExit(f"Gateway pid {pid} did not stop within 5s. Log: {info['log_file']}")
    pid_path.unlink(missing_ok=True)
    print(f"[+] Gateway stopped on port {args.port} (pid {pid})")
    return gateway_status_info(args.port)


def google_family_rotation_status(data: dict, family: str) -> dict:
    accounts = data.get("accounts", []) if isinstance(data, dict) else []
    state = data.get("accountState", {}) if isinstance(data.get("accountState"), dict) else {}
    cooldowns = state.get("cooldowns", {}) if isinstance(state.get("cooldowns"), dict) else {}
    now = time.time()
    cooldown_count = 0
    available_count = 0
    for account in accounts:
        if not isinstance(account, dict):
            continue
        email = account.get("email")
        cooldown_end = scoped_cooldown_expiry(cooldowns.get(email, 0), family)
        if cooldown_end > now:
            cooldown_count += 1
        else:
            available_count += 1
    return {
        "family": family,
        "account_count": len(accounts),
        "available_count": available_count,
        "cooldown_count": cooldown_count,
        "all_accounts_cooling_down": bool(accounts) and available_count == 0,
    }


def _read_codex_config_for_readiness(config: str) -> tuple[Path, str | None, str | None]:
    config_path = Path(os.path.expanduser(config))
    if not config_path.is_file():
        return config_path, None, f"Codex config not found: {config_path}"
    try:
        return config_path, config_path.read_text(encoding="utf-8"), None
    except Exception as exc:
        return config_path, None, f"Could not read Codex config: {redact_secret_text(str(exc))}"


def readiness_storage_diagnostics() -> dict[str, dict]:
    return {
        "account_store": account_store_diagnostics(),
        "provider_store": provider_store_diagnostics(providers_json_path_read_only()),
    }


def provider_capability_mismatches(providers: dict[str, dict]) -> list[dict[str, str]]:
    mismatches: list[dict[str, str]] = []
    for provider_id, provider in sorted(providers.items()):
        kind = provider.get("kind")
        auth_mode = provider_auth_mode(provider)
        if kind == "openai_chat" and auth_mode != "api_key":
            mismatches.append(
                {"provider": provider_id, "reason": "openai_chat routes require api_key auth"}
            )
        elif kind == "openai_responses" and not (
            provider_id == "xai-oauth" and auth_mode == "oauth"
        ):
            mismatches.append(
                {
                    "provider": provider_id,
                    "reason": "native Responses routing currently requires xai-oauth OAuth",
                }
            )
        elif kind not in {"openai_chat", "openai_responses"}:
            mismatches.append(
                {"provider": provider_id, "reason": f"unsupported provider kind: {kind}"}
            )
    return mismatches


def codex_ready_report(
    *,
    config: str,
    provider_id: str,
    expected_base_url: str,
    gateway_timeout: float = 2.0,
    gateway_token_env: str = "ANTIGRAVITY_GATEWAY_TOKEN",
    live: bool = False,
    live_model: str | None = None,
    live_timeout: float = 30.0,
    include_version_check: bool = True,
    selected_model: str | None = None,
    require_active_provider: bool = True,
) -> dict:
    checks: list[dict] = []

    def add(name: str, status: str, detail: str, **extra) -> None:
        checks.append({"name": name, "status": status, "detail": detail, **extra})

    config_path, config_content, config_error = _read_codex_config_for_readiness(config)
    active_model = ""
    canonical_model = ""
    gateway_ids: set[str] | None = None
    route = "unknown"
    service_snapshot: dict = {}
    capability_mismatches: list[dict[str, str]] = []
    parsed_gateway = urlparse(expected_base_url)
    gateway_port = parsed_gateway.port or 51122

    if config_error:
        add("codex_config", "fail", config_error)
    else:
        inspector = inspect_codex_gateway_config if require_active_provider else inspect_codex_provider_block_config
        ready, reason = inspector(config_content or "", provider_id=provider_id, expected_base_url=expected_base_url)
        add("codex_config", "pass" if ready else "fail", reason, path=str(config_path))
        parsed = parse_codex_config(config_content or "")
        active_model = str(selected_model or parsed.get("active_model") or "")
        try:
            canonical_model = validate_codex_model_id(active_model)
            add("selected_model", "pass", f"Codex model resolves to {canonical_model}", model=canonical_model)
        except ValueError as exc:
            add("selected_model", "fail", str(exc), model=active_model)

    try:
        status_info = gateway_status_info(gateway_port)
        add_gateway_reachability(
            status_info,
            host=parsed_gateway.hostname or "127.0.0.1",
            timeout=max(float(gateway_timeout), 0.1),
        )
        service_info = service_status(gateway_port)
        service_snapshot = service_info
        if status_info["running"]:
            add("gateway_process", "pass", f"gateway process is running on port {gateway_port}", process_status=status_info["status"])
        elif status_info.get("reachable"):
            add(
                "gateway_process",
                "pass",
                f"gateway is reachable at {status_info['reachable_base_url']} without a managed pid",
                process_status=status_info["status"],
                reachable=True,
                service=service_info,
            )
        elif service_info.get("installed"):
            add(
                "gateway_process",
                "warn",
                f"gateway process is {status_info['status']}, but durable service is installed",
                process_status=status_info["status"],
                service=service_info,
            )
        else:
            add(
                "gateway_process",
                "warn",
                f"gateway process is {status_info['status']} and no durable service is installed",
                process_status=status_info["status"],
                service=service_info,
            )
        add(
            "gateway_service",
            "pass" if service_info.get("installed") else "warn",
            "durable gateway service is installed" if service_info.get("installed") else "durable gateway service is not installed",
            service=service_info,
        )
    except Exception as exc:
        add("gateway_process", "warn", f"Could not inspect local gateway/service state: {redact_secret_text(str(exc))}")

    try:
        gateway_ids = gateway_model_ids(expected_base_url, timeout=gateway_timeout, token_env=gateway_token_env)
        add("gateway_models", "pass", f"Gateway advertised {len(gateway_ids)} model(s)")
    except RuntimeError as exc:
        add("gateway_models", "fail", redact_secret_text(str(exc)))

    selected_for_catalog = canonical_model or active_model
    if selected_for_catalog and gateway_ids is not None:
        if selected_for_catalog in gateway_ids:
            add("model_catalog", "pass", f"{selected_for_catalog} is advertised by /v1/models")
        else:
            add("model_catalog", "fail", f"{selected_for_catalog} is not advertised by /v1/models")

    provider_prefix, provider_model = split_provider_model(selected_for_catalog) if selected_for_catalog else (None, "")
    if selected_for_catalog and provider_prefix is not None:
        route = "byok"
        try:
            providers = _diagnostic_all_provider_configs()
            capability_mismatches = provider_capability_mismatches(providers)
        except Exception as exc:
            add("model_route", "fail", f"Could not load BYOK provider configuration: {redact_secret_text(str(exc))}")
        else:
            provider = providers.get(provider_prefix)
            if not provider:
                if provider_prefix == "xai-oauth":
                    oauth_status = xai_oauth_status()
                    add(
                        "model_route",
                        "fail",
                        "xAI OAuth provider is not logged in" if not oauth_status.get("ready") else "xAI OAuth provider is not visible",
                        auth=oauth_status,
                    )
                else:
                    add("model_route", "fail", f"BYOK provider '{provider_prefix}' is not configured")
            elif provider_key_status(provider, configured_label="key OK") != "key OK":
                credential_name = "OAuth login" if provider_auth_mode(provider) == "oauth" else "key"
                add("model_route", "fail", f"BYOK provider '{provider_prefix}' does not have a usable {credential_name}")
            else:
                configured_models = [
                    str(model.get("id") if isinstance(model, dict) else model)
                    for model in provider.get("models", [])
                ]
                if provider_model in configured_models:
                    add("model_route", "pass", f"{selected_for_catalog} routes to configured BYOK provider")
                else:
                    add("model_route", "warn", f"{selected_for_catalog} routes to BYOK, but the exact model is not listed")
    elif selected_for_catalog:
        route = "google"
        definition = native_model_definition(selected_for_catalog)
        if definition:
            add("model_route", "pass", f"{selected_for_catalog} routes to Google Antigravity backend {definition.backend_id}")
        else:
            add("model_route", "warn", f"{selected_for_catalog} is not a known built-in Google Antigravity model")
        family = native_model_family(selected_for_catalog)
        try:
            rotation = google_family_rotation_status(_diagnostic_load_accounts(), family)
        except Exception as exc:
            add("google_rotation", "fail", f"Could not load Google account rotation state: {redact_secret_text(str(exc))}", family=family)
        else:
            if rotation["available_count"] > 0:
                add("google_rotation", "pass", f"{rotation['available_count']} {family} account(s) available", **rotation)
            elif rotation["account_count"] > 0:
                add("google_rotation", "fail", f"All {family} accounts are cooling down", **rotation)
            else:
                add("google_rotation", "fail", f"No Google accounts configured for {family}", **rotation)

    if live:
        probe_model = live_model or selected_for_catalog or DEFAULT_CODEX_MODEL
        probe_model, live_model_error = _validate_google_live_model(probe_model)
        if live_model_error:
            add("live_generation", "fail", live_model_error, probe={"ok": False, "model": live_model or selected_for_catalog or DEFAULT_CODEX_MODEL})
        else:
            probe = gateway_generate_probe(
                expected_base_url,
                probe_model,
                timeout=live_timeout,
                token_env=gateway_token_env,
            )
            output_preview = str(probe.get("output_preview") or "")
            status = "pass" if probe.get("ok") and output_preview else "fail"
            if status == "pass":
                detail = (
                    f"{probe_model} generated a response in {probe.get('latency_ms')}ms "
                    f"(preview: {output_preview})"
                )
            else:
                detail = f"{probe_model} live generation failed: {probe.get('error') or 'unknown error'}"
                if probe.get("ok") and not output_preview:
                    detail = f"{probe_model} live generation returned an empty output"
            add("live_generation", status, detail, probe=probe)

    if include_version_check:
        version = version_check_result()
        add(
            "version_check",
            version["status"],
            version["detail"],
            installed=version.get("installed"),
            latest=version.get("latest"),
        )

    storage_diagnostics = readiness_storage_diagnostics()
    for name, store in storage_diagnostics.items():
        if not store.get("accessible"):
            status = "fail" if store.get("exists") else "warn"
        elif store.get("migration") == "pending":
            status = "warn"
        else:
            status = "pass"
        add(
            name,
            status,
            f"{store.get('format')} store; migration {store.get('migration')}",
            store=store,
        )
    if not capability_mismatches:
        try:
            capability_mismatches = provider_capability_mismatches(_diagnostic_all_provider_configs())
        except Exception as exc:
            capability_mismatches = [{"provider": "unknown", "reason": redact_secret_text(str(exc))}]
    add(
        "provider_capabilities",
        "warn" if capability_mismatches else "pass",
        f"{len(capability_mismatches)} provider capability mismatch(es)",
        mismatches=capability_mismatches,
    )

    failed = [check for check in checks if check["status"] == "fail"]
    ok = not failed
    next_command = "codex"
    if failed:
        first = failed[0]["name"]
        if first == "codex_config" and config_path.exists():
            next_command = "codex-antigravity setup --repair"
        elif first in {"codex_config", "selected_model"}:
            next_command = f"codex-antigravity setup --write --accounts 1 --model {DEFAULT_CODEX_MODEL}"
        elif first == "gateway_models":
            next_command = f"codex-antigravity start --background --port {gateway_port}"
        elif first == "model_catalog":
            if provider_prefix == "xai-oauth" and not xai_oauth_status().get("ready"):
                next_command = "codex-antigravity provider login xai-oauth"
            else:
                next_command = "codex-antigravity status && codex-antigravity doctor --codex-ready"
        elif first == "model_route" and provider_prefix == "xai-oauth":
            next_command = "codex-antigravity provider login xai-oauth"
        elif first == "google_rotation":
            next_command = "codex-antigravity setup-google --accounts 1"
        else:
            next_command = "codex-antigravity doctor --codex-ready"
    return {
        "ok": ok,
        "config": str(config_path),
        "provider_id": provider_id,
        "base_url": expected_base_url,
        "active_model": active_model,
        "canonical_model": canonical_model,
        "route": route,
        "checks": checks,
        "request_log": request_log_info(),
        "diagnostics": {
            **storage_diagnostics,
            "service": service_snapshot,
            "provider_capability_mismatches": capability_mismatches,
        },
        "next_command": next_command,
    }


def run_codex_ready_doctor(args) -> bool:
    report = codex_ready_report(
        config=args.config,
        provider_id=args.provider,
        expected_base_url=args.gateway_base_url,
        gateway_timeout=getattr(args, "gateway_timeout", 2.0),
        gateway_token_env=getattr(args, "gateway_token_env", "ANTIGRAVITY_GATEWAY_TOKEN"),
        live=getattr(args, "live", False),
        live_model=getattr(args, "live_model", None),
        live_timeout=getattr(args, "live_timeout", 30.0),
    )
    if getattr(args, "json", False):
        print(json.dumps(report, indent=2))
        return bool(report["ok"])
    print("=" * 60)
    print("              CODEX ANTIGRAVITY READINESS           ")
    print("=" * 60)
    for check in report["checks"]:
        label = {"pass": "PASS", "warn": "WARN", "fail": "FAIL"}.get(check["status"], "INFO")
        print(f"[{label}] {check['name']}: {check['detail']}")
    print(f"Next command: {report['next_command']}")
    print("=" * 60)
    return bool(report["ok"])


def run_setup_v2(args) -> None:
    print("=" * 60)
    print("             ANTI V2 WORKFLOW SETUP CHECK           ")
    print("=" * 60)
    skill_dir = Path(os.path.expanduser(args.skill_dir))
    destination = skill_dir / BUNDLED_CODEX_SKILL_NAME

    try:
        bundled_skill_root()
        print("[PASS] Bundled Anti skill: present")
    except RuntimeError as exc:
        print(f"[FAIL] Bundled Anti skill: {exc}")
        raise SystemExit(1) from exc

    if args.write:
        install_args = argparse.Namespace(
            skill_dir=args.skill_dir,
            force=args.force,
            dry_run=False,
            verify=args.verify_skill,
        )
        run_install_skill(install_args)
    elif destination.is_dir():
        description = codex_skill_short_description(destination)
        if codex_skill_matches_bundled(destination):
            print(f"[PASS] Installed Anti skill: {destination}")
        else:
            print(f"[WARN] Installed Anti skill differs from bundled V2 skill: {destination}")
            print("       Run `codex-antigravity setup-v2 --write --force` to back it up and refresh it.")
        if description:
            print(f"       Skill chip: Anti — {description}")
    else:
        print(f"[WARN] Installed Anti skill: missing at {destination}")
        print("       Run `codex-antigravity setup-v2 --write` or `codex-antigravity install-skill`.")

    gateway_ids = None
    try:
        ids = gateway_model_ids(
            args.base_url,
            timeout=args.timeout,
            token_env=getattr(args, "gateway_token_env", "ANTIGRAVITY_GATEWAY_TOKEN"),
        )
        gateway_ids = ids
        print(f"[PASS] Gateway /v1/models: {len(ids)} model(s)")
        for model in ("claude-opus-4-6", "claude-3.5-sonnet"):
            if model in ids:
                print(f"       - [PASS] {model}")
            else:
                print(f"       - [WARN] {model} not advertised")
    except RuntimeError as exc:
        print(f"[WARN] Gateway /v1/models: {redact_secret_text(str(exc))}")
        print("       Start the gateway with `codex-antigravity start` when you want live workflows.")

    providers = {}
    if args.check_byok:
        try:
            providers = all_provider_configs()
            stored_providers = load_provider_config().get("providers", {})
            stored_provider_ids = set(stored_providers) if isinstance(stored_providers, dict) else set()
        except Exception as exc:
            print(f"[WARN] BYOK provider visibility: could not load provider config ({redact_secret_text(str(exc))})")
            providers = {}
            stored_provider_ids = set()
        if providers:
            print(f"[PASS] BYOK provider visibility: {len(providers)} configured/env/local provider(s)")
            if gateway_ids is None:
                print("[WARN] BYOK gateway advertisement: unverified because /v1/models was not reachable")
            for provider_id, provider in providers.items():
                status = provider_key_status(provider, configured_label=provider_configured_label(provider_id, provider, stored_provider_ids))
                models = provider.get("models", [])
                print(f"       - {provider_id}: {status}, {len(models)} model(s)")
                if gateway_ids is not None and models:
                    missing = []
                    for model_entry in models:
                        provider_model = model_entry.get("id") if isinstance(model_entry, dict) else str(model_entry)
                        if provider_model and f"{provider_id}:{provider_model}" not in gateway_ids:
                            missing.append(f"{provider_id}:{provider_model}")
                    if missing:
                        sample = ", ".join(missing[:5])
                        suffix = f" (+{len(missing) - 5} more)" if len(missing) > 5 else ""
                        print(f"         [WARN] not advertised by gateway: {sample}{suffix}")
        else:
            print("[WARN] BYOK readiness requested but no providers are visible")
    else:
        print("[INFO] BYOK provider checks skipped; pass --check-byok to inspect provider readiness")

    if args.check_google:
        cid, csec = resolve_oauth_credentials()
        if cid and csec:
            print("[PASS] Google OAuth credentials: configured")
        else:
            print("[WARN] Google OAuth credentials: missing")
        accounts = load_accounts().get("accounts", [])
        print(f"[INFO] Google account rotation pool: {len(accounts)} account(s)")

    if args.check_byok and providers:
        unusable = [
            provider_id
            for provider_id, provider in providers.items()
            if provider_key_status(provider, configured_label="key OK") != "key OK"
        ]
        if unusable:
            print("[WARN] BYOK providers not usable: " + ", ".join(unusable))
        elif gateway_ids is None:
            print("[INFO] BYOK local readiness: all visible providers have usable keys or local keyless access")
            print("       Gateway model-picker visibility remains unverified until /v1/models is reachable.")
        else:
            print("[PASS] BYOK readiness: all visible providers have usable keys or local keyless access")

    print("=" * 60)


def account_rotation_lines(data: dict | None = None) -> list[str]:
    data = data or load_accounts()
    accounts = data.get("accounts", [])
    family_map = data.get("activeIndexByFamily", {}) if isinstance(data.get("activeIndexByFamily"), dict) else {}
    state = data.get("accountState", {}) if isinstance(data.get("accountState"), dict) else {}
    failures = state.get("failures", {}) if isinstance(state.get("failures"), dict) else {}
    cooldowns = state.get("cooldowns", {}) if isinstance(state.get("cooldowns"), dict) else {}
    counters = state.get("counters", {}) if isinstance(state.get("counters"), dict) else {}
    now = time.time()
    lines = [f"[*] Google account rotation pool: {len(accounts)} account(s)"]

    def counter_int(value) -> int:
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            return 0

    for idx, acc in enumerate(accounts):
        email = acc.get("email", "(missing email)")
        markers = []
        for family in ("gemini", "claude"):
            if family_map.get(family, 0) == idx:
                markers.append(f"{family} active")
        expires_at = normalize_epoch_seconds(acc.get("expiresAt", 0))
        token_status = "token OK" if expires_at > now + 300 else "will refresh"
        family_expiries = {
            family: scoped_cooldown_expiry(cooldowns.get(email, 0), family)
            for family in ("claude", "gemini")
        }
        cooldown_end = max(family_expiries.values())
        if cooldown_end > now:
            cooldown_status = f"cooldown {int(cooldown_end - now)}s"
        else:
            cooldown_status = "available"
        failure_count = failures.get(email, 0)
        failure_text = f", failures={failure_count}" if failure_count else ""
        counter_texts = []
        family_counters = counters.get(email, {}) if isinstance(counters, dict) else {}
        if isinstance(family_counters, dict):
            for family in ("claude", "gemini"):
                counter = family_counters.get(family)
                if not isinstance(counter, dict):
                    continue
                total = counter_int(counter.get("total_requests", 0))
                if not total:
                    continue
                counter_texts.append(
                    f"{family}: requests={total}, failures={counter_int(counter.get('failures', 0))}, "
                    f"429s={counter_int(counter.get('rate_limits', 0))}"
                )
        marker_text = f" [{', '.join(markers)}]" if markers else ""
        lines.append(f"    [{idx}] {email}{marker_text} - {token_status}, {cooldown_status}{failure_text}")
        for counter_text in counter_texts:
            lines.append(f"        usage: {counter_text}")
    return lines


def print_account_rotation_summary(data: dict | None = None) -> None:
    for line in account_rotation_lines(data):
        print(line)


def upsert_google_account(data: dict, account_entry: dict) -> dict:
    email = account_entry.get("email")
    if not email:
        raise ValueError("Google account email is required")
    accounts = data.setdefault("accounts", [])
    existing_idx = None
    for idx, acc in enumerate(accounts):
        if acc.get("email") == email:
            existing_idx = idx
            break

    if existing_idx is not None:
        accounts[existing_idx].update(account_entry)
    else:
        accounts.append(account_entry)

    state = data.setdefault("accountState", {})
    if isinstance(state, dict):
        for bucket_name in ("failures", "cooldowns"):
            bucket = state.get(bucket_name)
            if isinstance(bucket, dict):
                bucket.pop(email, None)

    return {"email": email, "created": existing_idx is None, "account_count": len(accounts)}


def _active_index_after_removal(value, removed_index: int, account_count: int) -> int:
    if account_count <= 0:
        return 0
    if not isinstance(value, int) or isinstance(value, bool):
        return 0
    if value > removed_index:
        value -= 1
    elif value == removed_index:
        value = min(removed_index, account_count - 1)
    if value < 0 or value >= account_count:
        return 0
    return value


def remove_google_account(email: str) -> dict:
    target_email = str(email or "").strip()
    if not target_email:
        raise ValueError("Google account email is required")
    result: dict = {}

    def mutate(data: dict) -> bool:
        accounts = data.get("accounts", [])
        if not isinstance(accounts, list):
            accounts = []
        removed_index = None
        for idx, account in enumerate(accounts):
            if isinstance(account, dict) and account.get("email") == target_email:
                removed_index = idx
                break
        if removed_index is None:
            raise ValueError(f"No configured Google account found for {target_email}")

        removed = accounts.pop(removed_index)
        data["accounts"] = accounts
        remaining = len(accounts)
        data["activeIndex"] = _active_index_after_removal(data.get("activeIndex"), removed_index, remaining)
        family_map = data.get("activeIndexByFamily")
        if not isinstance(family_map, dict):
            family_map = {}
        data["activeIndexByFamily"] = {
            family: _active_index_after_removal(family_map.get(family, 0), removed_index, remaining)
            for family in ("claude", "gemini")
        }
        state = data.get("accountState")
        if isinstance(state, dict):
            for bucket_name in ("failures", "cooldowns", "counters"):
                bucket = state.get(bucket_name)
                if isinstance(bucket, dict):
                    bucket.pop(target_email, None)
        result.update({"email": removed.get("email", target_email), "account_count": remaining})
        return True

    update_accounts(mutate)
    return result


def reset_google_account_state(email: str | None = None, *, all_accounts: bool = False) -> dict:
    target_email = str(email or "").strip()
    if all_accounts and target_email:
        raise ValueError("Pass either an email or --all, not both")
    if not all_accounts and not target_email:
        raise ValueError("Google account email is required unless --all is passed")
    result: dict = {"emails": [], "cleared": {"failures": 0, "cooldowns": 0}}

    def mutate(data: dict) -> bool:
        accounts = data.get("accounts", [])
        account_emails = [
            str(account.get("email"))
            for account in accounts
            if isinstance(account, dict) and account.get("email")
        ]
        targets = set(account_emails if all_accounts else [target_email])
        if not all_accounts and target_email not in targets.intersection(account_emails):
            raise ValueError(f"No configured Google account found for {target_email}")
        state = data.setdefault("accountState", {})
        if not isinstance(state, dict):
            state = {}
            data["accountState"] = state
        dirty = False
        for bucket_name in ("failures", "cooldowns"):
            bucket = state.get(bucket_name)
            if not isinstance(bucket, dict):
                continue
            for account_email in list(targets):
                if account_email in bucket:
                    bucket.pop(account_email, None)
                    result["cleared"][bucket_name] += 1
                    dirty = True
        result["emails"] = sorted(targets)
        return dirty

    update_accounts(mutate)
    return result


def require_safe_gateway_host(host: str, allow_remote: bool) -> None:
    if is_loopback_host(host):
        return
    if not allow_remote:
        raise SystemExit(
            "Refusing to bind the unauthenticated gateway to a non-loopback host. "
            "Use --allow-remote with ANTIGRAVITY_GATEWAY_TOKEN set to opt in."
        )
    try:
        token = validate_gateway_token_strength(os.environ.get("ANTIGRAVITY_GATEWAY_TOKEN"))
    except ValueError as e:
        raise SystemExit(str(e)) from e
    os.environ["ANTIGRAVITY_GATEWAY_TOKEN"] = token
    os.environ["ANTIGRAVITY_ALLOW_REMOTE"] = "1"


def provider_key_status(provider: dict, *, configured_label: str) -> str:
    if provider_auth_mode(provider) == "oauth":
        if provider.get("id") == "xai-oauth":
            return configured_label if xai_oauth_status().get("ready") else "missing oauth"
        return "unsupported oauth"
    try:
        api_key = validate_provider_api_key(resolve_api_key(provider))
    except ValueError:
        return "malformed key"
    return configured_label if api_key else "missing key"


def provider_configured_label(provider_id: str, provider: dict, stored_provider_ids: set[str]) -> str:
    if provider_auth_mode(provider) == "oauth":
        return "oauth OK"
    if provider_id in stored_provider_ids:
        return "configured"
    if has_provider_api_key_env(provider):
        return "env key"
    if provider_allows_keyless_local_use(provider):
        return "local preset"
    return "configured"


def toml_string(value: str) -> str:
    return json.dumps(str(value))


def validate_codex_provider_id(provider_id: str) -> str:
    if not CODEX_PROVIDER_ID_RE.fullmatch(str(provider_id)):
        raise ValueError("Codex provider id may only contain letters, numbers, underscores, and hyphens")
    return str(provider_id)


def validate_codex_model_id(model: str) -> str:
    value = str(model).strip()
    if not value:
        raise ValueError("Codex model id must be non-empty")
    if any(ch.isspace() or ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        raise ValueError("Codex model id must not contain whitespace or control characters")
    if ":" in value:
        return value
    return canonical_model_id(value)


def validate_codex_provider_name(provider_name: str) -> str:
    value = str(provider_name).strip()
    if not value:
        raise ValueError("Codex provider name must be non-empty")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        raise ValueError("Codex provider name must not contain control characters")
    return value


def render_codex_provider_table(
    *,
    provider_id: str = DEFAULT_CODEX_PROVIDER_ID,
    provider_name: str = DEFAULT_CODEX_PROVIDER_NAME,
    base_url: str = DEFAULT_CODEX_BASE_URL,
) -> str:
    provider_id = validate_codex_provider_id(provider_id)
    provider_name = validate_codex_provider_name(provider_name)
    base_url = validate_http_base_url(base_url, label="Codex gateway base URL")
    return "\n".join(
        [
            f"[model_providers.{provider_id}]",
            f"name = {toml_string(provider_name)}",
            f"base_url = {toml_string(base_url)}",
            'wire_api = "responses"',
        ]
    )


def render_codex_config_snippet(
    *,
    model: str = DEFAULT_CODEX_MODEL,
    provider_id: str = DEFAULT_CODEX_PROVIDER_ID,
    provider_name: str = DEFAULT_CODEX_PROVIDER_NAME,
    base_url: str = DEFAULT_CODEX_BASE_URL,
    activate: bool = False,
) -> str:
    model = validate_codex_model_id(model)
    provider_id = validate_codex_provider_id(provider_id)
    lines: list[str] = []
    if activate:
        lines.extend(
            [
                f"model = {toml_string(model)}",
                f"model_provider = {toml_string(provider_id)}",
                'wire_api = "responses"',
                "",
            ]
        )
    lines.extend(
        [
            render_codex_provider_table(
                provider_id=provider_id,
                provider_name=provider_name,
                base_url=base_url,
            ),
            "",
        ]
    )
    return "\n".join(lines)


def _toml_key(line: str) -> str | None:
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or "=" not in stripped:
        return None
    return stripped.split("=", 1)[0].strip()


def _is_toml_section(line: str) -> bool:
    stripped = line.split("#", 1)[0].strip()
    return stripped.startswith("[") and stripped.endswith("]")


def _toml_section_name(line: str) -> str | None:
    stripped = line.split("#", 1)[0].strip()
    if not stripped.startswith("[") or not stripped.endswith("]"):
        return None
    return stripped.strip("[]").strip()


def _upsert_root_keys(lines: list[str], values: dict[str, str]) -> list[str]:
    first_section = next((idx for idx, line in enumerate(lines) if _is_toml_section(line)), len(lines))
    root = list(lines[:first_section])
    rest = list(lines[first_section:])
    seen: set[str] = set()

    for idx, line in enumerate(root):
        key = _toml_key(line)
        if key in values:
            root[idx] = f"{key} = {values[key]}"
            seen.add(key)

    missing = [key for key in values if key not in seen]
    if missing:
        while root and not root[-1].strip():
            root.pop()
        if root:
            root.append("")
        root.extend(f"{key} = {values[key]}" for key in missing)
        if rest:
            root.append("")

    return root + rest


def _upsert_table(lines: list[str], section_name: str, values: dict[str, str]) -> list[str]:
    header = f"[{section_name}]"
    start = next((idx for idx, line in enumerate(lines) if _toml_section_name(line) == section_name), None)
    if start is None:
        updated = list(lines)
        while updated and not updated[-1].strip():
            updated.pop()
        if updated:
            updated.extend(["", header])
        else:
            updated.append(header)
        updated.extend(f"{key} = {value}" for key, value in values.items())
        return updated

    end = next((idx for idx in range(start + 1, len(lines)) if _is_toml_section(lines[idx])), len(lines))
    section = list(lines[start:end])
    seen: set[str] = set()
    for idx, line in enumerate(section[1:], start=1):
        key = _toml_key(line)
        if key in values:
            section[idx] = f"{key} = {values[key]}"
            seen.add(key)

    section.extend(f"{key} = {value}" for key, value in values.items() if key not in seen)
    return lines[:start] + section + lines[end:]


def merge_codex_config(
    existing: str,
    *,
    model: str = DEFAULT_CODEX_MODEL,
    provider_id: str = DEFAULT_CODEX_PROVIDER_ID,
    provider_name: str = DEFAULT_CODEX_PROVIDER_NAME,
    base_url: str = DEFAULT_CODEX_BASE_URL,
    activate: bool = False,
) -> str:
    model = validate_codex_model_id(model)
    provider_id = validate_codex_provider_id(provider_id)
    provider_name = validate_codex_provider_name(provider_name)
    base_url = validate_http_base_url(base_url, label="Codex gateway base URL")
    if not existing.strip():
        return render_codex_config_snippet(
            model=model,
            provider_id=provider_id,
            provider_name=provider_name,
            base_url=base_url,
            activate=activate,
        )

    lines = existing.splitlines()
    if activate:
        lines = _upsert_root_keys(
            lines,
            {
                "model": toml_string(model),
                "model_provider": toml_string(provider_id),
                "wire_api": '"responses"',
            },
        )
    lines = _upsert_table(
        lines,
        f"model_providers.{provider_id}",
        {
            "name": toml_string(provider_name),
            "base_url": toml_string(base_url),
            "wire_api": '"responses"',
        },
    )
    return "\n".join(lines).rstrip() + "\n"


def _strip_toml_inline_comment(value: str) -> str:
    in_single = False
    in_double = False
    escaped = False
    for idx, ch in enumerate(value):
        if escaped:
            escaped = False
            continue
        if in_double and ch == "\\":
            escaped = True
            continue
        if ch == '"' and not in_single:
            in_double = not in_double
            continue
        if ch == "'" and not in_double:
            in_single = not in_single
            continue
        if ch == "#" and not in_single and not in_double:
            return value[:idx]
    return value


def _parse_toml_string_value(raw_value: str) -> str:
    value = _strip_toml_inline_comment(raw_value).strip()
    if not value:
        return ""
    if value.startswith('"') and value.endswith('"'):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return ""
        return parsed if isinstance(parsed, str) else ""
    if value.startswith("'") and value.endswith("'"):
        return value[1:-1]
    return value


def parse_codex_config(content: str) -> dict[str, object]:
    active_provider = ""
    active_model = ""
    provider_tables: dict[str, dict[str, str]] = {}
    current_section = ""

    for line in content.splitlines():
        section_name = _toml_section_name(line)
        if section_name is not None:
            current_section = section_name
            continue
        key = _toml_key(line)
        if key is None:
            continue
        raw_value = line.split("=", 1)[1]
        value = _parse_toml_string_value(raw_value)
        if not current_section:
            if key == "model_provider":
                active_provider = value
            elif key == "model":
                active_model = value
            continue
        prefix = "model_providers."
        if current_section.startswith(prefix):
            table_provider = current_section[len(prefix):].strip().strip('"').strip("'")
            provider_tables.setdefault(table_provider, {})[key] = value

    return {
        "active_provider": active_provider,
        "active_model": active_model,
        "provider_tables": provider_tables,
    }


def inspect_codex_gateway_config(content: str, *, provider_id: str, expected_base_url: str) -> tuple[bool, str]:
    provider_id = validate_codex_provider_id(provider_id)
    expected_base_url = validate_http_base_url(expected_base_url, label="Codex gateway base URL")
    parsed = parse_codex_config(content)
    active_provider = parsed["active_provider"]
    provider_tables = parsed["provider_tables"]

    if active_provider != provider_id:
        return False, f"active model_provider is {active_provider or '(unset)'}, expected {provider_id}"
    provider_table = provider_tables.get(provider_id)
    if not provider_table:
        return False, f"missing [model_providers.{provider_id}] table"
    base_url = provider_table.get("base_url")
    if base_url != expected_base_url:
        return False, f"provider base_url is {base_url or '(unset)'}, expected {expected_base_url}"
    wire_api = provider_table.get("wire_api")
    if wire_api and wire_api != "responses":
        return False, f"provider wire_api is {wire_api}, expected responses"
    return True, "active provider points to this gateway server"


def inspect_codex_provider_block_config(content: str, *, provider_id: str, expected_base_url: str) -> tuple[bool, str]:
    provider_id = validate_codex_provider_id(provider_id)
    expected_base_url = validate_http_base_url(expected_base_url, label="Codex gateway base URL")
    parsed = parse_codex_config(content)
    provider_tables = parsed["provider_tables"]
    active_provider = parsed["active_provider"]

    provider_table = provider_tables.get(provider_id)
    if not provider_table:
        return False, f"missing [model_providers.{provider_id}] table"
    base_url = provider_table.get("base_url")
    if base_url != expected_base_url:
        return False, f"provider base_url is {base_url or '(unset)'}, expected {expected_base_url}"
    wire_api = provider_table.get("wire_api")
    if wire_api and wire_api != "responses":
        return False, f"provider wire_api is {wire_api}, expected responses"
    if active_provider == provider_id:
        return True, "provider block is installed and active"
    return True, f"provider block is installed; active model_provider is {active_provider or '(unset)'}"


def _write_private_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            delete=False,
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
        ) as f:
            temp_path = Path(f.name)
            os.chmod(temp_path, 0o600)
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, path)
        os.chmod(path, 0o600)
    except Exception:
        if temp_path and temp_path.exists():
            try:
                temp_path.unlink()
            except Exception:
                pass
        raise


def _codex_config_backup_path(config_path: Path) -> Path:
    backup_path = config_path.with_name(f"{config_path.name}.bak-{time.strftime('%Y%m%d%H%M%S')}")
    if not backup_path.exists():
        return backup_path
    for suffix in range(2, 100):
        candidate = config_path.with_name(f"{backup_path.name}-{suffix}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"Could not allocate a unique backup path for {config_path}")


def write_codex_config(
    config_path: Path,
    *,
    model: str = DEFAULT_CODEX_MODEL,
    provider_id: str = DEFAULT_CODEX_PROVIDER_ID,
    provider_name: str = DEFAULT_CODEX_PROVIDER_NAME,
    base_url: str = DEFAULT_CODEX_BASE_URL,
    activate: bool = False,
) -> tuple[bool, Path | None]:
    model = validate_codex_model_id(model)
    provider_id = validate_codex_provider_id(provider_id)
    provider_name = validate_codex_provider_name(provider_name)
    target_path = config_path.resolve() if config_path.is_symlink() else config_path
    existing = target_path.read_text(encoding="utf-8") if target_path.exists() else ""
    updated = merge_codex_config(
        existing,
        model=model,
        provider_id=provider_id,
        provider_name=provider_name,
        base_url=base_url,
        activate=activate,
    )
    if existing == updated:
        return False, None

    target_path.parent.mkdir(parents=True, exist_ok=True)
    backup_path = None
    if target_path.exists():
        backup_path = _codex_config_backup_path(target_path)
        _write_private_text(backup_path, existing)
    _write_private_text(target_path, updated)
    return True, backup_path


def configure_codex_write_command(args) -> str:
    parts = ["codex-antigravity", "configure-codex", "--write"]
    if getattr(args, "activate", False):
        parts.append("--activate")
    if args.config != "~/.codex/config.toml":
        parts.extend(["--config", args.config])
    if args.model != DEFAULT_CODEX_MODEL:
        parts.extend(["--model", args.model])
    if args.provider != DEFAULT_CODEX_PROVIDER_ID:
        parts.extend(["--provider", args.provider])
    if args.provider_name != DEFAULT_CODEX_PROVIDER_NAME:
        parts.extend(["--provider-name", args.provider_name])
    if args.base_url != DEFAULT_CODEX_BASE_URL:
        parts.extend(["--base-url", args.base_url])
    return " ".join(shlex.quote(part) for part in parts)


def gateway_start_command(base_url: str) -> str:
    parsed = urlparse(validate_http_base_url(base_url, label="Codex gateway base URL"))
    parts = ["codex-antigravity", "start"]
    if parsed.hostname and parsed.hostname not in {"localhost", "127.0.0.1"}:
        parts.extend(["--host", parsed.hostname])
    if parsed.port and parsed.port != 51122:
        parts.extend(["--port", str(parsed.port)])
    return " ".join(shlex.quote(part) for part in parts)


def run_configure_codex(args) -> None:
    config_path = Path(os.path.expanduser(args.config))
    activate = bool(getattr(args, "activate", False))
    try:
        snippet = render_codex_config_snippet(
            model=args.model,
            provider_id=args.provider,
            provider_name=args.provider_name,
            base_url=args.base_url,
            activate=activate,
        )
    except (OSError, RuntimeError, ValueError) as e:
        raise SystemExit(str(e)) from e

    if not args.write:
        print(snippet, end="")
        print(f"# To write this into {config_path}, run:")
        print(configure_codex_write_command(args))
        return

    try:
        changed, backup_path = write_codex_config(
            config_path,
            model=args.model,
            provider_id=args.provider,
            provider_name=args.provider_name,
            base_url=args.base_url,
            activate=activate,
        )
    except (OSError, RuntimeError, ValueError) as e:
        raise SystemExit(str(e)) from e
    if changed:
        print(f"[+] Updated Codex config: {config_path}")
        if backup_path:
            print(f"[+] Backup written: {backup_path}")
    else:
        print(f"[*] Codex provider block already points at this gateway: {config_path}")
    if activate:
        print(f"[*] Active Codex default set to {args.model} via provider {args.provider}.")
    else:
        print("[*] Installed provider block only; existing top-level model/model_provider were left unchanged.")
        print("[*] Add --activate only when you explicitly want this gateway to become the active Codex default.")
    print(f"[*] Start the gateway with: {gateway_start_command(args.base_url)}")
    print("[*] Optional sidecar skill: codex-antigravity install-skill")

def run_local_oauth_flow(*, select_account: bool = False) -> dict:
    # Verify environment credentials or credentials file exists
    cid, csec = resolve_oauth_credentials()
    if not cid or not csec:
        print("[!] No Google OAuth Client Credentials configured!")
        print("Please configure them via env vars or ~/.codex/antigravity-credentials.json first.")
        print("See the README.md for setup instructions.")
        sys.exit(1)

    print("[*] Initiating Google Antigravity OAuth login...")
    auth_info = authorize_antigravity(select_account=select_account)
    url = auth_info["url"]
    
    try:
        server = OAuthServer(("localhost", 51121), OAuthCallbackHandler)
    except OSError as e:
        raise SystemExit(
            "OAuth callback port 51121 is already in use. "
            "Stop the process using that port and run `codex-antigravity login` again."
        ) from e
    server.expected_state_id = auth_info["state_id"]
    server.timeout = 600
    try:
        print(f"[*] Opening browser authorization URL...")
        print(f"[*] If the browser doesn't open automatically, navigate to:\n{url}\n")
        webbrowser.open(url)
        
        # Wait for callback
        deadline = time.time() + 600
        while server.auth_code is None:
            if time.time() > deadline:
                print("[!] Timed out waiting for OAuth callback.")
                sys.exit(1)
            server.handle_request()

        print("[*] Callback received. Exchanging code for tokens...")
        try:
            returned_state = decode_state(server.auth_state or "")
        except Exception:
            print("[!] OAuth callback state was missing or invalid.")
            sys.exit(1)
        if returned_state.get("id") != auth_info["state_id"]:
            print("[!] OAuth callback state did not match the active login attempt.")
            sys.exit(1)

        # Retrieve verifier from oauth module verifier store
        from .oauth import get_pkce_verifier
        verifier_info = get_pkce_verifier(auth_info["state_id"])
        if not verifier_info:
            print("[!] PKCE verifier state not found or expired!")
            sys.exit(1)

        tokens = exchange_antigravity(server.auth_code, verifier_info["verifier"])
    finally:
        server.server_close()
    
    # Extract user profile email
    email = None
    try:
        import urllib.request
        req = urllib.request.Request(
            "https://www.googleapis.com/oauth2/v2/userinfo",
            headers={"Authorization": f"Bearer {tokens['access_token']}"}
        )
        with urllib.request.urlopen(req, timeout=OAUTH_HTTP_TIMEOUT_SECONDS) as resp:
            user_info = json.loads(resp.read().decode("utf-8"))
            email = user_info.get("email")
    except Exception as e:
        print(f"[!] Could not retrieve Google account email: {redact_secret_text(str(e))}")
        sys.exit(1)
    if not email:
        print("[!] Google account email was missing from userinfo response.")
        sys.exit(1)

    # Save to storage
    data = load_accounts()
    accounts = data.setdefault("accounts", [])
    
    # Check if account already exists, update if so, or add new
    existing_idx = None
    for idx, acc in enumerate(accounts):
        if acc.get("email") == email:
            existing_idx = idx
            break
            
    refresh_token = tokens.get("refresh_token")
    if not refresh_token and existing_idx is not None:
        refresh_token = accounts[existing_idx].get("refreshToken")
    if not refresh_token:
        print("[!] Google did not return a refresh token. Revoke this client grant and run login again.")
        sys.exit(1)

    account_entry = {
        "email": email,
        "refreshToken": refresh_token,
        "accessToken": tokens["access_token"],
        "expiresAt": int(time.time()) + token_expires_in_seconds(tokens),
    }
    
    result = upsert_google_account(data, account_entry)
    if result["created"]:
        print(f"[+] Successfully authenticated new Google Account: {email}")
    else:
        print(f"[+] Successfully re-authenticated and updated Google Account: {email}")
        
    save_accounts(data)
    print(f"[+] {email} is in the Google account rotation pool ({result['account_count']} total).")
    return result


def require_xai_oauth_provider_arg(provider: str) -> None:
    if provider != "xai-oauth":
        raise SystemExit("xAI SuperGrok OAuth uses provider id `xai-oauth`.")


def run_xai_oauth_browser_login(args) -> dict:
    require_xai_oauth_provider_arg(args.provider)
    pkce = generate_pkce()
    state_id = secrets.token_urlsafe(32)
    state = encode_state({"id": state_id})
    nonce = secrets.token_urlsafe(32)
    url = build_xai_authorize_url(pkce, state=state, nonce=nonce)

    try:
        server = OAuthServer(("127.0.0.1", 56121), OAuthCallbackHandler)
    except OSError as e:
        raise SystemExit(
            "xAI OAuth callback port 56121 is already in use. "
            "Stop the process using that port or run `codex-antigravity provider login xai-oauth --device`."
        ) from e
    server.expected_state_id = state_id
    server.timeout = 600
    try:
        print("[*] Initiating xAI Grok OAuth login...")
        print(f"[*] Callback URL: {XAI_OAUTH_REDIRECT_URI}")
        print(f"[*] If the browser does not open automatically, navigate to:\n{url}\n")
        webbrowser.open(url)
        deadline = time.time() + 600
        while server.auth_code is None:
            if time.time() > deadline:
                raise SystemExit("Timed out waiting for xAI OAuth callback.")
            server.handle_request()
        try:
            returned_state = decode_state(server.auth_state or "")
        except Exception as exc:
            raise SystemExit("xAI OAuth callback state was missing or invalid.") from exc
        if returned_state.get("id") != state_id:
            raise SystemExit("xAI OAuth callback state did not match the active login attempt.")
        tokens = exchange_xai_authorization_code(server.auth_code, pkce["verifier"])
    finally:
        server.server_close()
    saved = save_xai_oauth_token_response(tokens)
    print("[+] xAI Grok OAuth login saved for provider xai-oauth.")
    print(f"[*] Models will appear as xai-oauth:<model> once the gateway can read {xai_oauth_status().get('path', 'the encrypted token store')}.")
    return saved


def run_xai_oauth_device_login(args) -> dict:
    require_xai_oauth_provider_arg(args.provider)
    print("[*] Initiating xAI Grok OAuth device-code login...")
    device = request_xai_device_code()
    verification_url = device.get("verification_uri_complete") or device.get("verification_uri")
    print(f"[*] Open this URL in any browser: {verification_url}")
    print(f"[*] Enter code: {device.get('user_code')}")
    tokens = poll_xai_device_code_token(device)
    saved = save_xai_oauth_token_response(tokens)
    print("[+] xAI Grok OAuth login saved for provider xai-oauth.")
    return saved


def run_xai_oauth_login(args) -> dict:
    if getattr(args, "device", False) or getattr(args, "no_browser", False):
        return run_xai_oauth_device_login(args)
    return run_xai_oauth_browser_login(args)


def run_xai_oauth_status(args) -> dict:
    require_xai_oauth_provider_arg(args.provider)
    status = xai_oauth_status()
    if getattr(args, "json", False):
        print(json.dumps(status, indent=2))
    else:
        label = "ready" if status.get("ready") else "not ready"
        print(f"xAI OAuth provider xai-oauth: {label}")
        print(f"  token store: {status.get('path')}")
        if status.get("expires_in_seconds") is not None:
            print(f"  access token expires in: {status['expires_in_seconds']}s")
        if not status.get("ready"):
            print("  next command: codex-antigravity provider login xai-oauth")
    return status


def run_xai_oauth_refresh(args) -> dict:
    require_xai_oauth_provider_arg(args.provider)
    try:
        resolve_xai_oauth_access_token(force_refresh=True)
    except RuntimeError as exc:
        raise SystemExit(redact_secret_text(str(exc))) from exc
    status = xai_oauth_status()
    print("[+] Refreshed xAI OAuth access token for provider xai-oauth.")
    return status


def run_xai_oauth_logout(args) -> bool:
    require_xai_oauth_provider_arg(args.provider)
    if not getattr(args, "yes", False):
        raise SystemExit("Refusing to remove xAI OAuth tokens without --yes.")
    existed = clear_xai_oauth_tokens()
    if existed:
        print("[+] Removed xAI OAuth tokens for provider xai-oauth.")
    else:
        print("[*] No xAI OAuth tokens were configured.")
    return existed


def run_login(args) -> None:
    count = getattr(args, "count", 1)
    select_account = getattr(args, "select_account", False) or count > 1
    if count > 1:
        print(f"[*] Running {count} Google OAuth login flows.")
        print("[*] Choose a different Google account in each browser flow to build the rotation pool.")
    for attempt in range(count):
        if count > 1:
            print(f"[*] Login {attempt + 1}/{count}")
        run_local_oauth_flow(select_account=select_account)
    print_account_rotation_summary()


def run_setup_google(args) -> None:
    base_url = args.base_url or f"http://localhost:{args.port}/v1"
    try:
        render_codex_config_snippet(
            model=args.model,
            provider_id=args.provider,
            provider_name=args.provider_name,
            base_url=base_url,
        )
    except (OSError, RuntimeError, ValueError) as e:
        raise SystemExit(str(e)) from e
    cid, csec = resolve_oauth_credentials()
    if not cid or not csec:
        raise SystemExit(
            "Google OAuth client credentials are not configured. "
            "Set ANTIGRAVITY_CLIENT_ID and ANTIGRAVITY_CLIENT_SECRET, "
            "or create ~/.codex/antigravity-credentials.json before running setup-google."
        )
    run_login(argparse.Namespace(count=args.accounts, select_account=True))

    if not args.skip_codex_config:
        print("[*] Installing Codex provider block...")
        run_configure_codex(
            argparse.Namespace(
                write=True,
                config=args.config,
                model=args.model,
                provider=args.provider,
                provider_name=args.provider_name,
                base_url=base_url,
                activate=getattr(args, "activate", False),
            )
        )
    else:
        print("[*] Skipping Codex config write.")

    if not args.skip_doctor and getattr(args, "activate", False):
        print("[*] Running post-setup doctor...")
        if not run_doctor(expected_base_url=base_url, config=args.config, provider_id=args.provider):
            raise SystemExit("Google setup completed, but doctor found hard failures. Review the diagnostics above.")
    elif not args.skip_doctor:
        print("[*] Skipping active-provider doctor because --activate was not used.")
    print("[+] Google Antigravity OAuth setup is ready.")
    print(f"    Start the gateway with: codex-antigravity start --port {args.port}")
    print("    Optional Codex sidecar skill: codex-antigravity install-skill")


def _setup_check(
    checks: list[dict],
    name: str,
    status: str,
    detail: str,
    **extra,
) -> None:
    checks.append({"name": name, "status": status, "detail": detail, **extra})


def setup_service_followup_command(args) -> str:
    parts = ["codex-antigravity", "service", "install", "--port", str(args.port), "--host", str(args.host)]
    op_env_file = getattr(args, "op_env_file", None)
    op_environment = getattr(args, "op_environment", None)
    if op_env_file:
        parts.extend(["--op-env-file", str(op_env_file)])
    if op_environment:
        parts.extend(["--op-environment", str(op_environment)])
    return " ".join(shlex.quote(part) for part in parts)


def _print_setup_report(report: dict) -> None:
    print("=" * 60)
    print("              CODEX ANTIGRAVITY SETUP              ")
    print("=" * 60)
    mode = report.get("mode")
    if mode == "check":
        print("Mode: check (read-only; pass --write to modify Codex config, OAuth state, skills, or gateway processes)")
    elif mode:
        print(f"Mode: {mode}")
    for check in report["checks"]:
        label = {"pass": "PASS", "warn": "WARN", "fail": "FAIL", "skip": "SKIP"}.get(check["status"], "INFO")
        print(f"[{label}] {check['name']}: {check['detail']}")
    print(f"Next command: {report['next_command']}")
    print("=" * 60)


def byok_setup_next_command(provider_prefix: str, provider_model: str, provider: dict | None = None) -> str:
    if provider_prefix == "xai-oauth":
        return "codex-antigravity provider login xai-oauth"
    if provider is None:
        try:
            provider = provider_preset(provider_prefix)
        except ValueError:
            provider = {}
    env_name = provider.get("apiKeyEnv") or "PROVIDER_API_KEY"
    command = f"codex-antigravity provider set {provider_prefix} --api-key-env {env_name}"
    if provider_model:
        command += f" --model {provider_model}"
    return command


def setup_byok_preflight(provider_prefix: str, provider_model: str) -> tuple[str, str, dict | None]:
    if not provider_model:
        return "fail", f"BYOK model must include a model id after '{provider_prefix}:'", None
    try:
        providers = all_provider_configs()
    except Exception as exc:
        return "fail", f"Could not load BYOK provider configuration: {redact_secret_text(str(exc))}", None
    provider = providers.get(provider_prefix)
    if not provider and provider_prefix == "xai-oauth":
        try:
            provider = provider_preset("xai-oauth")
            provider["id"] = "xai-oauth"
        except ValueError:
            provider = None
    if not provider:
        return "fail", f"BYOK provider '{provider_prefix}' is not configured", None
    key_status = provider_key_status(provider, configured_label="key OK")
    if key_status != "key OK":
        credential_name = "OAuth login" if provider_auth_mode(provider) == "oauth" else "key"
        return "fail", f"BYOK provider '{provider_prefix}' does not have a usable {credential_name} ({key_status})", provider
    configured_models = [
        str(model.get("id") if isinstance(model, dict) else model)
        for model in provider.get("models", [])
    ]
    if provider_model not in configured_models:
        return "fail", f"{provider_prefix}:{provider_model} is not listed in provider '{provider_prefix}'", provider
    return "pass", f"{provider_prefix}:{provider_model} routes to configured BYOK provider", provider


def validate_oauth_credentials_with_google(
    client_id: str,
    client_secret: str,
    *,
    timeout: float = 5.0,
) -> tuple[str, str]:
    payload = {
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": "invalid-refresh-token-for-codex-antigravity-validation",
        "grant_type": "refresh_token",
    }
    req = urllib.request.Request(
        "https://oauth2.googleapis.com/token",
        data=urlencode(payload).encode("utf-8"),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout):
            return "warn", "Google token endpoint accepted an invalid refresh token unexpectedly; continuing"
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", errors="replace")
        except Exception:
            body = ""
        if "invalid_grant" in body:
            return "pass", "Google token endpoint accepted the OAuth client credentials"
        if "invalid_client" in body:
            return "fail", "Google token endpoint rejected the OAuth client credentials"
        return "warn", f"Google token endpoint returned HTTP {exc.code}; continuing without credential validation"
    except Exception as exc:
        return "warn", f"Could not validate OAuth credentials with Google; continuing ({redact_secret_text(str(exc))})"


def maybe_prompt_and_save_oauth_credentials(args, checks: list[dict]) -> tuple[str | None, str | None]:
    if getattr(args, "no_input", False):
        return None, None
    stdin = getattr(sys, "stdin", None)
    if not stdin or not stdin.isatty():
        return None, None
    print("[*] Google OAuth desktop-client credentials are missing.")
    print("    Create an OAuth desktop client in Google Cloud Console, then paste its values here.")
    print("    Local redirect URI: http://localhost:51121/oauth-callback")
    try:
        client_id = input("Google OAuth client id: ").strip()
        client_secret = getpass.getpass("Google OAuth client secret: ").strip()
        if not client_id.endswith(".apps.googleusercontent.com"):
            _setup_check(
                checks,
                "google_oauth_client_id_shape",
                "warn",
                "client id does not end with .apps.googleusercontent.com",
            )
        status, detail = validate_oauth_credentials_with_google(client_id, client_secret)
        _setup_check(checks, "google_oauth_credentials_validation", status, detail)
        if status == "fail":
            return None, None
        path = save_oauth_credentials(client_id, client_secret)
    except (EOFError, KeyboardInterrupt):
        raise SystemExit("OAuth credential entry was cancelled; Codex config was not modified.")
    except (OSError, RuntimeError, ValueError) as exc:
        raise SystemExit(f"Could not save OAuth credentials: {redact_secret_text(str(exc))}") from exc
    _setup_check(checks, "google_oauth_credentials_saved", "pass", f"saved private credentials file at {path}")
    return client_id, client_secret


def run_setup(args) -> dict:
    if getattr(args, "check", False) and getattr(args, "write", False):
        raise SystemExit("Use either --check or --write, not both.")
    if getattr(args, "json", False) and getattr(args, "write", False):
        raise SystemExit("setup --json is read-only; omit --write or use --check.")
    if getattr(args, "repair", False) and getattr(args, "check", False):
        raise SystemExit("Use either --repair or --check, not both.")
    if getattr(args, "repair", False) and getattr(args, "write", False):
        raise SystemExit("Use either --repair or --write, not both.")
    if getattr(args, "repair", False) and getattr(args, "json", False):
        raise SystemExit("setup --repair mutates Codex config; omit --json.")

    checks: list[dict] = []
    base_url = ""
    model = str(getattr(args, "model", "") or "")
    provider_prefix = None
    provider_model = ""
    google_route = True

    try:
        base_url = setup_effective_base_url(args)
        model = validate_codex_model_id(args.model)
        provider_prefix, provider_model = split_provider_model(model)
        if provider_prefix is not None and not provider_model:
            raise ValueError(f"BYOK model must include a model id after '{provider_prefix}:'")
        google_route = provider_prefix is None
        render_codex_config_snippet(
            model=model,
            provider_id=args.provider,
            provider_name=args.provider_name,
            base_url=base_url,
        )
        definition = native_model_definition(model)
        model_detail = f"{model}"
        if definition:
            model_detail += f" ({definition.display_name})"
        _setup_check(checks, "target_config", "pass", f"validated Codex provider config for {model_detail}")
    except (OSError, RuntimeError, ValueError) as exc:
        _setup_check(checks, "target_config", "fail", redact_secret_text(str(exc)))
        report = {
            "ok": False,
            "mode": "check" if args.check or not args.write else "write",
            "checks": checks,
            "next_command": "codex-antigravity setup --check",
        }
        if args.json:
            print(json.dumps(report, indent=2))
        else:
            _print_setup_report(report)
        raise SystemExit(1)

    if getattr(args, "repair", False):
        run_configure_codex(
            argparse.Namespace(
                write=True,
                config=args.config,
                model=model,
                provider=args.provider,
                provider_name=args.provider_name,
                base_url=base_url,
                activate=getattr(args, "activate", False),
            )
        )
        _setup_check(checks, "codex_config_repair", "pass", f"repaired {Path(os.path.expanduser(args.config))}")
        readiness = codex_ready_report(
            config=args.config,
            provider_id=args.provider,
            expected_base_url=base_url,
            gateway_timeout=args.gateway_timeout,
            gateway_token_env=args.gateway_token_env,
            live=getattr(args, "live", False),
            live_model=getattr(args, "live_model", None),
            live_timeout=getattr(args, "live_timeout", 30.0),
            selected_model=model,
            require_active_provider=getattr(args, "activate", False),
        )
        checks.extend({**check, "name": f"readiness.{check['name']}"} for check in readiness["checks"])
        ok = all(check["status"] != "fail" for check in checks)
        report = {
            "ok": ok,
            "mode": "repair",
            "model": model,
            "base_url": base_url,
            "checks": checks,
            "next_command": readiness["next_command"] if not ok else "codex",
        }
        _print_setup_report(report)
        if not ok:
            raise SystemExit(f"Setup repair completed with readiness failures. Next command: {report['next_command']}")
        return report

    if google_route:
        cid, csec = resolve_oauth_credentials()
        if args.write and (not cid or not csec):
            prompted_cid, prompted_csec = maybe_prompt_and_save_oauth_credentials(args, checks)
            cid = cid or prompted_cid
            csec = csec or prompted_csec
        if cid and csec:
            _setup_check(checks, "google_oauth_credentials", "pass", "configured")
        else:
            _setup_check(
                checks,
                "google_oauth_credentials",
                "fail",
                "missing ANTIGRAVITY_CLIENT_ID/ANTIGRAVITY_CLIENT_SECRET or ~/.codex/antigravity-credentials.json; run `codex-antigravity setup --write` to add them interactively",
            )
            if args.write:
                report = {
                    "ok": False,
                    "mode": "write",
                    "checks": checks,
                    "next_command": "codex-antigravity setup --write --accounts 1",
                }
                _print_setup_report(report)
                raise SystemExit("Google OAuth client credentials are not configured; Codex config was not modified.")
    else:
        _setup_check(checks, "google_oauth_credentials", "skip", f"{model} routes to BYOK")
        byok_status, byok_detail, byok_provider = setup_byok_preflight(provider_prefix or "", provider_model)
        if args.write and provider_prefix == "xai-oauth" and byok_status == "fail":
            run_xai_oauth_login(
                argparse.Namespace(
                    provider="xai-oauth",
                    device=getattr(args, "no_browser", False),
                    no_browser=getattr(args, "no_browser", False),
                )
            )
            _setup_check(checks, "xai_oauth_login", "pass", "completed xAI Grok OAuth login")
            byok_status, byok_detail, byok_provider = setup_byok_preflight(provider_prefix or "", provider_model)
        _setup_check(checks, "byok_provider", byok_status, byok_detail, provider=provider_prefix, model=provider_model)
        if args.write and byok_status == "fail":
            report = {
                "ok": False,
                "mode": "write",
                "model": model,
                "base_url": base_url,
                "checks": checks,
                "next_command": byok_setup_next_command(provider_prefix or "provider", provider_model, byok_provider),
            }
            _print_setup_report(report)
            raise SystemExit("BYOK provider is not ready; Codex config was not modified.")

    skill_dir = Path(os.path.expanduser(args.skill_dir))
    skill_path = skill_dir / BUNDLED_CODEX_SKILL_NAME
    try:
        bundled_skill_root()
        if skill_path.is_dir() and codex_skill_matches_bundled(skill_path):
            skill_status = "installed"
        elif skill_path.is_dir():
            skill_status = "present-but-different"
        else:
            skill_status = "missing"
        _setup_check(checks, "anti_skill", "pass" if skill_status == "installed" else "warn", skill_status, path=str(skill_path))
    except RuntimeError as exc:
        _setup_check(checks, "anti_skill", "fail", redact_secret_text(str(exc)))

    if args.check or not args.write:
        if args.install_skill:
            _setup_check(checks, "anti_skill_install", "skip", "--install-skill is only applied when --write is used")
        if args.start:
            _setup_check(checks, "gateway_start", "skip", "--start is only applied when --write is used")
        readiness = codex_ready_report(
            config=args.config,
            provider_id=args.provider,
            expected_base_url=base_url,
            gateway_timeout=args.gateway_timeout,
            gateway_token_env=args.gateway_token_env,
            live=getattr(args, "live", False),
            live_model=getattr(args, "live_model", None),
            live_timeout=getattr(args, "live_timeout", 30.0),
            selected_model=model,
            require_active_provider=getattr(args, "activate", False),
        )
        checks.extend({**check, "name": f"readiness.{check['name']}"} for check in readiness["checks"])
        ok = all(check["status"] != "fail" for check in checks)
        if ok:
            next_command = "codex"
        elif provider_prefix == "xai-oauth" and not xai_oauth_status().get("ready"):
            next_command = "codex-antigravity provider login xai-oauth"
        else:
            next_command = "codex-antigravity setup --write --accounts 1 --install-skill --start"
        report = {
            "ok": ok,
            "mode": "check",
            "model": model,
            "base_url": base_url,
            "checks": checks,
            "next_command": next_command,
        }
        if args.json:
            print(json.dumps(report, indent=2))
        else:
            _print_setup_report(report)
        return report

    if google_route:
        run_login(argparse.Namespace(count=args.accounts, select_account=True))
        _setup_check(checks, "google_login", "pass", f"completed {args.accounts} OAuth login flow(s)")

    run_configure_codex(
        argparse.Namespace(
            write=True,
            config=args.config,
            model=model,
            provider=args.provider,
            provider_name=args.provider_name,
            base_url=base_url,
            activate=getattr(args, "activate", False),
        )
    )
    _setup_check(checks, "codex_config_write", "pass", f"updated {Path(os.path.expanduser(args.config))}")

    if args.install_skill:
        run_install_skill(
            argparse.Namespace(
                skill_dir=args.skill_dir,
                force=args.force,
                dry_run=False,
                verify=args.verify_skill,
            )
        )
        _setup_check(checks, "anti_skill_install", "pass", f"installed bundled Anti skill under {skill_dir}")
    else:
        _setup_check(checks, "anti_skill_install", "skip", "pass --install-skill to install the optional $anti helper")

    gateway_ids: set[str] | None = None
    if args.start:
        try:
            start_gateway_background(
                argparse.Namespace(
                    host=args.host,
                    port=args.port,
                    allow_remote=args.allow_remote,
                    op_env_file=getattr(args, "op_env_file", None),
                    op_environment=getattr(args, "op_environment", None),
                )
            )
            gateway_ids = wait_for_gateway_model_ids(
                base_url,
                timeout=args.gateway_timeout,
                token_env=args.gateway_token_env,
            )
            _setup_check(checks, "gateway_start", "pass", f"started background gateway on {args.host}:{args.port} and /v1/models is reachable")
            _setup_check(
                checks,
                "gateway_service_followup",
                "warn",
                f"For reboot persistence, run: {setup_service_followup_command(args)}",
            )
        except RuntimeError as exc:
            _setup_check(checks, "gateway_start", "fail", redact_secret_text(str(exc)))
            report = {
                "ok": False,
                "mode": "write",
                "model": model,
                "base_url": base_url,
                "checks": checks,
                "next_command": f"codex-antigravity status --port {args.port}",
            }
            _print_setup_report(report)
            raise SystemExit(f"Gateway did not become ready. Next command: {report['next_command']}") from exc
        except SystemExit as exc:
            _setup_check(checks, "gateway_start", "fail", redact_secret_text(str(exc)))
            report = {
                "ok": False,
                "mode": "write",
                "model": model,
                "base_url": base_url,
                "checks": checks,
                "next_command": f"codex-antigravity start --background --port {args.port}",
            }
            _print_setup_report(report)
            raise
    else:
        _setup_check(checks, "gateway_start", "skip", "pass --start to start the gateway in the background")

    try:
        if gateway_ids is None:
            gateway_ids = gateway_model_ids(base_url, timeout=args.gateway_timeout, token_env=args.gateway_token_env)
        catalog_status = "pass" if model in gateway_ids else "fail"
        detail = f"/v1/models advertises {model}" if catalog_status == "pass" else f"/v1/models does not advertise {model}"
        _setup_check(checks, "gateway_models", catalog_status, detail, model_count=len(gateway_ids))
    except RuntimeError as exc:
        _setup_check(checks, "gateway_models", "fail", redact_secret_text(str(exc)))

    readiness = codex_ready_report(
        config=args.config,
        provider_id=args.provider,
        expected_base_url=base_url,
        gateway_timeout=args.gateway_timeout,
        gateway_token_env=args.gateway_token_env,
        live=getattr(args, "live", False),
        live_model=getattr(args, "live_model", None),
        live_timeout=getattr(args, "live_timeout", 30.0),
        selected_model=model,
        require_active_provider=getattr(args, "activate", False),
    )
    checks.extend({**check, "name": f"readiness.{check['name']}"} for check in readiness["checks"])
    ok = all(check["status"] != "fail" for check in checks)
    report = {
        "ok": ok,
        "mode": "write",
        "model": model,
        "base_url": base_url,
        "checks": checks,
        "next_command": readiness["next_command"] if not ok else "codex",
    }
    _print_setup_report(report)
    if not ok:
        raise SystemExit(f"Setup completed with readiness failures. Next command: {report['next_command']}")
    return report

def run_doctor(
    *,
    byok_only: bool = False,
    expected_base_url: str = DEFAULT_CODEX_BASE_URL,
    config: str = "~/.codex/config.toml",
    provider_id: str = DEFAULT_CODEX_PROVIDER_ID,
    live: bool = False,
    live_model: str | None = None,
    live_timeout: float = 30.0,
    gateway_token_env: str = "ANTIGRAVITY_GATEWAY_TOKEN",
) -> bool:
    print("=" * 60)
    print("           GOOGLE ANTIGRAVITY AUTH DOCTOR           ")
    print("=" * 60)
    healthy = True
    codex_config = Path(os.path.expanduser(config))
    codex_config_content = None
    codex_config_model = ""
    if codex_config.is_file():
        try:
            codex_config_content = codex_config.read_text(encoding="utf-8")
            parsed_codex_config = parse_codex_config(codex_config_content)
            codex_config_model = str(parsed_codex_config.get("active_model") or "")
        except Exception:
            codex_config_content = None
    
    # Check Client Credentials
    if byok_only:
        print("[INFO] Google OAuth Client Credentials: skipped (--byok-only)")
    else:
        cid, csec = resolve_oauth_credentials()
        if cid and csec:
            print(f"[PASS] Google OAuth Client Credentials: Configured (Client ID: ...{cid[-15:]})")
        else:
            healthy = False
            print("[FAIL] Google OAuth Client Credentials: Not Configured!")
            print("       Set ANTIGRAVITY_CLIENT_ID and ANTIGRAVITY_CLIENT_SECRET,")
            print("       or create ~/.codex/antigravity-credentials.json")
        
    # Check Token secure storage status
    try:
        from .storage import _get_encryption_key, KEYRING_SERVICE_NAME
        import keyring
        if os.environ.get("ANTIGRAVITY_STORAGE_KEY"):
            _get_encryption_key()
            print("[PASS] Token Storage Encryption: SECURE (ANTIGRAVITY_STORAGE_KEY configured)")
        elif keyring.get_password(KEYRING_SERVICE_NAME, "storage-encryption-key"):
            print("[PASS] Token Storage Encryption: SECURE (OS Keyring Integrated)")
        else:
            print("[WARN] Token Storage Encryption: PARTIAL (Using fallback key; keyring password lookup returned empty)")
    except Exception as e:
        print(f"[WARN] Token Storage Encryption: PARTIAL (Fallback active. Error: {redact_secret_text(str(e))})")
        
    # Check network connectivity to Google Antigravity backend
    if byok_only:
        print("[INFO] Google Antigravity Connectivity: skipped (--byok-only)")
    else:
        try:
            import urllib.request
            import urllib.error
            # cloudcode-pa.googleapis.com returns 404 on HEAD; POST to keepalive-health endpoint
            req = urllib.request.Request("https://cloudcode-pa.googleapis.com/v1internal:generateContent", method="POST",
                                         data=b'{"model":"gemini-3.5-flash-low","request":{"contents":[]}}',
                                         headers={"Content-Type": "application/json"})
            try:
                resp_ctx = urllib.request.urlopen(req, timeout=5.0)
            except urllib.error.HTTPError as e:
                if e.code in (401, 403):
                    print("[PASS] Google Antigravity Connectivity: ONLINE (authentication required)")
                    resp_ctx = None
                else:
                    raise
            if resp_ctx:
                with resp_ctx as resp:
                    if resp.status in (200, 401, 403):
                        print("[PASS] Google Antigravity Connectivity: ONLINE")
                    else:
                        healthy = False
                        print(f"[FAIL] Google Antigravity Connectivity: REACHABLE but status {resp.status}")
        except Exception as e:
            healthy = False
            print(f"[FAIL] Google Antigravity Connectivity: OFFLINE / TIMEOUT ({redact_secret_text(str(e))})")
        
    # Check accounts
    if byok_only:
        print("[INFO] Authenticated Accounts: skipped (--byok-only)")
    else:
        try:
            data = _diagnostic_load_accounts()
        except Exception as e:
            healthy = False
            print(f"[FAIL] Authenticated Accounts: could not load account store ({redact_secret_text(str(e))})")
        else:
            accounts = data.get("accounts", [])
            if accounts:
                print(f"[PASS] Authenticated Accounts: {len(accounts)} configured")
                for acc in accounts:
                    email = acc.get("email")
                    expires_at = normalize_epoch_seconds(acc.get("expiresAt", 0))
                    status = "ACTIVE" if expires_at > time.time() else "EXPIRED (will auto-refresh)"
                    print(f"       - {email} ({status})")
                print("       Rotation:")
                for line in account_rotation_lines(data)[1:]:
                    print(f"       {line.strip()}")
            else:
                healthy = False
                print("[WARN] Authenticated Accounts: 0 accounts found.")
                print("       Run `codex-antigravity login` to add an account.")

    # Check BYOK providers
    try:
        providers = _diagnostic_all_provider_configs()
        if providers:
            provider_statuses = {
                byok_provider_id: provider_key_status(provider, configured_label="key OK")
                for byok_provider_id, provider in providers.items()
            }
            bad_providers = [byok_provider_id for byok_provider_id, status in provider_statuses.items() if status != "key OK"]
            if bad_providers:
                healthy = False
                print(f"[FAIL] BYOK Providers: {len(providers)} configured, env-enabled, or local, {len(bad_providers)} not usable")
            else:
                print(f"[PASS] BYOK Providers: {len(providers)} configured, env-enabled, or local")
            for byok_provider_id, provider in providers.items():
                api_key_status = provider_statuses[byok_provider_id]
                models = provider.get("models", [])
                print(f"       - {byok_provider_id} ({api_key_status}, {len(models)} model(s), {provider.get('baseUrl')})")
            selected_provider_id, selected_provider_model = split_provider_model(codex_config_model) if codex_config_model else (None, "")
            if byok_only and selected_provider_id:
                selected_status = provider_statuses.get(selected_provider_id)
                if selected_provider_id not in providers:
                    healthy = False
                    print(
                        f"[FAIL] Selected BYOK model: {codex_config_model} points at provider "
                        f"'{selected_provider_id}', but that provider is not configured, env-enabled, or locally available."
                    )
                elif selected_status != "key OK":
                    healthy = False
                    print(
                        f"[FAIL] Selected BYOK model: {codex_config_model} points at provider "
                        f"'{selected_provider_id}', but its key status is {selected_status}."
                    )
                elif selected_provider_model not in [str(m.get("id") if isinstance(m, dict) else m) for m in providers[selected_provider_id].get("models", [])]:
                    healthy = False
                    print(
                        f"[FAIL] Selected BYOK model: {codex_config_model} is routed to '{selected_provider_id}', "
                        "but the exact model is not listed in that provider's model catalog."
                    )
        else:
            if byok_only:
                healthy = False
                print("[FAIL] BYOK Providers: none configured.")
            else:
                print("[INFO] BYOK Providers: none configured.")
    except Exception as e:
        if byok_only:
            healthy = False
            print(f"[FAIL] BYOK Providers: could not load provider config ({redact_secret_text(str(e))})")
        else:
            print(f"[WARN] BYOK Providers: could not load provider config ({redact_secret_text(str(e))})")
        
    # Check Codex config
    if codex_config.is_file():
        print(f"[PASS] Codex config.toml: Found ({codex_config})")
        try:
            if codex_config_content is None:
                codex_config_content = codex_config.read_text(encoding="utf-8")
            points_to_gateway, reason = inspect_codex_gateway_config(
                codex_config_content,
                provider_id=provider_id,
                expected_base_url=expected_base_url,
            )
            if points_to_gateway:
                print(f"       - Verified: {reason}.")
            else:
                healthy = False
                print(f"       - [FAIL] config.toml is not ready: {reason}.")
        except Exception as e:
            healthy = False
            print(f"       - [FAIL] could not inspect config.toml ({redact_secret_text(str(e))})")
    else:
        healthy = False
        print(f"[FAIL] Codex config.toml: Not found ({codex_config}).")
        print("       Run `codex-antigravity configure-codex --write` to install the gateway provider block.")

    if live:
        probe_model = live_model or codex_config_model or DEFAULT_CODEX_MODEL
        probe_model, live_model_error = _validate_google_live_model(probe_model)
        if live_model_error:
            healthy = False
            print(f"[FAIL] Live Generation Smoke: {live_model_error}")
        else:
            probe = gateway_generate_probe(
                expected_base_url,
                probe_model,
                timeout=live_timeout,
                token_env=gateway_token_env,
            )
            output_preview = str(probe.get("output_preview") or "")
            if probe.get("ok") and output_preview:
                print(
                    f"[PASS] Live Generation Smoke: {probe_model} responded in "
                    f"{probe.get('latency_ms')}ms ({output_preview})"
                )
            else:
                healthy = False
                reason = probe.get("error") or "unknown error"
                if probe.get("ok") and not output_preview:
                    reason = "empty output"
                print(f"[FAIL] Live Generation Smoke: {probe_model} failed ({reason})")

    version = version_check_result()
    if version["status"] == "warn":
        print(f"[WARN] Package Version: {version['detail']}")
    elif version["status"] == "pass":
        print(f"[PASS] Package Version: {version['detail']}")
    else:
        print(f"[INFO] Package Version: {version['detail']}")
        
    print("=" * 60)
    return healthy

def main():
    parser = argparse.ArgumentParser(description="Codex Antigravity Auth CLI Utility")
    subparsers = parser.add_subparsers(dest="command", required=True)
    
    # login
    login_parser = subparsers.add_parser("login", help="Authenticate Google Antigravity account(s) into the rotation pool")
    login_parser.add_argument("--count", type=positive_int, default=1, help="Number of browser login flows to run")
    login_parser.add_argument("--select-account", action="store_true", help="Force Google's account chooser during login")

    setup_parser = subparsers.add_parser(
        "setup",
        help="Primary guided setup for using Antigravity Claude from Codex",
    )
    setup_parser.add_argument("--check", action="store_true", help="Run read-only setup and Codex readiness checks")
    setup_parser.add_argument("--json", action="store_true", help="Print setup/readiness status as JSON")
    setup_parser.add_argument("--write", action="store_true", help="Run login and write the Codex provider block")
    setup_parser.add_argument(
        "--activate",
        action="store_true",
        help="Also make the gateway provider/model the active Codex default",
    )
    setup_parser.add_argument("--repair", action="store_true", help="Repair Codex provider config without OAuth login, skill install, or gateway start")
    setup_parser.add_argument("--no-input", action="store_true", help="Fail instead of prompting when OAuth credentials are missing")
    setup_parser.add_argument("--accounts", type=positive_int, default=1, help="Number of Google login flows when --write is used")
    setup_parser.add_argument("--no-browser", action="store_true", help="Use device-code login for xai-oauth setup instead of opening a browser")
    setup_parser.add_argument("--model", default=DEFAULT_CODEX_MODEL, help="Default Codex model to select")
    setup_parser.add_argument("--provider", default=DEFAULT_CODEX_PROVIDER_ID, help="Codex provider id")
    setup_parser.add_argument("--provider-name", default=DEFAULT_CODEX_PROVIDER_NAME, help="Provider display name")
    setup_parser.add_argument("--base-url", default=None, help="Gateway base URL ending in /v1; defaults to --port")
    setup_parser.add_argument("--config", default="~/.codex/config.toml", help="Codex config path")
    setup_parser.add_argument("--install-skill", action="store_true", help="Install or refresh the optional bundled $anti skill")
    setup_parser.add_argument("--skill-dir", default=DEFAULT_CODEX_SKILLS_DIR, help="Directory containing Codex skills")
    setup_parser.add_argument("--force", action="store_true", help="Back up and replace an existing anti skill when installing")
    setup_parser.add_argument("--verify-skill", action="store_true", help="Run installed Anti skill tests after install")
    setup_parser.add_argument("--start", action="store_true", help="Start the gateway in the background after writing config")
    setup_parser.add_argument("--port", type=int, default=51122, help="Gateway server port when --start is used")
    setup_parser.add_argument("--host", default="127.0.0.1", help="Gateway server host when --start is used")
    setup_parser.add_argument(
        "--op-env-file",
        help="Run a --start gateway through `op run --env-file PATH -- ...` so BYOK env keys come from 1Password",
    )
    setup_parser.add_argument(
        "--op-environment",
        help="Run a --start gateway through `op run --environment ID -- ...` for 1Password Environments beta",
    )
    setup_parser.add_argument(
        "--allow-remote",
        action="store_true",
        help="Allow non-loopback gateway clients when starting with a strong ANTIGRAVITY_GATEWAY_TOKEN",
    )
    setup_parser.add_argument("--gateway-timeout", type=float, default=2.0, help="Gateway model-catalog timeout")
    setup_parser.add_argument("--live", action="store_true", help="Run an explicit Google /v1/responses live generation smoke")
    setup_parser.add_argument("--live-model", help="Google model to use for --live; defaults to the selected setup model")
    setup_parser.add_argument("--live-timeout", type=float, default=30.0, help="Live generation smoke timeout")
    setup_parser.add_argument(
        "--gateway-token-env",
        default="ANTIGRAVITY_GATEWAY_TOKEN",
        help="Environment variable holding the gateway bearer token for remote gateways",
    )

    setup_google_parser = subparsers.add_parser(
        "setup-google",
        help="Write Codex config and sign Google Antigravity account(s) into rotation",
    )
    setup_google_parser.add_argument("--accounts", type=positive_int, default=1, help="Number of browser login flows to run")
    setup_google_parser.add_argument("--skip-codex-config", action="store_true", help="Do not write ~/.codex/config.toml")
    setup_google_parser.add_argument(
        "--activate",
        action="store_true",
        help="Also make the gateway provider/model the active Codex default",
    )
    setup_google_parser.add_argument("--skip-doctor", action="store_true", help="Do not run doctor after login")
    setup_google_parser.add_argument("--config", default="~/.codex/config.toml", help="Codex config path")
    setup_google_parser.add_argument("--model", default=DEFAULT_CODEX_MODEL, help="Default Codex model to select")
    setup_google_parser.add_argument("--provider", default=DEFAULT_CODEX_PROVIDER_ID, help="Codex provider id")
    setup_google_parser.add_argument("--provider-name", default=DEFAULT_CODEX_PROVIDER_NAME, help="Provider display name")
    setup_google_parser.add_argument("--base-url", default=None, help="Gateway base URL; defaults to --port")
    setup_google_parser.add_argument("--port", type=int, default=51122, help="Gateway server port to show in next-step output")

    setup_v2_parser = subparsers.add_parser(
        "setup-v2",
        help="Check Anti V2 workflow readiness and optionally install the bundled skill",
    )
    setup_v2_parser.add_argument("--skill-dir", default=DEFAULT_CODEX_SKILLS_DIR, help="Directory containing Codex skills")
    setup_v2_parser.add_argument("--base-url", default=DEFAULT_CODEX_BASE_URL, help="Gateway base URL ending in /v1")
    setup_v2_parser.add_argument("--timeout", type=float, default=2.0, help="Gateway model-catalog timeout")
    setup_v2_parser.add_argument(
        "--gateway-token-env",
        default="ANTIGRAVITY_GATEWAY_TOKEN",
        help="Environment variable holding the gateway bearer token for remote gateways",
    )
    setup_v2_parser.add_argument("--write", action="store_true", help="Install/update the bundled Anti skill")
    setup_v2_parser.add_argument("--force", action="store_true", help="Back up and replace an existing anti skill when --write is used")
    setup_v2_parser.add_argument("--verify-skill", action="store_true", help="Run installed Anti skill tests when --write is used")
    setup_v2_parser.add_argument("--check-google", action="store_true", help="Also inspect Google OAuth/account readiness")
    setup_v2_parser.add_argument("--check-byok", action="store_true", help="Also inspect BYOK provider key readiness")
    
    # doctor
    doctor_parser = subparsers.add_parser("doctor", help="Check status, health, configurations, and diagnosis")
    doctor_parser.add_argument("--byok-only", action="store_true", help="Skip Google OAuth/account checks")
    doctor_parser.add_argument("--gateway-base-url", default=DEFAULT_CODEX_BASE_URL, help="Expected Codex gateway base URL")
    doctor_parser.add_argument("--config", default="~/.codex/config.toml", help="Codex config path to verify")
    doctor_parser.add_argument("--provider", default=DEFAULT_CODEX_PROVIDER_ID, help="Codex provider id to verify")
    doctor_parser.add_argument("--codex-ready", action="store_true", help="Run native Codex model-picker readiness diagnostics")
    doctor_parser.add_argument("--json", action="store_true", help="Print doctor status as JSON when used with --codex-ready")
    doctor_parser.add_argument("--gateway-timeout", type=float, default=2.0, help="Gateway model-catalog timeout")
    doctor_parser.add_argument("--live", action="store_true", help="Run an explicit Google /v1/responses live generation smoke")
    doctor_parser.add_argument("--live-model", help="Google model to use for --live; defaults to the selected Codex model")
    doctor_parser.add_argument("--live-timeout", type=float, default=30.0, help="Live generation smoke timeout")
    doctor_parser.add_argument(
        "--gateway-token-env",
        default="ANTIGRAVITY_GATEWAY_TOKEN",
        help="Environment variable holding the gateway bearer token for remote gateways",
    )
    
    # accounts
    accounts_parser = subparsers.add_parser("accounts", help="List or manage configured Google accounts")
    accounts_sub = accounts_parser.add_subparsers(dest="accounts_action")
    accounts_sub.add_parser("list", help="List configured Google accounts")
    accounts_remove = accounts_sub.add_parser("remove", help="Remove a Google account from the encrypted rotation store")
    accounts_remove.add_argument("email", help="Google account email to remove")
    accounts_remove.add_argument("--yes", action="store_true", help="Confirm removal without prompting")
    accounts_reset = accounts_sub.add_parser("reset", help="Clear cooldown and failure state for one or all Google accounts")
    accounts_reset.add_argument("email", nargs="?", help="Google account email to reset")
    accounts_reset.add_argument("--all", action="store_true", dest="all_accounts", help="Reset all Google accounts")
    accounts_reset.add_argument("--yes", action="store_true", help="Confirm reset-all without prompting")

    configure_parser = subparsers.add_parser(
        "configure-codex",
        help="Print or write Codex config.toml settings for this gateway",
    )
    configure_parser.add_argument("--write", action="store_true", help="Update the Codex provider block in config.toml")
    configure_parser.add_argument(
        "--activate",
        action="store_true",
        help="Also make this provider/model the active Codex default",
    )
    configure_parser.add_argument("--config", default="~/.codex/config.toml", help="Codex config path")
    configure_parser.add_argument("--model", default=DEFAULT_CODEX_MODEL, help="Default Codex model to select")
    configure_parser.add_argument("--provider", default=DEFAULT_CODEX_PROVIDER_ID, help="Codex provider id")
    configure_parser.add_argument("--provider-name", default=DEFAULT_CODEX_PROVIDER_NAME, help="Provider display name")
    configure_parser.add_argument("--base-url", default=DEFAULT_CODEX_BASE_URL, help="Gateway base URL")

    install_skill_parser = subparsers.add_parser(
        "install-skill",
        help="Install the bundled Codex $anti sidecar skill into ~/.codex/skills",
    )
    install_skill_parser.add_argument(
        "--skill-dir",
        default=DEFAULT_CODEX_SKILLS_DIR,
        help="Directory containing Codex skills (default: ~/.codex/skills)",
    )
    install_skill_parser.add_argument("--force", action="store_true", help="Back up and replace an existing anti skill")
    install_skill_parser.add_argument("--dry-run", action="store_true", help="Show what would be installed without writing")
    install_skill_parser.add_argument("--verify", action="store_true", help="Run installed Anti skill tests after install")

    service_parser = subparsers.add_parser("service", help="Install, uninstall, or inspect a durable user gateway service")
    service_sub = service_parser.add_subparsers(dest="service_command", required=True)
    service_install = service_sub.add_parser("install", help="Install and start a per-user gateway service")
    service_install.add_argument("--port", type=int, default=51122, help="Gateway server port")
    service_install.add_argument("--host", default="127.0.0.1", help="Gateway server host")
    service_install.add_argument(
        "--op-env-file",
        help="Wrap the service command with `op run --env-file PATH -- ...` for BYOK provider keys",
    )
    service_install.add_argument(
        "--op-environment",
        help="Wrap the service command with `op run --environment ID -- ...` for 1Password Environments beta",
    )
    service_install.add_argument("--json", action="store_true", help="Print service status as JSON")
    service_uninstall = service_sub.add_parser("uninstall", help="Uninstall the per-user gateway service")
    service_uninstall.add_argument("--port", type=int, default=51122, help="Gateway server port")
    service_uninstall.add_argument("--json", action="store_true", help="Print service status as JSON")
    service_status_parser = service_sub.add_parser("status", help="Show gateway service status")
    service_status_parser.add_argument("--port", type=int, default=51122, help="Gateway server port")
    service_status_parser.add_argument("--json", action="store_true", help="Print service status as JSON")

    logs_parser = subparsers.add_parser("logs", help="Show, summarize, or clean sanitized gateway request logs")
    logs_parser.add_argument("logs_action", nargs="?", choices=["show", "clean", "summary"], default="show", help="Log action")
    logs_parser.add_argument("--tail", type=int, default=50, help="Number of recent entries to show")
    logs_parser.add_argument("--follow", action="store_true", help="Follow new request log entries")
    logs_parser.add_argument("--json", action="store_true", help="Print entries as JSON")
    logs_parser.add_argument("--since", default="24h", help="Summary window for `logs summary` (for example 30m, 24h, 7d, all)")

    models_parser = subparsers.add_parser("models", help="List and manage local model catalog overlays")
    models_sub = models_parser.add_subparsers(dest="models_command", required=True)
    models_list = models_sub.add_parser("list", help="List built-in and overlay models")
    models_list.add_argument("--json", action="store_true", help="Print model catalog as JSON")
    models_add = models_sub.add_parser("add", help="Add a local model catalog overlay")
    models_add.add_argument("id", help="Canonical model id to expose in /v1/models")
    models_add.add_argument("--backend-id", required=True, help="Backend model id sent to Antigravity")
    models_add.add_argument("--display-name", help="Model picker display name")
    models_add.add_argument("--family", choices=["claude", "gemini"], required=True, help="Model family")
    models_add.add_argument("--context-window", type=positive_int, required=True, help="Context window token count")
    models_add.add_argument(
        "--default-reasoning-level",
        choices=["low", "medium", "high", "xhigh"],
        default="high",
        help="Default Codex reasoning effort",
    )
    models_add.add_argument("--alias", action="append", help="Alias for setup/config input; repeatable")
    models_add.add_argument("--no-parallel-tool-calls", action="store_true", help="Advertise no parallel tool-call support")
    models_add.add_argument("--force", action="store_true", help="Allow intentional identifier shadowing in the overlay file")
    models_remove = models_sub.add_parser("remove", help="Remove a local model catalog overlay")
    models_remove.add_argument("id")
    models_sub.add_parser("doctor", help="Validate model overlay and runtime definitions")

    provider_parser = subparsers.add_parser("provider", help="Manage BYOK OpenAI-compatible providers")
    provider_sub = provider_parser.add_subparsers(dest="provider_command", required=True)
    provider_sub.add_parser("list", help="List BYOK providers")
    provider_sub.add_parser("presets", help="List built-in BYOK provider presets")
    provider_login = provider_sub.add_parser("login", help="Authenticate an OAuth-capable provider")
    provider_login.add_argument("provider", help="Provider id; currently xai-oauth")
    provider_login.add_argument("--device", action="store_true", help="Use xAI device-code OAuth flow")
    provider_login.add_argument("--no-browser", action="store_true", help="Alias for --device")
    provider_status = provider_sub.add_parser("status", help="Show OAuth provider status")
    provider_status.add_argument("provider", help="Provider id; currently xai-oauth")
    provider_status.add_argument("--json", action="store_true", help="Print status as JSON")
    provider_refresh = provider_sub.add_parser("refresh", help="Refresh OAuth provider tokens")
    provider_refresh.add_argument("provider", help="Provider id; currently xai-oauth")
    provider_logout = provider_sub.add_parser("logout", help="Remove OAuth provider tokens")
    provider_logout.add_argument("provider", help="Provider id; currently xai-oauth")
    provider_logout.add_argument("--yes", action="store_true", help="Confirm token removal")

    provider_set = provider_sub.add_parser("set", help="Configure a BYOK provider")
    provider_set.add_argument("provider", help="Provider id, e.g. openrouter, deepseek, xai, kimi, ollama, opencode, custom")
    provider_set.add_argument("--api-key", help="API key to store encrypted")
    provider_set.add_argument("--api-key-env", help="Environment variable name to read API key from")
    provider_set.add_argument(
        "--auth-mode",
        choices=["api-key", "api_key", "oauth"],
        help="Provider auth mode. Use xai-oauth for SuperGrok OAuth; use xai for XAI_API_KEY.",
    )
    provider_set.add_argument("--base-url", help="OpenAI-compatible base URL, e.g. https://api.deepseek.com/v1")
    provider_set.add_argument("--cloud", action="store_true", help="Use the preset cloud base URL when available")
    provider_set.add_argument("--model", action="append", dest="models", help="Provider model id to expose; repeatable")
    provider_set.add_argument("--display-name", help="Display name for model picker")
    provider_set.add_argument("--header", action="append", default=[], help="Extra HTTP header as Name:Value; repeatable")

    provider_remove = provider_sub.add_parser("remove", help="Remove a stored BYOK provider config")
    provider_remove.add_argument("provider")
    
    # start
    start_parser = subparsers.add_parser("start", help="Start the local Responses API gateway server")
    start_parser.add_argument("--port", type=int, default=51122, help="Gateway server port (default: 51122)")
    start_parser.add_argument("--host", default="127.0.0.1", help="Gateway server host (default: 127.0.0.1)")
    start_parser.add_argument(
        "--allow-remote",
        action="store_true",
        help="Allow non-loopback clients when ANTIGRAVITY_GATEWAY_TOKEN is set to at least 32 visible ASCII characters",
    )
    start_parser.add_argument("--background", action="store_true", help="Start the gateway as a background process with pid/log files")
    start_parser.add_argument(
        "--op-env-file",
        help="With --background, run the gateway through `op run --env-file PATH -- ...` for BYOK provider keys",
    )
    start_parser.add_argument(
        "--op-environment",
        help="With --background, run the gateway through `op run --environment ID -- ...` for 1Password Environments beta",
    )

    stop_parser = subparsers.add_parser("stop", help="Stop a background gateway started by codex-antigravity")
    stop_parser.add_argument("--port", type=int, default=51122, help="Gateway server port (default: 51122)")

    status_parser = subparsers.add_parser("status", help="Show background gateway pid/log status")
    status_parser.add_argument("--port", type=int, default=51122, help="Gateway server port (default: 51122)")
    status_parser.add_argument("--json", action="store_true", help="Print status as JSON")
    
    args = parser.parse_args()
    
    if args.command == "login":
        run_login(args)
    elif args.command == "setup":
        run_setup(args)
    elif args.command == "setup-google":
        run_setup_google(args)
    elif args.command == "setup-v2":
        run_setup_v2(args)
    elif args.command == "doctor":
        if args.codex_ready:
            if not run_codex_ready_doctor(args):
                sys.exit(1)
        elif not run_doctor(
            byok_only=args.byok_only,
            expected_base_url=args.gateway_base_url,
            config=args.config,
            provider_id=args.provider,
            live=getattr(args, "live", False),
            live_model=getattr(args, "live_model", None),
            live_timeout=getattr(args, "live_timeout", 30.0),
            gateway_token_env=getattr(args, "gateway_token_env", "ANTIGRAVITY_GATEWAY_TOKEN"),
        ):
            sys.exit(1)
    elif args.command == "accounts":
        run_accounts_command(args)
    elif args.command == "configure-codex":
        run_configure_codex(args)
    elif args.command == "install-skill":
        run_install_skill(args)
    elif args.command == "service":
        run_service_command(args)
    elif args.command == "logs":
        run_logs_command(args)
    elif args.command == "models":
        run_models_command(args)
    elif args.command == "provider":
        if args.provider_command == "login":
            run_xai_oauth_login(args)
        elif args.provider_command == "status":
            run_xai_oauth_status(args)
        elif args.provider_command == "refresh":
            run_xai_oauth_refresh(args)
        elif args.provider_command == "logout":
            run_xai_oauth_logout(args)
        elif args.provider_command == "presets":
            print("[*] Built-in BYOK provider presets:")
            for provider_id, preset in PROVIDER_PRESETS.items():
                models = ", ".join(preset.get("models", [])) or "(configure models)"
                auth_modes = ", ".join(
                    str(mode).replace("_", "-") for mode in preset.get("authModes", ["api_key"])
                )
                print(f"- {provider_id}: {preset.get('displayName')} @ {preset.get('baseUrl')} [{models}] auth: {auth_modes}")
                if preset.get("authNotes"):
                    print(f"  note: {preset['authNotes']}")
        elif args.provider_command == "list":
            providers = all_provider_configs()
            if not providers:
                print("[*] No BYOK providers configured. Use `codex-antigravity provider set ...`.")
                return
            stored_providers = load_provider_config().get("providers", {})
            stored_provider_ids = set(stored_providers) if isinstance(stored_providers, dict) else set()
            print("[*] BYOK Providers:")
            for provider_id, provider in providers.items():
                key_status = provider_key_status(
                    provider,
                    configured_label=provider_configured_label(provider_id, provider, stored_provider_ids),
                )
                models = provider.get("models", [])
                model_list = ", ".join(str(m.get("id") if isinstance(m, dict) else m) for m in models) or "(no models)"
                print(f"- {provider_id}: {provider.get('displayName', provider_id)} ({key_status})")
                print(f"  auth: {provider_auth_mode(provider).replace('_', '-')}")
                print(f"  base_url: {provider.get('baseUrl')}")
                print(f"  models: {model_list}")
        elif args.provider_command == "set":
            try:
                provider_id = validate_provider_id(args.provider)
            except (RuntimeError, ValueError) as e:
                raise SystemExit(str(e)) from e
            try:
                preset = provider_preset(provider_id)
            except ValueError:
                preset = {}
            base_url = args.base_url
            if args.cloud and preset.get("cloudBaseUrl"):
                base_url = preset["cloudBaseUrl"]
            headers = {}
            for header in args.header:
                name, sep, value = header.partition(":")
                if not sep or not name.strip():
                    raise SystemExit(f"Invalid --header value {header!r}; use Name:Value")
                headers[name.strip()] = value.strip()
            try:
                provider = set_provider_config(
                    provider_id,
                    api_key=args.api_key,
                    api_key_env=args.api_key_env,
                    auth_mode=args.auth_mode,
                    base_url=base_url,
                    models=args.models,
                    display_name=args.display_name,
                    headers=headers or None,
                )
            except (RuntimeError, ValueError) as e:
                raise SystemExit(redact_secret_text(str(e))) from e
            print(f"[+] Configured BYOK provider {provider['id']} at {provider.get('baseUrl')}")
            print(f"    auth: {provider_auth_mode(provider).replace('_', '-')}")
            if provider.get("models"):
                key_status = provider_key_status(provider, configured_label="key OK")
                if key_status == "key OK":
                    print("[+] Exposed models:")
                else:
                    key_hint = provider.get("apiKeyEnv") or "a provider API key"
                    print(f"[!] Models are configured but hidden until {key_hint} is available ({key_status}).")
                for model in provider["models"]:
                    model_id = model.get("id") if isinstance(model, dict) else model
                    print(f"    {provider['id']}:{model_id}")
        elif args.provider_command == "remove":
            try:
                existed = remove_provider_config(args.provider)
            except RuntimeError as e:
                raise SystemExit(str(e)) from e
            if existed:
                print(f"[+] Removed BYOK provider {args.provider}")
            else:
                print(f"[*] No stored BYOK provider named {args.provider}")
    elif args.command == "start":
        if args.background:
            start_gateway_background(args)
        else:
            if getattr(args, "op_env_file", None) or getattr(args, "op_environment", None):
                raise SystemExit("1Password gateway options require `codex-antigravity start --background`.")
            import uvicorn
            require_safe_gateway_host(args.host, args.allow_remote)
            print(f"[*] Starting local Responses API compatible gateway server on {args.host}:{args.port}...")
            uvicorn.run("codex_antigravity_auth.server:app", host=args.host, port=args.port, log_level="info")
    elif args.command == "stop":
        stop_gateway(args)
    elif args.command == "status":
        run_gateway_status(args)

if __name__ == "__main__":
    main()
