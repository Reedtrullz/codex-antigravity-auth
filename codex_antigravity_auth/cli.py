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
from .oauth import authorize_antigravity, exchange_antigravity
from .storage import load_accounts, save_accounts
from .constants import resolve_oauth_credentials

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
    
    print(f"[*] Opening browser authorization URL...")
    print(f"[*] If the browser doesn't open automatically, navigate to:\n{url}\n")
    webbrowser.open(url)
    
    # Wait for callback
    while server.auth_code is None:
        server.handle_request()
        
    print("[*] Callback received. Exchanging code for tokens...")
    # Retrieve verifier from oauth module verifier store
    from .oauth import get_pkce_verifier
    verifier_info = get_pkce_verifier(auth_info["state_id"])
    if not verifier_info:
        print("[!] PKCE verifier state not found or expired!")
        sys.exit(1)
        
    tokens = exchange_antigravity(server.auth_code, verifier_info["verifier"])
    
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
    except Exception:
        email = "unknown-google-account"

    # Save to storage
    data = load_accounts()
    accounts = data.setdefault("accounts", [])
    
    # Check if account already exists, update if so, or add new
    existing_idx = None
    for idx, acc in enumerate(accounts):
        if acc.get("email") == email:
            existing_idx = idx
            break
            
    account_entry = {
        "email": email,
        "refreshToken": tokens["refresh_token"],
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
        
    # Check accounts
    data = load_accounts()
    accounts = data.get("accounts", [])
    if accounts:
        print(f"[PASS] Authenticated Accounts: {len(accounts)} configured")
        for acc in accounts:
            email = acc.get("email")
            expires_at = acc.get("expiresAt", 0)
            status = "ACTIVE" if expires_at > time.time() else "EXPIRED (will auto-refresh)"
            print(f"       - {email} ({status})")
    else:
        print("[WARN] Authenticated Accounts: 0 accounts found.")
        print("       Run `codex-antigravity login` to add an account.")
        
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
    
    # start
    start_parser = subparsers.add_parser("start", help="Start the local Responses API gateway server")
    start_parser.add_argument("--port", type=int, default=51122, help="Gateway server port (default: 51122)")
    start_parser.add_argument("--host", default="127.0.0.1", help="Gateway server host (default: 127.0.0.1)")
    
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
            print(f"[{idx}] {acc.get('email')} (Expires: {time.ctime(acc.get('expiresAt', 0))})")
    elif args.command == "start":
        import uvicorn
        print(f"[*] Starting local Responses API compatible gateway server on {args.host}:{args.port}...")
        uvicorn.run("codex_antigravity_auth.server:app", host=args.host, port=args.port, log_level="info")

if __name__ == "__main__":
    main()
