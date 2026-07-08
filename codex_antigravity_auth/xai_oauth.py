from __future__ import annotations

import json
import math
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import urlencode

from .constants import get_codex_home
from .oauth import OAUTH_HTTP_TIMEOUT_SECONDS, token_expires_in_seconds
from .redaction import redact_secret_text
from .storage import load_secure_json_file, save_secure_json_file, update_secure_json_file

XAI_OAUTH_FILE = "antigravity-xai-oauth.json"
XAI_OAUTH_CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"
XAI_OAUTH_AUTHORIZE_URL = "https://auth.x.ai/oauth2/authorize"
XAI_OAUTH_TOKEN_URL = "https://auth.x.ai/oauth2/token"
XAI_OAUTH_DEVICE_CODE_URL = "https://auth.x.ai/oauth2/device/code"
XAI_OAUTH_DEVICE_GRANT_TYPE = "urn:ietf:params:oauth:grant-type:device_code"
XAI_OAUTH_SCOPE = "openid profile email offline_access grok-cli:access api:access"
XAI_OAUTH_REDIRECT_URI = "http://127.0.0.1:56121/callback"
XAI_OAUTH_REFRESH_SKEW_SECONDS = 120
XAI_OAUTH_DEVICE_DEFAULT_INTERVAL_SECONDS = 5.0
XAI_OAUTH_DEVICE_MIN_INTERVAL_SECONDS = 1.0
XAI_OAUTH_DEVICE_SLOW_DOWN_SECONDS = 5.0
XAI_OAUTH_DEVICE_DEFAULT_EXPIRES_SECONDS = 300.0


def get_xai_oauth_json_path() -> Path:
    path = get_codex_home() / XAI_OAUTH_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def default_xai_oauth_data() -> dict[str, Any]:
    return {"tokens": {}, "status": {}}


def _safe_string(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value:
        return None
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        return None
    return value


def _safe_epoch(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed) or parsed <= 0:
        return None
    return parsed


def normalize_xai_oauth_data(data: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(data, dict):
        data = {}
    raw_tokens = data.get("tokens")
    tokens = raw_tokens if isinstance(raw_tokens, dict) else {}
    normalized_tokens: dict[str, Any] = {}
    for key in ("accessToken", "refreshToken", "tokenType", "scope"):
        value = _safe_string(tokens.get(key))
        if value:
            normalized_tokens[key] = value
    expires_at = _safe_epoch(tokens.get("expiresAt"))
    if expires_at is not None:
        normalized_tokens["expiresAt"] = expires_at

    raw_status = data.get("status")
    status = raw_status if isinstance(raw_status, dict) else {}
    normalized_status: dict[str, Any] = {}
    for key in ("lastLoginAt", "lastRefreshAt", "updatedAt", "loggedOutAt"):
        value = _safe_epoch(status.get(key))
        if value is not None:
            normalized_status[key] = value
    for key in ("lastErrorClass",):
        value = _safe_string(status.get(key))
        if value:
            normalized_status[key] = value
    return {"tokens": normalized_tokens, "status": normalized_status}


def load_xai_oauth_data() -> dict[str, Any]:
    return load_secure_json_file(
        get_xai_oauth_json_path(),
        default_xai_oauth_data,
        normalize=normalize_xai_oauth_data,
        error_label="xAI OAuth tokens",
    )


def save_xai_oauth_data(data: dict[str, Any]) -> None:
    save_secure_json_file(
        get_xai_oauth_json_path(),
        normalize_xai_oauth_data(data),
        error_label="xAI OAuth tokens",
        default_factory=default_xai_oauth_data,
        normalize=normalize_xai_oauth_data,
    )


def _positive_seconds(value: Any, default: float) -> float:
    if isinstance(value, bool):
        return default
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(parsed) or parsed <= 0:
        return default
    return parsed


def _post_form(url: str, payload: dict[str, Any], timeout: float = OAUTH_HTTP_TIMEOUT_SECONDS) -> tuple[int, dict[str, Any]]:
    req = urllib.request.Request(
        url,
        data=urlencode(payload).encode("utf-8"),
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "codex-antigravity-auth",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            status = int(getattr(resp, "status", 200))
    except urllib.error.HTTPError as exc:
        status = int(exc.code)
        raw = exc.read().decode("utf-8", errors="replace")
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        return 0, {"error": "network_error", "error_description": redact_secret_text(str(reason))}
    try:
        parsed = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        parsed = {"error": "invalid_json", "error_description": raw[:500]}
    if not isinstance(parsed, dict):
        parsed = {"error": "invalid_response", "value": parsed}
    return status, parsed


def _oauth_error(prefix: str, status: int, payload: dict[str, Any]) -> RuntimeError:
    error = payload.get("error") or "oauth_error"
    description = payload.get("error_description") or payload.get("message") or ""
    detail = f"{error}: {description}" if description else str(error)
    return RuntimeError(f"{prefix} failed ({status}): {redact_secret_text(detail)}")


def build_xai_authorize_url(
    pkce: dict[str, str],
    *,
    state: str,
    nonce: str,
    authorize_url: str = XAI_OAUTH_AUTHORIZE_URL,
) -> str:
    params = {
        "response_type": "code",
        "client_id": XAI_OAUTH_CLIENT_ID,
        "redirect_uri": XAI_OAUTH_REDIRECT_URI,
        "scope": XAI_OAUTH_SCOPE,
        "code_challenge": pkce["challenge"],
        "code_challenge_method": "S256",
        "state": state,
        "nonce": nonce,
        "plan": "generic",
        "referrer": "codex-antigravity-auth",
    }
    return f"{authorize_url}?{urlencode(params)}"


def exchange_xai_authorization_code(
    code: str,
    verifier: str,
    *,
    post_form: Callable[[str, dict[str, Any], float], tuple[int, dict[str, Any]]] = _post_form,
    timeout: float = OAUTH_HTTP_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    status, payload = post_form(
        XAI_OAUTH_TOKEN_URL,
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": XAI_OAUTH_REDIRECT_URI,
            "client_id": XAI_OAUTH_CLIENT_ID,
            "code_verifier": verifier,
        },
        timeout,
    )
    if status < 200 or status >= 300:
        raise _oauth_error("xAI token exchange", status, payload)
    return payload


def request_xai_device_code(
    *,
    post_form: Callable[[str, dict[str, Any], float], tuple[int, dict[str, Any]]] = _post_form,
    timeout: float = OAUTH_HTTP_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    status, payload = post_form(
        XAI_OAUTH_DEVICE_CODE_URL,
        {"client_id": XAI_OAUTH_CLIENT_ID, "scope": XAI_OAUTH_SCOPE},
        timeout,
    )
    if status < 200 or status >= 300:
        raise _oauth_error("xAI device code request", status, payload)
    missing = [key for key in ("device_code", "user_code", "verification_uri") if not _safe_string(payload.get(key))]
    if missing:
        raise RuntimeError(f"xAI device code response is missing {', '.join(missing)}")
    return payload


def poll_xai_device_code_token(
    device: dict[str, Any],
    *,
    post_form: Callable[[str, dict[str, Any], float], tuple[int, dict[str, Any]]] = _post_form,
    sleep: Callable[[float], None] = time.sleep,
    now_values: Iterable[float] | None = None,
    timeout: float = OAUTH_HTTP_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    if not _safe_string(device.get("device_code")):
        raise RuntimeError("xAI device code response is missing device_code")
    now_iter = iter(now_values) if now_values is not None else None

    def now() -> float:
        if now_iter is not None:
            try:
                return float(next(now_iter))
            except StopIteration:
                return time.time()
        return time.time()

    expires_in = _positive_seconds(device.get("expires_in"), XAI_OAUTH_DEVICE_DEFAULT_EXPIRES_SECONDS)
    interval = max(
        _positive_seconds(device.get("interval"), XAI_OAUTH_DEVICE_DEFAULT_INTERVAL_SECONDS),
        XAI_OAUTH_DEVICE_MIN_INTERVAL_SECONDS,
    )
    deadline = now() + expires_in

    while now() < deadline:
        status, payload = post_form(
            XAI_OAUTH_TOKEN_URL,
            {
                "grant_type": XAI_OAUTH_DEVICE_GRANT_TYPE,
                "client_id": XAI_OAUTH_CLIENT_ID,
                "device_code": device["device_code"],
            },
            timeout,
        )
        if 200 <= status < 300:
            return payload
        error = str(payload.get("error") or "")
        if error == "authorization_pending":
            sleep(interval)
            continue
        if error == "slow_down":
            interval += XAI_OAUTH_DEVICE_SLOW_DOWN_SECONDS
            sleep(interval)
            continue
        if error in {"access_denied", "authorization_denied"}:
            raise RuntimeError("xAI device authorization was denied")
        if error == "expired_token":
            raise RuntimeError("xAI device authorization timed out")
        raise _oauth_error("xAI device authorization", status, payload)
    raise RuntimeError("xAI device authorization timed out")


def _token_payload_from_response(
    token_response: dict[str, Any],
    *,
    now: float,
    existing_refresh_token: str | None = None,
) -> dict[str, Any]:
    access_token = _safe_string(token_response.get("access_token"))
    if not access_token:
        raise RuntimeError("xAI OAuth response did not include an access token")
    refresh_token = _safe_string(token_response.get("refresh_token")) or existing_refresh_token
    expires_at = now + token_expires_in_seconds(token_response)
    tokens = {
        "accessToken": access_token,
        "expiresAt": expires_at,
        "tokenType": _safe_string(token_response.get("token_type")) or "Bearer",
    }
    if refresh_token:
        tokens["refreshToken"] = refresh_token
    scope = _safe_string(token_response.get("scope"))
    if scope:
        tokens["scope"] = scope
    return tokens


def save_xai_oauth_token_response(
    token_response: dict[str, Any],
    *,
    now: float | None = None,
    refresh: bool = False,
) -> dict[str, Any]:
    timestamp = time.time() if now is None else float(now)
    saved: dict[str, Any] = {}

    def mutate(data: dict[str, Any]) -> None:
        current = normalize_xai_oauth_data(data)
        existing_refresh = current.get("tokens", {}).get("refreshToken")
        tokens = _token_payload_from_response(
            token_response,
            now=timestamp,
            existing_refresh_token=existing_refresh,
        )
        status = current.get("status", {})
        status["updatedAt"] = timestamp
        if refresh:
            status["lastRefreshAt"] = timestamp
        else:
            status["lastLoginAt"] = timestamp
        status.pop("lastErrorClass", None)
        data.clear()
        data.update({"tokens": tokens, "status": status})
        saved.update(data)

    update_secure_json_file(
        get_xai_oauth_json_path(),
        default_xai_oauth_data,
        mutate,
        normalize=normalize_xai_oauth_data,
        error_label="xAI OAuth tokens",
    )
    return normalize_xai_oauth_data(saved)


def clear_xai_oauth_tokens(*, now: float | None = None) -> bool:
    timestamp = time.time() if now is None else float(now)

    def mutate(data: dict[str, Any]) -> bool:
        current = normalize_xai_oauth_data(data)
        existed = bool(current.get("tokens"))
        data.clear()
        data.update({"tokens": {}, "status": {"loggedOutAt": timestamp, "updatedAt": timestamp}})
        return existed

    return bool(
        update_secure_json_file(
            get_xai_oauth_json_path(),
            default_xai_oauth_data,
            mutate,
            normalize=normalize_xai_oauth_data,
            error_label="xAI OAuth tokens",
        )
    )


def xai_token_is_expiring(tokens: dict[str, Any], *, now: float | None = None, skew_seconds: float = XAI_OAUTH_REFRESH_SKEW_SECONDS) -> bool:
    access_token = _safe_string(tokens.get("accessToken"))
    if not access_token:
        return True
    expires_at = _safe_epoch(tokens.get("expiresAt"))
    if expires_at is None:
        return True
    timestamp = time.time() if now is None else float(now)
    return expires_at <= timestamp + max(0.0, float(skew_seconds))


def refresh_xai_oauth_access_token(
    refresh_token: str,
    *,
    post_form: Callable[[str, dict[str, Any], float], tuple[int, dict[str, Any]]] = _post_form,
    timeout: float = OAUTH_HTTP_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    status, payload = post_form(
        XAI_OAUTH_TOKEN_URL,
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": XAI_OAUTH_CLIENT_ID,
        },
        timeout,
    )
    if status < 200 or status >= 300:
        raise _oauth_error("xAI token refresh", status, payload)
    return payload


def resolve_xai_oauth_access_token(
    *,
    force_refresh: bool = False,
    now: float | None = None,
    post_form: Callable[[str, dict[str, Any], float], tuple[int, dict[str, Any]]] = _post_form,
) -> str:
    timestamp = time.time() if now is None else float(now)
    data = load_xai_oauth_data()
    tokens = data.get("tokens", {})
    access_token = _safe_string(tokens.get("accessToken"))
    if access_token and not force_refresh and not xai_token_is_expiring(tokens, now=timestamp):
        return access_token
    refresh_token = _safe_string(tokens.get("refreshToken"))
    if not refresh_token:
        raise RuntimeError("xAI OAuth is not logged in. Run `codex-antigravity provider login xai-oauth`.")
    refreshed = refresh_xai_oauth_access_token(refresh_token, post_form=post_form)
    saved = save_xai_oauth_token_response(refreshed, now=timestamp, refresh=True)
    refreshed_access = _safe_string(saved.get("tokens", {}).get("accessToken"))
    if not refreshed_access:
        raise RuntimeError("xAI token refresh did not produce a usable access token")
    return refreshed_access


def xai_oauth_status(*, now: float | None = None) -> dict[str, Any]:
    timestamp = time.time() if now is None else float(now)
    try:
        data = load_xai_oauth_data()
    except RuntimeError as exc:
        return {
            "provider": "xai-oauth",
            "auth_mode": "oauth",
            "ready": False,
            "configured": False,
            "error": redact_secret_text(str(exc)),
        }
    tokens = data.get("tokens", {})
    access_token = _safe_string(tokens.get("accessToken"))
    refresh_token = _safe_string(tokens.get("refreshToken"))
    expires_at = _safe_epoch(tokens.get("expiresAt"))
    expires_in = None if expires_at is None else int(expires_at - timestamp)
    expired = expires_at is not None and expires_at <= timestamp
    refreshable = bool(refresh_token)
    configured = bool(access_token or refresh_token)
    return {
        "provider": "xai-oauth",
        "auth_mode": "oauth",
        "ready": configured and (bool(access_token and not expired) or refreshable),
        "configured": configured,
        "has_access_token": bool(access_token),
        "has_refresh_token": refreshable,
        "expires_at": expires_at,
        "expires_in_seconds": expires_in,
        "expired": bool(expired),
        "path": str(get_xai_oauth_json_path()),
    }
