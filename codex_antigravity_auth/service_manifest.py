"""Nonsecret service intent, checked persistence and operational runtime identity."""
from __future__ import annotations

import hashlib
from importlib.metadata import version, PackageNotFoundError
import json
import os
from pathlib import Path
import re
import stat
import sys
import uuid

from .secure_store import SecureStore
from .skills.anti.scripts.anti_lib.file_protection import verify_regular_descriptor

MAX_BYTES = 262144
MODULE = 'codex_antigravity_auth.cli'
MARKER = re.compile(r'^[0-9a-f]{32}$')
DIGEST = re.compile(r'^[0-9a-f]{64}$')
_runtime = None


def package_version():
    try:
        return version('codex-antigravity-auth')
    except PackageNotFoundError:
        return 'unknown'


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def read_file(path):
    path = Path(path)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES:
        raise ValueError('Unsupported service file; preserve it for manual inspection')
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
    with os.fdopen(fd, 'rb') as stream:
        verify_regular_descriptor(stream.fileno(), path)
        raw = stream.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError('Service file exceeds the inspection limit')
    return raw


def write_json(path, value):
    SecureStore().atomic_write_text(path, json.dumps(value, indent=2, sort_keys=True) + '\n')


def settings(*, port, host, unified, client_home, state_home, op_env_file=None, op_environment=None):
    from .onepassword import onepassword_command_prefix
    op_env_file = str(Path(op_env_file).expanduser().absolute()) if op_env_file else None
    prefix = onepassword_command_prefix(op_env_file=op_env_file, op_environment=op_environment)
    if prefix:
        prefix[0] = str(Path(prefix[0]).absolute())
    result = {'port': port, 'host': host, 'unified': bool(unified),
              'clientHome': str(client_home), 'stateHome': str(state_home),
              'executable': sys.executable, 'module': MODULE, 'packageVersion': package_version(),
              'opEnvFile': str(Path(op_env_file).expanduser().absolute()) if op_env_file else None,
              'opEnvironment': op_environment, 'wrapper': prefix}
    validate_settings(result)
    return result


def _text(value, *, nullable=False):
    return (nullable and value is None) or (isinstance(value, str) and 0 < len(value) <= 8192
                                           and not any(ord(c) < 32 or ord(c) == 127 for c in value))


def validate_settings(value):
    required = {'port','host','unified','clientHome','stateHome','executable','module','packageVersion',
                'opEnvFile','opEnvironment','wrapper'}
    if not isinstance(value, dict) or set(value) != required:
        raise ValueError('Malformed service launch settings')
    if (type(value['port']) is not int or not 1 <= value['port'] <= 65535
            or value['host'] not in {'127.0.0.1', 'localhost', '::1'}
            or type(value['unified']) is not bool or value['module'] != MODULE):
        raise ValueError('Unsupported service launch settings')
    for key in ('clientHome','stateHome','executable','packageVersion'):
        if not _text(value[key]) or (key != 'packageVersion' and not Path(value[key]).is_absolute()):
            raise ValueError('Malformed service launch path/version')
    if not _text(value['opEnvFile'], nullable=True) or not _text(value['opEnvironment'], nullable=True):
        raise ValueError('Malformed secret-runtime reference')
    if value['opEnvFile'] and value['opEnvironment']:
        raise ValueError('Conflicting secret-runtime references')
    wrapper = value['wrapper']
    if not isinstance(wrapper, list) or any(not _text(item) for item in wrapper):
        raise ValueError('Malformed service wrapper')
    reference = value['opEnvFile'] or value['opEnvironment']
    if reference:
        flag = '--env-file' if value['opEnvFile'] else '--environment'
        if len(wrapper) != 5 or wrapper[1:] != ['run', flag, reference, '--'] or not Path(wrapper[0]).is_absolute():
            raise ValueError('Malformed service wrapper reference')
    elif wrapper:
        raise ValueError('Unexpected service wrapper')


def command_for(value, marker=None):
    validate_settings(value)
    command = [value['executable'], '-m', MODULE, 'start', '--quiet-runtime-console', '--process-log',
               str(Path(value['stateHome']) / f"antigravity-gateway-{value['port']}.log"),
               '--port', str(value['port']), '--host', value['host'],
               '--client-home', value['clientHome'], '--state-home', value['stateHome']]
    # Obtain the established process-log name rather than coupling to legacy logs.
    from .process_logs import log_path
    command[6] = str(log_path(Path(value['stateHome']), value['port']))
    if value['unified']:
        command.append('--unified-model-picker')
    if marker is not None:
        if not MARKER.fullmatch(marker):
            raise ValueError('Malformed service identity')
        command += ['--service-id', marker]
    return [*value['wrapper'], *command]


def runtime_digest(value):
    return digest({key: value[key] for key in ('port','host','unified','clientHome','stateHome','executable','module','packageVersion')})


def reference_stat(value):
    path = value.get('opEnvFile')
    if not path:
        return None
    from .setup_profiles import SetupError, _read_file
    path = Path(path).absolute()
    before = path.lstat()
    try:
        raw = _read_file(path, limit=MAX_BYTES)
    except SetupError:
        raise ValueError('Unsafe or unstable environment reference') from None
    if raw is None:
        raise FileNotFoundError('Environment reference disappeared during capture')
    after = path.lstat()
    identity = lambda info: (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
    # The reader anchors parents and verifies owner, single-link identity and
    # stable bytes. Recheck the path so the retained mtime describes those bytes;
    # compare path stats only, preserving Windows' distinct fstat ctime semantics.
    if identity(before) != identity(after) or len(raw) != after.st_size:
        raise ValueError('Environment reference changed during capture')
    return {'size': len(raw), 'mtimeNs': after.st_mtime_ns,
            'sha256': hashlib.sha256(raw).hexdigest()}


def new_manifest(value, platform, definition_hash, marker):
    result = {'schemaVersion': 1, 'platform': platform, 'serviceId': marker, 'settings': value,
              'definitionHash': definition_hash, 'referenceStat': reference_stat(value),
              'state': 'prepared', 'backupId': uuid.uuid4().hex}
    validate(result)
    return result


def validate(value):
    required = {'schemaVersion','platform','serviceId','settings','definitionHash','referenceStat','state','backupId'}
    if (not isinstance(value, dict) or set(value) != required or type(value['schemaVersion']) is not int
            or value['schemaVersion'] != 1 or value['platform'] not in {'macos','linux','windows'}
            or value['state'] not in {'prepared','applied'}):
        raise ValueError('Unsupported service manifest; preserve it for manual inspection')
    validate_settings(value['settings'])
    for key in ('serviceId','backupId'):
        if not isinstance(value[key], str) or not MARKER.fullmatch(value[key]):
            raise ValueError('Malformed service manifest identity')
    if not isinstance(value['definitionHash'], str) or not DIGEST.fullmatch(value['definitionHash']):
        raise ValueError('Malformed service definition hash')
    ref = value['referenceStat']
    if ref is not None:
        # Legacy metadata remains readable for explicit repair. It cannot match
        # a current byte fingerprint, so inspection reports drift until repaired.
        if (not isinstance(ref, dict) or set(ref) not in ({'size','mtimeNs'}, {'size','mtimeNs','sha256'})
                or any(type(ref[key]) is not int or ref[key] < 0 for key in ('size','mtimeNs'))
                or ref['size'] > MAX_BYTES
                or ('sha256' in ref and (not isinstance(ref['sha256'], str) or not DIGEST.fullmatch(ref['sha256'])))):
            raise ValueError('Malformed service reference metadata')
    if bool(value['settings']['opEnvFile']) != (ref is not None):
        raise ValueError('Incoherent service reference metadata')
    return value


def load(path, *, port, platform):
    raw = read_file(path)
    if raw is None:
        return None
    try:
        value = validate(json.loads(raw))
    except (UnicodeError, RecursionError, json.JSONDecodeError, TypeError) as exc:
        raise ValueError('Malformed service manifest; preserve it for manual inspection') from exc
    if value['settings']['port'] != port or value['platform'] != platform:
        raise ValueError('Service manifest belongs to another target')
    return value


def configure_runtime(args):
    global _runtime
    marker = getattr(args, 'service_id', None)
    _runtime = None
    if marker is None:
        return
    if not MARKER.fullmatch(marker) or args.background:
        raise ValueError('Service identity requires a foreground service start')
    from .namespaces import client_home, gateway_home
    # Service mode is explicit, avoiding inherited picker flags changing the
    # recorded launch intent in a service manager's long-lived environment.
    os.environ['ANTIGRAVITY_UNIFIED_MODEL_PICKER'] = '1' if args.unified_model_picker else '0'
    value = {'port': args.port, 'host': args.host, 'unified': bool(args.unified_model_picker),
             'clientHome': str(client_home()), 'stateHome': str(gateway_home()),
             'executable': sys.executable, 'module': MODULE, 'packageVersion': package_version()}
    _runtime = {'serviceId': marker, 'launchHash': runtime_digest(value),
                'packageVersion': value['packageVersion'], 'pid': os.getpid()}


def runtime_identity():
    return dict(_runtime) if _runtime is not None else None
