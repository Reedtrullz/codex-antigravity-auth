import sys
import os
import argparse
import http.server
import socketserver
import webbrowser
import time
import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from .byok import (
    PROVIDER_PRESETS,
    all_provider_configs,
    provider_preset,
    remove_provider_config,
    resolve_api_key,
    set_provider_config,
)
from .oauth import authorize_antigravity, decode_state, exchange_antigravity
from .storage import load_accounts, save_accounts
from .constants import is_loopback_host, resolve_oauth_credentials

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
        with urllib.request.urlopen(req) as resp:
            user_info = json.loads(resp.read().decode("utf-8"))
            email = user_info.get("email")
    except Exception as e:
        print(f"[!] Could not retrieve Google account email: {e}")
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
        "expiresAt": int(time.time()) + tokens.get("expires_in", 3600),
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
        print(f"[WARN] Token Storage Encryption: PARTIAL (Fallback active. Error: {e})")
        
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
        print(f"[FAIL] Google Antigravity Connectivity: OFFLINE / TIMEOUT ({e})")
        
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
                api_key_status = "key OK" if resolve_api_key(provider) else "missing key"
                models = provider.get("models", [])
                print(f"       - {provider_id} ({api_key_status}, {len(models)} model(s), {provider.get('baseUrl')})")
        else:
            print("[INFO] BYOK Providers: none configured.")
    except Exception as e:
        print(f"[WARN] BYOK Providers: could not load provider config ({e})")
        
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
        print("       Configure model_provider to point to http://localhost:51122/v1.")
        
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
                key_status = "configured" if resolve_api_key(provider) else "missing key"
                models = provider.get("models", [])
                model_list = ", ".join(str(m.get("id") if isinstance(m, dict) else m) for m in models) or "(no models)"
                print(f"- {provider_id}: {provider.get('displayName', provider_id)} ({key_status})")
                print(f"  base_url: {provider.get('baseUrl')}")
                print(f"  models: {model_list}")
        elif args.provider_command == "set":
            try:
                preset = provider_preset(args.provider)
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
            provider = set_provider_config(
                args.provider,
                api_key=args.api_key,
                api_key_env=args.api_key_env,
                base_url=base_url,
                models=args.models,
                display_name=args.display_name,
                headers=headers or None,
            )
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
