"""Non-secret route receipts for immutable reuse; no availability attestation."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

VERSION = 1


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()


def _adapter_revision():
    # Capture at import, alongside the loaded adapter implementation. This is not
    # a provider revision: it only binds reuse to this gateway's transformation.
    try:
        root = Path(__file__).parent
        sources = sorted(path for path in root.rglob('*.py') if '__pycache__' not in path.parts)
        if not sources or any(path.is_symlink() for path in sources):
            return None
        return digest({path.relative_to(root).as_posix():hashlib.sha256(path.read_bytes()).hexdigest()
                       for path in sources})
    except OSError:
        return None


ADAPTER_REVISION = _adapter_revision()


def identity(route, model, *, provider=None, auth=None, backend=None, endpoint=None):
    from .models import native_model_definition
    from .byok import normalize_byok_model_id
    from .unified import openai_responses_url, strip_reserved_openai_prefix
    from .constants import ANTIGRAVITY_ENDPOINT_PROD
    if ADAPTER_REVISION is None:
        return None
    value = {'version':VERSION,'route':route,'adapter':ADAPTER_REVISION}
    if route == 'antigravity':
        definition = native_model_definition(model)
        if definition is None:return None
        value.update(canonical=definition.id, backend=backend or definition.backend_id,
                     definition=vars(definition),endpoint=endpoint or ANTIGRAVITY_ENDPOINT_PROD)
    elif route == 'openai':
        if auth is None:return None
        try:url = openai_responses_url(auth)
        except (ValueError, AttributeError):return None
        value.update(canonical=strip_reserved_openai_prefix(model),backend=backend or strip_reserved_openai_prefix(model),
                     endpoint=url,kind=auth.kind)
    elif route == 'byok' and isinstance(provider,dict):
        provider_id = provider.get('id')
        if not isinstance(provider_id,str):return None
        selected = model.split(':',1)[1] if model.startswith(provider_id+':') else model
        selected = normalize_byok_model_id(selected,provider_id)
        declaration = next((entry for entry in provider.get('models',[]) if isinstance(entry,dict)
                            and normalize_byok_model_id(entry.get('id',''),provider_id)==selected), None)
        value.update(canonical=provider_id+':'+selected,backend=backend or selected,endpoint=provider.get('baseUrl'),
                     kind=provider.get('kind'),capabilities=provider.get('capabilities'),model_declaration=declaration,
                     headers=provider.get('headers') or {})
    else:
        return None
    try:
        return {'version':VERSION,'sha256':digest(value)}
    except (TypeError, ValueError, RecursionError):
        return None


def observe(value):
    """Attach the receipt after actual request preparation, before transport."""
    from .request_budget import CURRENT_BUDGET
    budget = CURRENT_BUDGET.get()
    if budget is not None:
        scope = getattr(budget.request,'scope',None)
        if isinstance(scope,dict):
            scope.setdefault('state',{})['routing_identity'] = value
