"""Optional unified Codex model picker: OpenAI + Antigravity via one gateway.

Classic mode (default) preserves the historical behaviour: ``/v1/models``
advertises Antigravity native models (+ overlays) and BYOK providers, and
every non-``provider:model`` request routes to the Google Antigravity backend.

Unified mode (opt-in via ``--unified-model-picker`` /
``ANTIGRAVITY_UNIFIED_MODEL_PICKER=1``) turns the gateway into a single
provider/router seen by Codex::

    Codex (model_provider = antigravity-unified)
      -> gateway
        -> Antigravity (Claude/Gemini) for native models
        -> OpenAI upstream for gpt-*/codex models
        -> existing BYOK routing for provider:model ids

The router is registry-based (no fragile ``startswith`` cascades):

* BYOK: ``split_provider_model()`` prefix (existing registry).
* Antigravity: ``native_model_definition()`` (built-ins + overlays).
* OpenAI: curated registry below, extendable via
  ``ANTIGRAVITY_OPENAI_MODELS`` (comma-separated).

OpenAI authentication deliberately avoids copying Codex internals by default:

* Preferred: explicit ``OPENAI_API_KEY`` (env) or
  ``~/.codex/antigravity-openai.json`` (``{"api_key": ..., "base_url": ...}``)
  proxied to ``https://api.openai.com/v1/responses`` (Responses API native).
* Optional ChatGPT-subscription reuse: ``ANTIGRAVITY_OPENAI_USE_CODEX_AUTH=1``
  reads ``~/.codex/auth.json`` (or ``$CODEX_HOME/auth.json``) read-only and
  proxies to ``https://chatgpt.com/backend-api/codex/responses``. No refresh
  is attempted; an expired token surfaces as a clear 401 telling the user to
  run ``codex login`` again. This path is explicit because the auth file
  format is owned by Codex and may change.

No access tokens are ever logged; all error paths use redaction helpers.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .redaction import redact_secret_text


UNIFIED_ENV_VAR = "ANTIGRAVITY_UNIFIED_MODEL_PICKER"
OPENAI_API_KEY_ENV = "OPENAI_API_KEY"
OPENAI_BASE_URL_ENV = "OPENAI_BASE_URL"
OPENAI_MODELS_ENV = "ANTIGRAVITY_OPENAI_MODELS"
OPENAI_USE_CODEX_AUTH_ENV = "ANTIGRAVITY_OPENAI_USE_CODEX_AUTH"
OPENAI_CONFIG_FILE = "~/.codex/antigravity-openai.json"

DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"
CODEX_UPSTREAM_RESPONSES_URL = "https://chatgpt.com/backend-api/codex/responses"

OPENAI_UPSTREAM_TIMEOUT_SECONDS = 120.0


@dataclass(frozen=True)
class OpenAIModel:
    id: str
    display_name: str
    context_window: int


# Curated Codex/OpenAI ids actually reachable through the OpenAI upstream.
# Extend without code changes via ANTIGRAVITY_OPENAI_MODELS="gpt-5.6,my-model".
# (No single reliable dynamic source covers both the API-key path and the
# ChatGPT-subscription path, hence an explicit registry + env override.)
DEFAULT_OPENAI_MODELS: tuple[OpenAIModel, ...] = (
    OpenAIModel(id="gpt-5.6", display_name="GPT-5.6", context_window=400_000),
    OpenAIModel(id="gpt-5.6-codex", display_name="GPT-5.6 Codex", context_window=400_000),
    OpenAIModel(id="gpt-5.5", display_name="GPT-5.5", context_window=400_000),
    OpenAIModel(id="gpt-5.4", display_name="GPT-5.4", context_window=400_000),
)


class OpenAIUpstreamAuthError(ValueError):
    """Raised when OpenAI upstream credentials are missing or invalid."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class OpenAIAuth:
    kind: str  # "api_key" | "codex_oauth"
    base_url: str | None = None  # api_key path only
    api_key: str | None = None  # api_key path only
    access_token: str | None = None  # codex_oauth path only
    account_id: str | None = None  # codex_oauth path only


def _env_truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def is_unified_mode_enabled() -> bool:
    """Return True when the unified picker/router is explicitly opted in."""
    return _env_truthy(UNIFIED_ENV_VAR)


def _normalize_id(value: object) -> str:
    return str(value or "").strip().lower()


def list_openai_models() -> list[OpenAIModel]:
    """Return the OpenAI registry, honouring ANTIGRAVITY_OPENAI_MODELS override."""
    raw = os.environ.get(OPENAI_MODELS_ENV, "").strip()
    if not raw:
        return list(DEFAULT_OPENAI_MODELS)
    models: list[OpenAIModel] = []
    seen: set[str] = set()
    for chunk in raw.split(","):
        model_id = chunk.strip()
        if not model_id:
            continue
        if any(ch.isspace() or ord(ch) < 0x20 or ord(ch) == 0x7F for ch in model_id):
            continue
        if len(model_id) > 128:
            continue
        lowered = model_id.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        # Preserve curated metadata when the override names a known id.
        known = next((m for m in DEFAULT_OPENAI_MODELS if m.id.lower() == lowered), None)
        if known is not None:
            models.append(known)
        else:
            models.append(OpenAIModel(id=model_id, display_name=model_id, context_window=400_000))
    return models or list(DEFAULT_OPENAI_MODELS)


def openai_model_ids() -> set[str]:
    return {_normalize_id(m.id) for m in list_openai_models()}


def strip_reserved_openai_prefix(model: object) -> str:
    """Strip explicit ``openai:`` / ``openai-responses:`` prefixes if present."""
    text = str(model or "").strip()
    lowered = text.lower()
    for prefix in ("openai-responses:", "openai-responses/", "openai:", "openai/"):
        if lowered.startswith(prefix):
            return text[len(prefix):].strip()
    return text


def is_openai_model(model: object) -> bool:
    """Exact registry match (no prefix heuristics) for OpenAI/Codex ids."""
    text = _normalize_id(strip_reserved_openai_prefix(model))
    if not text or ":" in text or "/" in text:
        return False
    return text in openai_model_ids()


def is_antigravity_model(model: object) -> bool:
    """True when the id resolves to a native (built-in + overlay) definition."""
    # Local import to avoid a hard cycle at module import time.
    from .models import native_model_definition

    try:
        return native_model_definition(str(model)) is not None
    except Exception:
        return False


def is_byok_model(model: object) -> bool:
    """True when the id carries an explicit BYOK provider prefix."""
    from .byok import split_provider_model

    try:
        provider_id, _ = split_provider_model(str(model))
    except Exception:
        return False
    return provider_id is not None


def classify_route(model: object, *, unified_enabled: bool | None = None) -> str:
    """Central router: ``byok`` | ``openai`` | ``antigravity`` | ``unknown``.

    ``openai-disabled`` is returned when an OpenAI id is requested while
    unified mode is off, so callers can hint at ``--unified-model-picker``
    instead of misrouting to Antigravity.
    """
    if unified_enabled is None:
        unified_enabled = is_unified_mode_enabled()
    text = str(model or "")
    # Reserved OpenAI prefixes (openai:model) are explicit OpenAI routing,
    # not BYOK, mirroring models.RESERVED_GOOGLE_MODEL_PREFIXES semantics.
    stripped = strip_reserved_openai_prefix(text)
    if stripped != text.strip():
        antigravity_stripped = is_antigravity_model(stripped)
        openai_stripped = is_openai_model(stripped)
        if openai_stripped and not antigravity_stripped:
            return "openai" if unified_enabled else "openai-disabled"
        if antigravity_stripped:
            return "antigravity"
        return "unknown" if unified_enabled else "antigravity"
    if is_byok_model(text):
        return "byok"
    # Registry-based, no startswith cascade. Antigravity wins on overlap
    # (e.g. an overlay shadowing an OpenAI id) and is documented as such.
    antigravity = is_antigravity_model(text)
    openai = is_openai_model(text)
    if antigravity:
        return "antigravity"
    if openai:
        return "openai" if unified_enabled else "openai-disabled"
    if unified_enabled:
        return "unknown"
    # Classic passthrough: unknown direct backend ids still reach Google,
    # preserving historical behaviour for users who never opt in.
    return "antigravity"


def openai_catalog() -> list[dict[str, Any]]:
    """Catalog entries for /v1/models in unified mode (no secrets)."""
    entries: list[dict[str, Any]] = []
    for model in list_openai_models():
        entries.append(
            {
                "id": model.id,
                "display_name": model.display_name,
                "context_window": model.context_window,
                "family": "openai",
                "default_reasoning_level": "high",
                "supports_parallel_tool_calls": True,
            }
        )
    return entries


def _codex_home() -> Path:
    override = os.environ.get("CODEX_HOME", "").strip()
    if override:
        return Path(override).expanduser()
    return Path(os.path.expanduser("~/.codex"))


def _read_json_file(path: Path) -> dict[str, Any] | None:
    try:
        if not path.is_file() or path.is_symlink():
            return None
        text = path.read_text(encoding="utf-8")
    except Exception:
        return None
    try:
        data = json.loads(text)
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _validate_api_key(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if any(ord(ch) < 0x21 or ord(ch) > 0x7E for ch in text):
        return None
    return text


def resolve_openai_auth() -> OpenAIAuth:
    """Resolve explicit OpenAI upstream credentials (never logs secrets)."""
    api_key = _validate_api_key(os.environ.get(OPENAI_API_KEY_ENV))
    base_url_raw = os.environ.get(OPENAI_BASE_URL_ENV, "").strip()
    if api_key:
        base_url = _validate_base_url_or_default(base_url_raw)
        return OpenAIAuth(kind="api_key", base_url=base_url, api_key=api_key)

    config = _read_json_file(Path(os.path.expanduser(OPENAI_CONFIG_FILE)))
    if config:
        file_key = _validate_api_key(config.get("api_key") or config.get("apiKey"))
        if file_key:
            file_base = config.get("base_url") or config.get("baseUrl") or ""
            return OpenAIAuth(
                kind="api_key",
                base_url=_validate_base_url_or_default(str(file_base or "").strip()),
                api_key=file_key,
            )

    if _env_truthy(OPENAI_USE_CODEX_AUTH_ENV):
        return _resolve_codex_oauth_auth()

    raise OpenAIUpstreamAuthError(
        401,
        "OpenAI upstream authentication is not configured. "
        "Set OPENAI_API_KEY (or ~/.codex/antigravity-openai.json with an api_key), "
        "or opt into ChatGPT subscription reuse with "
        "ANTIGRAVITY_OPENAI_USE_CODEX_AUTH=1 after `codex login`.",
    )


def _validate_base_url_or_default(raw: str) -> str:
    if not raw:
        return DEFAULT_OPENAI_BASE_URL
    # Reuse BYOK URL validation so unified stays consistent with providers.
    from .byok import validate_http_base_url

    try:
        return validate_http_base_url(raw, label="OpenAI upstream base URL")
    except ValueError as exc:
        raise OpenAIUpstreamAuthError(400, str(exc)) from exc


def _resolve_codex_oauth_auth() -> OpenAIAuth:
    """Read Codex ChatGPT credentials read-only (no refresh, no writes)."""
    candidates = [
        _codex_home() / "auth.json",
        Path(os.path.expanduser("~/.codex/auth.json")),
    ]
    data: dict[str, Any] | None = None
    for path in candidates:
        data = _read_json_file(path)
        if data:
            break
    if not data:
        raise OpenAIUpstreamAuthError(
            401,
            "Codex ChatGPT auth was requested (ANTIGRAVITY_OPENAI_USE_CODEX_AUTH=1) "
            "but no readable ~/.codex/auth.json was found. Run `codex login` first.",
        )
    # Codex currently stores credentials under ``tokens``. Keep the older
    # observed ``OPENAI_API_KEY`` dictionary shape as a compatibility fallback.
    nested = data.get("tokens")
    if not isinstance(nested, dict):
        nested = data.get("OPENAI_API_KEY")
    if isinstance(nested, dict):
        data = {**data, **nested}
    access_token = data.get("access_token") or data.get("accessToken")
    account_id = data.get("account_id") or data.get("accountId")
    if not isinstance(access_token, str) or not access_token.strip():
        raise OpenAIUpstreamAuthError(
            401,
            "Codex ChatGPT auth file did not contain a usable access token. "
            "Run `codex login` again.",
        )
    account = str(account_id).strip() if isinstance(account_id, str) and account_id.strip() else None
    return OpenAIAuth(kind="codex_oauth", access_token=access_token.strip(), account_id=account)


def openai_responses_url(auth: OpenAIAuth) -> str:
    if auth.kind == "codex_oauth":
        return CODEX_UPSTREAM_RESPONSES_URL
    base = (auth.base_url or DEFAULT_OPENAI_BASE_URL).rstrip("/")
    return base if base.endswith("/responses") else f"{base}/responses"


def openai_request_headers(auth: OpenAIAuth) -> dict[str, str]:
    if auth.kind == "codex_oauth":
        headers = {
            "Authorization": f"Bearer {auth.access_token}",
            "Content-Type": "application/json",
            "originator": "codex_cli_rs",
        }
        if auth.account_id:
            headers["ChatGPT-Account-Id"] = auth.account_id
        return headers
    return {
        "Authorization": f"Bearer {auth.api_key}",
        "Content-Type": "application/json",
    }


def build_openai_payload(codex_req: dict[str, Any], model: str, *, stream: bool) -> dict[str, Any]:
    """Native Responses passthrough (no chat translation needed)."""
    payload = dict(codex_req)
    payload.pop("metadata", None)
    payload["model"] = model
    payload["stream"] = stream
    return payload


def openai_auth_status() -> dict[str, Any]:
    """Health-safe OpenAI readiness (never includes secrets)."""
    try:
        auth = resolve_openai_auth()
    except OpenAIUpstreamAuthError as exc:
        return {"configured": False, "http_status": exc.status_code, "detail": str(exc)}
    except Exception as exc:  # pragma: no cover - defensive
        return {"configured": False, "http_status": 500, "detail": redact_secret_text(str(exc))}
    if auth.kind == "codex_oauth":
        return {"configured": True, "kind": "codex_oauth", "account_present": bool(auth.account_id)}
    return {"configured": True, "kind": "api_key", "base_url": auth.base_url}


def unknown_model_error(model: str) -> dict[str, Any]:
    return {
        "message": (
            f"Unknown model '{model}'. In unified mode only Antigravity native models, "
            "configured OpenAI/Codex models, and configured BYOK provider:model ids are routed."
        ),
        "route": "unknown",
    }


def openai_disabled_error(model: str) -> dict[str, Any]:
    return {
        "message": (
            f"OpenAI model '{model}' requires the unified model picker. "
            "Restart the gateway with --unified-model-picker "
            "(ANTIGRAVITY_UNIFIED_MODEL_PICKER=1) and configure an OpenAI upstream."
        ),
        "route": "openai-disabled",
    }
