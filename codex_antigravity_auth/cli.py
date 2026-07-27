import sys
import os
import argparse
import getpass
import http.server
import math
import re
import shlex
import socketserver
import subprocess
import webbrowser
import time
import json
import tempfile
import secrets
import shutil
import urllib.error
import urllib.request
from importlib import metadata as importlib_metadata
from importlib.resources import as_file, files
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from .byok import (
    PROVIDER_PRESETS,
    all_provider_configs,
    all_provider_configs_read_only,
    has_provider_api_key_env,
    load_provider_config,
    provider_auth_mode,
    provider_capabilities,
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
_DEFAULT_GET_CODEX_HOME = get_codex_home


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
    # as_file keeps zip-imported Traversable sources working with copytree.
    with as_file(source) as source_path:
        shutil.copytree(
            source_path,
            target,
            dirs_exist_ok=True,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store"),
        )
    for path in target.rglob("*"):
        if path.is_file():
            path.chmod(0o755 if path.parent.name == "scripts" and path.suffix == ".py" else 0o644)


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


from . import cli_doctor, cli_service, cli_setup  # noqa: E402


# Re-export moved helpers so `codex_antigravity_auth.cli.<name>` imports and
# test patch targets keep working after the cli_* module split.
_codex_home_read_only = cli_service._codex_home_read_only
add_gateway_reachability = cli_service.add_gateway_reachability
gateway_base_url_for_port = cli_service.gateway_base_url_for_port
gateway_model_ids = cli_service.gateway_model_ids
gateway_pid_matches = cli_service.gateway_pid_matches
gateway_process_command = cli_service.gateway_process_command
gateway_runtime_paths = cli_service.gateway_runtime_paths
gateway_status_info = cli_service.gateway_status_info
local_gateway_base_url = cli_service.local_gateway_base_url
process_is_running = cli_service.process_is_running
reachable_gateway_status_info = cli_service.reachable_gateway_status_info
read_pid_file = cli_service.read_pid_file
run_gateway_status = cli_service.run_gateway_status
run_logs_command = cli_service.run_logs_command
run_service_command = cli_service.run_service_command
start_gateway_background = cli_service.start_gateway_background
stop_gateway = cli_service.stop_gateway
wait_for_gateway_model_ids = cli_service.wait_for_gateway_model_ids
_diagnostic_all_provider_configs = cli_doctor._diagnostic_all_provider_configs
_diagnostic_load_accounts = cli_doctor._diagnostic_load_accounts
_installed_package_version = cli_doctor._installed_package_version
_read_codex_config_for_readiness = cli_doctor._read_codex_config_for_readiness
_read_version_cache = cli_doctor._read_version_cache
_responses_output_preview = cli_doctor._responses_output_preview
_source_checkout_version = cli_doctor._source_checkout_version
_validate_google_live_model = cli_doctor._validate_google_live_model
_version_cache_path = cli_doctor._version_cache_path
_version_tuple = cli_doctor._version_tuple
_write_version_cache = cli_doctor._write_version_cache
codex_ready_report = cli_doctor.codex_ready_report
gateway_generate_probe = cli_doctor.gateway_generate_probe
google_family_rotation_status = cli_doctor.google_family_rotation_status
latest_pypi_version = cli_doctor.latest_pypi_version
provider_capability_mismatches = cli_doctor.provider_capability_mismatches
readiness_storage_diagnostics = cli_doctor.readiness_storage_diagnostics
run_codex_ready_doctor = cli_doctor.run_codex_ready_doctor
run_doctor = cli_doctor.run_doctor
version_check_result = cli_doctor.version_check_result
_print_setup_report = cli_setup._print_setup_report
_setup_check = cli_setup._setup_check
byok_setup_next_command = cli_setup.byok_setup_next_command
maybe_prompt_and_save_oauth_credentials = cli_setup.maybe_prompt_and_save_oauth_credentials
run_local_oauth_flow = cli_setup.run_local_oauth_flow
run_login = cli_setup.run_login
run_setup = cli_setup.run_setup
run_setup_google = cli_setup.run_setup_google
run_setup_v2 = cli_setup.run_setup_v2
setup_byok_preflight = cli_setup.setup_byok_preflight
setup_effective_base_url = cli_setup.setup_effective_base_url
setup_service_followup_command = cli_setup.setup_service_followup_command
validate_oauth_credentials_with_google = cli_setup.validate_oauth_credentials_with_google
