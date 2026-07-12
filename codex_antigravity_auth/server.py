import json
import asyncio
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
from .account_state import scoped_cooldown_expiry
from .byok import (
    PROVIDER_AUTH_MODE_API_KEY,
    PROVIDER_AUTH_MODE_OAUTH,
    all_provider_configs,
    provider_capabilities,
    provider_auth_mode,
    resolve_api_key,
    split_provider_model,
    validate_provider_api_key,
    validate_provider_id,
)
from .transform import (
    function_call_arguments_json,
    function_call_arguments_string,
    safe_project_id,
    transform_chat_response,
    usage_counts,
    valid_function_name,
)
from .constants import get_platform, is_loopback_host, validate_gateway_token_strength
from .models import (
    canonical_model_id,
    native_model_capabilities,
    native_model_catalog,
    native_model_family,
)
from .observability import request_log_info, write_request_record
from .redaction import redact_secret_text
from .google_transport import (
    AccountLease,
    GoogleResponseAccumulator,
    GoogleTransport,
    outcome_for_backend_error,
    outcome_for_http_status,
)
from .openai_transport import (
    ChatResponseAccumulator,
    NativeResponsesStreamAdapter,
    OpenAICompatibleTransport,
    TransportConfigError,
)
from .response_protocol import (
    AttemptOutcome,
    CapabilityError,
    ProviderCapabilities,
    TerminalKind,
    response_from_result,
    validate_capabilities,
)
from .storage import load_accounts
from .xai_oauth import resolve_xai_oauth_access_token, xai_oauth_status


@asynccontextmanager
async def gateway_lifespan(_app: FastAPI):
    schedule_refresh_accounts_ahead(force=True)
    yield


app = FastAPI(title="Codex Antigravity Gateway", lifespan=gateway_lifespan)
account_manager = AccountManager()
_last_refresh_ahead_at = 0.0
_refresh_ahead_task: asyncio.Task | None = None
REFRESH_AHEAD_THROTTLE_SECONDS = 60.0
STREAM_ERROR_CODE_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
REQUEST_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
MUTATING_JSON_PATHS = {"/v1/responses"}
MODEL_CATALOG_PROVIDER_TIMEOUT_SECONDS = 2.0
GOOGLE_BACKEND_TIMEOUT_SECONDS = 60.0
GOOGLE_BACKEND_TIMEOUT_MIN_SECONDS = 1.0
GOOGLE_BACKEND_TIMEOUT_MAX_SECONDS = 600.0
GOOGLE_BACKEND_TIMEOUT_METADATA_KEY = "antigravity_backend_timeout_seconds"
TEST_CLIENT_HOSTS = {"testserver"}
REQUEST_BOUNDARY_CAPABILITIES = ProviderCapabilities(
    native_responses=True,
    parallel_tool_calls=True,
    structured_output=True,
    stop_sequences=True,
    reasoning=True,
    streaming_usage=True,
)
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


async def acquire_active_account_for_request(model: str) -> dict | None:
    return await run_in_threadpool(account_manager.acquire_account, model)


async def release_account_for_request(email: str | None) -> None:
    await run_in_threadpool(account_manager.release_account, email)


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


async def record_attempt_outcome(
    email: str,
    model: str,
    outcome: AttemptOutcome,
    *,
    status_code: int | None = None,
    usage: dict | None = None,
    error_class: str | None = None,
) -> None:
    await record_account_request(
        email,
        model,
        status="success" if outcome.category == "success" else "failure",
        status_code=status_code,
        error_class=None if outcome.category == "success" else (error_class or outcome.category),
        usage=usage,
    )


def schedule_refresh_accounts_ahead(*, force: bool = False) -> bool:
    global _last_refresh_ahead_at, _refresh_ahead_task
    now = time.monotonic()
    if _refresh_ahead_task is not None and not _refresh_ahead_task.done():
        return False
    if not force and now - _last_refresh_ahead_at < REFRESH_AHEAD_THROTTLE_SECONDS:
        return False
    _last_refresh_ahead_at = now
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return False

    async def _refresh_runner() -> None:
        try:
            await run_in_threadpool(account_manager.refresh_expiring_accounts, 300)
        except Exception:
            return

    _refresh_ahead_task = loop.create_task(_refresh_runner())
    return True


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
        for family in ("claude", "gemini"):
            cooldown_end = scoped_cooldown_expiry(cooldowns.get(email, 0), family)
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
        cooldown_end = scoped_cooldown_expiry(cooldowns.get(email, 0), family)
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
    attempt_count: int | None = None,
) -> dict:
    diagnostics = google_rotation_diagnostics(
        model,
        retry_after_seconds=retry_after_seconds,
        retry_after_source=retry_after_source,
        rotation_attempted=rotation_attempted,
    )
    if attempt_count is not None:
        safe_attempt_count = max(0, int(attempt_count))
        diagnostics["attempt_count"] = safe_attempt_count
        diagnostics["attempted_account_refs"] = [
            f"account-{index}" for index in range(1, safe_attempt_count + 1)
        ]
    return {
        "message": safe_error_detail(message),
        "diagnostics": diagnostics,
    }


def provider_has_usable_key(provider: dict) -> bool:
    auth_mode = provider_auth_mode(provider)
    if auth_mode == PROVIDER_AUTH_MODE_OAUTH:
        return provider.get("id") == "xai-oauth" and bool(xai_oauth_status().get("ready"))
    if auth_mode != PROVIDER_AUTH_MODE_API_KEY:
        return False
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


def provider_model_catalog(created: int) -> list[dict]:
    byok_models = []
    try:
        providers = all_provider_configs()
    except Exception:
        return byok_models
    for provider_id, provider in providers.items():
        try:
            usable = provider_has_usable_key(provider)
        except Exception:
            usable = False
        if not usable:
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
            try:
                capabilities = provider_capabilities(provider, provider_model)
            except ValueError:
                continue
            byok_models.append(
                {
                    **codex_model_metadata(
                        model_id,
                        f"{provider.get('displayName', provider_id)}: {display_name}",
                        context_window,
                        provider_id,
                        created,
                        supports_parallel_tool_calls=capabilities.parallel_tool_calls,
                    )
                }
            )
    return byok_models


async def provider_model_catalog_fail_soft(created: int) -> list[dict]:
    try:
        return await asyncio.wait_for(
            run_in_threadpool(provider_model_catalog, created),
            timeout=MODEL_CATALOG_PROVIDER_TIMEOUT_SECONDS,
        )
    except Exception:
        return []


def provider_health_catalog() -> list[dict]:
    providers = []
    try:
        provider_configs = all_provider_configs()
    except Exception:
        return providers
    for provider_id, provider in provider_configs.items():
        models = provider.get("models", [])
        try:
            usable = provider_has_usable_key(provider)
        except Exception:
            usable = False
        providers.append(
            {
                "id": provider_id,
                "kind": provider.get("kind"),
                "usable": usable,
                "model_count": len(models) if isinstance(models, list) else 0,
            }
        )
    return providers


async def provider_health_catalog_fail_soft() -> tuple[list[dict], str]:
    try:
        providers = await asyncio.wait_for(
            run_in_threadpool(provider_health_catalog),
            timeout=MODEL_CATALOG_PROVIDER_TIMEOUT_SECONDS,
        )
        return providers, "ok"
    except asyncio.TimeoutError:
        return [], "timeout"
    except Exception:
        return [], "error"


@app.get("/v1/models")
async def list_models():
    """Return model catalog so Codex Desktop can populate its picker dropdown."""
    created = int(time.time())
    byok_models = await provider_model_catalog_fail_soft(created)
    models = [
        codex_model_metadata(
            m["id"],
            m["display_name"],
            m["context_window"],
            "google-antigravity",
            created,
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
    providers, provider_catalog_status = await provider_health_catalog_fail_soft()
    catalog = native_model_catalog()
    return {
        "ok": True,
        "model_count": len(catalog),
        "advertised_native_models": [model["id"] for model in catalog],
        "configured_route_families": {
            "google": bool(catalog),
            "byok": providers,
        },
        "provider_catalog_status": provider_catalog_status,
        "accounts": account_health_summary(),
        "request_log": request_log_info(),
    }

def build_headers(account: dict) -> dict:
    project_id = safe_project_id(account.get("projectId")) or safe_project_id(account.get("managedProjectId"))
    fingerprint = account.get("fingerprint")
    return GoogleTransport(timeout=GOOGLE_BACKEND_TIMEOUT_SECONDS, platform_name=get_platform()).build_headers(
        AccountLease(
            email=account.get("email", ""),
            project_id=project_id,
            access_token=account["accessToken"],
            fingerprint=fingerprint if isinstance(fingerprint, dict) else None,
        )
    )


def build_openai_compatible_headers(provider: dict) -> dict:
    try:
        return OpenAICompatibleTransport(timeout=120.0).build_headers(provider)
    except TransportConfigError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


def chat_completions_url(provider: dict) -> str:
    try:
        return OpenAICompatibleTransport(timeout=120.0).chat_completions_url(provider)
    except TransportConfigError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


def responses_api_url(provider: dict) -> str:
    try:
        return OpenAICompatibleTransport(timeout=120.0).responses_url(provider)
    except TransportConfigError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


def openai_compatible_timeout(provider: dict) -> float:
    try:
        return OpenAICompatibleTransport(timeout=120.0).provider_timeout(provider)
    except TransportConfigError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


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
    metadata = value.get("metadata")
    if metadata is not None:
        if not isinstance(metadata, dict):
            raise HTTPException(status_code=400, detail="metadata must be an object")
        normalized_metadata = {}
        run_id = metadata.get("run_id")
        if run_id is not None:
            if not isinstance(run_id, str) or not REQUEST_RUN_ID_RE.fullmatch(run_id):
                raise HTTPException(
                    status_code=400,
                    detail="metadata.run_id must be 1-128 characters using letters, numbers, '_', '-', '.', or ':'",
                )
            normalized_metadata["run_id"] = run_id
        backend_timeout = metadata.get(GOOGLE_BACKEND_TIMEOUT_METADATA_KEY)
        if backend_timeout is not None:
            validate_finite_number_option(
                backend_timeout,
                f"metadata.{GOOGLE_BACKEND_TIMEOUT_METADATA_KEY}",
                minimum=GOOGLE_BACKEND_TIMEOUT_MIN_SECONDS,
                maximum=GOOGLE_BACKEND_TIMEOUT_MAX_SECONDS,
            )
            normalized_metadata[GOOGLE_BACKEND_TIMEOUT_METADATA_KEY] = float(backend_timeout)
        value["metadata"] = normalized_metadata
    validate_response_generation_options(value)
    validate_response_tool_choice(value)
    try:
        validate_capabilities(value, REQUEST_BOUNDARY_CAPABILITIES)
    except CapabilityError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return value


def google_backend_timeout_from_metadata(metadata: object) -> float:
    if isinstance(metadata, dict):
        value = metadata.get(GOOGLE_BACKEND_TIMEOUT_METADATA_KEY)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value)):
            return max(
                GOOGLE_BACKEND_TIMEOUT_MIN_SECONDS,
                min(GOOGLE_BACKEND_TIMEOUT_MAX_SECONDS, float(value)),
            )
    return GOOGLE_BACKEND_TIMEOUT_SECONDS


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
        prepared = OpenAICompatibleTransport(timeout=120.0).prepare_chat_request(
            codex_req,
            provider,
            provider_model,
            stream=stream,
        )
    except TransportConfigError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    return prepared.payload, prepared.url, prepared.headers, prepared.timeout


async def xai_oauth_headers(*, force_refresh: bool = False) -> dict:
    try:
        if force_refresh:
            token = await run_in_threadpool(resolve_xai_oauth_access_token, force_refresh=True)
        else:
            token = await run_in_threadpool(resolve_xai_oauth_access_token)
    except RuntimeError as e:
        raise HTTPException(status_code=401, detail=redact_secret_text(str(e))) from e
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


async def prepare_xai_oauth_responses_request(
    codex_req: dict,
    provider: dict,
    provider_model: str,
    *,
    stream: bool,
    force_refresh: bool = False,
) -> tuple[dict, str, dict, float]:
    payload = dict(codex_req)
    payload["model"] = provider_model
    payload["stream"] = stream
    headers = await xai_oauth_headers(force_refresh=force_refresh)
    url = responses_api_url(provider)
    timeout = openai_compatible_timeout(provider)
    return payload, url, headers, timeout

@app.post("/v1/responses")
async def create_response(request: Request):
    request_id = f"req_{secrets.token_hex(8)}"
    request_started = time.monotonic()
    request_run_id: str | None = None

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
        terminal_kind: str | None = None,
        terminal_reason: str | None = None,
        attempt_count: int | None = None,
        rotation_count: int | None = None,
        cooldown_scope: str | None = None,
        cooldown_category: str | None = None,
        outcome_category: str | None = None,
        cancelled: bool = False,
    ) -> None:
        record = {
            "request_id": request_id,
            "run_id": request_run_id,
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
            "terminal_kind": terminal_kind,
            "terminal_reason": terminal_reason,
            "attempt_count": attempt_count,
            "rotation_count": rotation_count,
            "cooldown_scope": cooldown_scope,
            "cooldown_category": cooldown_category,
            "outcome_category": outcome_category,
            "cancelled": cancelled,
        }
        await run_in_threadpool(write_request_record, record)

    try:
        codex_req = await request.json()
    except Exception:
        await log_request("failed", http_status=400, error_class="invalid_json", error="Invalid JSON body")
        raise HTTPException(status_code=400, detail="Invalid JSON body")
    codex_req = validate_response_request_body(codex_req)
    request_metadata = codex_req.pop("metadata", None)
    if isinstance(request_metadata, dict) and isinstance(request_metadata.get("run_id"), str):
        request_run_id = request_metadata["run_id"]
    google_backend_timeout = google_backend_timeout_from_metadata(request_metadata)

    reject_unsupported_previous_response(codex_req)
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
        try:
            validate_capabilities(
                codex_req,
                provider_capabilities(provider, provider_model),
            )
        except (CapabilityError, ValueError) as exc:
            await log_request(
                "failed",
                model=model,
                route="byok",
                provider=provider_id,
                stream=stream,
                http_status=400,
                error_class="unsupported_route_capability",
                error=exc,
            )
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        provider_kind = provider.get("kind")
        if provider_kind == "openai_responses":
            if provider_id != "xai-oauth" or provider_auth_mode(provider) != PROVIDER_AUTH_MODE_OAUTH:
                await log_request(
                    "failed",
                    model=model,
                    route="byok",
                    provider=provider_id,
                    stream=stream,
                    http_status=500,
                    error_class="unsupported_provider_auth",
                    error=f"Unsupported Responses provider auth mode: {provider_auth_mode(provider)}",
                )
                raise HTTPException(status_code=500, detail=f"Unsupported Responses provider auth mode: {provider_auth_mode(provider)}")
            if stream:
                payload, url, headers, timeout = await prepare_xai_oauth_responses_request(
                    codex_req,
                    provider,
                    provider_model,
                    stream=True,
                )
                await log_request("stream_started", model=model, route="byok", provider=provider_id, stream=True)

                async def logged_xai_oauth_stream() -> AsyncGenerator[str, None]:
                    terminal_status = "ended"
                    terminal_http_status = None
                    terminal_error_class = None
                    terminal_error = None
                    terminal_usage = None
                    try:
                        async for chunk in xai_oauth_responses_sse_generator(payload, url, headers, timeout, provider, provider_model, codex_req):
                            for line in chunk.splitlines():
                                if not line.startswith("data: ") or line == "data: [DONE]":
                                    continue
                                try:
                                    event = json.loads(line[6:])
                                except json.JSONDecodeError:
                                    continue
                                event_type = event.get("type")
                                if event_type == "response.completed":
                                    terminal_status = "success"
                                    terminal_http_status = 200
                                    response_payload = event.get("response", {})
                                    if isinstance(response_payload, dict):
                                        terminal_usage = response_payload.get("usage")
                                elif event_type == "response.failed":
                                    terminal_status = "failed"
                                    response_payload = event.get("response", {})
                                    error_payload = response_payload.get("error", {}) if isinstance(response_payload, dict) else {}
                                    if isinstance(error_payload, dict):
                                        terminal_error_class = error_payload.get("code")
                                        terminal_error = error_payload.get("message")
                            yield chunk
                    except Exception as exc:
                        terminal_status = "failed"
                        terminal_error_class = "stream_exception"
                        terminal_error = exc
                        raise
                    finally:
                        await log_request(
                            terminal_status,
                            model=model,
                            route="byok",
                            provider=provider_id,
                            stream=True,
                            http_status=terminal_http_status,
                            usage=terminal_usage,
                            error_class=terminal_error_class,
                            error=terminal_error,
                        )

                return StreamingResponse(
                    logged_xai_oauth_stream(),
                    media_type="text/event-stream",
                )
            try:
                response = await create_xai_oauth_response(codex_req, provider, provider_model, model)
            except HTTPException as exc:
                await log_request(
                    "failed",
                    model=model,
                    route="byok",
                    provider=provider_id,
                    stream=False,
                    http_status=exc.status_code,
                    error_class="xai_oauth_error",
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
        if provider_kind != "openai_chat":
            await log_request(
                "failed",
                model=model,
                route="byok",
                provider=provider_id,
                stream=stream,
                http_status=500,
                error_class="unsupported_provider_kind",
                error=f"Unsupported BYOK provider kind: {provider_kind}",
            )
            raise HTTPException(status_code=500, detail=f"Unsupported BYOK provider kind: {provider_kind}")
        if stream:
            payload, url, headers, timeout = prepare_openai_compatible_request(codex_req, provider, provider_model, stream=True)
            await log_request("stream_started", model=model, route="byok", provider=provider_id, stream=True)

            async def logged_byok_stream() -> AsyncGenerator[str, None]:
                terminal_status = "ended"
                terminal_http_status = None
                terminal_error_class = None
                terminal_error = None
                terminal_usage = None
                try:
                    async for chunk in openai_compatible_sse_generator(payload, url, headers, timeout, provider, model):
                        for line in chunk.splitlines():
                            if not line.startswith("data: ") or line == "data: [DONE]":
                                continue
                            try:
                                event = json.loads(line[6:])
                            except json.JSONDecodeError:
                                continue
                            event_type = event.get("type")
                            if event_type == "response.completed":
                                terminal_status = "success"
                                terminal_http_status = 200
                                response_payload = event.get("response", {})
                                if isinstance(response_payload, dict):
                                    terminal_usage = response_payload.get("usage")
                            elif event_type == "response.failed":
                                terminal_status = "failed"
                                response_payload = event.get("response", {})
                                error_payload = response_payload.get("error", {}) if isinstance(response_payload, dict) else {}
                                if isinstance(error_payload, dict):
                                    terminal_error_class = error_payload.get("code")
                                    terminal_error = error_payload.get("message")
                        yield chunk
                except Exception as exc:
                    terminal_status = "failed"
                    terminal_error_class = "stream_exception"
                    terminal_error = exc
                    raise
                finally:
                    await log_request(
                        terminal_status,
                        model=model,
                        route="byok",
                        provider=provider_id,
                        stream=True,
                        http_status=terminal_http_status,
                        usage=terminal_usage,
                        error_class=terminal_error_class,
                        error=terminal_error,
                    )

            return StreamingResponse(
                logged_byok_stream(),
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
    
    try:
        validate_capabilities(codex_req, native_model_capabilities(model))
    except CapabilityError as exc:
        await log_request(
            "failed",
            model=model,
            route="google",
            family=native_model_family(model),
            stream=stream,
            http_status=400,
            error_class="unsupported_route_capability",
            error=exc,
        )
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    schedule_refresh_accounts_ahead()

    # 1. Select account automatically from pool
    family = native_model_family(model)
    account = await acquire_active_account_for_request(model)
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
        
    google_transport = GoogleTransport(
        timeout=google_backend_timeout,
        platform_name=get_platform(),
        client_factory=httpx.AsyncClient,
    )

    def account_lease(selected_account: dict) -> AccountLease:
        project_id = safe_project_id(selected_account.get("projectId")) or safe_project_id(
            selected_account.get("managedProjectId")
        )
        fingerprint = selected_account.get("fingerprint")
        return AccountLease(
            email=selected_account.get("email", ""),
            project_id=project_id,
            access_token=selected_account["accessToken"],
            fingerprint=fingerprint if isinstance(fingerprint, dict) else None,
        )

    # Perform HTTP POST request to Antigravity endpoint with error recovery & rotation
    async def request_backend(selected_account: dict) -> httpx.Response | None:
        try:
            if stream:
                return None
            return await google_transport.post(codex_req, account_lease(selected_account))
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
        response_attempts = [account]
        rotation_attempted = False
        cooldown_scope: str | None = None
        cooldown_category: str | None = None
        try:
            res = await request_backend(response_account)
            if not res:
                new_account = await acquire_active_account_for_request(model)
                rotation_attempted = True
                if new_account:
                    await record_attempt_outcome(
                        response_account.get("email", ""),
                        model,
                        AttemptOutcome(scope="none", category="transport"),
                        status_code=502,
                        error_class="connection_error",
                    )
                    response_attempts.append(new_account)
                    response_account = new_account
                    res = await request_backend(response_account)

            if not res:
                await record_attempt_outcome(
                    response_account.get("email", ""),
                    model,
                    AttemptOutcome(scope="none", category="transport"),
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
                        attempt_count=len(response_attempts),
                    ),
                )

            if res.status_code in (401, 403, 429):
                retry_after_seconds = retry_after_seconds_from_response(res)
                retry_after_source = retry_after_source_from_response(res)
                reason = "Rate limited / Quota exceeded" if res.status_code == 429 else f"Auth failure {res.status_code}: {safe_error_detail(res.text)}"
                cooldown_scope = "family" if res.status_code == 429 else "account"
                cooldown_category = "rate_limit" if res.status_code == 429 else "auth"
                await mark_account_failure(
                    response_account["email"],
                    reason,
                    retry_after_seconds,
                    model=model,
                    status_code=res.status_code,
                )
                new_account = await acquire_active_account_for_request(model)
                rotation_attempted = True
                if new_account:
                    await record_attempt_outcome(
                        response_account.get("email", ""),
                        model,
                        AttemptOutcome(
                            scope="family" if res.status_code == 429 else "account",
                            category="rate_limit" if res.status_code == 429 else "auth",
                            retry_after_seconds=retry_after_seconds,
                        ),
                        status_code=res.status_code,
                    )
                    response_attempts.append(new_account)
                    response_account = new_account
                    res = await request_backend(response_account)
                if not res:
                    await record_attempt_outcome(
                        response_account.get("email", ""),
                        model,
                        AttemptOutcome(scope="none", category="transport"),
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
                            attempt_count=len(response_attempts),
                        ),
                    )

            if res.status_code in (401, 403):
                retry_after_seconds = retry_after_seconds_from_response(res)
                retry_after_source = retry_after_source_from_response(res)
                await mark_account_failure(
                    response_account["email"],
                    f"Auth failure {res.status_code}: {safe_error_detail(res.text)}",
                    retry_after_seconds,
                    model=model,
                    status_code=res.status_code,
                )
                await record_attempt_outcome(
                    response_account.get("email", ""),
                    model,
                    outcome_for_http_status(res.status_code),
                    status_code=res.status_code,
                    error_class="auth_failure",
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
                await record_attempt_outcome(
                    response_account.get("email", ""),
                    model,
                    outcome_for_http_status(429),
                    status_code=429,
                    error_class="rate_limited",
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
                await record_attempt_outcome(
                    response_account.get("email", ""),
                    model,
                    outcome_for_http_status(res.status_code),
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
                    await record_attempt_outcome(
                        response_account.get("email", ""),
                        model,
                        outcome_for_backend_error(code, message),
                        status_code=status_code_from_backend_error(code, message),
                        error_class=code,
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
                provider_result = google_transport.parse_response(gemini_resp)
                codex_resp = response_from_result(
                    provider_result,
                    response_id=provider_result.provider_response_id or f"resp_{secrets.token_hex(6)}",
                    model=model,
                    created_at=int(time.time()),
                )
                request_succeeded = provider_result.terminal.kind is not TerminalKind.FAILED
                await record_attempt_outcome(
                    response_account.get("email", ""),
                    model,
                    AttemptOutcome(
                        scope="none",
                        category="success" if request_succeeded else "transport",
                    ),
                    status_code=200,
                    usage=codex_resp.get("usage") if isinstance(codex_resp, dict) else None,
                    error_class=None if request_succeeded else provider_result.terminal.error_code,
                )
                await log_request(
                    "success" if request_succeeded else "failed",
                    model=model,
                    route="google",
                    family=family,
                    stream=False,
                    http_status=200,
                    rotation_attempted=rotation_attempted,
                    usage=codex_resp.get("usage") if isinstance(codex_resp, dict) else None,
                    error_class=None if request_succeeded else provider_result.terminal.error_code,
                    error=None if request_succeeded else provider_result.terminal.error_message,
                    terminal_kind=provider_result.terminal.kind.value,
                    terminal_reason=provider_result.terminal.reason,
                    attempt_count=len(response_attempts),
                    rotation_count=max(0, len(response_attempts) - 1),
                    outcome_category="success" if request_succeeded else "transport",
                    cooldown_scope=cooldown_scope,
                    cooldown_category=cooldown_category,
                )
                return codex_resp
            except HTTPException:
                raise
            except Exception as e:
                await record_attempt_outcome(
                    response_account.get("email", ""),
                    model,
                    AttemptOutcome(scope="none", category="transport"),
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
        finally:
            for used_account in response_attempts:
                await release_account_for_request(used_account.get("email"))

    # Handle standard SSE streaming response path
    stream_attempts = [account]
    recorded_stream_attempts: set[str] = set()

    async def record_stream_attempt(
        selected_account: dict,
        outcome: AttemptOutcome,
        *,
        status_code: int | None = None,
        usage: dict | None = None,
        error_class: str | None = None,
    ) -> None:
        email = selected_account.get("email", "")
        if email in recorded_stream_attempts:
            return
        await record_attempt_outcome(
            email,
            model,
            outcome,
            status_code=status_code,
            usage=usage,
            error_class=error_class,
        )
        recorded_stream_attempts.add(email)

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
        google_stream_accumulator = GoogleResponseAccumulator()

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
                google_stream_accumulator.mark_done()
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
            google_stream_accumulator.consume(parsed)
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
        attempts = stream_attempts
        attempt_num = 0
        while attempt_num < len(attempts):
            stream_retry_requested = False
            stream_account = attempts[attempt_num]
            try:
                async with google_transport.stream(codex_req, account_lease(stream_account)) as res:
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
                await record_stream_attempt(
                    stream_account,
                    outcome_for_backend_error(last_error_code, last_error or ""),
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
                rotated = await acquire_active_account_for_request(model)
                if rotated and rotated.get("email") != stream_account.get("email"):
                    await record_stream_attempt(
                        stream_account,
                        outcome_for_backend_error(last_error_code, last_error or ""),
                        error_class=last_error_code,
                    )
                    usage = None
                    last_error = None
                    last_error_code = "backend_error"
                    attempts.append(rotated)
                    attempt_num += 1
                    continue
                if rotated:
                    await release_account_for_request(rotated.get("email"))
            elif attempt_num == 0:
                rotated = await acquire_active_account_for_request(model)
                if rotated and rotated.get("email") != stream_account.get("email"):
                    await record_stream_attempt(
                        stream_account,
                        outcome_for_backend_error(last_error_code, last_error or ""),
                        error_class=last_error_code,
                    )
                    attempts.append(rotated)
                    attempt_num += 1
                    continue
                if rotated:
                    await release_account_for_request(rotated.get("email"))
            break

        if not completed:
            final_account = attempts[min(attempt_num, len(attempts) - 1)]
            await record_stream_attempt(
                final_account,
                outcome_for_backend_error(last_error_code, last_error or ""),
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
                
        provider_result = google_stream_accumulator.finalize()
        if provider_result.terminal.kind is TerminalKind.FAILED:
            final_account = attempts[min(attempt_num, len(attempts) - 1)]
            error_code = provider_result.terminal.error_code or "backend_error"
            error_message = provider_result.terminal.error_message or "Google Antigravity stream failed"
            await record_stream_attempt(
                final_account,
                AttemptOutcome(scope="none", category="transport"),
                status_code=200,
                error_class=error_code,
            )
            await log_request(
                "failed",
                model=model,
                route="google",
                family=family,
                stream=True,
                http_status=200,
                error_class=error_code,
                error=error_message,
                rotation_attempted=len(attempts) > 1,
                usage=usage,
            )
            async for event in fail_stream(error_code, error_message):
                yield event
            return

        # 4. Final terminal events
        if text_output_index is not None:
            message_item = {'type': 'message', 'id': msg_id, 'status': 'completed', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': output_text, 'annotations': []}]}
            yield stream_event({'type': 'response.output_text.done', 'response_id': response_id, 'item_id': msg_id, 'output_index': text_output_index, 'content_index': 0, 'text': output_text})
            yield stream_event({'type': 'response.content_part.done', 'response_id': response_id, 'item_id': msg_id, 'output_index': text_output_index, 'content_index': 0, 'part': {'type': 'output_text', 'text': output_text, 'annotations': []}})
            yield stream_event({'type': 'response.output_item.done', 'response_id': response_id, 'output_index': text_output_index, 'item': message_item})
            indexed_output_items.append((text_output_index, message_item))
        refusal_output = next(
            (
                item
                for item in provider_result.output
                if item.get("type") == "message"
                and isinstance(item.get("content"), list)
                and item["content"]
                and isinstance(item["content"][0], dict)
                and item["content"][0].get("type") == "refusal"
            ),
            None,
        )
        if refusal_output is not None:
            refusal_index = next_output_index
            next_output_index += 1
            yield stream_event(
                {
                    "type": "response.output_item.added",
                    "response_id": response_id,
                    "output_index": refusal_index,
                    "item": refusal_output,
                }
            )
            yield stream_event(
                {
                    "type": "response.output_item.done",
                    "response_id": response_id,
                    "output_index": refusal_index,
                    "item": refusal_output,
                }
            )
            indexed_output_items.append((refusal_index, refusal_output))
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
            'status': provider_result.terminal.kind.value,
        }
        if provider_result.terminal.kind is TerminalKind.INCOMPLETE:
            done_response["incomplete_details"] = {
                "reason": provider_result.terminal.incomplete_reason or provider_result.terminal.reason
            }
        if usage:
            done_response["usage"] = usage
        final_account = attempts[min(attempt_num, len(attempts) - 1)]
        await record_stream_attempt(
            final_account,
            AttemptOutcome(scope="none", category="success"),
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
            terminal_kind=provider_result.terminal.kind.value,
            terminal_reason=provider_result.terminal.reason,
            attempt_count=len(attempts),
            rotation_count=max(0, len(attempts) - 1),
            outcome_category="success",
        )
        yield stream_event({'type': f'response.{provider_result.terminal.kind.value}', 'response': done_response})
        yield "data: [DONE]\n\n"

    async def managed_sse_generator() -> AsyncGenerator[str, None]:
        try:
            async for chunk in sse_generator():
                yield chunk
        finally:
            cancelled = any(
                used_account.get("email", "") not in recorded_stream_attempts
                for used_account in stream_attempts
            )
            if cancelled:
                await log_request(
                    "cancelled",
                    model=model,
                    route="google",
                    family=family,
                    stream=True,
                    terminal_kind="failed",
                    terminal_reason="cancelled",
                    attempt_count=len(stream_attempts),
                    rotation_count=max(0, len(stream_attempts) - 1),
                    outcome_category="cancelled",
                    cancelled=True,
                    error_class="cancelled",
                )
            for used_account in stream_attempts:
                await record_stream_attempt(
                    used_account,
                    AttemptOutcome(scope="none", category="cancelled"),
                    error_class="cancelled",
                )
                await release_account_for_request(used_account.get("email"))

    return StreamingResponse(managed_sse_generator(), media_type="text/event-stream")


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


def xai_oauth_entitlement_detail(provider_model: str, body: str | None = None) -> str:
    suffix = f": {safe_error_detail(body)}" if body else ""
    return (
        f"xAI OAuth returned HTTP 403 for {provider_model}{suffix}. "
        "Your SuperGrok/X Premium account may not be entitled for this OAuth API surface, "
        "or the grant may need to be refreshed. Run `codex-antigravity provider login xai-oauth` "
        f"again, or use the API-key route `xai:{provider_model}` with XAI_API_KEY."
    )


async def create_xai_oauth_response(codex_req: dict, provider: dict, provider_model: str, display_model: str) -> dict:
    payload, url, headers, timeout = await prepare_xai_oauth_responses_request(
        codex_req,
        provider,
        provider_model,
        stream=False,
    )

    async def post_once(request_headers: dict) -> httpx.Response:
        async with httpx.AsyncClient(timeout=timeout) as client:
            return await client.post(url, json=payload, headers=request_headers)

    try:
        res = await post_once(headers)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"{provider['id']} connection error: {safe_error_detail(e)}") from e
    if res.status_code == 401:
        _payload, _url, refreshed_headers, _timeout = await prepare_xai_oauth_responses_request(
            codex_req,
            provider,
            provider_model,
            stream=False,
            force_refresh=True,
        )
        try:
            res = await post_once(refreshed_headers)
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"{provider['id']} connection error after refresh: {safe_error_detail(e)}") from e
    if res.status_code == 403:
        raise HTTPException(status_code=403, detail=xai_oauth_entitlement_detail(provider_model, res.text))
    if res.status_code != 200:
        raise HTTPException(status_code=res.status_code, detail=f"{provider['id']} API error: {safe_error_detail(res.text)}")
    try:
        payload = res.json()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"{provider['id']} response parsing failed: {safe_error_detail(e)}") from e
    if not isinstance(payload, dict):
        raise HTTPException(status_code=500, detail=f"{provider['id']} response parsing failed: response was not an object")
    try:
        return OpenAICompatibleTransport(timeout=timeout).validate_native_response(
            payload,
            display_model=display_model,
        )
    except ValueError as e:
        raise HTTPException(
            status_code=502,
            detail=f"{provider['id']} returned an invalid Responses payload: {safe_error_detail(e)}",
        ) from e


async def xai_oauth_responses_sse_generator(
    payload: dict,
    url: str,
    headers: dict,
    timeout: float,
    provider: dict,
    provider_model: str,
    codex_req: dict,
) -> AsyncGenerator[str, None]:
    emitted_output = False
    retried = False
    active_headers = headers
    display_model = str(codex_req.get("model") or f"xai-oauth:{provider_model}")
    adapter = NativeResponsesStreamAdapter(display_model=display_model)

    def serialize(event: dict) -> str:
        return "data: " + json.dumps(event) + "\n\n"

    while True:
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                async with client.stream("POST", url, json=payload, headers=active_headers) as res:
                    if res.status_code == 401 and not emitted_output and not retried:
                        retried = True
                        _payload, _url, active_headers, _timeout = await prepare_xai_oauth_responses_request(
                            codex_req,
                            provider,
                            provider_model,
                            stream=True,
                            force_refresh=True,
                        )
                        continue
                    if res.status_code == 403:
                        yield (
                            "data: "
                            + json.dumps(
                                {
                                    "type": "response.failed",
                                    "response": {
                                        "status": "failed",
                                        "model": payload.get("model"),
                                        "error": {
                                            "code": "xai_oauth_forbidden",
                                            "message": xai_oauth_entitlement_detail(provider_model, (await res.aread()).decode("utf-8", errors="ignore")),
                                        },
                                    },
                                }
                            )
                            + "\n\n"
                        )
                        yield "data: [DONE]\n\n"
                        return
                    if res.status_code != 200:
                        body = (await res.aread()).decode("utf-8", errors="ignore")
                        yield (
                            "data: "
                            + json.dumps(
                                {
                                    "type": "response.failed",
                                    "response": {
                                        "status": "failed",
                                        "model": payload.get("model"),
                                        "error": {
                                            "code": "backend_error",
                                            "message": f"{provider['id']} returned HTTP {res.status_code}: {safe_error_detail(body)}",
                                        },
                                    },
                                }
                            )
                            + "\n\n"
                        )
                        yield "data: [DONE]\n\n"
                        return
                    async for raw_chunk in res.aiter_bytes():
                        for event in adapter.consume_bytes(raw_chunk):
                            if event.get("type", "").startswith(("response.output", "response.reasoning")):
                                emitted_output = True
                            yield serialize(event)
                    for event in adapter.finish():
                        yield serialize(event)
                    yield "data: [DONE]\n\n"
                    return
        except Exception as e:
            yield (
                "data: "
                + json.dumps(
                    {
                        "type": "response.failed",
                        "response": {
                            "status": "failed",
                            "model": payload.get("model"),
                            "error": {"code": "connection_error", "message": safe_error_detail(e)},
                        },
                    }
                )
                + "\n\n"
            )
            yield "data: [DONE]\n\n"
            return


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
    chat_stream_accumulator = ChatResponseAccumulator()

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
            chat_stream_accumulator.mark_done()
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
        chat_stream_accumulator.consume(parsed)
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

    provider_result = chat_stream_accumulator.finalize()
    if provider_result.terminal.kind is TerminalKind.FAILED:
        async for event in fail_stream(
            provider_result.terminal.error_code or "backend_error",
            provider_result.terminal.error_message or "Provider stream failed",
        ):
            yield event
        return

    refusal_output = next(
        (
            item
            for item in provider_result.output
            if item.get("type") == "message"
            and isinstance(item.get("content"), list)
            and item["content"]
            and isinstance(item["content"][0], dict)
            and item["content"][0].get("type") == "refusal"
        ),
        None,
    )
    if refusal_output is not None:
        refusal_index = next_output_index
        next_output_index += 1
        yield stream_event(
            {
                "type": "response.output_item.added",
                "response_id": response_id,
                "output_index": refusal_index,
                "item": refusal_output,
            }
        )
        yield stream_event(
            {
                "type": "response.output_item.done",
                "response_id": response_id,
                "output_index": refusal_index,
                "item": refusal_output,
            }
        )
        indexed_output_items.append((refusal_index, refusal_output))

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
        "status": provider_result.terminal.kind.value,
        "model": display_model,
        "output": done_output,
    }
    if usage:
        done_response["usage"] = usage
    if provider_result.terminal.kind is TerminalKind.INCOMPLETE:
        done_response["incomplete_details"] = {
            "reason": provider_result.terminal.incomplete_reason or provider_result.terminal.reason
        }
    yield stream_event({'type': f'response.{provider_result.terminal.kind.value}', 'response': done_response})
    yield "data: [DONE]\n\n"
