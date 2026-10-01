"""Explicit, bounded catalog/probe observations; never infer new capabilities."""
from __future__ import annotations

import asyncio
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import time

import httpx

from . import byok, models, unified
from .capability_catalog import contract, native_contract
from .endpoint_policy import httpx_client_options
from .redaction import redact_secret_text
from .secure_store import SecureStore, file_lock

VERSION = 1
MAX_BYTES = 512 * 1024
MAX_PAGES = 4
MAX_MODELS = 2000
MAX_TTL = 86400
DISCOVERY_TTL = 3600
PROBE_TTL = 600
_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}\Z')
_SHA = re.compile(r'[0-9a-f]{64}\Z')
_STATUSES = {'complete', 'partial', 'error', 'unsupported'}
_TERMINALS = {'completed', 'failed', 'incomplete', 'empty', 'refusal', 'queued', 'in_progress', 'cancelled', 'malformed', 'http_error', 'timeout', 'transport_error'}


class ObservationError(ValueError):
    pass


def public_id(value):
    if not isinstance(value, str) or not _ID.fullmatch(value) or value.lower().startswith(('http:', 'https:')) or redact_secret_text(value) != value:
        raise ObservationError('Invalid or sensitive model identifier.')
    return value


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def provider_identity(provider_id, provider):
    # No key, environment value, display label, or header value is retained.
    return {'provider':public_id(provider_id), 'route':'byok', 'configuration':digest({
        'id':provider_id, 'kind':provider.get('kind', 'openai_chat'), 'baseUrl':provider.get('baseUrl'),
        'authMode':byok.provider_auth_mode(provider)})}


def cache_path(kind, identifier):
    # Reading observations never creates the client namespace or an encryption key.
    return models.model_overlay_path().parent / 'antigravity-model-observations' / (digest([kind, identifier]) + '.json')


def _validate(record, kind):
    common = {'schemaVersion', 'kind', 'identity', 'observedAt', 'expiresAt', 'status', 'diagnostics'}
    extra = {'models', 'pages'} if kind == 'discovery' else {'terminal', 'httpStatus', 'generationOk'}
    if not isinstance(record, dict) or set(record) != common | extra or type(record.get('schemaVersion')) is not int or record.get('schemaVersion') != VERSION or record.get('kind') != kind:
        raise ObservationError('Unsupported or malformed observation record.')
    identity = record['identity']
    keys = {'provider','route','configuration'} if kind == 'discovery' else {'model','route','configuration','capability','gateway'}
    if not isinstance(identity, dict) or set(identity) != keys:
        raise ObservationError('Malformed observation identity.')
    public_id(identity['provider' if kind == 'discovery' else 'model'])
    if identity['route'] not in {'byok','antigravity','openai'} or not isinstance(identity['configuration'], str) or not _SHA.fullmatch(identity['configuration']):
        raise ObservationError('Malformed observation route.')
    if kind == 'probe' and (identity['capability'] != 'text_generation' or not isinstance(identity['gateway'], str) or not _SHA.fullmatch(identity['gateway'])):
        raise ObservationError('Malformed probe identity.')
    if any(type(record[key]) is not int for key in ('observedAt','expiresAt')) or not 0 <= record['observedAt'] <= record['expiresAt'] <= record['observedAt'] + MAX_TTL:
        raise ObservationError('Malformed observation timestamps.')
    if record['status'] not in _STATUSES or not isinstance(record['diagnostics'], list) or len(record['diagnostics']) > 20:
        raise ObservationError('Malformed observation status.')
    allowed = {'invalid_records','page_limit','model_limit','byte_limit','timeout','transport_error','invalid_json',
               'invalid_catalog','pagination_invalid','pagination_cycle','discovery_unavailable','http_error','redirect_refused',
               'unsupported_encoding','invalid_configuration'}
    if any(not isinstance(code, str) or code not in allowed for code in record['diagnostics']):
        raise ObservationError('Malformed observation diagnostic.')
    if kind == 'discovery':
        if not isinstance(record['models'], list) or len(record['models']) > MAX_MODELS or type(record['pages']) is not int or not 0 <= record['pages'] <= MAX_PAGES:
            raise ObservationError('Malformed discovery count.')
        for identifier in record['models']: public_id(identifier)
        if len(set(record['models'])) != len(record['models']): raise ObservationError('Duplicate discovery identifiers.')
    elif (record['terminal'] not in _TERMINALS or type(record['generationOk']) is not bool
          or record['generationOk'] != (record['terminal'] == 'completed')
          or (record['httpStatus'] is not None and (type(record['httpStatus']) is not int or not 100 <= record['httpStatus'] <= 599))):
        raise ObservationError('Malformed generation observation.')
    if kind == 'probe' and (record['status'] != ('complete' if record['generationOk'] else 'error')
                           or (record['generationOk'] and not (record['httpStatus'] is not None and 200 <= record['httpStatus'] < 300))):
        raise ObservationError('Inconsistent generation observation.')
    return record


def read_observation(kind, identifier, identity, *, now=None):
    path = cache_path(kind, identifier)
    try:
        if path.is_symlink(): raise ObservationError('Symlinked observation cache.')
        flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
        with os.fdopen(os.open(path, flags), 'rb') as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode): raise ObservationError('Observation cache must be a regular file.')
            raw = stream.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES: raise ObservationError('Observation cache exceeds its limit.')
        record = _validate(json.loads(raw), kind)
    except FileNotFoundError:
        return {'state':'missing', 'record':None}
    except (ValueError, OSError, TypeError, RecursionError):
        return {'state':'invalid', 'record':None}
    clock = int(time.time() if now is None else now)
    state = ('mismatch' if record['identity'] != identity else 'stale'
             if record['observedAt'] > clock or record['expiresAt'] <= clock else 'fresh')
    return {'state':state, 'record':record}


def save_observation(record, identifier):
    _validate(record, record['kind'])
    encoded = json.dumps(record, sort_keys=True, indent=2)
    if len(encoded.encode()) > MAX_BYTES: raise ObservationError('Observation cache exceeds its limit.')
    SecureStore().atomic_write_text(cache_path(record['kind'], identifier), encoded, mode=0o600)


def _record(kind, identity, ttl):
    now = int(time.time())
    return {'schemaVersion':VERSION, 'kind':kind, 'identity':identity, 'observedAt':now, 'expiresAt':now+ttl,
            'status':'complete', 'diagnostics':[]}


def _timeout(value):
    if type(value) not in (float, int) or not 0 < value <= 30:
        raise ObservationError('Network timeout must be greater than zero and at most 30 seconds.')
    return float(value)


def providers_read_only():
    try:
        return byok.all_provider_configs_read_only()
    except Exception:
        raise ObservationError('Provider configuration could not be read.') from None


def selected_provider(provider_id, configs=None):
    provider_id = byok.validate_provider_id(provider_id)
    configs = providers_read_only() if configs is None else configs
    provider = configs.get(provider_id)
    if provider is None and provider_id in byok.PROVIDER_PRESETS:
        provider = byok.merged_provider_config(provider_id)
    if provider is None: raise ObservationError('Provider is not configured.')
    return provider_id, provider


def _provider_headers(provider):
    try:
        byok.validate_supported_provider_kind(provider)
        byok.validate_supported_provider_auth_mode(provider.get('id', 'custom'), byok.provider_auth_mode(provider))
        headers = dict(byok.validate_provider_headers(provider.get('headers')) or {})
        key = byok.validate_provider_api_key(byok.resolve_api_key(provider))
        if key: headers['Authorization'] = 'Bearer ' + key
        headers.update({'Accept':'application/json', 'Accept-Encoding':'identity'})
        return headers
    except ValueError:
        raise ObservationError('Provider discovery configuration is invalid or unsupported.') from None


def _contains_private_value(identifier, values):
    return any(value and (identifier == value or (len(value) >= 6 and value in identifier)) for value in values)


async def _response_json(client, method, url, *, headers, params=None, body=None):
    async with client.stream(method, url, headers=headers, params=params, json=body) as response:
        status = response.status_code
        if not 200 <= status < 300: return status, None
        if response.headers.get('Content-Encoding', 'identity').lower() != 'identity':
            raise ObservationError('unsupported_encoding')
        raw = bytearray()
        async for chunk in response.aiter_raw():
            if len(raw) + len(chunk) > MAX_BYTES: raise ObservationError('byte_limit')
            raw.extend(chunk)
        try:
            value = json.loads(raw, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        except (ValueError, UnicodeError, RecursionError):
            raise ObservationError('invalid_json') from None
        return status, value


def _next_page(payload, identifiers):
    if any(payload.get(key) for key in ('next', 'next_url', 'next_page_url', 'links')):
        raise ObservationError('pagination_invalid')
    tokens = [key for key in ('next_page_token','next_cursor') if payload.get(key) not in (None, '')]
    if len(tokens) > 1 or (tokens and payload.get('has_more') is False): raise ObservationError('pagination_invalid')
    for key, parameter in (('next_page_token','page_token'), ('next_cursor','cursor')):
        value = payload.get(key)
        if value is not None and value != '':
            if not isinstance(value, str) or len(value) > 200 or any(ord(char) < 32 for char in value):
                raise ObservationError('pagination_invalid')
            return {parameter:value}
    more = payload.get('has_more', False)
    if type(more) is not bool: raise ObservationError('pagination_invalid')
    if more:
        last = payload.get('last_id') or (identifiers[-1] if identifiers else None)
        try: return {'after':public_id(last)}
        except ObservationError: raise ObservationError('pagination_invalid') from None
    return None


def discover(provider_id, *, network=False, timeout=10, configs=None):
    provider_id, provider = selected_provider(provider_id, configs)
    identity = provider_identity(provider_id, provider)
    if not network: return read_observation('discovery', provider_id, identity)
    timeout = _timeout(timeout)
    headers = _provider_headers(provider)
    private_values = [value for name,value in headers.items() if name.lower() not in {'accept','accept-encoding'}]
    private_values += [value.split(' ',1)[1] for value in private_values if value.lower().startswith('bearer ')]
    base = byok.validate_http_base_url(provider.get('baseUrl'))
    record = {**_record('discovery', identity, DISCOVERY_TTL), 'models':[], 'pages':0}
    async def fetch():
        params = None
        seen = set()
        known_models = set()
        async with httpx.AsyncClient(**httpx_client_options(base, timeout=timeout)) as client:
            for _ in range(MAX_PAGES):
                status, payload = await _response_json(client, 'GET', base+'/models', headers=headers, params=params)
                record['pages'] += 1
                if status in {404,405,501}:
                    record['status'] = 'unsupported' if not record['models'] else 'partial'
                    record['diagnostics'].append('discovery_unavailable'); return
                if not 200 <= status < 300:
                    raise ObservationError('redirect_refused' if 300 <= status < 400 else 'http_error')
                if not isinstance(payload, dict) or not isinstance(payload.get('data'), list):
                    raise ObservationError('invalid_catalog')
                page_ids = []
                for entry in payload['data'][:MAX_MODELS]:
                    try:
                        identifier = public_id(entry.get('id') if isinstance(entry, dict) else None)
                        if _contains_private_value(identifier, private_values): raise ObservationError('invalid_records')
                    except ObservationError:
                        if 'invalid_records' not in record['diagnostics']: record['diagnostics'].append('invalid_records')
                        record['status'] = 'partial'; continue
                    page_ids.append(identifier)
                    if identifier not in known_models:
                        if len(record['models']) >= MAX_MODELS: raise ObservationError('model_limit')
                        record['models'].append(identifier)
                        known_models.add(identifier)
                if len(payload['data']) > MAX_MODELS: raise ObservationError('model_limit')
                params = _next_page(payload, page_ids)
                if params is None: return
                marker = digest(params)
                if marker in seen: raise ObservationError('pagination_cycle')
                seen.add(marker)
            raise ObservationError('page_limit')
    try:
        asyncio.run(asyncio.wait_for(fetch(), timeout))
    except (asyncio.TimeoutError, httpx.TimeoutException):
        record['diagnostics'].append('timeout'); record['status'] = 'partial' if record['models'] else 'error'
    except (httpx.HTTPError, OSError):
        record['diagnostics'].append('transport_error'); record['status'] = 'partial' if record['models'] else 'error'
    except ObservationError as exc:
        record['diagnostics'].append(str(exc)); record['status'] = 'partial' if record['models'] else 'error'
    record['observedAt'] = int(time.time())
    record['expiresAt'] = record['observedAt'] + DISCOVERY_TTL
    save_observation(record, provider_id)
    return {'state':'fresh', 'record':record}


def describe_model(identifier, *, base_url='http://127.0.0.1:51122/v1', configs=None):
    identifier = public_id(identifier)
    try: models.load_model_overlays(strict=True)
    except ValueError: raise ObservationError('Model declarations could not be read.') from None
    configs = providers_read_only() if configs is None else configs
    route = unified.classify_route(identifier, provider_configs=configs)
    diagnostics = []
    canonical = identifier
    declaration = None
    configuration = None
    discovery = {'state':'not_supported', 'record':None}
    if route == 'byok':
        provider_id, backend = byok.split_provider_model(identifier, provider_configs=configs)
        provider_id, provider = selected_provider(provider_id, configs)
        backend = public_id(byok.normalize_byok_model_id(backend, provider_id))
        canonical = public_id(provider_id + ':' + backend)
        identity = provider_identity(provider_id, provider)
        configuration = identity['configuration']
        discovery = read_observation('discovery', provider_id, identity)
        try:
            byok.validate_supported_provider_kind(provider)
            byok.validate_http_base_url(provider.get('baseUrl'))
            if not byok.resolve_api_key(provider) and not byok.provider_allows_keyless_local_use(provider): diagnostics.append('missing_credentials')
            entries = provider.get('models', [])
            selected = next((entry for entry in entries if byok.normalize_byok_model_id(
                entry.get('id') if isinstance(entry, dict) else entry, provider_id) == backend), None)
            if selected is not None:
                capabilities = byok.provider_capabilities(provider, backend)
                declared = dict(provider.get('capabilities') or {})
                if isinstance(selected, dict): declared.update(selected.get('capabilities') or {})
                declaration = contract(canonical_id=canonical, backend_id=backend, route='byok', family=provider_id,
                    aliases=[], capabilities=capabilities, context_window=selected.get('context_window', selected.get('contextWindow')) if isinstance(selected, dict) else None,
                    declaration_source='provider_configuration', declared_capabilities=declared)
            else: diagnostics.append('model_not_declared')
        except ValueError:
            diagnostics.append('invalid_configuration')
        configuration = digest({'provider':identity, 'declaration':declaration})
    elif route == 'antigravity':
        definition = models.native_model_definition(identifier)
        if definition:
            canonical = definition.id
            declaration = native_contract(definition, source='builtin' if definition in models.NATIVE_MODELS else 'overlay')
        else: diagnostics.append('backend_passthrough_not_declared')
    elif route in {'openai','openai-disabled'}:
        canonical = unified.strip_reserved_openai_prefix(identifier).lower()
        entry = next((entry for entry in unified.openai_catalog() if entry['id'].lower() == canonical), None)
        if entry:
            canonical = entry['id']
            declaration = contract(canonical_id=canonical, backend_id=canonical, route='openai', family='openai', aliases=[],
                capabilities=unified.openai_model_capabilities(canonical), context_window=entry['context_window'], declaration_source='openai_registry')
        if route == 'openai-disabled': diagnostics.append('unified_picker_disabled')
    else: diagnostics.append('unknown_route')
    configuration = configuration or digest({'route':route, 'declaration':declaration})
    identity = {'model':public_id(canonical), 'route':route, 'configuration':configuration, 'capability':'text_generation',
                'gateway':digest(byok.validate_http_base_url(base_url))}
    observation = read_observation('probe', canonical, identity) if route in {'byok','antigravity','openai'} else {'state':'missing','record':None}
    recent = observation['state'] == 'fresh' and observation['record']['generationOk'] and not diagnostics
    return {'requested':identifier, 'canonical_id':canonical, 'route':route, 'declaration':declaration,
            'configuration_diagnostics':diagnostics, 'discovery':discovery, 'probe':observation,
            'recent_generation_ok':bool(recent), 'availability':'unknown', 'probe_identity':identity}


def probe(identifier, *, network=False, base_url='http://127.0.0.1:51122/v1', token_env='ANTIGRAVITY_GATEWAY_TOKEN', timeout=10, configs=None):
    if not network: raise ObservationError('Generation probing requires --network.')
    timeout = _timeout(timeout)
    report = describe_model(identifier, base_url=base_url, configs=configs)
    if report['route'] not in {'byok','antigravity','openai'} or report['configuration_diagnostics']:
        raise ObservationError('Resolve model configuration diagnostics before probing.')
    base = byok.validate_http_base_url(base_url)
    token_env = byok.validate_provider_api_key_env(token_env)
    token = byok.validate_provider_api_key(os.environ.get(token_env or '', '') or None)
    if token and _contains_private_value(report['canonical_id'], [token]):
        raise ObservationError('Model identifier contains a transport credential.')
    headers = {'Accept':'application/json','Accept-Encoding':'identity'}
    if token: headers['Authorization'] = 'Bearer ' + token
    record = {**_record('probe', report['probe_identity'], PROBE_TTL), 'terminal':'transport_error', 'httpStatus':None, 'generationOk':False}
    async def fetch():
        async with httpx.AsyncClient(**httpx_client_options(base, timeout=timeout)) as client:
            return await _response_json(client, 'POST', base+'/responses', headers=headers,
                body={'model':report['canonical_id'], 'input':'Reply with the single word: ready', 'max_output_tokens':16, 'stream':False})
    try:
        status, payload = asyncio.run(asyncio.wait_for(fetch(), timeout))
        record['httpStatus'] = status
        if 200 <= status < 300:
            from .cli_doctor import _generation_probe_outcome
            terminal, _, _ = _generation_probe_outcome(payload)
            record['terminal'] = terminal if terminal in _TERMINALS else 'malformed'
            record['generationOk'] = record['terminal'] == 'completed'
        else: record['terminal'] = 'http_error'
    except (asyncio.TimeoutError, httpx.TimeoutException): record['terminal'] = 'timeout'
    except (httpx.HTTPError, OSError): record['terminal'] = 'transport_error'
    except ObservationError: record['terminal'] = 'malformed'
    record['status'] = 'complete' if record['generationOk'] else 'error'
    record['observedAt'] = int(time.time())
    record['expiresAt'] = record['observedAt'] + PROBE_TTL
    save_observation(record, report['canonical_id'])
    return record


def import_discovered(provider_id, selected, *, write=False, accept_digest=None):
    provider_id, provider = selected_provider(provider_id)
    identity = provider_identity(provider_id, provider)
    observation = read_observation('discovery', provider_id, identity)
    if observation['state'] != 'fresh' or observation['record']['status'] not in {'complete','partial'}:
        raise ObservationError('A fresh matching discovery preview is required.')
    identifiers = list(dict.fromkeys(public_id(item) for item in selected))
    if not identifiers or any(item not in observation['record']['models'] for item in identifiers):
        raise ObservationError('Select explicit model IDs from the discovery preview.')
    identifiers = list(dict.fromkeys(public_id(byok.normalize_byok_model_id(item, provider_id)) for item in identifiers))
    existing = {byok.normalize_byok_model_id(item.get('id') if isinstance(item, dict) else item, provider_id) for item in provider.get('models', [])}
    additions = [item for item in identifiers if item not in existing]
    folded = {item.lower() for item in existing}
    for item in additions:
        if item.lower() in folded: raise ObservationError('Discovered identifier collides with the catalog case policy.')
        folded.add(item.lower())
    try:
        inherited = contract(canonical_id=provider_id, backend_id=provider_id, route='byok', family=provider_id,
            aliases=[], capabilities=byok.provider_capabilities(provider), context_window=None,
            declaration_source='provider_configuration', declared_capabilities=provider.get('capabilities') or {})
    except ValueError:
        raise ObservationError('Provider capability declarations are invalid.') from None
    proposal = {'provider':provider_id, 'add':additions, 'discovery_status':observation['record']['status'],
                'capabilities_promoted':False, 'inherited_capabilities':inherited['effective'],
                'configuration':identity['configuration'],
                'before':digest({'models':provider.get('models', []),'capabilities':provider.get('capabilities')})}
    proposal['digest'] = digest(proposal)
    if write:
        if accept_digest != proposal['digest']: raise ObservationError('Saving requires --accept-digest from the matching preview.')
        from .storage import update_secure_json_file
        def mutate(data):
            current = data.setdefault('providers', {}).get(provider_id, {})
            merged = byok.merged_provider_config(provider_id, current)
            if provider_identity(provider_id, merged) != identity: raise ObservationError('Provider changed after preview; preview again.')
            if digest({'models':merged.get('models', []),'capabilities':merged.get('capabilities')}) != proposal['before']:
                raise ObservationError('Provider models changed after preview; preview again.')
            data['providers'][provider_id] = {**current, 'models':[*merged.get('models', []), *additions]}
        update_secure_json_file(byok.get_providers_json_path(), byok.default_provider_config, mutate,
                               normalize=byok.normalize_provider_config, error_label='BYOK providers')
    return {**proposal, 'saved':write}


def import_overlay(path, *, write=False, accept_digest=None):
    path = Path(path)
    if path.is_symlink() or not path.is_file(): raise ObservationError('Overlay import source must be a regular file without symlinks.')
    with path.open('rb') as stream: raw = stream.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES: raise ObservationError('Overlay import exceeds its byte limit.')
    incoming = models.parse_model_overlay_toml(raw.decode('utf-8'))
    if not incoming: raise ObservationError('Overlay import contains no model declarations.')
    if len(incoming) > MAX_MODELS: raise ObservationError('Overlay import exceeds the model count limit.')
    for model in incoming:
        public_id(model.id); public_id(model.backend_id)
        for alias in model.aliases: public_id(alias)
        if redact_secret_text(model.display_name) != model.display_name: raise ObservationError('Overlay display name contains sensitive text.')
    target = models.model_overlay_path()
    def plan():
        models.invalidate_model_overlay_cache()
        current = models.load_model_overlays(strict=True)
        try:
            with target.open('rb') as stream: current_bytes = stream.read(MAX_BYTES + 1)
        except FileNotFoundError:
            current_bytes = None
        if current_bytes is not None and len(current_bytes) > MAX_BYTES:
            raise ObservationError('Existing overlay exceeds the import byte limit.')
        combined = [*models.NATIVE_MODELS, *current]
        for model in incoming:
            if models.model_identifier_collisions(model, tuple(combined)):
                raise ObservationError('Imported identifiers collide with existing declarations.')
            combined.append(model)
        proposal = {'add':[model.id for model in incoming], 'source':hashlib.sha256(raw).hexdigest(),
                    'definitions':[asdict(model) for model in incoming],
                    'before':hashlib.sha256(current_bytes).hexdigest() if current_bytes is not None else None, 'capabilities_promoted':False}
        proposal['digest'] = digest(proposal)
        return current, proposal
    if write:
        with file_lock(target):
            current, proposal = plan()
            if accept_digest != proposal['digest']: raise ObservationError('Saving requires --accept-digest from the matching preview.')
            SecureStore()._atomic_write_bytes_unlocked(target, models.render_model_overlay_toml([*current, *incoming]).encode(), mode=0o600)
            models.invalidate_model_overlay_cache()
    else: _, proposal = plan()
    return {**proposal, 'saved':write}
