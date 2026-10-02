"""Versioned public route/transport metadata; contains no credentials or probes."""
from __future__ import annotations

from .models import NATIVE_MODELS, NativeModel, capabilities_for_native_definition, alias_map_for_models, required_output_bridge
from .response_protocol import ProviderCapabilities

CATALOG_VERSION = 1
# These adapters emit these output kinds. Upstream media support does not add
# image/audio/video output until a gateway adapter can actually represent it.
OUTPUT_TYPES = ["text", "reasoning", "function_call", "refusal"]


def contract(*, canonical_id: str, backend_id: str, route: str, family: str,
             aliases, capabilities: ProviderCapabilities, context_window: int | None,
             declaration_source: str, declared_capabilities: dict | None = None) -> dict:
    known_context = isinstance(context_window, int) and not isinstance(context_window, bool) and context_window > 0
    result = {
        "version": CATALOG_VERSION,
        "canonical_id": canonical_id,
        "backend_id": backend_id,
        "route": route,
        "family": family,
        "aliases": sorted(set(aliases) - {canonical_id}),
        "declaration_source": declaration_source,
        "availability": "unknown",
        "context_limit": {"known": known_context, "tokens": context_window if known_context else None,
                          "basis": "declared" if known_context else "unknown"},
        "transport": {
            "input_modalities": ["text", "image"],
            "image_forms": ["data_url", "url"],
            "output_types": list(OUTPUT_TYPES),
            "opaque_reasoning_replay": capabilities.opaque_reasoning_replay,
            "tools": True, "structured_output": True,
        },
        "effective": {
            "input_modalities": sorted(capabilities.input_modalities),
            "image_forms": sorted(capabilities.image_forms) if "image" in capabilities.input_modalities else [],
            "output_types": list(OUTPUT_TYPES),
            "parallel_tool_calls": capabilities.parallel_tool_calls,
            "structured_output": capabilities.structured_output,
            "reasoning_efforts": list(capabilities.reasoning_effort_levels),
            "reasoning_replay": capabilities.reasoning_replay,
            "streaming": True,
            "tools": "function" in capabilities.tool_choice_modes,
        },
    }
    # The transport knows how to encode these features. Missing model/provider
    # declarations are still unknown backend support, never advertised as true.
    if declared_capabilities is not None:
        for name, key in (("tools", "tool_choice_modes"), ("parallel_tool_calls", "parallel_tool_calls"), ("structured_output", "structured_output")):
            if key not in declared_capabilities:
                result["effective"][name] = None
    effective_outputs = ["text", "refusal"]
    if result["effective"]["tools"] is True:
        effective_outputs.append("function_call")
    if capabilities.reasoning:
        effective_outputs.append("reasoning")
    result["effective"]["output_types"] = effective_outputs
    result["effective"]["reasoning_mapping"] = capabilities.reasoning_effort_parameter
    result["declared_backend"] = {
        "input_modalities": list(result["effective"]["input_modalities"]),
        "tools": result["effective"]["tools"],
        "output_types": list(effective_outputs),
        "structured_output": result["effective"]["structured_output"],
        "context_limit": dict(result["context_limit"]),
        "availability": "unknown",
    }
    result['audio_input'] = {'version':1,'format':'pcm_wav','transport_supported':capabilities.pcm_wav_probe,
        'backend_acceptance':'unverified' if capabilities.pcm_wav_probe else 'unsupported',
        'requires_probe_opt_in':True,'content_type':'antigravity_audio','streaming':False,
        'max_files':2,'max_file_bytes':2*1024*1024,'max_total_bytes':4*1024*1024,'max_duration_seconds':30,
        'listening_verification':'not_run'}
    return result


def native_contract(model: NativeModel, *, source="builtin", aliases=None) -> dict:
    capabilities = capabilities_for_native_definition(model)
    result = contract(canonical_id=model.id, backend_id=model.backend_id, route="antigravity",
                      family=model.family, aliases=aliases if aliases is not None else (*model.aliases, model.backend_id),
                      capabilities=capabilities, context_window=model.context_window,
                      declaration_source=source)
    bridge = required_output_bridge(model)
    result["gateway_generation"] = {"supported":not bool(bridge), "required_output_bridge":bridge}
    if bridge:
        result["effective"]["output_types"] = []
    return result


def standalone_snapshot() -> dict:
    """No overlays, credentials, dynamic discovery or provider availability claims."""
    aliases = alias_map_for_models(NATIVE_MODELS)
    return {"generated_from": "codex_antigravity_auth.models.NATIVE_MODELS; run scripts/generate_capability_snapshot.py",
            "capability_catalog_version": CATALOG_VERSION,
            "data": [{"id": model.id, "capabilities": native_contract(model, aliases=[name for name, target in aliases.items() if target == model.id])}
                     for model in NATIVE_MODELS if not required_output_bridge(model)]}
