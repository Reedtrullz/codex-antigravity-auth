"""Observed service intent and explicit registration changes, never PID signalling."""
from __future__ import annotations

from contextlib import ExitStack
import json
import os
from pathlib import Path
import subprocess
import urllib.request
import uuid
import xml.etree.ElementTree as ET

from . import service_manifest as manifest
from .endpoint_policy import open_http_request
from .secure_store import SecureStore, file_lock


def _service():
    from . import service
    return service


def manifest_path(port):
    return _service()._codex_home_read_only() / 'services' / f'gateway-{int(port)}.json'


def definition_path(port, platform):
    service = _service()
    if platform == 'macos':
        return service.macos_launch_agent_path(port)
    if platform == 'linux':
        return service.linux_systemd_unit_path(port)
    return None


def desired_settings(port, host, *, op_env_file=None, op_environment=None, unified_model_picker=False):
    service = _service()
    return manifest.settings(port=port, host=host, unified=unified_model_picker,
                             client_home=service.client_home(home=service._service_home()),
                             state_home=service._codex_home_read_only(),
                             op_env_file=op_env_file, op_environment=op_environment)


def definition(value, marker, platform):
    service = _service()
    command = manifest.command_for(value, marker)
    if platform == 'macos':
        return service.render_macos_launch_agent(value['port'], value['host'], _command=command).encode()
    if platform == 'linux':
        return service.render_linux_systemd_unit(value['port'], value['host'], _command=command).encode()
    if platform == 'windows':
        return subprocess.list2cmdline(command).encode()
    raise ValueError('Unsupported service platform')


def load_record(path, *, port, platform):
    record = manifest.load(path, port=port, platform=platform)
    if record is not None:
        expected = definition(record['settings'], record['serviceId'], platform)
        if manifest.digest(expected.decode()) != record['definitionHash']:
            raise ValueError('Incoherent service definition metadata; preserve recovery files')
    return record


def windows_action(raw):
    if len(raw) > manifest.MAX_BYTES or '<!DOCTYPE' in raw.upper() or '<!ENTITY' in raw.upper():
        raise ValueError('Unsupported scheduled task definition')
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise ValueError('Malformed scheduled task definition') from exc
    actions = root.findall('./{*}Actions/{*}Exec')
    if len(actions) != 1 or len(root.findall('./{*}Actions/*')) != 1:
        raise ValueError('Unsupported scheduled task actions')
    command = actions[0].findtext('{*}Command') or ''
    arguments = actions[0].findtext('{*}Arguments') or ''
    if not command:
        raise ValueError('Scheduled task has no command')
    return (subprocess.list2cmdline([command.strip('"')]) + (' ' + arguments if arguments else '')).encode()


def installed_definition(port, platform):
    path = definition_path(port, platform)
    if path is not None:
        return manifest.read_file(path)
    service = _service()
    command = ['schtasks', '/Query', '/TN', service.service_task_name(port)]
    query = service._run([*command, '/XML', '/HRESULT'])
    if query.returncode == 0:
        return windows_action(str(query.stdout))
    # /HRESULT avoids interpreting localized stderr or treating timeouts,
    # permission failures and a missing schtasks executable as missing tasks.
    # Confirm FILE_NOT_FOUND independently; even then Create omits /F, so a
    # task appearing after inspection cannot be overwritten by this path.
    missing = 0x80070002  # HRESULT_FROM_WIN32(ERROR_FILE_NOT_FOUND)
    if (query.returncode & 0xffffffff) == missing:
        confirmation = service._run([*command, '/FO', 'LIST', '/HRESULT'])
        if (confirmation.returncode & 0xffffffff) == missing:
            return None
    raise ValueError('Scheduled task definition could not be inspected; registration was preserved')


def inspect(port, platform, *, installed):
    path = manifest_path(port)
    result = {'manifest_path': str(path), 'drift': [], 'owned_ready': False}
    try:
        record = load_record(path, port=port, platform=platform)
        if record is None:
            result['drift'] = ['manifest_missing'] if installed else []
            result['identity_status'] = 'unrecorded'
            return result
        value = record['settings']
        result['recorded_version'] = value['packageVersion']
        result['expected_identity'] = {'serviceId': record['serviceId'], 'launchHash': manifest.runtime_digest(value),
                                       'packageVersion': value['packageVersion']}
        result['recovery_paths'] = [str(path.parent / 'backups' / record['backupId']), str(path)]
        if record['state'] != 'applied':
            result['drift'].append('incomplete_registration')
        if not Path(value['executable']).is_file():
            result['drift'].append('interpreter_missing')
        try:
            current = desired_settings(port, value['host'], op_env_file=value['opEnvFile'],
                                       op_environment=value['opEnvironment'], unified_model_picker=value['unified'])
            if current != value:
                result['drift'].append('desired_settings_changed')
            if manifest.reference_stat(value) != record['referenceStat']:
                result['drift'].append('secret_reference_file_changed')
        except (OSError, ValueError):
            result['drift'].append('runtime_reference_unavailable')
        expected = definition(value, record['serviceId'], platform)
        if manifest.digest(expected.decode()) != record['definitionHash']:
            raise ValueError('Incoherent service definition metadata')
        actual = installed_definition(port, platform) if installed else None
        if installed and actual != expected:
            result['drift'].append('installed_definition_changed')
        result['identity_status'] = 'not_checked'
    except (OSError, ValueError, ET.ParseError):
        result['drift'].append('manifest_or_definition_unreadable')
        result['identity_status'] = 'unverified'
    return result


def status(port, *, platform_name=None):
    service = _service()
    platform = platform_name or service.service_platform()
    info = service._platform_status(port, platform_name=platform)
    info['port'] = port
    info.update(inspect(port, platform, installed=bool(info.get('installed'))))
    if info.get('installed') and info['drift']:
        info['state'] = 'degraded'
    return info


def probe_runtime(port, *, timeout=0.75):
    # This endpoint reads only process identity, never account/provider stores.
    headers = {'Accept': 'application/json'}
    token = os.environ.get('ANTIGRAVITY_GATEWAY_TOKEN', '').strip()
    if token:
        headers['Authorization'] = f'Bearer {token}'
    request = urllib.request.Request(f'http://localhost:{int(port)}/health/runtime', headers=headers)
    try:
        with open_http_request(request, timeout=timeout) as response:
            raw = response.read(16385)
        if len(raw) > 16384:
            return None
        payload = json.loads(raw)
        return payload.get('service') if isinstance(payload, dict) else None
    except (OSError, ValueError, RecursionError):
        return None


def observe(info, gateway, *, identity=None):
    expected = info.get('expected_identity')
    port = gateway.get('port', info.get('port'))
    if identity is None and gateway.get('reachable') and type(port) is int:
        identity = probe_runtime(port)
    matched = (isinstance(expected, dict) and isinstance(identity, dict)
               and all(identity.get(key) == value for key, value in expected.items())
               and type(identity.get('pid')) is int and identity['pid'] > 0)
    owned = bool(matched and info.get('installed') and info.get('active')
                 and gateway.get('reachable') and not info.get('drift') and not info.get('error'))
    base = _service()._service_result(info, action=info.get('action', 'status'), changed=bool(info.get('changed')), error=info.get('error'))
    result = {**base, 'owned_ready': owned, 'reachable': bool(gateway.get('reachable')),
              'identity_status': 'matched' if matched else ('foreign_or_unverified' if gateway.get('reachable') else 'unreachable')}
    if info.get('error') or info.get('state') == 'failed':
        result['state'] = 'failed'
    elif owned:
        result['state'] = 'ready'
    elif info.get('installed') and (info.get('drift') or gateway.get('reachable')):
        result['state'] = 'degraded'
    return result


def _owned_definition(raw, record, platform):
    if raw is None:
        return True  # Confirmed missing registration can be restored.
    expected = definition(record['settings'], record['serviceId'], platform)
    if raw == expected:
        return True
    if platform not in {'macos', 'linux'}:
        return False
    # Permit only canonical loopback-host/picker flag edits. Comparing the
    # entire generated definition preserves executable, wrapper, Label, unit
    # commands, namespace paths and unknown manager settings as one boundary.
    # Searching for familiar argv tokens does not prove service ownership.
    for host in ('127.0.0.1', 'localhost', '::1'):
        for unified in (False, True):
            value = {**record['settings'], 'host': host, 'unified': unified}
            if raw == definition(value, record['serviceId'], platform):
                return True
    return False


def _activate(port, platform, path, command, *, replace_existing=False):
    service = _service()
    if platform == 'macos':
        target = f'gui/{service._launchd_uid()}'
        stop = service._run(['launchctl', 'bootout', target, str(path)])
        start = service._run(['launchctl', 'bootstrap', target, str(path)])
        if start.returncode:
            return [stop, start], False
        enable = service._run(['launchctl', 'enable', f'{target}/{service.service_label(port)}'])
        return [stop, start, enable], enable.returncode == 0
    if platform == 'linux':
        reload = service._run(['systemctl', '--user', 'daemon-reload'])
        if reload.returncode:
            return [reload], False
        enable = service._run(['systemctl', '--user', 'enable', path.name])
        if enable.returncode:
            return [reload, enable], False
        restart = service._run(['systemctl', '--user', 'restart', path.name])
        return [reload, enable, restart], all(item.returncode == 0 for item in (reload, enable, restart))
    create = service._run(['schtasks', '/Create', *(['/F'] if replace_existing else []),
                          '/SC', 'ONLOGON', '/TN', service.service_task_name(port), '/TR', command])
    run = service._run(['schtasks', '/Run', '/TN', service.service_task_name(port)]) if create.returncode == 0 else None
    return [item for item in (create, run) if item is not None], bool(run is not None and run.returncode == 0)


def install(port, host, *, platform_name=None, op_env_file=None, op_environment=None, unified_model_picker=False):
    service = _service()
    platform = platform_name or service.service_platform()
    if os.environ.get('SUDO_USER') and getattr(os, 'geteuid', lambda: 1)() == 0:
        raise ValueError('Install user services without sudo to preserve target-user ownership')
    value = desired_settings(port, host, op_env_file=op_env_file, op_environment=op_environment,
                             unified_model_picker=unified_model_picker)
    marker = uuid.uuid4().hex
    raw = definition(value, marker, platform)
    record = manifest.new_manifest(value, platform, manifest.digest(raw.decode()), marker)
    target, path = definition_path(port, platform), manifest_path(port)
    with ExitStack() as locks:
        locks.enter_context(file_lock(path))
        if target is not None:
            locks.enter_context(file_lock(target))
        before_manifest = manifest.read_file(path)
        previous = load_record(path, port=port, platform=platform)
        before_definition = installed_definition(port, platform)
        if before_definition is not None and (previous is None or not _owned_definition(before_definition, previous, platform)):
            raise ValueError('Existing service has no matching ownership record; inspect and uninstall it before installing')
        backups = path.parent / 'backups' / record['backupId']
        evidence = []
        observed = {'platform': platform, 'installed': before_definition is not None, 'active': False, 'reachable': False}
        try:
            for name, content in (('intent.json', before_manifest), ('service-definition', before_definition)):
                if content is not None:
                    SecureStore().atomic_write_bytes(backups / name, content)
            manifest.write_json(path, record)
            if target is not None:
                SecureStore().atomic_write_bytes(target, raw)
                observed['installed'] = True
            evidence, success = _activate(port, platform, target, raw.decode(), replace_existing=before_definition is not None)
            observed = service._platform_status(port, platform_name=platform)
            success = success and observed.get('installed') and observed.get('active')
            if not success:
                raise RuntimeError('Service registration was not observed as active')
            record['state'] = 'applied'
            manifest.write_json(path, record)
        except (OSError, ValueError, RuntimeError) as exc:
            return service._service_result(
                {**observed,
                 'recovery_paths': [str(backups), str(path), *([str(target)] if target else [])]},
                action='install', changed=True, error=f'Service registration incomplete ({type(exc).__name__}); preserve recovery paths',
                commands=tuple(service._command_evidence(item) for item in evidence))
        result = status(port, platform_name=platform)
        return {**result, 'action': 'install', 'changed': True,
                'commands': [service._command_evidence(item) for item in evidence]}


def _repair(port, *, write=False, platform_name=None, **overrides):
    service = _service()
    platform = platform_name or service.service_platform()
    record = load_record(manifest_path(port), port=port, platform=platform)
    if record is None:
        raise ValueError('No service intent recorded; inspect and reinstall the service with explicit settings')
    old = record['settings']
    options = {'host': old['host'], 'op_env_file': old['opEnvFile'],
               'op_environment': old['opEnvironment'], 'unified_model_picker': old['unified']}
    options.update({key: value for key, value in overrides.items() if value is not None})
    if overrides.get('op_env_file') is not None:
        options['op_environment'] = None
    elif overrides.get('op_environment') is not None:
        options['op_env_file'] = None
    if options.pop('clear_secret_runtime', False):
        options.update(op_env_file=None, op_environment=None)
    desired = desired_settings(port, **options)
    if not write:
        return {**status(port, platform_name=platform), 'action': 'repair', 'changed': False,
                'plan': {'executable_changed': desired['executable'] != old['executable'],
                         'version_changed': desired['packageVersion'] != old['packageVersion'],
                         'settings_changed': desired != old, 'write_required': True}}
    result = install(port, platform_name=platform, **options)
    result['action'] = 'repair'
    return result


def _restart(port, *, write=False, platform_name=None):
    service = _service()
    platform = platform_name or service.service_platform()
    info = status(port, platform_name=platform)
    if not write:
        return {**info, 'action': 'restart', 'changed': False, 'plan': {'write_required': True}}
    if not info.get('installed') or info.get('drift'):
        raise ValueError('Service configuration is not verified; inspect and repair before restart')
    if platform == 'macos':
        commands = [['launchctl', 'kickstart', '-k', f'gui/{service._launchd_uid()}/{service.service_label(port)}']]
    elif platform == 'linux':
        commands = [['systemctl', '--user', 'restart', service.linux_systemd_unit_path(port).name]]
    else:
        commands = [['schtasks', '/End', '/TN', service.service_task_name(port)],
                    ['schtasks', '/Run', '/TN', service.service_task_name(port)]]
    evidence = [service._run(command) for command in commands]
    result = status(port, platform_name=platform)
    error = None if all(item.returncode == 0 for item in evidence) and result.get('active') else 'Service restart was not observed as active'
    return service._service_result(result, action='restart', changed=True, error=error,
                                   commands=tuple(service._command_evidence(item) for item in evidence))


def _mutation(operation, port, *, write=False, platform_name=None, **kwargs):
    platform = platform_name or _service().service_platform()
    if not write:
        return operation(port, write=False, platform_name=platform, **kwargs)
    with ExitStack() as locks:
        locks.enter_context(file_lock(manifest_path(port)))
        target = definition_path(port, platform)
        if target is not None:
            locks.enter_context(file_lock(target))
        return operation(port, write=True, platform_name=platform, **kwargs)


def repair(port, *, write=False, platform_name=None, **overrides):
    return _mutation(_repair, port, write=write, platform_name=platform_name, **overrides)


def restart(port, *, write=False, platform_name=None):
    return _mutation(_restart, port, write=write, platform_name=platform_name)
