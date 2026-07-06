import json
import math
import os
import secrets
import time
import httpx
import email.utils
import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from urllib.parse import urlparse
from typing import AsyncGenerator
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool
from .accounts import AccountManager
from .byok import (
    all_provider_configs,
    resolve_api_key,
    split_provider_model,
    validate_http_base_url,
    validate_provider_api_key,
    validate_provider_headers,
    validate_provider_id,
)
from .transform import (
    function_call_arguments_json,
    function_call_arguments_string,
    safe_project_id,
    transform_chat_response,
    transform_request,
    transform_request_to_chat,
    transform_response,
    usage_counts,
    valid_function_name,
)
from .constants import ANTIGRAVITY_ENDPOINT_PROD, get_platform, is_loopback_host, validate_gateway_token_strength
from .models import canonical_model_id, native_model_catalog, native_model_family
from .observability import request_log_info, write_request_record
from .redaction import redact_secret_text
from .storage import load_accounts


@asynccontextmanager
async def gateway_lifespan(_app: FastAPI):
    await refresh_accounts_ahead_if_due(force=True)
    yield


app = FastAPI(title="Codex Antigravity Gateway", lifespan=gateway_lifespan)
account_manager = AccountManager()
_last_refresh_ahead_at = 0.0
STREAM_ERROR_CODE_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
MUTATING_JSON_PATHS = {"/v1/responses"}
TEST_CLIENT_HOSTS = {"testserver"}
GOOGLE_ACCOUNT_SCOPED_STREAM_ERROR_TERMS = (
    "401",
    "403",
    "429",
    "auth",
    "permission_denied",
    "quota",
    "rate limit",
    "rate_limit",
    "resource_exhausted",
    "unauthenticated",
)


def request_origin_matches(request: Request, origin: str) -> bool:
    try:
        parsed_origin = urlparse(origin)
        origin_port = parsed_origin.port
    except ValueError:
        return False
    if parsed_origin.scheme not in {"http", "https"} or not parsed_origin.hostname:
        return False

    request_url = request.url
    request_port = request_url.port
    if origin_port is None:
        origin_port = 443 if parsed_origin.scheme == "https" else 80
    if request_port is None:
        request_port = 443 if request_url.scheme == "https" else 80
    return (
        parsed_origin.scheme == request_url.scheme
        and parsed_origin.hostname.lower() == (request_url.hostname or "").lower()
        and origin_port == request_port
    )


def request_uses_loopback_host(request: Request, client_host: str | None = None) -> bool:
    hostname = request.url.hostname
    if is_loopback_host(hostname):
        return True
    return (hostname or "").lower() in TEST_CLIENT_HOSTS and client_host == "testclient"


def mutating_json_request_guard(request: Request) -> JSONResponse | None:
    if request.method.upper() not in {"POST", "PUT", "PATCH"}:
        return None
    if request.url.path not in MUTATING_JSON_PATHS:
        return None

    content_type = request.headers.get("content-type", "")
    media_type = content_type.split(";", 1)[0].strip().lower()
    if media_type != "application/json":
        return JSONResponse(
            status_code=415,
            content={"detail": "Mutating gateway requests must use Content-Type: application/json."},
        )

    client_host = request.client.host if request.client else None
    if is_loopback_host(client_host) and not request_uses_loopback_host(request, client_host):
        return JSONResponse(status_code=403, content={"detail": "Loopback gateway requests must use a loopback Host."})

    if request.headers.get("sec-fetch-site", "").lower() == "cross-site":
        return JSONResponse(status_code=403, content={"detail": "Cross-site browser requests are not allowed."})

    origin = request.headers.get("origin")
    if origin and not request_origin_matches(request, origin):
        return JSONResponse(status_code=403, content={"detail": "Cross-origin browser requests are not allowed."})

    return None


@app.middleware("http")
async def require_remote_gateway_token(request: Request, call_next):
    client_host = request.client.host if request.client else None
    if is_loopback_host(client_host):
        guard_response = mutating_json_request_guard(request)
        if guard_response is not None:
            return guard_response
        return await call_next(request)

    allow_remote = os.environ.get("ANTIGRAVITY_ALLOW_REMOTE") == "1"
    try:
        token = validate_gateway_token_strength(os.environ.get("ANTIGRAVITY_GATEWAY_TOKEN")) if allow_remote else ""
    except ValueError as e:
        return JSONResponse(status_code=403, content={"detail": str(e)})
    expected_auth = f"Bearer {token}" if token else ""
    supplied_auth = request.headers.get("authorization", "")
    if allow_remote and token and secrets.compare_digest(supplied_auth, expected_auth):
        guard_response = mutating_json_request_guard(request)
        if guard_response is not None:
            return guard_response
        return await call_next(request)

    return JSONResponse(
        status_code=403,
        content={"detail": "Remote access requires ANTIGRAVITY_ALLOW_REMOTE=1 and a valid bearer token."},
    )

def safe_error_detail(value: object) -> str:
    return redact_secret_text(str(value))


async def select_active_account_for_request(model: str) -> dict | None:
    return await run_in_threadpool(account_manager.select_active_account, model)


async def mark_account_failure(
    email: str,
    reason: str,
    retry_after_seconds: float | None = None,
    *,
    model: str | None = None,
    status_code: int | None = None,
) -> None:
    await run_in_threadpool(
        account_manager.mark_failure,
        email,
        reason,
        retry_after_seconds,
        model=model,
        status_code=status_code,
    )


async def record_account_request(
    email: str,
    model: str,
    *,
    status: str,
    status_code: int | None = None,
    error_class: str | None = None,
    usage: dict | None = None,
) -> None:
    await run_in_threadpool(
        account_manager.record_request,
        email,
        model,
        status=status,
        status_code=status_code,
        error_class=error_class,
        usage=usage,
    )


async def refresh_accounts_ahead_if_due(*, force: bool = False) -> None:
    global _last_refresh_ahead_at
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return
    now = time.monotonic()
    if not force and now - _last_refresh_ahead_at < 60:
        return
    _last_refresh_ahead_at = now
    try:
        await run_in_threadpool(account_manager.refresh_expiring_accounts, 300)
    except Exception:
        return


def account_health_summary() -> dict:
    try:
        data = load_accounts()
    except Exception:
        return {"configured_accounts": 0, "cooldowns": {}, "counters": {}, "load_error": "account store unavailable"}
    accounts = data.get("accounts", []) if isinstance(data, dict) else []
    state = data.get("accountState", {}) if isinstance(data.get("accountState"), dict) else {}
    cooldowns = state.get("cooldowns", {}) if isinstance(state.get("cooldowns"), dict) else {}
    counters = state.get("counters", {}) if isinstance(state.get("counters"), dict) else {}
    now = time.time()
    cooldown_summary: dict[str, dict[str, int]] = {
        "claude": {"cooling_down": 0, "available": 0},
        "gemini": {"cooling_down": 0, "available": 0},
    }
    counter_summary: dict[str, dict[str, int]] = {
        "claude": {"total_requests": 0, "failures": 0, "rate_limits": 0},
        "gemini": {"total_requests": 0, "failures": 0, "rate_limits": 0},
    }
    for account in accounts:
        if not isinstance(account, dict):
            continue
        email = str(account.get("email") or "")
        cooldown_end = normalize_epoch_seconds(cooldowns.get(email, 0))
        for family in ("claude", "gemini"):
            if cooldown_end > now:
                cooldown_summary[family]["cooling_down"] += 1
            else:
                cooldown_summary[family]["available"] += 1
        family_counters = counters.get(email, {}) if isinstance(counters, dict) else {}
        if not isinstance(family_counters, dict):
            continue
        for family, raw_counter in family_counters.items():
            if family not in counter_summary or not isinstance(raw_counter, dict):
                continue
            for key in ("total_requests", "failures", "rate_limits"):
                value = raw_counter.get(key, 0)
                try:
                    parsed = int(value)
                except (TypeError, ValueError):
                    parsed = 0
                counter_summary[family][key] += max(0, parsed)
    return {
        "configured_accounts": len(accounts),
        "cooldowns": cooldown_summary,
        "counters": counter_summary,
    }


def finite_retry_after_seconds(value: object) -> float | None:
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(seconds):
        return None
    return max(0.0, seconds)


def normalize_epoch_seconds(value: object) -> float:
    try:
        seconds = float(value or 0)
    except (TypeError, ValueError):
        return 0
    if not math.isfinite(seconds):
        return 0
    if seconds > 10_000_000_000:
        seconds = seconds / 1000
    return seconds


def retry_after_seconds_from_response(res: httpx.Response) -> float | None:
    retry_after = res.headers.get("retry-after")
    if retry_after:
        parsed_seconds = finite_retry_after_seconds(retry_after)
        if parsed_seconds is not None:
            return parsed_seconds
        else:
            try:
                retry_at = email.utils.parsedate_to_datetime(retry_after)
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=timezone.utc)
                return max(0.0, (retry_at - datetime.now(timezone.utc)).total_seconds())
            except Exception:
                pass

    try:
        payload = res.json()
    except Exception:
        return None

    details = []
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict) and isinstance(error.get("details"), list):
            details.extend(error["details"])
        if isinstance(payload.get("details"), list):
            details.extend(payload["details"])

    for detail in details:
        if not isinstance(detail, dict):
            continue
        retry_delay = detail.get("retryDelay")
        if isinstance(retry_delay, str):
            match = re.fullmatch(r"(\d+(?:\.\d+)?)s", retry_delay)
            if match:
                return float(match.group(1))
        if isinstance(retry_delay, dict):
            seconds = retry_delay.get("seconds", 0)
            nanos = retry_delay.get("nanos", 0)
            try:
                parsed_seconds = finite_retry_after_seconds(float(seconds) + (float(nanos) / 1_000_000_000))
            except (TypeError, ValueError):
                continue
            if parsed_seconds is not None:
                return parsed_seconds
    return None


def retry_after_source_from_response(res: httpx.Response) -> str | None:
    if res.headers.get("retry-after"):
        return "retry-after-header"
    try:
        payload = res.json()
    except Exception:
        return None
    details = []
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict) and isinstance(error.get("details"), list):
            details.extend(error["details"])
        if isinstance(payload.get("details"), list):
            details.extend(payload["details"])
    for detail in details:
        if isinstance(detail, dict) and "retryDelay" in detail:
            return "payload-retry-delay"
    return None


def google_rotation_diagnostics(
    model: str,
    *,
    retry_after_seconds: float | None = None,
    retry_after_source: str | None = None,
    rotation_attempted: bool = False,
) -> dict:
    family = native_model_family(model)
    try:
        data = load_accounts()
    except Exception:
        data = {}
    accounts = data.get("accounts", []) if isinstance(data, dict) else []
    state = data.get("accountState", {}) if isinstance(data.get("accountState"), dict) else {}
    cooldowns = state.get("cooldowns", {}) if isinstance(state.get("cooldowns"), dict) else {}
    now = time.time()
    cooldown_count = 0
    for account in accounts:
        if not isinstance(account, dict):
            continue
        email = account.get("email")
        cooldown_end = normalize_epoch_seconds(cooldowns.get(email, 0))
        if cooldown_end > now:
            cooldown_count += 1
    return {
        "selected_account_family": family,
        "account_count": len(accounts),
        "cooldown_count": cooldown_count,
        "retry_after_seconds": retry_after_seconds,
        "retry_after_source": retry_after_source,
        "rotation_attempted": rotation_attempted,
        "all_accounts_cooling_down": bool(accounts) and cooldown_count >= len(accounts),
        "all_claude_accounts_cooling_down": bool(accounts) and family == "claude" and cooldown_count >= len(accounts),
    }


def google_failure_detail(
    model: str,
    message: str,
    *,
    retry_after_seconds: float | None = None,
    retry_after_source: str | None = None,
    rotation_attempted: bool = False,
) -> dict:
    return {
        "message": safe_error_detail(message),
        "diagnostics": google_rotation_diagnostics(
            model,
            retry_after_seconds=retry_after_seconds,
            retry_after_source=retry_after_source,
            rotation_attempted=rotation_attempted,
        ),
    }


def provider_has_usable_key(provider: dict) -> bool:
    try:
        return bool(validate_provider_api_key(resolve_api_key(provider)))
    except ValueError:
        return False


def codex_model_metadata(
    model_id: str,
    display_name: str,
    context_window: int,
    owned_by: str,
    created: int,
    *,
    default_reasoning_level: str = "high",
    supports_parallel_tool_calls: bool = True,
) -> dict:
    reasoning_levels = [
        {"effort": "low", "description": "Fast responses with lighter reasoning"},
        {"effort": "medium", "description": "Balances speed and reasoning depth"},
        {"effort": "high", "description": "Greater reasoning depth for complex problems"},
        {"effort": "xhigh", "description": "Extra high reasoning depth for complex problems"},
    ]
    return {
        "id": model_id,
        "slug": model_id,
        "object": "model",
        "created": created,
        "owned_by": owned_by,
        "display_name": display_name,
        "description": f"{display_name} via the local Codex Antigravity gateway.",
        "supports_parallel_tool_calls": supports_parallel_tool_calls,
        "context_window": context_window,
        "max_context_window": context_window,
        "auto_compact_token_limit": None,
        "reasoning_summary_format": "experimental",
        "default_reasoning_summary": "none",
        "supports_reasoning_summaries": False,
        "supported_reasoning_levels": reasoning_levels,
        "default_reasoning_level": default_reasoning_level,
        "support_verbosity": False,
        "default_verbosity": "medium",
        "truncation_policy": {"mode": "tokens", "limit": 10000},
        "experimental_supported_tools": [],
        "shell_type": "shell_command",
        "visibility": "list",
        "minimal_client_version": "0.124.0",
        "supported_in_api": True,
        "availability_nux": None,
        "upgrade": None,
        "priority": 0,
        "base_instructions": "Follow the instructions supplied by the Codex client for each request.",
        "instructions_variables": {},
    }


@app.get("/v1/models")
async def list_models():
    """Return model catalog so Codex Desktop can populate its picker dropdown."""
    import time
    byok_models = []
    for provider_id, provider in all_provider_configs().items():
        if not provider_has_usable_key(provider):
            continue
        for model_entry in provider.get("models", []):
            if isinstance(model_entry, dict):
                provider_model = model_entry.get("id")
                display_name = model_entry.get("display_name") or model_entry.get("displayName") or provider_model
                context_window = model_entry.get("context_window") or model_entry.get("contextWindow") or 128000
            else:
                provider_model = str(model_entry)
                display_name = provider_model
                context_window = 128000
            if not provider_model:
                continue
            model_id = f"{provider_id}:{provider_model}"
            byok_models.append({
                **codex_model_metadata(
                    model_id,
                    f"{provider.get('displayName', provider_id)}: {display_name}",
                    context_window,
                    provider_id,
                    int(time.time()),
                )
            })
    models = [
        codex_model_metadata(
            m["id"],
            m["display_name"],
            m["context_window"],
            "google-antigravity",
            int(time.time()),
            default_reasoning_level=m.get("default_reasoning_level", "high"),
            supports_parallel_tool_calls=bool(m.get("supports_parallel_tool_calls", True)),
        )
        for m in native_model_catalog()
    ] + byok_models
    return {
        "object": "list",
        "data": models,
        "models": models,
    }


@app.get("/health")
async def health(request: Request):
    client_host = request.client.host if request.client else None
    if not request_uses_loopback_host(request, client_host):
        raise HTTPException(status_code=403, detail="Health checks are loopback-only.")
    providers = []
    try:
        provider_configs = all_provider_configs()
    except Exception:
        provider_configs = {}
    for provider_id, provider in provider_configs.items():
        models = provider.get("models", [])
        providers.append(
            {
                "id": provider_id,
                "kind": provider.get("kind"),
                "usable": provider_has_usable_key(provider),
                "model_count": len(models) if isinstance(models, list) else 0,
            }
        )
    catalog = native_model_catalog()
    return {
        "ok": True,
        "model_count": len(catalog),
        "advertised_native_models": [model["id"] for model in catalog],
        "configured_route_families": {
            "google": bool(catalog),
            "byok": providers,
        },
        "accounts": account_health_summary(),
        "request_log": request_log_info(),
    }

def safe_header_string(value: object) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    if any(ord(ch) < 0x20 or ord(ch) > 0x7E for ch in value):
        return None
    return value


def safe_client_metadata(value: object) -> dict:
    if not isinstance(value, dict):
        return {}

    metadata = {}
    for key, raw_value in value.items():
        safe_key = safe_header_string(key)
        if safe_key is None:
            continue
        if isinstance(raw_value, str):
            safe_value = safe_header_string(raw_value)
            if safe_value is not None:
                metadata[safe_key] = safe_value
        elif raw_value is None or isinstance(raw_value, bool):
            metadata[safe_key] = raw_value
        elif isinstance(raw_value, (int, float)) and math.isfinite(raw_value):
            metadata[safe_key] = raw_value
    return metadata


def build_headers(account: dict) -> dict:
    platform = get_platform()
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/537.36 (KHTML, like Gecko) Antigravity/2.0.0 Chrome/138.0.7204.235 Electron/37.3.1 Safari/537.36",
        "X-Goog-Api-Client": "google-cloud-sdk vscode_cloudshelleditor/0.1",
        "Client-Metadata": f'{{"ideType":"ANTIGRAVITY","platform":"{platform}","pluginType":"GEMINI"}}',
        "Content-Type": "application/json",
        "Authorization": f"Bearer {account['accessToken']}",
    }
    
    # Fingerprints are locally persisted, so tolerate stale or malformed data.
    fp = account.get("fingerprint")
    if isinstance(fp, dict):
        user_agent = safe_header_string(fp.get("userAgent"))
        api_client = safe_header_string(fp.get("apiClient"))
        if user_agent is not None:
            headers["User-Agent"] = user_agent
        if api_client is not None:
            headers["X-Goog-Api-Client"] = api_client
        if fp.get("clientMetadata"):
            metadata = safe_client_metadata(fp.get("clientMetadata"))
            device_id = safe_header_string(fp.get("deviceId"))
            session_token = safe_header_string(fp.get("sessionToken"))
            if device_id is not None:
                metadata["deviceId"] = device_id
            if session_token is not None:
                metadata["sessionToken"] = session_token
            if metadata:
                headers["Client-Metadata"] = json.dumps(metadata)
            
    return headers


def build_openai_compatible_headers(provider: dict) -> dict:
    api_key = resolve_api_key(provider)
    try:
        api_key = validate_provider_api_key(api_key)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"Provider '{provider['id']}' {str(e)}") from e
    if not api_key:
        raise HTTPException(
            status_code=401,
            detail=f"No API key configured for provider '{provider['id']}'. Set {provider.get('apiKeyEnv', 'provider API key')} or run provider set.",
        )
    provider_headers = provider.get("headers", {}) or {}
    try:
        provider_headers = validate_provider_headers(provider_headers)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    headers.update(provider_headers or {})
    return headers


def chat_completions_url(provider: dict) -> str:
    base_url = provider.get("baseUrl", "")
    if not isinstance(base_url, str):
        raise HTTPException(status_code=400, detail=f"Provider '{provider['id']}' baseUrl must be a string")
    if not base_url.strip():
        raise HTTPException(status_code=500, detail=f"Provider '{provider['id']}' has no baseUrl configured")
    try:
        base_url = validate_http_base_url(base_url, label=f"Provider '{provider['id']}' baseUrl")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    if base_url.endswith("/chat/completions"):
        url = base_url
    else:
        url = f"{base_url}/chat/completions"
    return url


def openai_compatible_timeout(provider: dict) -> float:
    timeout = provider.get("timeout", 120.0)
    if (
        not isinstance(timeout, (int, float))
        or isinstance(timeout, bool)
        or not math.isfinite(float(timeout))
        or float(timeout) <= 0
    ):
        raise HTTPException(status_code=400, detail=f"Provider '{provider['id']}' timeout must be a positive number")
    return float(timeout)


def reject_unsupported_previous_response(codex_req: dict) -> None:
    if codex_req.get("previous_response_id"):
        raise HTTPException(
            status_code=400,
            detail="previous_response_id is not supported by this stateless gateway; resend the full conversation in input.",
        )


def validate_response_request_body(value: object) -> dict:
    if not isinstance(value, dict):
        raise HTTPException(status_code=400, detail="Request JSON body must be an object")
    instructions = value.get("instructions")
    if instructions is not None and not isinstance(instructions, str):
        raise HTTPException(status_code=400, detail="instructions must be a string")
    reasoning = value.get("reasoning")
    if reasoning is not None and not isinstance(reasoning, dict):
        raise HTTPException(status_code=400, detail="reasoning must be an object")
    validate_response_generation_options(value)
    validate_response_tool_choice(value)
    return value


def validate_finite_number_option(value: object, field_name: str, *, minimum: float, maximum: float | None = None) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise HTTPException(status_code=400, detail=f"{field_name} must be a finite number")
    number = float(value)
    if number < minimum or (maximum is not None and number > maximum):
        if maximum is None:
            raise HTTPException(status_code=400, detail=f"{field_name} must be greater than or equal to {minimum:g}")
        raise HTTPException(status_code=400, detail=f"{field_name} must be between {minimum:g} and {maximum:g}")


def validate_response_generation_options(codex_req: dict) -> None:
    if "temperature" in codex_req:
        validate_finite_number_option(codex_req["temperature"], "temperature", minimum=0.0, maximum=2.0)
    if "top_p" in codex_req:
        validate_finite_number_option(codex_req["top_p"], "top_p", minimum=0.0, maximum=1.0)
    if "max_output_tokens" in codex_req:
        value = codex_req["max_output_tokens"]
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise HTTPException(status_code=400, detail="max_output_tokens must be a positive integer")
    if "stop" in codex_req:
        stop = codex_req["stop"]
        values = [stop] if isinstance(stop, str) else stop
        if not isinstance(values, list) or not values:
            raise HTTPException(status_code=400, detail="stop must be a string or a non-empty list of strings")
        for item in values:
            if not isinstance(item, str) or not item or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in item):
                raise HTTPException(status_code=400, detail="stop values must be non-empty strings without control characters")


def validate_response_tool_choice(codex_req: dict) -> None:
    if "tool_choice" not in codex_req:
        return
    tool_choice = codex_req.get("tool_choice")
    if isinstance(tool_choice, str):
        if tool_choice not in {"auto", "none", "required"}:
            raise HTTPException(status_code=400, detail="tool_choice must be auto, none, required, or a function choice object")
        return
    if not isinstance(tool_choice, dict) or tool_choice.get("type") != "function":
        raise HTTPException(status_code=400, detail="tool_choice must be auto, none, required, or a function choice object")
    nested = tool_choice.get("function")
    name = tool_choice.get("name") or (nested.get("name") if isinstance(nested, dict) else None)
    if not valid_function_name(name):
        raise HTTPException(
            status_code=400,
            detail="tool_choice function name must contain only letters, numbers, underscores, and hyphens, and be 1-64 characters",
        )


def response_stream_flag(codex_req: dict) -> bool:
    if "stream" not in codex_req:
        return False
    stream = codex_req.get("stream")
    if not isinstance(stream, bool):
        raise HTTPException(status_code=400, detail="stream must be a boolean")
    return stream


def response_model_id(codex_req: dict) -> str:
    raw_model = codex_req.get("model", "gemini-3.5-flash-high")
    if not isinstance(raw_model, str):
        raise HTTPException(status_code=400, detail="model must be a string")
    model = raw_model.strip()
    if not model:
        raise HTTPException(status_code=400, detail="model must be non-empty")
    if any(ch.isspace() or ord(ch) < 0x20 or ord(ch) == 0x7F for ch in model):
        raise HTTPException(status_code=400, detail="model must not contain whitespace or control characters")
    if ":" not in model:
        return canonical_model_id(model)
    return model


def validate_provider_model_id(provider_id: str | None, provider_model: str) -> None:
    if provider_id is None:
        return
    if not provider_id:
        raise HTTPException(status_code=400, detail="BYOK provider id must be non-empty")
    try:
        validate_provider_id(provider_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    if not provider_model:
        raise HTTPException(status_code=400, detail=f"Provider '{provider_id}' model id must be non-empty")


def chat_tool_call_delta_index(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        index = int(value)
    except (TypeError, ValueError):
        return None
    if index < 0:
        return None
    return index


def stream_string(value: object) -> str | None:
    return value if isinstance(value, str) else None


def safe_stream_error_code(value: object) -> str:
    if isinstance(value, bool):
        return "backend_error"
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        value = str(int(value))
    if isinstance(value, str):
        value = value.strip()
        if STREAM_ERROR_CODE_RE.fullmatch(value):
            return value
    return "backend_error"


def stream_error_from_payload(parsed: object) -> tuple[str, str] | None:
    if not isinstance(parsed, dict) or "error" not in parsed:
        return None
    error = parsed.get("error")
    if error is None:
        return None
    if isinstance(error, dict):
        raw_code = error.get("code") or error.get("status")
        message = stream_string(error.get("message")) or stream_string(error.get("status"))
        if raw_code is None and not message:
            return None
        return safe_stream_error_code(raw_code), message or "Backend stream returned an error"
    if isinstance(error, str):
        error = error.strip()
        if error:
            return "backend_error", error
        return None
    if not error:
        return None
    return "backend_error", "Backend stream returned an error"


def backend_error_from_payload(parsed: object) -> tuple[str, str] | None:
    stream_error = stream_error_from_payload(parsed)
    if stream_error is not None:
        return stream_error
    if isinstance(parsed, dict) and isinstance(parsed.get("response"), dict):
        return stream_error_from_payload(parsed["response"])
    return None


def status_code_from_backend_error(code: str, message: str) -> int:
    combined = f"{code} {message}".lower()
    if code in {"400", "401", "403", "404", "408", "409", "429"}:
        return int(code)
    if "invalid_argument" in combined:
        return 400
    if "unauthenticated" in combined:
        return 401
    if "permission_denied" in combined:
        return 403
    if "not_found" in combined:
        return 404
    if "resource_exhausted" in combined or "rate" in combined or "quota" in combined:
        return 429
    return 502


def google_stream_error_is_account_scoped(code: str, message: str) -> bool:
    combined = f"{code} {message}".lower()
    return any(term in combined for term in GOOGLE_ACCOUNT_SCOPED_STREAM_ERROR_TERMS)


def prepare_openai_compatible_request(
    codex_req: dict,
    provider: dict,
    provider_model: str,
    *,
    stream: bool,
) -> tuple[dict, str, dict, float]:
    try:
        payload = transform_request_to_chat({**codex_req, "stream": stream}, provider_model)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    payload["stream"] = stream
    headers = build_openai_compatible_headers(provider)
    url = chat_completions_url(provider)
    timeout = openai_compatible_timeout(provider)
    return payload, url, headers, timeout

@app.post("/v1/responses")
async def create_response(request: Request):
    request_id = f"req_{secrets.token_hex(8)}"
    request_started = time.monotonic()

    async def log_request(
        status: str,
        *,
        model: str = "",
        route: str = "unknown",
        provider: str | None = None,
        family: str | None = None,
        stream: bool = False,
        http_status: int | None = None,
        retry_after_source: str | None = None,
        rotation_attempted: bool = False,
        usage: dict | None = None,
        error_class: str | None = None,
        error: object | None = None,
    ) -> None:
        record = {
            "request_id": request_id,
            "model": model,
            "route": route,
            "provider": provider,
            "family": family,
            "stream": stream,
            "status": status,
            "latency_ms": int((time.monotonic() - request_started) * 1000),
            "http_status": http_status,
            "retry_after_source": retry_after_source,
            "rotation_attempted": rotation_attempted,
            "usage": usage,
            "error_class": error_class,
            "error": safe_error_detail(error) if error is not None else None,
        }
        await run_in_threadpool(write_request_record, record)

    try:
        codex_req = await request.json()
    except Exception:
        await log_request("failed", http_status=400, error_class="invalid_json", error="Invalid JSON body")
        raise HTTPException(status_code=400, detail="Invalid JSON body")
    codex_req = validate_response_request_body(codex_req)

    reject_unsupported_previous_response(codex_req)
    await refresh_accounts_ahead_if_due()
        
    model = response_model_id(codex_req)
    codex_req["model"] = model
    stream = response_stream_flag(codex_req)
    provider_id, provider_model = split_provider_model(model)
    validate_provider_model_id(provider_id, provider_model)
    if provider_id is not None:
        providers = all_provider_configs()
        provider = providers.get(provider_id)
        if not provider:
            await log_request(
                "failed",
                model=model,
                route="byok",
                provider=provider_id,
                stream=stream,
                http_status=404,
                error_class="provider_not_configured",
                error=f"BYOK provider '{provider_id}' is not configured",
            )
            raise HTTPException(status_code=404, detail=f"BYOK provider '{provider_id}' is not configured")
        if provider.get("kind") != "openai_chat":
            await log_request(
                "failed",
                model=model,
                route="byok",
                provider=provider_id,
                stream=stream,
                http_status=500,
                error_class="unsupported_provider_kind",
                error=f"Unsupported BYOK provider kind: {provider.get('kind')}",
            )
            raise HTTPException(status_code=500, detail=f"Unsupported BYOK provider kind: {provider.get('kind')}")
        if stream:
            payload, url, headers, timeout = prepare_openai_compatible_request(codex_req, provider, provider_model, stream=True)
            await log_request("stream_started", model=model, route="byok", provider=provider_id, stream=True)
            return StreamingResponse(
                openai_compatible_sse_generator(payload, url, headers, timeout, provider, model),
                media_type="text/event-stream",
            )
        try:
            response = await create_openai_compatible_response(codex_req, provider, provider_model, model)
        except HTTPException as exc:
            await log_request(
                "failed",
                model=model,
                route="byok",
                provider=provider_id,
                stream=False,
                http_status=exc.status_code,
                error_class="byok_error",
                error=exc.detail,
            )
            raise
        await log_request(
            "success",
            model=model,
            route="byok",
            provider=provider_id,
            stream=False,
            http_status=200,
            usage=response.get("usage") if isinstance(response, dict) else None,
        )
        return response
    
    # 1. Select account automatically from pool
    family = native_model_family(model)
    account = await select_active_account_for_request(model)
    if not account:
        await log_request(
            "failed",
            model=model,
            route="google",
            family=family,
            stream=stream,
            http_status=500,
            error_class="no_google_accounts",
            error="No Google accounts available",
        )
        raise HTTPException(
            status_code=500,
            detail=google_failure_detail(
                model,
                "No Google accounts available. Run `codex-antigravity login` to connect an account.",
            ),
        )
        
    def build_google_request(selected_account: dict) -> tuple[dict, dict]:
        project_id = safe_project_id(selected_account.get("projectId")) or safe_project_id(
            selected_account.get("managedProjectId")
        )
        return transform_request(codex_req, project_id=project_id), build_headers(selected_account)
    
    # Route target action based on streaming mode
    action = "streamGenerateContent" if stream else "generateContent"
    backend_url = f"{ANTIGRAVITY_ENDPOINT_PROD}/v1internal:{action}"
    if stream:
        backend_url += "?alt=sse"
    
    # Perform HTTP POST request to Antigravity endpoint with error recovery & rotation
    async def request_backend(selected_account: dict) -> httpx.Response | None:
        antigravity_req, headers = build_google_request(selected_account)
        async with httpx.AsyncClient(timeout=60.0) as client:
            try:
                # Do NOT stream the response inside this function, we just do normal or stream connection
                if stream:
                    # Let the StreamingResponse generator handle actual streaming
                    return None
                else:
                    res = await client.post(backend_url, json=antigravity_req, headers=headers)
                    return res
            except Exception as e:
                await mark_account_failure(
                    selected_account["email"],
                    f"Connection error: {safe_error_detail(e)}",
                    model=model,
                )
                return None

    # Handle standard non-streaming response path
    if not stream:
        response_account = account
        rotation_attempted = False
        res = await request_backend(response_account)
        if not res:
            # Retry with rotated account on connection failures
            new_account = await select_active_account_for_request(model)
            rotation_attempted = True
            if new_account:
                response_account = new_account
                res = await request_backend(response_account)
                
        if not res:
            await record_account_request(
                response_account.get("email", ""),
                model,
                status="failure",
                status_code=502,
                error_class="connection_error",
            )
            await log_request(
                "failed",
                model=model,
                route="google",
                family=family,
                stream=False,
                http_status=502,
                rotation_attempted=rotation_attempted,
                error_class="connection_error",
                error="Failed to communicate with Antigravity backend after rotation",
            )
            raise HTTPException(
                status_code=502,
                detail=google_failure_detail(
                    model,
                    "Failed to communicate with Antigravity backend after rotation",
                    rotation_attempted=rotation_attempted,
                ),
            )

        if res.status_code in (401, 403, 429):
            retry_after_seconds = retry_after_seconds_from_response(res)
            retry_after_source = retry_after_source_from_response(res)
            reason = "Rate limited / Quota exceeded" if res.status_code == 429 else f"Auth failure {res.status_code}: {safe_error_detail(res.text)}"
            await mark_account_failure(
                response_account["email"],
                reason,
                retry_after_seconds,
                model=model,
                status_code=res.status_code,
            )
            new_account = await select_active_account_for_request(model)
            rotation_attempted = True
            if new_account:
                response_account = new_account
                res = await request_backend(response_account)
            if not res:
                await record_account_request(
                    response_account.get("email", ""),
                    model,
                    status="failure",
                    status_code=502,
                    error_class="connection_error",
                )
                await log_request(
                    "failed",
                    model=model,
                    route="google",
                    family=family,
                    stream=False,
                    http_status=502,
                    retry_after_source=retry_after_source,
                    rotation_attempted=rotation_attempted,
                    error_class="connection_error",
                    error="Failed to communicate with Antigravity backend after rotation",
                )
                raise HTTPException(
                    status_code=502,
                    detail=google_failure_detail(
                        model,
                        "Failed to communicate with Antigravity backend after rotation",
                        retry_after_seconds=retry_after_seconds,
                        retry_after_source=retry_after_source,
                        rotation_attempted=rotation_attempted,
                    ),
                )
            
        if res.status_code in (401, 403):
            # Token might be invalidated or verification required
            retry_after_seconds = retry_after_seconds_from_response(res)
            retry_after_source = retry_after_source_from_response(res)
            await mark_account_failure(
                response_account["email"],
                f"Auth failure {res.status_code}: {safe_error_detail(res.text)}",
                retry_after_seconds,
                model=model,
                status_code=res.status_code,
            )
            await log_request(
                "failed",
                model=model,
                route="google",
                family=family,
                stream=False,
                http_status=res.status_code,
                retry_after_source=retry_after_source,
                rotation_attempted=rotation_attempted,
                error_class="auth_failure",
                error=res.text,
            )
            raise HTTPException(
                status_code=res.status_code,
                detail=google_failure_detail(
                    model,
                    f"Google Authentication failure: {safe_error_detail(res.text)}",
                    retry_after_seconds=retry_after_seconds,
                    retry_after_source=retry_after_source,
                    rotation_attempted=rotation_attempted,
                ),
            )
            
        if res.status_code == 429:
            retry_after_seconds = retry_after_seconds_from_response(res)
            retry_after_source = retry_after_source_from_response(res)
            await mark_account_failure(
                response_account["email"],
                "Rate limited / Quota exceeded",
                retry_after_seconds,
                model=model,
                status_code=429,
            )
            await log_request(
                "failed",
                model=model,
                route="google",
                family=family,
                stream=False,
                http_status=429,
                retry_after_source=retry_after_source,
                rotation_attempted=rotation_attempted,
                error_class="rate_limited",
                error="Antigravity account rate limit reached",
            )
            raise HTTPException(
                status_code=429,
                detail=google_failure_detail(
                    model,
                    "Antigravity account rate limit reached. Auto-switching to next account.",
                    retry_after_seconds=retry_after_seconds,
                    retry_after_source=retry_after_source,
                    rotation_attempted=rotation_attempted,
                ),
            )
            
        if res.status_code != 200:
            await record_account_request(
                response_account.get("email", ""),
                model,
                status="failure",
                status_code=res.status_code,
                error_class="backend_http_error",
            )
            await log_request(
                "failed",
                model=model,
                route="google",
                family=family,
                stream=False,
                http_status=res.status_code,
                retry_after_source=retry_after_source_from_response(res),
                rotation_attempted=rotation_attempted,
                error_class="backend_http_error",
                error=res.text,
            )
            raise HTTPException(
                status_code=res.status_code,
                detail=google_failure_detail(
                    model,
                    f"Google Antigravity API error: {safe_error_detail(res.text)}",
                    retry_after_seconds=retry_after_seconds_from_response(res),
                    retry_after_source=retry_after_source_from_response(res),
                    rotation_attempted=rotation_attempted,
                ),
            )
            
        try:
            gemini_resp = res.json()
            # If the response is wrapped as a list (stream chunk structure)
            if isinstance(gemini_resp, list) and gemini_resp:
                gemini_resp = gemini_resp[0]
            backend_error = backend_error_from_payload(gemini_resp)
            if backend_error:
                code, message = backend_error
                if google_stream_error_is_account_scoped(code, message):
                    await mark_account_failure(
                        response_account["email"],
                        f"Backend payload error {code}: {safe_error_detail(message)}",
                        model=model,
                    )
                await log_request(
                    "failed",
                    model=model,
                    route="google",
                    family=family,
                    stream=False,
                    http_status=status_code_from_backend_error(code, message),
                    rotation_attempted=rotation_attempted,
                    error_class=code,
                    error=message,
                )
                raise HTTPException(
                    status_code=status_code_from_backend_error(code, message),
                    detail=google_failure_detail(
                        model,
                        f"Google Antigravity API error: {safe_error_detail(message)}",
                        rotation_attempted=rotation_attempted,
                    ),
                )
            codex_resp = transform_response(gemini_resp, model)
            await record_account_request(
                response_account.get("email", ""),
                model,
                status="success",
                status_code=200,
                usage=codex_resp.get("usage") if isinstance(codex_resp, dict) else None,
            )
            await log_request(
                "success",
                model=model,
                route="google",
                family=family,
                stream=False,
                http_status=200,
                rotation_attempted=rotation_attempted,
                usage=codex_resp.get("usage") if isinstance(codex_resp, dict) else None,
            )
            return codex_resp
        except HTTPException:
            raise
        except Exception as e:
            await record_account_request(
                response_account.get("email", ""),
                model,
                status="failure",
                status_code=500,
                error_class="translation_error",
            )
            await log_request(
                "failed",
                model=model,
                route="google",
                family=family,
                stream=False,
                http_status=500,
                rotation_attempted=rotation_attempted,
                error_class="translation_error",
                error=e,
            )
            raise HTTPException(status_code=500, detail=f"Response translation failed: {safe_error_detail(e)}")

    # Handle standard SSE streaming response path
    async def sse_generator() -> AsyncGenerator[str, None]:
        import uuid
        response_id = f"resp_{uuid.uuid4().hex[:12]}"
        created_at = int(time.time())
        output_text = ""
        reasoning_text = ""
        indexed_output_items = []
        usage = None
        next_output_index = 0
        text_output_index = None
        sequence_number = 0
        stream_failed = False
        last_error = None
        last_error_code = "backend_error"
        backend_output_started = False
        stream_retry_requested = False

        def stream_event(payload: dict) -> str:
            nonlocal sequence_number
            payload = dict(payload)
            payload["sequence_number"] = sequence_number
            sequence_number += 1
            return f"data: {json.dumps(payload)}\n\n"
        
        # 1. response.created
        yield stream_event({'type': 'response.created', 'response': {'id': response_id, 'object': 'response', 'status': 'in_progress'}})

        msg_id = f"msg_{uuid.uuid4().hex[:8]}"

        def start_text_message_events():
            nonlocal next_output_index, text_output_index
            if text_output_index is not None:
                return
            text_output_index = next_output_index
            next_output_index += 1
            yield stream_event({'type': 'response.output_item.added', 'response_id': response_id, 'output_index': text_output_index, 'item': {'type': 'message', 'id': msg_id, 'role': 'assistant', 'status': 'in_progress', 'content': []}})
            yield stream_event({'type': 'response.content_part.added', 'response_id': response_id, 'item_id': msg_id, 'output_index': text_output_index, 'content_index': 0, 'part': {'type': 'output_text', 'text': '', 'annotations': []}})

        async def fail_stream(code: str, message: str) -> AsyncGenerator[str, None]:
            message = safe_error_detail(message)
            yield stream_event({'type': 'error', 'error': {'code': code, 'message': message}})
            yield stream_event({'type': 'response.failed', 'response': {'id': response_id, 'object': 'response', 'status': 'failed', 'error': {'code': code, 'message': message}}})
            yield "data: [DONE]\n\n"

        async def parse_stream_line(line: str) -> AsyncGenerator[str, None]:
            nonlocal output_text, reasoning_text, usage, next_output_index, stream_failed, last_error, last_error_code, backend_output_started, stream_retry_requested
            line = line.strip()
            if not line or not line.startswith("data:"):
                return
            data_payload = line[5:].strip()
            if data_payload == "[DONE]":
                return
            try:
                parsed = json.loads(data_payload)
            except json.JSONDecodeError as e:
                stream_failed = True
                async for event in fail_stream("invalid_stream_chunk", f"Invalid Antigravity SSE JSON chunk: {e}"):
                    yield event
                return
            # If list-wrapped chunk format
            if isinstance(parsed, list) and parsed:
                parsed = parsed[0]
            if not isinstance(parsed, dict):
                return
            stream_error = stream_error_from_payload(parsed)
            if "response" in parsed and isinstance(parsed["response"], dict):
                parsed = parsed["response"]
                if stream_error is None:
                    stream_error = stream_error_from_payload(parsed)
            if stream_error:
                code, message = stream_error
                last_error_code = code
                last_error = f"Google Antigravity stream returned {code}: {message}"
                if google_stream_error_is_account_scoped(code, message):
                    await mark_account_failure(
                        stream_account["email"],
                        f"Streaming backend error {code}: {safe_error_detail(message)}",
                        model=model,
                    )
                    if not backend_output_started:
                        stream_retry_requested = True
                        return
                stream_failed = True
                async for event in fail_stream(code, message):
                    yield event
                return
            if isinstance(parsed.get("usageMetadata"), dict):
                usage_meta = parsed["usageMetadata"]
                usage = usage_counts(
                    usage_meta.get("promptTokenCount", 0),
                    usage_meta.get("candidatesTokenCount", 0),
                    usage_meta.get("totalTokenCount", 0),
                )
            candidates = parsed.get("candidates", [])
            if not isinstance(candidates, list):
                return
            for cand in candidates:
                if not isinstance(cand, dict):
                    continue
                content = cand.get("content", {})
                if not isinstance(content, dict):
                    continue
                parts = content.get("parts", [])
                if not isinstance(parts, list):
                    continue
                for part in parts:
                    if not isinstance(part, dict):
                        continue
                    # Yield reasoning/thinking blocks in separate reasoning events
                    if part.get("thought") is True or part.get("type") == "thinking":
                        thought_text = stream_string(part.get("text")) or stream_string(part.get("thinking"))
                        if thought_text:
                            reasoning_text += thought_text
                            backend_output_started = True
                            yield stream_event({'type': 'response.reasoning_text.delta', 'response_id': response_id, 'delta': thought_text})
                    elif "text" in part:
                        text = stream_string(part.get("text"))
                        if text is None:
                            continue
                        output_text += text
                        backend_output_started = True
                        for event in start_text_message_events():
                            yield event
                        yield stream_event({'type': 'response.output_text.delta', 'response_id': response_id, 'item_id': msg_id, 'output_index': text_output_index, 'content_index': 0, 'delta': text})
                    elif "functionCall" in part:
                        fc = part["functionCall"]
                        if not isinstance(fc, dict):
                            continue
                        name = stream_string(fc.get("name"))
                        if not valid_function_name(name):
                            continue
                        call_id = stream_string(fc.get("id")) or f"call_{uuid.uuid4().hex[:8]}"
                        item_id = f"fc_{uuid.uuid4().hex[:8]}"
                        output_index = next_output_index
                        next_output_index += 1
                        args = fc.get("args", {})
                        arguments = function_call_arguments_json(args)
                        item = {
                            "type": "function_call",
                            "id": item_id,
                            "call_id": call_id,
                            "name": name,
                            "arguments": "",
                        }
                        backend_output_started = True
                        yield stream_event({'type': 'response.output_item.added', 'response_id': response_id, 'output_index': output_index, 'item': item})
                        if arguments:
                            yield stream_event({'type': 'response.function_call_arguments.delta', 'response_id': response_id, 'item_id': item_id, 'output_index': output_index, 'delta': arguments})
                        item["arguments"] = arguments
                        yield stream_event({'type': 'response.function_call_arguments.done', 'response_id': response_id, 'item_id': item_id, 'output_index': output_index, 'name': name, 'arguments': arguments})
                        yield stream_event({'type': 'response.output_item.done', 'response_id': response_id, 'output_index': output_index, 'item': item})
                        indexed_output_items.append((output_index, dict(item)))

        completed = False
        attempts = [account]
        attempt_num = 0
        while attempt_num < len(attempts):
            stream_retry_requested = False
            stream_account = attempts[attempt_num]
            stream_req, stream_headers = build_google_request(stream_account)
            try:
                async with httpx.AsyncClient(timeout=60.0) as client:
                    async with client.stream("POST", backend_url, json=stream_req, headers=stream_headers) as res:
                        if res.status_code != 200:
                            if res.status_code in (401, 403, 429):
                                body_bytes = await res.aread()
                                body_text = body_bytes.decode("utf-8", errors="ignore")
                                await mark_account_failure(
                                    stream_account["email"],
                                    f"Streaming HTTP {res.status_code}: {safe_error_detail(body_text)}",
                                    retry_after_seconds_from_response(res),
                                    model=model,
                                    status_code=res.status_code,
                                )
                            last_error_code = "backend_error"
                            last_error = f"Google Antigravity returned HTTP {res.status_code}"
                        else:
                            buffer = ""
                            async for chunk in res.aiter_text():
                                buffer += chunk
                                while "\n" in buffer:
                                    line, buffer = buffer.split("\n", 1)
                                    async for event in parse_stream_line(line):
                                        yield event
                                    if stream_failed or stream_retry_requested:
                                        break
                                if stream_failed or stream_retry_requested:
                                    break
                            if buffer.strip() and not stream_failed and not stream_retry_requested:
                                async for event in parse_stream_line(buffer):
                                    yield event
                            if not stream_failed and not stream_retry_requested:
                                completed = True
            except Exception as e:
                await mark_account_failure(
                    stream_account["email"],
                    f"Streaming connection error: {safe_error_detail(e)}",
                    model=model,
                )
                last_error_code = "connection_error"
                last_error = safe_error_detail(e)

            if stream_failed:
                await record_account_request(
                    stream_account.get("email", ""),
                    model,
                    status="failure",
                    status_code=None,
                    error_class=last_error_code,
                )
                await log_request(
                    "failed",
                    model=model,
                    route="google",
                    family=family,
                    stream=True,
                    error_class=last_error_code,
                    error=last_error or "Google Antigravity stream failed",
                    rotation_attempted=len(attempts) > 1,
                    usage=usage,
                )
                return
            if completed:
                break
            if stream_retry_requested and attempt_num == 0:
                rotated = await select_active_account_for_request(model)
                if rotated and rotated.get("email") != stream_account.get("email"):
                    usage = None
                    last_error = None
                    last_error_code = "backend_error"
                    attempts.append(rotated)
                    attempt_num += 1
                    continue
            elif attempt_num == 0:
                rotated = await select_active_account_for_request(model)
                if rotated and rotated.get("email") != stream_account.get("email"):
                    attempts.append(rotated)
                    attempt_num += 1
                    continue
            break

        if not completed:
            final_account = attempts[min(attempt_num, len(attempts) - 1)]
            await record_account_request(
                final_account.get("email", ""),
                model,
                status="failure",
                status_code=None,
                error_class=last_error_code,
            )
            await log_request(
                "failed",
                model=model,
                route="google",
                family=family,
                stream=True,
                error_class=last_error_code,
                error=last_error or "Google Antigravity stream failed",
                rotation_attempted=len(attempts) > 1,
                usage=usage,
            )
            async for event in fail_stream(last_error_code, last_error or "Google Antigravity stream failed"):
                yield event
            return
                
        # 4. Final completion events
        if text_output_index is not None:
            message_item = {'type': 'message', 'id': msg_id, 'status': 'completed', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': output_text, 'annotations': []}]}
            yield stream_event({'type': 'response.output_text.done', 'response_id': response_id, 'item_id': msg_id, 'output_index': text_output_index, 'content_index': 0, 'text': output_text})
            yield stream_event({'type': 'response.content_part.done', 'response_id': response_id, 'item_id': msg_id, 'output_index': text_output_index, 'content_index': 0, 'part': {'type': 'output_text', 'text': output_text, 'annotations': []}})
            yield stream_event({'type': 'response.output_item.done', 'response_id': response_id, 'output_index': text_output_index, 'item': message_item})
            indexed_output_items.append((text_output_index, message_item))
        done_output = []
        if reasoning_text:
            done_output.append({
                "type": "reasoning",
                "id": f"rs_{uuid.uuid4().hex[:8]}",
                "encrypted_content": "",
                "step_by_step_summary": reasoning_text,
            })
        done_output.extend(item for _output_index, item in sorted(indexed_output_items, key=lambda entry: entry[0]))
        done_response = {
            'id': response_id,
            'object': 'response',
            'created_at': created_at,
            'model': model,
            'output': done_output,
            'status': 'completed',
        }
        if usage:
            done_response["usage"] = usage
        final_account = attempts[min(attempt_num, len(attempts) - 1)]
        await record_account_request(
            final_account.get("email", ""),
            model,
            status="success",
            status_code=200,
            usage=usage,
        )
        await log_request(
            "success",
            model=model,
            route="google",
            family=family,
            stream=True,
            http_status=200,
            rotation_attempted=len(attempts) > 1,
            usage=usage,
        )
        yield stream_event({'type': 'response.completed', 'response': done_response})
        yield "data: [DONE]\n\n"

    return StreamingResponse(sse_generator(), media_type="text/event-stream")


async def create_openai_compatible_response(codex_req: dict, provider: dict, provider_model: str, display_model: str) -> dict:
    payload, url, headers, timeout = prepare_openai_compatible_request(codex_req, provider, provider_model, stream=False)
    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            res = await client.post(url, json=payload, headers=headers)
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"{provider['id']} connection error: {safe_error_detail(e)}") from e
    if res.status_code != 200:
        raise HTTPException(status_code=res.status_code, detail=f"{provider['id']} API error: {safe_error_detail(res.text)}")
    try:
        chat_resp = res.json()
        backend_error = backend_error_from_payload(chat_resp)
        if backend_error:
            code, message = backend_error
            raise HTTPException(
                status_code=status_code_from_backend_error(code, message),
                detail=f"{provider['id']} API error: {safe_error_detail(message)}",
            )
        return transform_chat_response(chat_resp, display_model)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"{provider['id']} response translation failed: {safe_error_detail(e)}") from e


async def openai_compatible_sse_generator(
    payload: dict,
    url: str,
    headers: dict,
    timeout: float,
    provider: dict,
    display_model: str,
) -> AsyncGenerator[str, None]:
    import uuid

    response_id = f"resp_{uuid.uuid4().hex[:12]}"
    created_at = int(time.time())
    msg_id = f"msg_{uuid.uuid4().hex[:8]}"
    output_text = ""
    reasoning_text = ""
    usage = None
    tool_calls: dict[int, dict] = {}
    tool_seen_order: list[int] = []
    tool_output_indices: dict[int, int] = {}
    indexed_output_items = []
    next_output_index = 0
    text_output_index = None
    sequence_number = 0
    stream_failed = False

    def stream_event(payload: dict) -> str:
        nonlocal sequence_number
        payload = dict(payload)
        payload["sequence_number"] = sequence_number
        sequence_number += 1
        return f"data: {json.dumps(payload)}\n\n"

    yield stream_event({'type': 'response.created', 'response': {'id': response_id, 'object': 'response', 'status': 'in_progress', 'model': display_model}})

    def start_text_message_events():
        nonlocal next_output_index, text_output_index
        if text_output_index is not None:
            return
        text_output_index = next_output_index
        next_output_index += 1
        yield stream_event({'type': 'response.output_item.added', 'response_id': response_id, 'output_index': text_output_index, 'item': {'type': 'message', 'id': msg_id, 'role': 'assistant', 'status': 'in_progress', 'content': []}})
        yield stream_event({'type': 'response.content_part.added', 'response_id': response_id, 'item_id': msg_id, 'output_index': text_output_index, 'content_index': 0, 'part': {'type': 'output_text', 'text': '', 'annotations': []}})

    async def fail_stream(code: str, message: str) -> AsyncGenerator[str, None]:
        message = safe_error_detail(message)
        yield stream_event({'type': 'error', 'error': {'code': code, 'message': message}})
        yield stream_event({'type': 'response.failed', 'response': {'id': response_id, 'object': 'response', 'status': 'failed', 'model': display_model, 'error': {'code': code, 'message': message}}})
        yield "data: [DONE]\n\n"

    async def parse_chat_stream_line(line: str) -> AsyncGenerator[str, None]:
        nonlocal output_text, reasoning_text, usage, next_output_index, stream_failed
        line = line.strip()
        if not line or not line.startswith("data:"):
            return
        data_payload = line[5:].strip()
        if data_payload == "[DONE]":
            return
        try:
            parsed = json.loads(data_payload)
        except json.JSONDecodeError as e:
            stream_failed = True
            async for event in fail_stream("invalid_stream_chunk", f"Invalid {provider['id']} SSE JSON chunk: {e}"):
                yield event
            return
        if not isinstance(parsed, dict):
            return
        stream_error = stream_error_from_payload(parsed)
        if stream_error:
            code, message = stream_error
            stream_failed = True
            async for event in fail_stream(code, message):
                yield event
            return
        if isinstance(parsed.get("usage"), dict):
            provider_usage = parsed["usage"]
            usage = usage_counts(
                provider_usage.get("prompt_tokens", provider_usage.get("input_tokens", 0)),
                provider_usage.get("completion_tokens", provider_usage.get("output_tokens", 0)),
                provider_usage.get("total_tokens", 0),
            )
        choices = parsed.get("choices", []) or []
        if not isinstance(choices, list):
            return
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            delta = choice.get("delta", {}) or {}
            if not isinstance(delta, dict):
                continue
            reasoning_content = stream_string(delta.get("reasoning_content"))
            if reasoning_content:
                reasoning_text += reasoning_content
                yield stream_event({'type': 'response.reasoning_text.delta', 'response_id': response_id, 'delta': reasoning_content})
            content_delta = stream_string(delta.get("content"))
            if content_delta:
                output_text += content_delta
                for event in start_text_message_events():
                    yield event
                yield stream_event({'type': 'response.output_text.delta', 'response_id': response_id, 'item_id': msg_id, 'output_index': text_output_index, 'content_index': 0, 'delta': content_delta})
            tool_deltas = delta.get("tool_calls", []) or []
            if not isinstance(tool_deltas, list):
                continue
            for tool_delta in tool_deltas:
                if not isinstance(tool_delta, dict):
                    continue
                idx = chat_tool_call_delta_index(tool_delta.get("index", 0))
                if idx is None:
                    continue
                fn = tool_delta.get("function", {}) or {}
                if not isinstance(fn, dict):
                    continue
                generated_call_id = stream_string(tool_delta.get("id")) or f"call_{uuid.uuid4().hex[:8]}"
                if idx not in tool_calls:
                    tool_seen_order.append(idx)
                    tool_calls[idx] = {
                        "id": f"fc_{uuid.uuid4().hex[:8]}",
                        "type": "function_call",
                        "call_id": generated_call_id,
                        "name": "",
                        "arguments": "",
                    }
                state = tool_calls[idx]
                tool_call_id = stream_string(tool_delta.get("id"))
                if tool_call_id:
                    state["call_id"] = tool_call_id
                buffered_arguments = state.get("arguments", "")
                name_delta = stream_string(fn.get("name"))
                arguments_delta = stream_string(fn.get("arguments"))
                if name_delta:
                    state["name"] += name_delta
                new_tool_item = (
                    idx not in tool_output_indices
                    and valid_function_name(state["name"])
                    and bool(arguments_delta)
                    and not name_delta
                )
                if new_tool_item:
                    tool_output_indices[idx] = next_output_index
                    next_output_index += 1
                    item = dict(state)
                    item["arguments"] = ""
                    yield stream_event({'type': 'response.output_item.added', 'response_id': response_id, 'output_index': tool_output_indices[idx], 'item': item})
                    if buffered_arguments:
                        yield stream_event({'type': 'response.function_call_arguments.delta', 'response_id': response_id, 'item_id': state['id'], 'output_index': tool_output_indices[idx], 'delta': buffered_arguments})
                if arguments_delta:
                    state["arguments"] += arguments_delta
                    if idx in tool_output_indices:
                        yield stream_event({'type': 'response.function_call_arguments.delta', 'response_id': response_id, 'item_id': state['id'], 'output_index': tool_output_indices[idx], 'delta': arguments_delta})

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream("POST", url, json=payload, headers=headers) as res:
                if res.status_code != 200:
                    body = (await res.aread()).decode("utf-8", errors="ignore")
                    async for event in fail_stream("backend_error", f"{provider['id']} returned HTTP {res.status_code}: {body}"):
                        yield event
                    return
                buffer = ""
                async for chunk in res.aiter_text():
                    buffer += chunk
                    while "\n" in buffer:
                        line, buffer = buffer.split("\n", 1)
                        async for event in parse_chat_stream_line(line):
                            yield event
                        if stream_failed:
                            break
                    if stream_failed:
                        break
                if buffer.strip() and not stream_failed:
                    async for event in parse_chat_stream_line(buffer):
                        yield event
    except Exception as e:
        async for event in fail_stream("connection_error", safe_error_detail(e)):
            yield event
        return

    if stream_failed:
        return

    for idx in tool_seen_order:
        item = tool_calls[idx]
        if not valid_function_name(item.get("name")):
            continue
        if idx not in tool_output_indices:
            tool_output_indices[idx] = next_output_index
            next_output_index += 1
            added_item = dict(item)
            added_item["arguments"] = ""
            yield stream_event({'type': 'response.output_item.added', 'response_id': response_id, 'output_index': tool_output_indices[idx], 'item': added_item})
            if item.get("arguments"):
                yield stream_event({'type': 'response.function_call_arguments.delta', 'response_id': response_id, 'item_id': item['id'], 'output_index': tool_output_indices[idx], 'delta': item['arguments']})

    for idx in sorted(tool_output_indices, key=lambda tool_idx: tool_output_indices[tool_idx]):
        item = tool_calls[idx]
        if not valid_function_name(item.get("name")):
            continue
        output_index = tool_output_indices[idx]
        arguments = function_call_arguments_string(item.get("arguments", ""))
        item["arguments"] = arguments
        yield stream_event({'type': 'response.function_call_arguments.done', 'response_id': response_id, 'item_id': item['id'], 'output_index': output_index, 'name': item.get('name', ''), 'arguments': arguments})
        yield stream_event({'type': 'response.output_item.done', 'response_id': response_id, 'output_index': output_index, 'item': item})
        indexed_output_items.append((output_index, dict(item)))

    if text_output_index is not None:
        message_item = {'type': 'message', 'id': msg_id, 'status': 'completed', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': output_text, 'annotations': []}]}
        yield stream_event({'type': 'response.output_text.done', 'response_id': response_id, 'item_id': msg_id, 'output_index': text_output_index, 'content_index': 0, 'text': output_text})
        yield stream_event({'type': 'response.content_part.done', 'response_id': response_id, 'item_id': msg_id, 'output_index': text_output_index, 'content_index': 0, 'part': {'type': 'output_text', 'text': output_text, 'annotations': []}})
        yield stream_event({'type': 'response.output_item.done', 'response_id': response_id, 'output_index': text_output_index, 'item': message_item})
        indexed_output_items.append((text_output_index, message_item))
    done_output = []
    if reasoning_text:
        done_output.append({
            "type": "reasoning",
            "id": f"rs_{uuid.uuid4().hex[:8]}",
            "encrypted_content": "",
            "step_by_step_summary": reasoning_text,
        })
    done_output.extend(item for _output_index, item in sorted(indexed_output_items, key=lambda entry: entry[0]))
    done_response = {
        "id": response_id,
        "object": "response",
        "created_at": created_at,
        "status": "completed",
        "model": display_model,
        "output": done_output,
    }
    if usage:
        done_response["usage"] = usage
    yield stream_event({'type': 'response.completed', 'response': done_response})
    yield "data: [DONE]\n\n"
