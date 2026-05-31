# User-Facing model name mappings to Antigravity API actual backend values.
# E.g. gemini-3.5-flash-high maps to gemini-3-flash-agent
# Sonnet 4.6 maps to claude-sonnet-4-6

MODEL_MAP = {
    # Gemini 3.5 Flash tiers
    "gemini-3.5-flash": "gemini-3.5-flash-low",
    "gemini-3.5-flash-high": "gemini-3-flash-agent",
    "gemini-3.5-flash-medium": "gemini-3.5-flash-low",
    "gemini-3.5-flash-low": "gemini-3.5-flash-low",
    
    # Gemini 3.1 Pro
    "gemini-3.1-pro": "gemini-3.1-pro-low",
    "gemini-3.1-pro-high": "gemini-3.1-pro-high",
    
    # Claude models
    "claude-3-5-sonnet": "claude-sonnet-4-6",
    "claude-3.5-sonnet": "claude-sonnet-4-6",
    "claude-sonnet-4-6": "claude-sonnet-4-6",
    "claude-opus-4-6": "claude-opus-4-6-thinking",
    "claude-opus-4-6-thinking": "claude-opus-4-6-thinking",
}

def resolve_backend_model(model: str) -> str:
    """Resolve Codex-provided model name to official Antigravity backend model."""
    lower = str(model).lower()
    if lower in MODEL_MAP:
        return MODEL_MAP[lower]
    # Remove any provider prefixes like "openai-responses/" or "openai/"
    if "/" in lower:
        parts = lower.split("/")
        return resolve_backend_model(parts[-1])
    # Normalize hyphens to dots (codex-shim slug normalization)
    slug_variants = {
        lower.replace("-3-5-", "-3.5-"),
        lower.replace("-3-1-", "-3.1-"),
        lower.replace("-4-6", "-4.6"),
    }
    for variant in slug_variants:
        if variant in MODEL_MAP:
            return MODEL_MAP[variant]
    return lower
