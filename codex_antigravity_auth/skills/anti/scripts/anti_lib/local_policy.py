"""Explicit loopback-only settings and catalog checks, shared without probes."""
import hashlib
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

from .endpoint_policy import is_loopback_endpoint, validate_endpoint_url

VERSION = 1
ENVIRONMENT = 'ANTIGRAVITY_LOCAL_ONLY'


class LocalPolicyError(ValueError):
    pass


def unique_fields(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise LocalPolicyError('local profile contains a duplicate field')
        result[key] = value
    return result


def environment_enabled():
    return os.environ.get(ENVIRONMENT, '').strip().lower() in {'1','true','yes'}


def loopback_url(value):
    try:
        value = validate_endpoint_url(value)
    except ValueError as exc:
        raise LocalPolicyError('local-only policy requires a valid loopback HTTP(S) endpoint') from exc
    if not is_loopback_endpoint(urlsplit(value).hostname):
        raise LocalPolicyError('local-only policy refuses a non-loopback endpoint')
    return value.rstrip('/')


def endpoint_scope(value):
    try:
        loopback_url(value)
        return 'loopback'
    except ValueError:
        return 'remote_or_invalid'


def require_catalog(registry):
    contract = getattr(registry, 'local_only_contract', None)
    if not isinstance(contract, dict) or type(contract.get('version')) is not int or contract['version'] != VERSION:
        raise LocalPolicyError('local-only gateway contract is missing; start a compatible gateway with --local-only')
    if contract.get('enabled') is not True:
        raise LocalPolicyError('gateway local-only mode is not enabled; start the gateway with --local-only')


def require_model(registry, model, *, stage):
    require_catalog(registry)
    key = str(model).lower()
    caps = registry.entries.get(key) or registry.entries.get(registry.canonical(key)) or {}
    if caps.get('route') != 'byok' or caps.get('destination_scope') != 'loopback':
        raise LocalPolicyError(f'local-only {stage} stage has no declared loopback route for the selected model')


def model_id(value):
    if not isinstance(value,str) or not value or len(value)>512 or any(ch.isspace() or ord(ch)<32 or 127 <= ord(ch) < 160 for ch in value):
        raise LocalPolicyError('local profile model IDs must be nonempty single-line identifiers')
    return value


def profile(gateway, models, judge, fallback=None, fallback_policy='never'):
    if not isinstance(models,list) or not 1 <= len(models) <= 32:
        raise LocalPolicyError('local profile requires one to 32 explicit reviewer models')
    if fallback_policy not in {'never','on-timeout','on-retryable'}:
        raise LocalPolicyError('invalid local profile fallback policy')
    if fallback_policy != 'never' and fallback is None:
        raise LocalPolicyError('local profile fallback policy requires an explicit fallback model')
    return {'version':VERSION,'local_only':True,'gateway':loopback_url(gateway),
            'models':[model_id(value) for value in models], 'judge':model_id(judge),
            'fallback_model':model_id(fallback) if fallback is not None else None,
            'fallback_policy':fallback_policy}


def prepare_args(args):
    if args is None: return None
    if hasattr(args,'_local_policy'): return args._local_policy
    path = getattr(args,'local_profile',None)
    selected = None
    if path:
        conflicts = {'--base-url','--model','--judge','--fallback-model','--fallback-policy'} & set(getattr(args,'_provided_options',()))
        if conflicts:
            raise LocalPolicyError('choose --local-profile or explicit gateway/model/judge/fallback flags, not both')
        try:
            with Path(path).expanduser().open('rb') as handle: raw=handle.read(65537)
            if len(raw)>65536: raise LocalPolicyError('local profile exceeds 64 KiB')
            value=json.loads(raw, object_pairs_hook=unique_fields)
            if not isinstance(value,dict) or type(value.get('version')) is not int or value['version']!=VERSION or value.get('local_only') is not True:
                raise LocalPolicyError('unsupported local profile version or policy')
            if set(value) - {'version','local_only','gateway','models','judge','fallback_model','fallback_policy'}:
                raise LocalPolicyError('local profile contains unsupported fields; credentials do not belong in this file')
            selected=profile(value.get('gateway'),value.get('models'),value.get('judge'),value.get('fallback_model'),value.get('fallback_policy','never'))
        except (OSError,ValueError,TypeError) as exc:
            if isinstance(exc,LocalPolicyError): raise
            raise LocalPolicyError('local profile could not be read as valid JSON settings') from exc
        single = getattr(args,'command','') in {'consult','review','plan'} or (getattr(args,'command','') == 'workflow' and getattr(args,'name','') == 'plan-deep')
        if single and len(selected['models']) != 1:
            raise LocalPolicyError('this single-model command requires a profile with exactly one reviewer model')
        args.base_url=selected['gateway']
        args.local_only=True
        args.model=(selected['models'] if getattr(args,'command','') in {'panel','moa','fusion','compare','workflow','smoke'} else selected['models'][0])
        args.judge=selected['judge']
        args.fallback_model=selected['fallback_model']
        args.fallback_policy=selected['fallback_policy']
        selected={**selected,'profile_sha256':hashlib.sha256(raw).hexdigest()}
    if getattr(args,'local_only',False) is True:
        gateway=loopback_url(args.base_url)
        selected={**(selected or {}),'enabled':True,'gateway':gateway}
    args._local_policy=selected
    return selected
