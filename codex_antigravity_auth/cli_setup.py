"""Interactive setup and login commands (split from cli.py)."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import shlex
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path
from urllib.parse import urlencode, urlparse

from . import cli as _cli


def setup_effective_base_url(args) -> str:
    raw_base_url = getattr(args, "base_url", None)
    if not raw_base_url:
        return _cli.gateway_base_url_for_port(getattr(args, "port", 51122))
    base_url = _cli.validate_http_base_url(raw_base_url, label="gateway base URL")
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


def run_setup_v2(args) -> None:
    print("=" * 60)
    print("             ANTI V2 WORKFLOW SETUP CHECK           ")
    print("=" * 60)
    skill_dir = Path(os.path.expanduser(args.skill_dir))
    destination = skill_dir / _cli.BUNDLED_CODEX_SKILL_NAME

    try:
        _cli.bundled_skill_root()
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
        _cli.run_install_skill(install_args)
    elif destination.is_dir():
        description = _cli.codex_skill_short_description(destination)
        if _cli.codex_skill_matches_bundled(destination):
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
        ids = _cli.gateway_model_ids(
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
        print(f"[WARN] Gateway /v1/models: {_cli.redact_secret_text(str(exc))}")
        print("       Start the gateway with `codex-antigravity start` when you want live workflows.")

    providers = {}
    if args.check_byok:
        try:
            providers = _cli.all_provider_configs()
            stored_providers = _cli.load_provider_config().get("providers", {})
            stored_provider_ids = set(stored_providers) if isinstance(stored_providers, dict) else set()
        except Exception as exc:
            print(f"[WARN] BYOK provider visibility: could not load provider config ({_cli.redact_secret_text(str(exc))})")
            providers = {}
            stored_provider_ids = set()
        if providers:
            print(f"[PASS] BYOK provider visibility: {len(providers)} configured/env/local provider(s)")
            if gateway_ids is None:
                print("[WARN] BYOK gateway advertisement: unverified because /v1/models was not reachable")
            for provider_id, provider in providers.items():
                status = _cli.provider_key_status(provider, configured_label=_cli.provider_configured_label(provider_id, provider, stored_provider_ids))
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
        cid, csec = _cli.resolve_oauth_credentials()
        if cid and csec:
            print("[PASS] Google OAuth credentials: configured")
        else:
            print("[WARN] Google OAuth credentials: missing")
        accounts = _cli.load_accounts().get("accounts", [])
        print(f"[INFO] Google account rotation pool: {len(accounts)} account(s)")

    if args.check_byok and providers:
        unusable = [
            provider_id
            for provider_id, provider in providers.items()
            if _cli.provider_key_status(provider, configured_label="key OK") != "key OK"
        ]
        if unusable:
            print("[WARN] BYOK providers not usable: " + ", ".join(unusable))
        elif gateway_ids is None:
            print("[INFO] BYOK local readiness: all visible providers have usable keys or local keyless access")
            print("       Gateway model-picker visibility remains unverified until /v1/models is reachable.")
        else:
            print("[PASS] BYOK readiness: all visible providers have usable keys or local keyless access")

    print("=" * 60)


def run_local_oauth_flow(*, select_account: bool = False) -> dict:
    # Verify environment credentials or credentials file exists
    cid, csec = _cli.resolve_oauth_credentials()
    if not cid or not csec:
        print("[!] No Google OAuth Client Credentials configured!")
        print("Please configure them via env vars or ~/.codex/antigravity-credentials.json first.")
        print("See the README.md for setup instructions.")
        sys.exit(1)

    print("[*] Initiating Google Antigravity OAuth login...")
    auth_info = _cli.authorize_antigravity(select_account=select_account)
    url = auth_info["url"]

    try:
        server = _cli.OAuthServer(("localhost", 51121), _cli.OAuthCallbackHandler)
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
            returned_state = _cli.decode_state(server.auth_state or "")
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

        tokens = _cli.exchange_antigravity(server.auth_code, verifier_info["verifier"])
    finally:
        server.server_close()

    # Discover the Cloud Code Assist project for this account.
    # The backend rejects requests without a valid project id (403 VALIDATION_REQUIRED).
    print("[*] Discovering Cloud Code Assist project...")
    project_id = None
    try:
        from .oauth import discover_project_id
        project_id = discover_project_id(tokens["access_token"])
    except Exception as exc:
        print(f"[!] Project discovery failed: {exc}")
    if project_id:
        print(f"[+] Discovered project: {project_id}")
    else:
        print("[!] WARNING: Could not discover project id. Requests may fail with 403.")

    # Extract user profile email
    email = None
    try:
        import urllib.request
        req = urllib.request.Request(
            "https://www.googleapis.com/oauth2/v2/userinfo",
            headers={"Authorization": f"Bearer {tokens['access_token']}"}
        )
        with urllib.request.urlopen(req, timeout=_cli.OAUTH_HTTP_TIMEOUT_SECONDS) as resp:
            user_info = json.loads(resp.read().decode("utf-8"))
            email = user_info.get("email")
    except Exception as e:
        print(f"[!] Could not retrieve Google account email: {_cli.redact_secret_text(str(e))}")
        sys.exit(1)
    if not email:
        print("[!] Google account email was missing from userinfo response.")
        sys.exit(1)

    # Save to storage
    data = _cli.load_accounts()
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
        "expiresAt": int(time.time()) + _cli.token_expires_in_seconds(tokens),
    }
    if project_id:
        account_entry["projectId"] = project_id

    result = _cli.upsert_google_account(data, account_entry)
    if result["created"]:
        print(f"[+] Successfully authenticated new Google Account: {email}")
    else:
        print(f"[+] Successfully re-authenticated and updated Google Account: {email}")

    _cli.save_accounts(data)
    print(f"[+] {email} is in the Google account rotation pool ({result['account_count']} total).")
    return result


def run_login(args) -> None:
    count = getattr(args, "count", 1)
    select_account = getattr(args, "select_account", False) or count > 1
    if count > 1:
        print(f"[*] Running {count} Google OAuth login flows.")
        print("[*] Choose a different Google account in each browser flow to build the rotation pool.")
    for attempt in range(count):
        if count > 1:
            print(f"[*] Login {attempt + 1}/{count}")
        _cli.run_local_oauth_flow(select_account=select_account)
    _cli.print_account_rotation_summary()


def run_setup_google(args) -> None:
    base_url = args.base_url or f"http://localhost:{args.port}/v1"
    try:
        _cli.render_codex_config_snippet(
            model=args.model,
            provider_id=args.provider,
            provider_name=args.provider_name,
            base_url=base_url,
        )
    except (OSError, RuntimeError, ValueError) as e:
        raise SystemExit(str(e)) from e
    cid, csec = _cli.resolve_oauth_credentials()
    if not cid or not csec:
        raise SystemExit(
            "Google OAuth client credentials are not configured. "
            "Set ANTIGRAVITY_CLIENT_ID and ANTIGRAVITY_CLIENT_SECRET, "
            "or create ~/.codex/antigravity-credentials.json before running setup-google."
        )
    _cli.run_login(argparse.Namespace(count=args.accounts, select_account=True))

    if not args.skip_codex_config:
        print("[*] Installing Codex provider block...")
        _cli.run_configure_codex(
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
        if not _cli.run_doctor(expected_base_url=base_url, config=args.config, provider_id=args.provider):
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
    if provider is None:
        try:
            provider = _cli.provider_preset(provider_prefix)
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
        providers = _cli.all_provider_configs()
    except Exception as exc:
        return "fail", f"Could not load BYOK provider configuration: {_cli.redact_secret_text(str(exc))}", None
    provider = providers.get(provider_prefix)
    if not provider:
        return "fail", f"BYOK provider '{provider_prefix}' is not configured", None
    key_status = _cli.provider_key_status(provider, configured_label="key OK")
    if key_status != "key OK":
        credential_name = "OAuth login" if _cli.provider_auth_mode(provider) == "oauth" else "key"
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
        return "warn", f"Could not validate OAuth credentials with Google; continuing ({_cli.redact_secret_text(str(exc))})"


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
            _cli._setup_check(
                checks,
                "google_oauth_client_id_shape",
                "warn",
                "client id does not end with .apps.googleusercontent.com",
            )
        status, detail = _cli.validate_oauth_credentials_with_google(client_id, client_secret)
        _cli._setup_check(checks, "google_oauth_credentials_validation", status, detail)
        if status == "fail":
            return None, None
        path = _cli.save_oauth_credentials(client_id, client_secret)
    except (EOFError, KeyboardInterrupt):
        raise SystemExit("OAuth credential entry was cancelled; Codex config was not modified.")
    except (OSError, RuntimeError, ValueError) as exc:
        raise SystemExit(f"Could not save OAuth credentials: {_cli.redact_secret_text(str(exc))}") from exc
    _cli._setup_check(checks, "google_oauth_credentials_saved", "pass", f"saved private credentials file at {path}")
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
        base_url = _cli.setup_effective_base_url(args)
        model = _cli.validate_codex_model_id(args.model)
        provider_prefix, provider_model = _cli.split_provider_model(model)
        if provider_prefix is not None and not provider_model:
            raise ValueError(f"BYOK model must include a model id after '{provider_prefix}:'")
        google_route = provider_prefix is None
        _cli.render_codex_config_snippet(
            model=model,
            provider_id=args.provider,
            provider_name=args.provider_name,
            base_url=base_url,
        )
        definition = _cli.native_model_definition(model)
        model_detail = f"{model}"
        if definition:
            model_detail += f" ({definition.display_name})"
        _cli._setup_check(checks, "target_config", "pass", f"validated Codex provider config for {model_detail}")
    except (OSError, RuntimeError, ValueError) as exc:
        _cli._setup_check(checks, "target_config", "fail", _cli.redact_secret_text(str(exc)))
        report = {
            "ok": False,
            "mode": "check" if args.check or not args.write else "write",
            "checks": checks,
            "next_command": "codex-antigravity setup --check",
        }
        if args.json:
            print(json.dumps(report, indent=2))
        else:
            _cli._print_setup_report(report)
        raise SystemExit(1)

    if getattr(args, "repair", False):
        _cli.run_configure_codex(
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
        _cli._setup_check(checks, "codex_config_repair", "pass", f"repaired {Path(os.path.expanduser(args.config))}")
        readiness = _cli.codex_ready_report(
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
        _cli._print_setup_report(report)
        if not ok:
            raise SystemExit(f"Setup repair completed with readiness failures. Next command: {report['next_command']}")
        return report

    if google_route:
        cid, csec = _cli.resolve_oauth_credentials()
        if args.write and (not cid or not csec):
            prompted_cid, prompted_csec = _cli.maybe_prompt_and_save_oauth_credentials(args, checks)
            cid = cid or prompted_cid
            csec = csec or prompted_csec
        if cid and csec:
            _cli._setup_check(checks, "google_oauth_credentials", "pass", "configured")
        else:
            _cli._setup_check(
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
                _cli._print_setup_report(report)
                raise SystemExit("Google OAuth client credentials are not configured; Codex config was not modified.")
    else:
        _cli._setup_check(checks, "google_oauth_credentials", "skip", f"{model} routes to BYOK")
        byok_status, byok_detail, byok_provider = _cli.setup_byok_preflight(provider_prefix or "", provider_model)
        if byok_status == "fail" and provider_prefix == "xai-oauth":
            migration_command = (
                "codex-antigravity provider set xai --api-key-env XAI_API_KEY --model grok-build-0.1"
            )
            byok_status = "warn"
            byok_detail = (
                "xAI OAuth support has been removed. Migrate with: "
                f"{migration_command}"
            )
            print(f"[!] {byok_detail}")
        _cli._setup_check(checks, "byok_provider", byok_status, byok_detail, provider=provider_prefix, model=provider_model)
        if args.write and byok_status == "fail":
            report = {
                "ok": False,
                "mode": "write",
                "model": model,
                "base_url": base_url,
                "checks": checks,
                "next_command": _cli.byok_setup_next_command(provider_prefix or "provider", provider_model, byok_provider),
            }
            _cli._print_setup_report(report)
            raise SystemExit("BYOK provider is not ready; Codex config was not modified.")

    skill_dir = Path(os.path.expanduser(args.skill_dir))
    skill_path = skill_dir / _cli.BUNDLED_CODEX_SKILL_NAME
    try:
        _cli.bundled_skill_root()
        if skill_path.is_dir() and _cli.codex_skill_matches_bundled(skill_path):
            skill_status = "installed"
        elif skill_path.is_dir():
            skill_status = "present-but-different"
        else:
            skill_status = "missing"
        _cli._setup_check(checks, "anti_skill", "pass" if skill_status == "installed" else "warn", skill_status, path=str(skill_path))
    except RuntimeError as exc:
        _cli._setup_check(checks, "anti_skill", "fail", _cli.redact_secret_text(str(exc)))

    if args.check or not args.write:
        if args.install_skill:
            _cli._setup_check(checks, "anti_skill_install", "skip", "--install-skill is only applied when --write is used")
        if args.start:
            _cli._setup_check(checks, "gateway_start", "skip", "--start is only applied when --write is used")
        readiness = _cli.codex_ready_report(
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
            _cli._print_setup_report(report)
        return report

    if google_route:
        _cli.run_login(argparse.Namespace(count=args.accounts, select_account=True))
        _cli._setup_check(checks, "google_login", "pass", f"completed {args.accounts} OAuth login flow(s)")

    _cli.run_configure_codex(
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
    _cli._setup_check(checks, "codex_config_write", "pass", f"updated {Path(os.path.expanduser(args.config))}")

    if args.install_skill:
        _cli.run_install_skill(
            argparse.Namespace(
                skill_dir=args.skill_dir,
                force=args.force,
                dry_run=False,
                verify=args.verify_skill,
            )
        )
        _cli._setup_check(checks, "anti_skill_install", "pass", f"installed bundled Anti skill under {skill_dir}")
    else:
        _cli._setup_check(checks, "anti_skill_install", "skip", "pass --install-skill to install the optional $anti helper")

    gateway_ids: set[str] | None = None
    if args.start:
        try:
            _cli.start_gateway_background(
                argparse.Namespace(
                    host=args.host,
                    port=args.port,
                    allow_remote=args.allow_remote,
                    op_env_file=getattr(args, "op_env_file", None),
                    op_environment=getattr(args, "op_environment", None),
                )
            )
            gateway_ids = _cli.wait_for_gateway_model_ids(
                base_url,
                timeout=args.gateway_timeout,
                token_env=args.gateway_token_env,
            )
            _cli._setup_check(checks, "gateway_start", "pass", f"started background gateway on {args.host}:{args.port} and /v1/models is reachable")
            _cli._setup_check(
                checks,
                "gateway_service_followup",
                "warn",
                f"For reboot persistence, run: {_cli.setup_service_followup_command(args)}",
            )
        except RuntimeError as exc:
            _cli._setup_check(checks, "gateway_start", "fail", _cli.redact_secret_text(str(exc)))
            report = {
                "ok": False,
                "mode": "write",
                "model": model,
                "base_url": base_url,
                "checks": checks,
                "next_command": f"codex-antigravity status --port {args.port}",
            }
            _cli._print_setup_report(report)
            raise SystemExit(f"Gateway did not become ready. Next command: {report['next_command']}") from exc
        except SystemExit as exc:
            _cli._setup_check(checks, "gateway_start", "fail", _cli.redact_secret_text(str(exc)))
            report = {
                "ok": False,
                "mode": "write",
                "model": model,
                "base_url": base_url,
                "checks": checks,
                "next_command": f"codex-antigravity start --background --port {args.port}",
            }
            _cli._print_setup_report(report)
            raise
    else:
        _cli._setup_check(checks, "gateway_start", "skip", "pass --start to start the gateway in the background")

    try:
        if gateway_ids is None:
            gateway_ids = _cli.gateway_model_ids(base_url, timeout=args.gateway_timeout, token_env=args.gateway_token_env)
        catalog_status = "pass" if model in gateway_ids else "fail"
        detail = f"/v1/models advertises {model}" if catalog_status == "pass" else f"/v1/models does not advertise {model}"
        _cli._setup_check(checks, "gateway_models", catalog_status, detail, model_count=len(gateway_ids))
    except RuntimeError as exc:
        _cli._setup_check(checks, "gateway_models", "fail", _cli.redact_secret_text(str(exc)))

    readiness = _cli.codex_ready_report(
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
    _cli._print_setup_report(report)
    if not ok:
        raise SystemExit(f"Setup completed with readiness failures. Next command: {report['next_command']}")
    return report
