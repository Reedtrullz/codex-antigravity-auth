import sys
import os
import argparse
import http.server
import re
import shlex
import socketserver
import webbrowser
import time
import json
import tempfile
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from .byok import (
    PROVIDER_PRESETS,
    all_provider_configs,
    provider_preset,
    remove_provider_config,
    resolve_api_key,
    set_provider_config,
    validate_http_base_url,
    validate_provider_api_key,
    validate_provider_id,
)
from .oauth import (
    OAUTH_HTTP_TIMEOUT_SECONDS,
    authorize_antigravity,
    decode_state,
    exchange_antigravity,
    token_expires_in_seconds,
)
from .storage import load_accounts, save_accounts
from .constants import is_loopback_host, resolve_oauth_credentials
from .redaction import redact_secret_text

DEFAULT_CODEX_PROVIDER_ID = "antigravity"
DEFAULT_CODEX_PROVIDER_NAME = "Google Antigravity"
DEFAULT_CODEX_MODEL = "gemini-3.5-flash-high"
DEFAULT_CODEX_BASE_URL = "http://localhost:51122/v1"
CODEX_PROVIDER_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


class OAuthCallbackHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # Suppress logging of HTTP requests to keep CLI clean
        pass

    def do_GET(self):
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        
        if "code" in query:
            code = query["code"][0]
            # Store globally on server to be grabbed by parent thread
            self.server.auth_code = code
            self.server.auth_state = query.get("state", [None])[0]
            self.wfile.write(b"""
            <html>
            <head><style>body { font-family: sans-serif; text-align: center; margin-top: 50px; background-color: #f4f7f6; }</style></head>
            <body>
                <h1 style="color: #4caf50;">Authentication Successful!</h1>
                <p>You can close this tab and return to the terminal.</p>
            </body>
            </html>
            """)
        else:
            self.wfile.write(b"""
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

def normalize_epoch_seconds(value):
    try:
        ts = float(value or 0)
    except (TypeError, ValueError):
        return 0
    if ts > 10_000_000_000:
        ts = ts / 1000
    return ts


def require_safe_gateway_host(host: str, allow_remote: bool) -> None:
    if is_loopback_host(host):
        return
    if not allow_remote:
        raise SystemExit(
            "Refusing to bind the unauthenticated gateway to a non-loopback host. "
            "Use --allow-remote with ANTIGRAVITY_GATEWAY_TOKEN set to opt in."
        )
    if not os.environ.get("ANTIGRAVITY_GATEWAY_TOKEN"):
        raise SystemExit("ANTIGRAVITY_GATEWAY_TOKEN must be set when --allow-remote is used.")
    os.environ["ANTIGRAVITY_ALLOW_REMOTE"] = "1"


def provider_key_status(provider: dict, *, configured_label: str) -> str:
    try:
        api_key = validate_provider_api_key(resolve_api_key(provider))
    except ValueError:
        return "malformed key"
    return configured_label if api_key else "missing key"


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
    return value


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
) -> str:
    model = validate_codex_model_id(model)
    provider_id = validate_codex_provider_id(provider_id)
    return "\n".join(
        [
            f"model = {toml_string(model)}",
            f"model_provider = {toml_string(provider_id)}",
            'wire_api = "responses"',
            "",
            render_codex_provider_table(
                provider_id=provider_id,
                provider_name=provider_name,
                base_url=base_url,
            ),
            "",
        ]
    )


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
        )

    lines = existing.splitlines()
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
) -> tuple[bool, Path | None]:
    model = validate_codex_model_id(model)
    provider_id = validate_codex_provider_id(provider_id)
    provider_name = validate_codex_provider_name(provider_name)
    existing = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
    updated = merge_codex_config(
        existing,
        model=model,
        provider_id=provider_id,
        provider_name=provider_name,
        base_url=base_url,
    )
    if existing == updated:
        return False, None

    config_path.parent.mkdir(parents=True, exist_ok=True)
    backup_path = None
    if config_path.exists():
        backup_path = _codex_config_backup_path(config_path)
        _write_private_text(backup_path, existing)
    _write_private_text(config_path, updated)
    return True, backup_path


def configure_codex_write_command(args) -> str:
    parts = ["codex-antigravity", "configure-codex", "--write"]
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


def run_configure_codex(args) -> None:
    config_path = Path(os.path.expanduser(args.config))
    try:
        snippet = render_codex_config_snippet(
            model=args.model,
            provider_id=args.provider,
            provider_name=args.provider_name,
            base_url=args.base_url,
        )
    except ValueError as e:
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
        )
    except ValueError as e:
        raise SystemExit(str(e)) from e
    if changed:
        print(f"[+] Updated Codex config: {config_path}")
        if backup_path:
            print(f"[+] Backup written: {backup_path}")
    else:
        print(f"[*] Codex config already points at this gateway: {config_path}")
    print("[*] Start the gateway with: codex-antigravity start")

def run_local_oauth_flow():
    # Verify environment credentials or credentials file exists
    cid, csec = resolve_oauth_credentials()
    if not cid or not csec:
        print("[!] No Google OAuth Client Credentials configured!")
        print("Please configure them via env vars or ~/.codex/antigravity-credentials.json first.")
        print("See the README.md for setup instructions.")
        sys.exit(1)

    print("[*] Initiating Google Antigravity OAuth login...")
    auth_info = authorize_antigravity()
    url = auth_info["url"]
    
    server = OAuthServer(("localhost", 51121), OAuthCallbackHandler)
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
    
    if existing_idx is not None:
        accounts[existing_idx].update(account_entry)
        print(f"[+] Successfully re-authenticated and updated Google Account: {email}")
    else:
        accounts.append(account_entry)
        print(f"[+] Successfully authenticated new Google Account: {email}")
        
    save_accounts(data)

def run_doctor():
    print("=" * 60)
    print("           GOOGLE ANTIGRAVITY AUTH DOCTOR           ")
    print("=" * 60)
    
    # Check Client Credentials
    cid, csec = resolve_oauth_credentials()
    if cid and csec:
        print(f"[PASS] Google OAuth Client Credentials: Configured (Client ID: ...{cid[-15:]})")
    else:
        print("[FAIL] Google OAuth Client Credentials: Not Configured!")
        print("       Set ANTIGRAVITY_CLIENT_ID and ANTIGRAVITY_CLIENT_SECRET,")
        print("       or create ~/.codex/antigravity-credentials.json")
        
    # Check Token secure storage status
    try:
        from .storage import _get_encryption_key, KEYRING_SERVICE_NAME
        import keyring
        stored_key = keyring.get_password(KEYRING_SERVICE_NAME, "storage-encryption-key")
        if stored_key:
            print("[PASS] Token Storage Encryption: SECURE (OS Keyring Integrated)")
        else:
            print("[WARN] Token Storage Encryption: PARTIAL (Using fallback key; keyring password lookup returned empty)")
    except Exception as e:
        print(f"[WARN] Token Storage Encryption: PARTIAL (Fallback active. Error: {redact_secret_text(str(e))})")
        
    # Check network connectivity to Google Antigravity backend
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
                    print(f"[FAIL] Google Antigravity Connectivity: REACHABLE but status {resp.status}")
    except Exception as e:
        print(f"[FAIL] Google Antigravity Connectivity: OFFLINE / TIMEOUT ({redact_secret_text(str(e))})")
        
    # Check accounts
    data = load_accounts()
    accounts = data.get("accounts", [])
    if accounts:
        print(f"[PASS] Authenticated Accounts: {len(accounts)} configured")
        for acc in accounts:
            email = acc.get("email")
            expires_at = normalize_epoch_seconds(acc.get("expiresAt", 0))
            status = "ACTIVE" if expires_at > time.time() else "EXPIRED (will auto-refresh)"
            print(f"       - {email} ({status})")
    else:
        print("[WARN] Authenticated Accounts: 0 accounts found.")
        print("       Run `codex-antigravity login` to add an account.")

    # Check BYOK providers
    try:
        providers = all_provider_configs()
        if providers:
            print(f"[PASS] BYOK Providers: {len(providers)} configured or env-enabled")
            for provider_id, provider in providers.items():
                api_key_status = provider_key_status(provider, configured_label="key OK")
                models = provider.get("models", [])
                print(f"       - {provider_id} ({api_key_status}, {len(models)} model(s), {provider.get('baseUrl')})")
        else:
            print("[INFO] BYOK Providers: none configured.")
    except Exception as e:
        print(f"[WARN] BYOK Providers: could not load provider config ({redact_secret_text(str(e))})")
        
    # Check Codex config
    codex_config = Path(os.path.expanduser("~/.codex/config.toml"))
    if codex_config.is_file():
        print(f"[PASS] Codex config.toml: Found (~/.codex/config.toml)")
        try:
            with open(codex_config, "r") as f:
                content = f.read()
                if "base_url" in content and "localhost:51122" in content:
                    print("       - Verified: Pointing correctly to this gateway server.")
                else:
                    print("       - [WARN] config.toml found but not pointing to localhost:51122.")
        except Exception:
            pass
    else:
        print("[WARN] Codex config.toml: Not found.")
        print("       Run `codex-antigravity configure-codex --write` to install the gateway provider block.")
        
    print("=" * 60)

def main():
    parser = argparse.ArgumentParser(description="Codex Antigravity Auth CLI Utility")
    subparsers = parser.add_subparsers(dest="command", required=True)
    
    # login
    subparsers.add_parser("login", help="Authenticate with a Google account using OAuth PKCE flow")
    
    # doctor
    subparsers.add_parser("doctor", help="Check status, health, configurations, and diagnosis")
    
    # accounts
    subparsers.add_parser("accounts", help="List all configured accounts")

    configure_parser = subparsers.add_parser(
        "configure-codex",
        help="Print or write Codex config.toml settings for this gateway",
    )
    configure_parser.add_argument("--write", action="store_true", help="Update ~/.codex/config.toml in place")
    configure_parser.add_argument("--config", default="~/.codex/config.toml", help="Codex config path")
    configure_parser.add_argument("--model", default=DEFAULT_CODEX_MODEL, help="Default Codex model to select")
    configure_parser.add_argument("--provider", default=DEFAULT_CODEX_PROVIDER_ID, help="Codex provider id")
    configure_parser.add_argument("--provider-name", default=DEFAULT_CODEX_PROVIDER_NAME, help="Provider display name")
    configure_parser.add_argument("--base-url", default=DEFAULT_CODEX_BASE_URL, help="Gateway base URL")

    provider_parser = subparsers.add_parser("provider", help="Manage BYOK OpenAI-compatible providers")
    provider_sub = provider_parser.add_subparsers(dest="provider_command", required=True)
    provider_sub.add_parser("list", help="List BYOK providers")
    provider_sub.add_parser("presets", help="List built-in BYOK provider presets")

    provider_set = provider_sub.add_parser("set", help="Configure a BYOK provider")
    provider_set.add_argument("provider", help="Provider id, e.g. openrouter, deepseek, xai, kimi, ollama, opencode, custom")
    provider_set.add_argument("--api-key", help="API key to store encrypted")
    provider_set.add_argument("--api-key-env", help="Environment variable name to read API key from")
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
    start_parser.add_argument("--allow-remote", action="store_true", help="Allow non-loopback clients when ANTIGRAVITY_GATEWAY_TOKEN is set")
    
    args = parser.parse_args()
    
    if args.command == "login":
        run_local_oauth_flow()
    elif args.command == "doctor":
        run_doctor()
    elif args.command == "accounts":
        data = load_accounts()
        accounts = data.get("accounts", [])
        if not accounts:
            print("[*] No configured accounts found. Run `codex-antigravity login` first.")
            return
        print("[*] Configured Google Accounts:")
        for idx, acc in enumerate(accounts):
            print(f"[{idx}] {acc.get('email')} (Expires: {time.ctime(normalize_epoch_seconds(acc.get('expiresAt', 0)))})")
    elif args.command == "configure-codex":
        run_configure_codex(args)
    elif args.command == "provider":
        if args.provider_command == "presets":
            print("[*] Built-in BYOK provider presets:")
            for provider_id, preset in PROVIDER_PRESETS.items():
                models = ", ".join(preset.get("models", [])) or "(configure models)"
                print(f"- {provider_id}: {preset.get('displayName')} @ {preset.get('baseUrl')} [{models}]")
        elif args.provider_command == "list":
            providers = all_provider_configs()
            if not providers:
                print("[*] No BYOK providers configured. Use `codex-antigravity provider set ...`.")
                return
            print("[*] BYOK Providers:")
            for provider_id, provider in providers.items():
                key_status = provider_key_status(provider, configured_label="configured")
                models = provider.get("models", [])
                model_list = ", ".join(str(m.get("id") if isinstance(m, dict) else m) for m in models) or "(no models)"
                print(f"- {provider_id}: {provider.get('displayName', provider_id)} ({key_status})")
                print(f"  base_url: {provider.get('baseUrl')}")
                print(f"  models: {model_list}")
        elif args.provider_command == "set":
            try:
                provider_id = validate_provider_id(args.provider)
            except ValueError as e:
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
                    base_url=base_url,
                    models=args.models,
                    display_name=args.display_name,
                    headers=headers or None,
                )
            except ValueError as e:
                raise SystemExit(str(e)) from e
            print(f"[+] Configured BYOK provider {provider['id']} at {provider.get('baseUrl')}")
            if provider.get("models"):
                print("[+] Exposed models:")
                for model in provider["models"]:
                    model_id = model.get("id") if isinstance(model, dict) else model
                    print(f"    {provider['id']}:{model_id}")
        elif args.provider_command == "remove":
            existed = remove_provider_config(args.provider)
            if existed:
                print(f"[+] Removed BYOK provider {args.provider}")
            else:
                print(f"[*] No stored BYOK provider named {args.provider}")
    elif args.command == "start":
        import uvicorn
        require_safe_gateway_host(args.host, args.allow_remote)
        print(f"[*] Starting local Responses API compatible gateway server on {args.host}:{args.port}...")
        uvicorn.run("codex_antigravity_auth.server:app", host=args.host, port=args.port, log_level="info")

if __name__ == "__main__":
    main()
