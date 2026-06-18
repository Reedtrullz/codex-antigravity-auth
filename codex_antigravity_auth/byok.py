import os
from pathlib import Path
from typing import Any

from .storage import load_secure_json_file, save_secure_json_file, update_secure_json_file


PROVIDERS_FILE = "~/.codex/antigravity-providers.json"


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


def normalize_provider_config(data: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(data, dict):
        data = {}
    data.setdefault("providers", {})
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
    save_secure_json_file(path, normalize_provider_config(data), error_label="BYOK providers")


def provider_preset(provider_id: str) -> dict[str, Any]:
    preset = PROVIDER_PRESETS.get(provider_id)
    if not preset:
        raise ValueError(f"Unknown provider preset: {provider_id}")
    return dict(preset)


def resolve_api_key(provider: dict[str, Any]) -> str | None:
    if provider.get("apiKey"):
        return provider["apiKey"]
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
            current["baseUrl"] = base_url.rstrip("/")
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
    return merged_provider_config(provider_id, updated)


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
