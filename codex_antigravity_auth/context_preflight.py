"""Gateway context assessments using catalog provenance, never model-name guesses."""
from __future__ import annotations

from .skills.anti.scripts.anti_lib.context_budget import (
    assess, binding_key, ContextLimitExceeded, VerifiedContext,
)
from .models import native_model_definition
from .unified import classify_route, openai_catalog, strip_reserved_openai_prefix
from .byok import split_provider_model, normalize_byok_model_id

# Deliberately empty. Existing built-in/provider context windows are declarations,
# not current verified deployment limits. Future entries require reviewed source
# evidence and a compatible complete-request counter; request JSON cannot opt in.
VERIFIED_CONTEXTS: dict[str, VerifiedContext] = {}


def declaration(request, providers, *, deployment=None, route=None):
    model = request.get('model', '')
    route = route or classify_route(model, provider_configs=providers)
    declared = None
    identity = {'route': route, 'model': model}
    if route == 'antigravity':
        definition = native_model_definition(model)
        if definition is not None:
            declared = definition.context_window
            identity.update(backend=definition.backend_id, declaration=vars(definition))
    elif route in {'openai', 'openai-disabled'}:
        selected = strip_reserved_openai_prefix(model).lower()
        entry = next((row for row in openai_catalog() if row['id'].lower() == selected), None)
        if entry is not None:
            declared = entry.get('context_window')
            identity['declaration'] = entry
        if not deployment:
            return declared, None  # Auth kind/actual endpoint are not established.
        identity['deployment'] = deployment
    elif route == 'byok':
        provider_id, selected = split_provider_model(model, provider_configs=providers)
        provider = providers.get(provider_id, {})
        selected = normalize_byok_model_id(selected, provider_id)
        for row in provider.get('models', []):
            if isinstance(row, dict) and normalize_byok_model_id(row.get('id', ''), provider_id) == selected:
                declared = row.get('context_window') or row.get('contextWindow')
                identity['model_declaration'] = row
                break
        # Bind evidence to the endpoint/adapter/config, excluding credentials.
        identity.update(provider=provider_id, backend=selected, endpoint=provider.get('baseUrl'),
                        kind=provider.get('kind'), capabilities=provider.get('capabilities'),
                        header_declarations=provider.get('headers'))
    return declared, binding_key(identity)


def inspect(request, providers, *, deployment=None, route=None):
    declared, binding = declaration(request, providers, deployment=deployment, route=route)
    return assess(request, declared_tokens=declared, binding=binding, evidence=VERIFIED_CONTEXTS.get(binding))


def enforce(request, providers, *, deployment=None, route=None):
    report = inspect(request, providers, deployment=deployment, route=route)
    if report['status'] == 'reject':
        raise ContextLimitExceeded(report)
    return report
