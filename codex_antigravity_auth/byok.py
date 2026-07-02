import os
import re
import math
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .storage import load_secure_json_file, save_secure_json_file, update_secure_json_file


PROVIDERS_FILE = "~/.codex/antigravity-providers.json"
PROVIDER_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
ENV_VAR_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
HTTP_HEADER_NAME_RE = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
RESERVED_PROVIDER_HEADER_NAMES = {
    "authorization",
    "proxy-authorization",
    "content-type",
    "content-length",
    "host",
    "transfer-encoding",
}


PROVIDER_PRESETS: dict[str, dict[str, Any]] = {
    "openrouter": {
        "displayName": "OpenRouter",
        "kind": "openai_chat",
        "baseUrl": "https://openrouter.ai/api/v1",
        "apiKeyEnv": "OPENROUTER_API_KEY",
        "models": ["openrouter/auto"],
        "headers": {
            "HTTP-Referer": "https://github.com/Reedtrullz/codex-antigravity-auth",
            "X-Title": "Codex Antigravity Auth",
        },
    },
    "deepseek": {
        "displayName": "DeepSeek",
        "kind": "openai_chat",
        "baseUrl": "https://api.deepseek.com",
        "apiKeyEnv": "DEEPSEEK_API_KEY",
        "models": ["deepseek-v4-flash", "deepseek-v4-pro", "deepseek-chat", "deepseek-reasoner"],
    },
    "xai": {
        "displayName": "xAI",
        "kind": "openai_chat",
        "baseUrl": "https://api.x.ai/v1",
        "apiKeyEnv": "XAI_API_KEY",
        "models": ["grok-4.3", "grok-code-fast-1"],
    },
    "kimi": {
        "displayName": "Kimi",
        "kind": "openai_chat",
        "baseUrl": "https://api.moonshot.ai/v1",
        "apiKeyEnv": "KIMI_API_KEY",
        "apiKeyEnvAliases": ["MOONSHOT_API_KEY"],
        "models": ["kimi-k2-0711-preview", "kimi-k2-turbo-preview"],
    },
    "ollama": {
        "displayName": "Ollama",
        "kind": "openai_chat",
        "baseUrl": "http://localhost:11434/v1",
        "cloudBaseUrl": "https://ollama.com/v1",
        "apiKeyEnv": "OLLAMA_API_KEY",
        "models": ["gpt-oss:20b", "qwen3:8b"],
        "apiKeyOptional": True,
        "defaultApiKey": "ollama",
    },
    "opencode": {
        "displayName": "OpenCode-compatible",
        "kind": "openai_chat",
        "baseUrl": "http://localhost:4096/v1",
        "apiKeyEnv": "OPENCODE_API_KEY",
        "models": [],
    },
    "custom": {
        "displayName": "Custom OpenAI-compatible",
        "kind": "openai_chat",
        "baseUrl": "http://localhost:8000/v1",
        "apiKeyEnv": "OPENAI_COMPATIBLE_API_KEY",
        "models": [],
        "apiKeyOptional": True,
    },
}


def get_providers_json_path() -> Path:
    p = Path(os.path.expanduser(PROVIDERS_FILE))
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def default_provider_config() -> dict[str, Any]:
    return {"providers": {}}


def validate_provider_id(provider_id: str) -> str:
    provider_id = str(provider_id)
    if not PROVIDER_ID_RE.fullmatch(provider_id):
        raise ValueError("BYOK provider id may only contain letters, numbers, underscores, and hyphens")
    return provider_id


def valid_provider_id(provider_id: str) -> bool:
    return bool(PROVIDER_ID_RE.fullmatch(str(provider_id)))


def validate_http_base_url(base_url: Any, *, label: str = "base URL") -> str:
    value = _non_empty_string(base_url)
    if not value:
        raise ValueError(f"{label} must be a non-empty absolute http(s) URL")
    value = value.rstrip("/")
    if any(ch.isspace() or ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        raise ValueError(f"{label} must not contain whitespace or control characters")
    try:
        parsed = urlparse(value)
        hostname = parsed.hostname
    except ValueError as e:
        raise ValueError(f"{label} must be an absolute http(s) URL") from e
    try:
        port = parsed.port
    except ValueError as e:
        raise ValueError(f"{label} must include a valid port if a port is specified") from e
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or not hostname:
        raise ValueError(f"{label} must be an absolute http(s) URL")
    if ":" in hostname:
        valid_ipv6_netloc = f"[{hostname}]" + (f":{port}" if port is not None else "")
        if parsed.netloc.lower() != valid_ipv6_netloc.lower():
            raise ValueError(f"{label} must be an absolute http(s) URL")
    if parsed.username or parsed.password:
        raise ValueError(f"{label} must not include username or password")
    if parsed.query or parsed.fragment:
        raise ValueError(f"{label} must not include query strings or fragments")
    return value


def _non_empty_string(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def _contains_control_character(value: str) -> bool:
    return any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value)


def validate_provider_api_key(api_key: Any) -> str | None:
    if api_key is None:
        return None
    if not isinstance(api_key, str):
        raise ValueError("BYOK provider API key must be a string")
    value = api_key.strip()
    if not value:
        return ""
    if _contains_control_character(value):
        raise ValueError("BYOK provider API key must not contain control characters")
    return value


def validate_provider_api_key_env(env_name: Any) -> str | None:
    if env_name is None:
        return None
    if not isinstance(env_name, str):
        raise ValueError("BYOK provider API key env var name must be a string")
    value = env_name.strip()
    if not value:
        return ""
    if not ENV_VAR_NAME_RE.fullmatch(value):
        raise ValueError(
            "BYOK provider API key env var name must contain only letters, numbers, "
            "and underscores, and must not start with a number"
        )
    return value


def validate_provider_display_name(display_name: Any) -> str | None:
    if display_name is None:
        return None
    if not isinstance(display_name, str):
        raise ValueError("BYOK provider display name must be a string")
    value = display_name.strip()
    if not value:
        return ""
    if _contains_control_character(value):
        raise ValueError("BYOK provider display name must not contain control characters")
    return value


def validate_provider_model_id(model_id: Any) -> str | None:
    if model_id is None:
        return None
    if not isinstance(model_id, str):
        raise ValueError("BYOK provider model id must be a string")
    value = model_id.strip()
    if not value:
        return ""
    if any(ch.isspace() or ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        raise ValueError("BYOK provider model ids must not contain whitespace or control characters")
    return value


def validate_provider_models(models: list[str] | None) -> list[str] | None:
    if models is None:
        return None
    if not isinstance(models, list):
        raise ValueError("BYOK provider models must be a list")
    normalized: list[str] = []
    for model in models:
        model_id = validate_provider_model_id(model)
        if model_id:
            normalized.append(model_id)
    return normalized


def _normalize_models(value: Any) -> list[Any]:
    if isinstance(value, (str, dict)):
        raw_models = [value]
    elif isinstance(value, list):
        raw_models = value
    else:
        return []

    models: list[Any] = []
    for model in raw_models:
        if isinstance(model, str):
            try:
                model_id = validate_provider_model_id(model)
            except ValueError:
                model_id = None
            if model_id:
                models.append(model_id)
            continue
        if isinstance(model, dict):
            try:
                model_id = validate_provider_model_id(model.get("id"))
            except ValueError:
                model_id = None
            if not model_id:
                continue
            normalized_model = dict(model)
            normalized_model["id"] = model_id
            for display_key in ("displayName", "display_name"):
                if display_key in normalized_model:
                    try:
                        display_name = validate_provider_display_name(normalized_model.get(display_key))
                    except ValueError:
                        display_name = None
                    if display_name:
                        normalized_model[display_key] = display_name
                    else:
                        normalized_model.pop(display_key, None)
            for context_key in ("context_window", "contextWindow"):
                if context_key in normalized_model:
                    context_window = normalized_model.get(context_key)
                    if (
                        not isinstance(context_window, int)
                        or isinstance(context_window, bool)
                        or context_window <= 0
                    ):
                        normalized_model.pop(context_key, None)
            models.append(normalized_model)
    return models


def _normalize_headers(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    headers: dict[str, str] = {}
    for key, raw_value in value.items():
        if key is None or raw_value is None:
            continue
        header_name = str(key).strip()
        header_value = str(raw_value).strip()
        if (
            not header_name
            or not header_value
            or not HTTP_HEADER_NAME_RE.fullmatch(header_name)
            or header_name.lower() in RESERVED_PROVIDER_HEADER_NAMES
            or _contains_control_character(header_value)
        ):
            continue
        headers[header_name] = header_value
    return headers


def validate_provider_headers(headers: dict[str, Any] | None) -> dict[str, str] | None:
    if headers is None:
        return None
    if not isinstance(headers, dict):
        raise ValueError("BYOK provider headers must be a mapping")

    normalized: dict[str, str] = {}
    reserved = ", ".join(sorted(RESERVED_PROVIDER_HEADER_NAMES))
    for key, raw_value in headers.items():
        header_name = str(key).strip()
        header_value = str(raw_value).strip()
        if not header_name or not HTTP_HEADER_NAME_RE.fullmatch(header_name):
            raise ValueError("BYOK provider header names must be valid HTTP header names")
        if header_name.lower() in RESERVED_PROVIDER_HEADER_NAMES:
            raise ValueError(f"BYOK provider headers must not override managed headers: {reserved}")
        if not header_value or _contains_control_character(header_value):
            raise ValueError("BYOK provider header values must be non-empty and must not contain control characters")
        normalized[header_name] = header_value
    return normalized or None


def normalize_provider_entry(provider: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(provider)

    if "kind" in normalized:
        kind = _non_empty_string(normalized.get("kind"))
        if kind == "openai_chat":
            normalized["kind"] = kind
        else:
            normalized.pop("kind", None)
    if "displayName" in normalized:
        try:
            display_name = validate_provider_display_name(normalized.get("displayName"))
        except ValueError:
            display_name = None
        if display_name:
            normalized["displayName"] = display_name
        else:
            normalized.pop("displayName", None)
    if "baseUrl" in normalized:
        base_url = _non_empty_string(normalized.get("baseUrl"))
        if base_url:
            try:
                normalized["baseUrl"] = validate_http_base_url(base_url, label="BYOK provider baseUrl")
            except ValueError:
                normalized.pop("baseUrl", None)
        else:
            normalized.pop("baseUrl", None)
    if "apiKeyEnv" in normalized:
        try:
            api_key_env = validate_provider_api_key_env(normalized.get("apiKeyEnv"))
        except ValueError:
            api_key_env = None
        if api_key_env:
            normalized["apiKeyEnv"] = api_key_env
        else:
            normalized.pop("apiKeyEnv", None)
    if "apiKey" in normalized:
        try:
            api_key = validate_provider_api_key(normalized.get("apiKey"))
        except ValueError:
            api_key = None
        if api_key:
            normalized["apiKey"] = api_key
        else:
            normalized.pop("apiKey", None)
    aliases = normalized.get("apiKeyEnvAliases")
    if "apiKeyEnvAliases" in normalized:
        if isinstance(aliases, str):
            aliases = [aliases]
        if isinstance(aliases, list):
            normalized_aliases = []
            for alias in aliases:
                try:
                    normalized_alias = validate_provider_api_key_env(alias)
                except ValueError:
                    normalized_alias = None
                if normalized_alias:
                    normalized_aliases.append(normalized_alias)
            if normalized_aliases:
                normalized["apiKeyEnvAliases"] = normalized_aliases
            else:
                normalized.pop("apiKeyEnvAliases", None)
        else:
            normalized.pop("apiKeyEnvAliases", None)
    if "models" in normalized:
        normalized["models"] = _normalize_models(normalized.get("models"))
    headers = normalized.get("headers")
    if "headers" in normalized:
        normalized_headers = _normalize_headers(headers)
        if normalized_headers:
            normalized["headers"] = normalized_headers
        else:
            normalized.pop("headers", None)
    if "timeout" in normalized:
        timeout = normalized.get("timeout")
        if (
            not isinstance(timeout, (int, float))
            or isinstance(timeout, bool)
            or not math.isfinite(float(timeout))
            or float(timeout) <= 0
        ):
            normalized.pop("timeout", None)
    if "apiKeyOptional" in normalized and not isinstance(normalized.get("apiKeyOptional"), bool):
        normalized.pop("apiKeyOptional", None)

    return normalized


def normalize_provider_config(data: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(data, dict):
        data = {}
    providers = data.get("providers")
    if not isinstance(providers, dict):
        data["providers"] = {}
    else:
        normalized_providers = {}
        for provider_id, provider in providers.items():
            provider_id = str(provider_id)
            if not valid_provider_id(provider_id) or not isinstance(provider, dict):
                continue
            normalized = normalize_provider_entry(provider)
            if provider_id not in PROVIDER_PRESETS and not _non_empty_string(normalized.get("baseUrl")):
                continue
            normalized_providers[provider_id] = normalized
        data["providers"] = normalized_providers
    return data


def load_provider_config() -> dict[str, Any]:
    path = get_providers_json_path()
    return load_secure_json_file(
        path,
        default_provider_config,
        normalize=normalize_provider_config,
        error_label="BYOK providers",
    )


def save_provider_config(data: dict[str, Any]) -> None:
    path = get_providers_json_path()
    save_secure_json_file(
        path,
        normalize_provider_config(data),
        error_label="BYOK providers",
        default_factory=default_provider_config,
        normalize=normalize_provider_config,
    )


def provider_preset(provider_id: str) -> dict[str, Any]:
    preset = PROVIDER_PRESETS.get(provider_id)
    if not preset:
        raise ValueError(f"Unknown provider preset: {provider_id}")
    return dict(preset)


def resolve_api_key(provider: dict[str, Any]) -> str | None:
    api_key = _non_empty_string(provider.get("apiKey"))
    if api_key:
        return api_key
    env_names = []
    if provider.get("apiKeyEnv"):
        env_names.append(provider["apiKeyEnv"])
    env_names.extend(provider.get("apiKeyEnvAliases", []))
    for env_name in env_names:
        if os.environ.get(env_name):
            return os.environ[env_name]
    if provider.get("apiKeyOptional"):
        return provider.get("defaultApiKey", "not-needed")
    return None


def has_provider_api_key_env(provider: dict[str, Any]) -> bool:
    env_names = []
    if provider.get("apiKeyEnv"):
        env_names.append(provider["apiKeyEnv"])
    env_names.extend(provider.get("apiKeyEnvAliases", []))
    return any(os.environ.get(env_name) for env_name in env_names)


def merged_provider_config(provider_id: str, stored: dict[str, Any] | None = None) -> dict[str, Any]:
    if provider_id in PROVIDER_PRESETS:
        merged = provider_preset(provider_id)
    else:
        merged = {"displayName": provider_id, "kind": "openai_chat", "models": []}
    if stored:
        merged.update(stored)
    merged["id"] = provider_id
    return merged


def all_provider_configs(include_env_enabled: bool = True) -> dict[str, dict[str, Any]]:
    data = load_provider_config()
    providers: dict[str, dict[str, Any]] = {}
    for provider_id, stored in data.get("providers", {}).items():
        if isinstance(stored, dict):
            providers[provider_id] = merged_provider_config(provider_id, stored)

    if include_env_enabled:
        for provider_id, preset in PROVIDER_PRESETS.items():
            if provider_id in providers:
                continue
            merged = merged_provider_config(provider_id, None)
            if has_provider_api_key_env(merged):
                providers[provider_id] = merged

    return providers


def set_provider_config(
    provider_id: str,
    *,
    api_key: str | None = None,
    api_key_env: str | None = None,
    base_url: str | None = None,
    models: list[str] | None = None,
    display_name: str | None = None,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    provider_id = validate_provider_id(provider_id)
    if base_url is not None:
        base_url = validate_http_base_url(base_url, label="BYOK provider base URL")
    elif provider_id not in PROVIDER_PRESETS:
        existing_provider = load_provider_config().get("providers", {}).get(provider_id, {})
        existing_base_url = existing_provider.get("baseUrl") if isinstance(existing_provider, dict) else None
        if not _non_empty_string(existing_base_url):
            raise ValueError("BYOK provider base URL is required for custom providers")
    api_key = validate_provider_api_key(api_key)
    api_key_env = validate_provider_api_key_env(api_key_env)
    models = validate_provider_models(models)
    display_name = validate_provider_display_name(display_name)
    headers = validate_provider_headers(headers)
    updated: dict[str, Any] = {}

    def mutate(data: dict[str, Any]) -> None:
        providers = data.setdefault("providers", {})
        current = dict(providers.get(provider_id, {}))
        preset = PROVIDER_PRESETS.get(provider_id, {})
        current.setdefault("kind", preset.get("kind", "openai_chat"))
        current.setdefault("displayName", preset.get("displayName", provider_id))
        current.setdefault("baseUrl", preset.get("baseUrl", ""))
        if api_key is not None:
            current["apiKey"] = api_key
        if api_key_env is not None:
            current["apiKeyEnv"] = api_key_env
        if base_url is not None:
            current["baseUrl"] = base_url
        if models is not None:
            current["models"] = models
        if display_name is not None:
            current["displayName"] = display_name
        if headers is not None:
            current["headers"] = headers
        providers[provider_id] = current
        updated.update(current)

    update_secure_json_file(
        get_providers_json_path(),
        default_provider_config,
        mutate,
        normalize=normalize_provider_config,
        error_label="BYOK providers",
    )
    return merged_provider_config(provider_id, normalize_provider_entry(updated))


def remove_provider_config(provider_id: str) -> bool:
    def mutate(data: dict[str, Any]) -> bool:
        providers = data.setdefault("providers", {})
        existed = provider_id in providers
        providers.pop(provider_id, None)
        return existed

    return bool(update_secure_json_file(
        get_providers_json_path(),
        default_provider_config,
        mutate,
        normalize=normalize_provider_config,
        error_label="BYOK providers",
    ))


def split_provider_model(model: str) -> tuple[str | None, str]:
    model = str(model)
    if ":" in model:
        provider_id, provider_model = model.split(":", 1)
        if provider_id in PROVIDER_PRESETS or provider_id in all_provider_configs(include_env_enabled=False):
            return provider_id, provider_model
    if "/" in model:
        provider_id, provider_model = model.split("/", 1)
        if provider_id in PROVIDER_PRESETS or provider_id in all_provider_configs(include_env_enabled=False):
            return provider_id, provider_model
    return None, model
