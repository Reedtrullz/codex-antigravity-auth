"""Version-check, probe, readiness, and doctor commands (split from cli.py)."""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from importlib import metadata as importlib_metadata
from pathlib import Path
from urllib.parse import urlparse

from . import cli as _cli


_orig_load_accounts = _cli.load_accounts


def _diagnostic_load_accounts() -> dict:
    if _cli.load_accounts is not _orig_load_accounts:
        return _cli.load_accounts()
    return _cli.load_accounts_read_only()


_orig_all_provider_configs = _cli.all_provider_configs


def _diagnostic_all_provider_configs() -> dict[str, dict]:
    if _cli.all_provider_configs is not _orig_all_provider_configs:
        return _cli.all_provider_configs()
    return _cli.all_provider_configs_read_only()


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
        result["error"] = _cli.redact_secret_text(f"HTTP {exc.code}: {detail}{hint}")[:500]
        return result
    except Exception as exc:
        result["latency_ms"] = int((time.monotonic() - started) * 1000)
        result["error"] = _cli.redact_secret_text(str(exc))[:500]
        return result
    result["latency_ms"] = int((time.monotonic() - started) * 1000)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        result["error"] = f"Gateway returned non-JSON data: {_cli.redact_secret_text(str(exc))}"
        return result
    preview = _cli.redact_secret_text(_cli._responses_output_preview(payload)).replace("\n", " ").strip()
    result["output_preview"] = preview[:80]
    result["ok"] = 200 <= int(result["http_status"] or 0) < 300
    if not result["ok"] and not result["error"]:
        result["error"] = _cli.redact_secret_text(str(payload))[:500]
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
_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
_FREE_OPENROUTER_VISION_MODELS = frozenset({
    "nvidia/nemotron-nano-12b-v2-vl:free",
    "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free",
    "google/gemma-4-31b-it:free",
    "google/gemma-4-26b-a4b-it:free",
})


def openrouter_reachability_check(*, timeout: float = 5.0) -> dict:
    """Probe OpenRouter API reachability for the vision sidecar."""
    started = time.time()
    result = {"ok": False, "latency_ms": 0, "error": None}
    req = urllib.request.Request(
        _OPENROUTER_BASE_URL + "/models",
        headers={"Accept": "application/json"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if 200 <= resp.status < 300:
                result["ok"] = True
            else:
                result["error"] = f"HTTP {resp.status}"
    except Exception as exc:
        result["error"] = _cli.redact_secret_text(str(exc))[:200]
    result["latency_ms"] = int((time.time() - started) * 1000)
    return result


def _opencodex_vision_sidecar_config() -> dict | None:
    """Read the opencodex vision sidecar config, returning None if unavailable."""
    config_path = Path(os.path.expanduser("~/.opencodex/config.json"))
    try:
        if not config_path.is_file():
            return None
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    sidecar = data.get("visionSidecar")
    if not isinstance(sidecar, dict) or not sidecar.get("enabled"):
        return None
    return sidecar


def vision_sidecar_readiness() -> dict:
    """Check opencodex vision sidecar readiness for OpenRouter backend."""
    result: dict = {"ok": False, "backend": None, "model": None, "checks": []}
    sidecar = _opencodex_vision_sidecar_config()
    if sidecar is None:
        result["checks"].append({"name": "sidecar_config", "status": "warn", "detail": "vision sidecar is not enabled in ~/.opencodex/config.json"})
        return result
    backend = sidecar.get("backend")
    model = sidecar.get("model")
    result["backend"] = backend
    result["model"] = model
    if backend != "openrouter":
        result["checks"].append({"name": "sidecar_backend", "status": "pass", "detail": f"vision sidecar backend is {backend} (not openrouter)"})
    else:
        result["checks"].append({"name": "sidecar_backend", "status": "pass", "detail": "vision sidecar backend is openrouter"})
        if model and model in _FREE_OPENROUTER_VISION_MODELS:
            result["checks"].append({"name": "sidecar_model", "status": "pass", "detail": f"vision sidecar model {model} is a known free vision-capable model"})
        elif model:
            result["checks"].append({"name": "sidecar_model", "status": "warn", "detail": f"vision sidecar model {model} is not in the known free vision model list"})
        else:
            result["checks"].append({"name": "sidecar_model", "status": "fail", "detail": "no vision sidecar model configured"})
    non_warn_checks = [c for c in result["checks"] if c["status"] != "warn"]
    # An all-warn (or empty) check list must not report ok: all() over an
    # empty sequence is vacuously True, which would mask a future warn-only
    # configuration as ready.
    result["ok"] = bool(non_warn_checks) and all(c["status"] == "pass" for c in non_warn_checks)
    return result


def _installed_package_version() -> str | None:
    source_version = _cli._source_checkout_version()
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
    return _cli._codex_home_read_only() / _cli.VERSION_CACHE_FILE


def _read_version_cache(now: float) -> dict | None:
    path = _cli._version_cache_path()
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
    if now - float(checked_at) > _cli.VERSION_CHECK_MAX_AGE_SECONDS:
        return None
    return data


def _write_version_cache(latest: str) -> None:
    payload = json.dumps({"checked_at": time.time(), "latest": latest}, indent=2, sort_keys=True) + "\n"
    _cli._write_private_text(_cli._version_cache_path(), payload)


def latest_pypi_version(timeout: float = 2.0) -> str | None:
    req = urllib.request.Request(_cli.PYPI_PROJECT_JSON_URL, headers={"Accept": "application/json"}, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    info = payload.get("info") if isinstance(payload, dict) else None
    latest = info.get("version") if isinstance(info, dict) else None
    return latest if isinstance(latest, str) and latest else None


def version_check_result(*, timeout: float = 2.0) -> dict:
    installed = _cli._installed_package_version()
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
    cache = _cli._read_version_cache(now)
    latest = cache.get("latest") if cache else None
    if latest is None:
        try:
            latest = _cli.latest_pypi_version(timeout=timeout)
            if latest:
                _cli._write_version_cache(latest)
        except Exception:
            result["detail"] = "version check unavailable"
            return result
    result["latest"] = latest
    if not installed or not latest:
        result["detail"] = "version check unavailable"
        return result
    installed_tuple = _cli._version_tuple(installed)
    latest_tuple = _cli._version_tuple(latest)
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
        canonical = _cli.validate_codex_model_id(model)
    except ValueError as exc:
        return None, f"live model is invalid: {exc}"
    provider_prefix, _provider_model = _cli.split_provider_model(canonical)
    if provider_prefix is not None:
        return None, "live generation smoke currently supports Google Antigravity models only"
    return canonical, None


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
        cooldown_end = _cli.scoped_cooldown_expiry(cooldowns.get(email, 0), family)
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
        return config_path, None, f"Could not read Codex config: {_cli.redact_secret_text(str(exc))}"


def readiness_storage_diagnostics() -> dict[str, dict]:
    return {
        "account_store": _cli.account_store_diagnostics(),
        "provider_store": _cli.provider_store_diagnostics(_cli.providers_json_path_read_only()),
    }


def provider_capability_mismatches(providers: dict[str, dict]) -> list[dict[str, str]]:
    mismatches: list[dict[str, str]] = []
    for provider_id, provider in sorted(providers.items()):
        kind = provider.get("kind")
        auth_mode = _cli.provider_auth_mode(provider)
        try:
            _cli.provider_capabilities(provider)
        except ValueError as exc:
            mismatches.append({"provider": provider_id, "reason": str(exc)})
            continue
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

    config_path, config_content, config_error = _cli._read_codex_config_for_readiness(config)
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
        inspector = _cli.inspect_codex_gateway_config if require_active_provider else _cli.inspect_codex_provider_block_config
        ready, reason = inspector(config_content or "", provider_id=provider_id, expected_base_url=expected_base_url)
        add("codex_config", "pass" if ready else "fail", reason, path=str(config_path))
        parsed = _cli.parse_codex_config(config_content or "")
        active_model = str(selected_model or parsed.get("active_model") or "")
        try:
            canonical_model = _cli.validate_codex_model_id(active_model)
            add("selected_model", "pass", f"Codex model resolves to {canonical_model}", model=canonical_model)
        except ValueError as exc:
            add("selected_model", "fail", str(exc), model=active_model)

    try:
        status_info = _cli.gateway_status_info(gateway_port)
        _cli.add_gateway_reachability(
            status_info,
            host=parsed_gateway.hostname or "127.0.0.1",
            timeout=max(float(gateway_timeout), 0.1),
        )
        service_info = _cli.service_status(gateway_port)
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
        add("gateway_process", "warn", f"Could not inspect local gateway/service state: {_cli.redact_secret_text(str(exc))}")

    try:
        gateway_ids = _cli.gateway_model_ids(expected_base_url, timeout=gateway_timeout, token_env=gateway_token_env)
        add("gateway_models", "pass", f"Gateway advertised {len(gateway_ids)} model(s)")
    except RuntimeError as exc:
        add("gateway_models", "fail", _cli.redact_secret_text(str(exc)))

    selected_for_catalog = canonical_model or active_model
    if selected_for_catalog and gateway_ids is not None:
        if selected_for_catalog in gateway_ids:
            add("model_catalog", "pass", f"{selected_for_catalog} is advertised by /v1/models")
        else:
            add("model_catalog", "fail", f"{selected_for_catalog} is not advertised by /v1/models")

    provider_prefix, provider_model = _cli.split_provider_model(selected_for_catalog) if selected_for_catalog else (None, "")
    if selected_for_catalog and provider_prefix is not None:
        route = "byok"
        try:
            providers = _cli._diagnostic_all_provider_configs()
            capability_mismatches = _cli.provider_capability_mismatches(providers)
        except Exception as exc:
            add("model_route", "fail", f"Could not load BYOK provider configuration: {_cli.redact_secret_text(str(exc))}")
        else:
            provider = providers.get(provider_prefix)
            if not provider:
                if provider_prefix == "xai-oauth":
                    oauth_status = _cli.xai_oauth_status()
                    add(
                        "model_route",
                        "fail",
                        "xAI OAuth provider is not logged in" if not oauth_status.get("ready") else "xAI OAuth provider is not visible",
                        auth=oauth_status,
                    )
                else:
                    add("model_route", "fail", f"BYOK provider '{provider_prefix}' is not configured")
            elif _cli.provider_key_status(provider, configured_label="key OK") != "key OK":
                credential_name = "OAuth login" if _cli.provider_auth_mode(provider) == "oauth" else "key"
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
        definition = _cli.native_model_definition(selected_for_catalog)
        if definition:
            add("model_route", "pass", f"{selected_for_catalog} routes to Google Antigravity backend {definition.backend_id}")
        else:
            add("model_route", "warn", f"{selected_for_catalog} is not a known built-in Google Antigravity model")
        family = _cli.native_model_family(selected_for_catalog)
        try:
            rotation = _cli.google_family_rotation_status(_cli._diagnostic_load_accounts(), family)
        except Exception as exc:
            add("google_rotation", "fail", f"Could not load Google account rotation state: {_cli.redact_secret_text(str(exc))}", family=family)
        else:
            if rotation["available_count"] > 0:
                add("google_rotation", "pass", f"{rotation['available_count']} {family} account(s) available", **rotation)
            elif rotation["account_count"] > 0:
                add("google_rotation", "fail", f"All {family} accounts are cooling down", **rotation)
            else:
                add("google_rotation", "fail", f"No Google accounts configured for {family}", **rotation)

    if live:
        probe_model = live_model or selected_for_catalog or _cli.DEFAULT_CODEX_MODEL_ID
        probe_model, live_model_error = _cli._validate_google_live_model(probe_model)
        if live_model_error:
            add("live_generation", "fail", live_model_error, probe={"ok": False, "model": live_model or selected_for_catalog or _cli.DEFAULT_CODEX_MODEL_ID})
        else:
            probe = _cli.gateway_generate_probe(
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
        version = _cli.version_check_result()
        add(
            "version_check",
            version["status"],
            version["detail"],
            installed=version.get("installed"),
            latest=version.get("latest"),
        )

    storage_diagnostics = _cli.readiness_storage_diagnostics()
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
            capability_mismatches = _cli.provider_capability_mismatches(_cli._diagnostic_all_provider_configs())
        except Exception as exc:
            capability_mismatches = [{"provider": "unknown", "reason": _cli.redact_secret_text(str(exc))}]
    add(
        "provider_capabilities",
        "warn" if capability_mismatches else "pass",
        f"{len(capability_mismatches)} provider capability mismatch(es)",
        mismatches=capability_mismatches,
    )
    # Vision sidecar readiness
    try:
        sidecar = _cli.vision_sidecar_readiness()
    except Exception as exc:
        add("vision_sidecar", "warn", f"Could not inspect vision sidecar: {_cli.redact_secret_text(str(exc))}")
    else:
        for check in sidecar.get("checks", []):
            add(f"vision_sidecar_{check['name']}", check["status"], check["detail"])
        if sidecar.get("ok") and sidecar.get("backend") == "openrouter":
            or_reachability = _cli.openrouter_reachability_check()
            # Third-party reachability is advisory for the gateway's own
            # readiness: a transient openrouter.ai blip (or offline dev
            # machine) must not flip the whole codex-ready gate to failed.
            status = "pass" if or_reachability["ok"] else "warn"
            add(
                "openrouter_reachability",
                status,
                f"OpenRouter API {'reachable' if or_reachability['ok'] else 'unreachable'} in {or_reachability['latency_ms']}ms"
            )
            # Check OpenRouter provider has API key configured
            try:
                providers = _cli._diagnostic_all_provider_configs()
                or_provider = providers.get("openrouter")
                if or_provider and _cli.resolve_api_key(or_provider):
                    add("openrouter_provider", "pass", "OpenRouter provider has a usable API key")
                else:
                    add("openrouter_provider", "warn", "OpenRouter provider is not configured with an API key")
            except Exception:
                add("openrouter_provider", "warn", "Could not verify OpenRouter provider configuration")


    failed = [check for check in checks if check["status"] == "fail"]
    ok = not failed
    next_command = "codex"
    if failed:
        first = failed[0]["name"]
        if first == "codex_config" and config_path.exists():
            next_command = "codex-antigravity setup --repair"
        elif first in {"codex_config", "selected_model"}:
            next_command = f"codex-antigravity setup --write --accounts 1 --model {_cli.DEFAULT_CODEX_MODEL_ID}"
        elif first == "gateway_models":
            next_command = f"codex-antigravity start --background --port {gateway_port}"
        elif first == "model_catalog":
            if provider_prefix == "xai-oauth" and not _cli.xai_oauth_status().get("ready"):
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
        "request_log": _cli.request_log_info(),
        "diagnostics": {
            **storage_diagnostics,
            "service": service_snapshot,
            "provider_capability_mismatches": capability_mismatches,
        },
        "next_command": next_command,
    }


def run_codex_ready_doctor(args) -> bool:
    report = _cli.codex_ready_report(
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


def run_doctor(
    *,
    byok_only: bool = False,
    expected_base_url: str = _cli.DEFAULT_CODEX_BASE_URL,
    config: str = "~/.codex/config.toml",
    provider_id: str = _cli.DEFAULT_CODEX_PROVIDER_ID,
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
            parsed_codex_config = _cli.parse_codex_config(codex_config_content)
            codex_config_model = str(parsed_codex_config.get("active_model") or "")
        except Exception:
            codex_config_content = None

    # Check Client Credentials
    if byok_only:
        print("[INFO] Google OAuth Client Credentials: skipped (--byok-only)")
    else:
        cid, csec = _cli.resolve_oauth_credentials()
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
        print(f"[WARN] Token Storage Encryption: PARTIAL (Fallback active. Error: {_cli.redact_secret_text(str(e))})")

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
            print(f"[FAIL] Google Antigravity Connectivity: OFFLINE / TIMEOUT ({_cli.redact_secret_text(str(e))})")

    # Check accounts
    if byok_only:
        print("[INFO] Authenticated Accounts: skipped (--byok-only)")
    else:
        try:
            data = _cli._diagnostic_load_accounts()
        except Exception as e:
            healthy = False
            print(f"[FAIL] Authenticated Accounts: could not load account store ({_cli.redact_secret_text(str(e))})")
        else:
            accounts = data.get("accounts", [])
            if accounts:
                print(f"[PASS] Authenticated Accounts: {len(accounts)} configured")
                for acc in accounts:
                    email = acc.get("email")
                    expires_at = _cli.normalize_epoch_seconds(acc.get("expiresAt", 0))
                    status = "ACTIVE" if expires_at > time.time() else "EXPIRED (will auto-refresh)"
                    print(f"       - {email} ({status})")
                print("       Rotation:")
                for line in _cli.account_rotation_lines(data)[1:]:
                    print(f"       {line.strip()}")
            else:
                healthy = False
                print("[WARN] Authenticated Accounts: 0 accounts found.")
                print("       Run `codex-antigravity login` to add an account.")

    # Check BYOK providers
    try:
        providers = _cli._diagnostic_all_provider_configs()
        if providers:
            provider_statuses = {
                byok_provider_id: _cli.provider_key_status(provider, configured_label="key OK")
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
            selected_provider_id, selected_provider_model = _cli.split_provider_model(codex_config_model) if codex_config_model else (None, "")
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
            print(f"[FAIL] BYOK Providers: could not load provider config ({_cli.redact_secret_text(str(e))})")
        else:
            print(f"[WARN] BYOK Providers: could not load provider config ({_cli.redact_secret_text(str(e))})")

    # Check Codex config
    if codex_config.is_file():
        print(f"[PASS] Codex config.toml: Found ({codex_config})")
        try:
            if codex_config_content is None:
                codex_config_content = codex_config.read_text(encoding="utf-8")
            points_to_gateway, reason = _cli.inspect_codex_gateway_config(
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
            print(f"       - [FAIL] could not inspect config.toml ({_cli.redact_secret_text(str(e))})")
    else:
        healthy = False
        print(f"[FAIL] Codex config.toml: Not found ({codex_config}).")
        print("       Run `codex-antigravity configure-codex --write` to install the gateway provider block.")

    if live:
        probe_model = live_model or codex_config_model or _cli.DEFAULT_CODEX_MODEL_ID
        probe_model, live_model_error = _cli._validate_google_live_model(probe_model)
        if live_model_error:
            healthy = False
            print(f"[FAIL] Live Generation Smoke: {live_model_error}")
        else:
            probe = _cli.gateway_generate_probe(
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

    version = _cli.version_check_result()
    if version["status"] == "warn":
        print(f"[WARN] Package Version: {version['detail']}")
    elif version["status"] == "pass":
        print(f"[PASS] Package Version: {version['detail']}")
    else:
        print(f"[INFO] Package Version: {version['detail']}")

    print("=" * 60)
    return healthy
