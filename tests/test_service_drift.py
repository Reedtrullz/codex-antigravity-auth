"""Temporary service-manager fixtures and synthetic runtime identity only."""
from argparse import Namespace
from copy import deepcopy
import json
import os
from pathlib import Path
import plistlib
import socket
import subprocess
import sys
from unittest.mock import Mock
from urllib.parse import urlparse
import xml.etree.ElementTree as ET

import pytest
from fake_upstream import upstream

from codex_antigravity_auth import cli, service, service_drift as drift, service_manifest as manifest


@pytest.fixture
def host(tmp_path, monkeypatch):
    monkeypatch.setattr(service, '_service_home', lambda: tmp_path)
    monkeypatch.setattr(service, '_codex_home_read_only', lambda: tmp_path / 'state')
    monkeypatch.setattr(service, 'client_home', lambda **kwargs: tmp_path / 'client')
    monkeypatch.setattr(service, 'macos_launch_agent_path', lambda port: tmp_path / 'launch' / f'{port}.plist')
    monkeypatch.setattr(service, 'linux_systemd_unit_path', lambda port: tmp_path / 'units' / f'{port}.service')
    monkeypatch.setattr(manifest, 'package_version', lambda: '2.4.2')
    monkeypatch.delenv('SUDO_USER', raising=False)
    monkeypatch.setattr(drift, 'probe_runtime', lambda *a, **k: None)
    class Host:
        active = False
        enabled = False
        windows_raw = None
        fail = None
        calls = []
        def run(self, command, **kwargs):
            self.calls.append(command)
            code, text = 0, ''
            if self.fail and self.fail in command:
                return subprocess.CompletedProcess(command, 1, '', 'synthetic manager failure')
            if command[0] == 'launchctl':
                if command[1] == 'bootstrap': self.active = True
                if command[1] == 'bootout': self.active = False
                if command[1] == 'print':
                    code, text = (0, 'state = running\n') if self.active else (1, '')
            elif command[0] == 'systemctl':
                if 'enable' in command: self.enabled = True
                if 'restart' in command: self.active = True
                if 'is-enabled' in command: code = 0 if self.enabled else 1
                if 'is-active' in command: code = 0 if self.active else 1
            elif command[0] == 'schtasks':
                if '/Create' in command:
                    record = manifest.load(drift.manifest_path(51122), port=51122, platform='windows')
                    args = manifest.command_for(record['settings'], record['serviceId'])
                    task = ET.Element('Task', xmlns='http://schemas.microsoft.com/windows/2004/02/mit/task')
                    action = ET.SubElement(ET.SubElement(task, 'Actions'), 'Exec')
                    ET.SubElement(action, 'Command').text = args[0]
                    ET.SubElement(action, 'Arguments').text = subprocess.list2cmdline(args[1:])
                    self.windows_raw = ET.tostring(task, encoding='unicode')
                elif '/Query' in command:
                    if self.windows_raw is None: code = 0x80070002 if '/HRESULT' in command else 1
                    elif '/XML' in command: text = self.windows_raw
                    else: text = 'Status: Running\n' if self.active else 'Status: Ready\n'
                elif '/Run' in command: self.active = True
                elif '/End' in command: self.active = False
            else:
                raise AssertionError('Unexpected service-manager command')
            return subprocess.CompletedProcess(command, code, text, '')
    fake = Host()
    fake.calls = []
    monkeypatch.setattr(service, '_run', fake.run)
    return fake


@pytest.mark.parametrize('platform', ['macos', 'linux', 'windows'])
def test_install_records_nonsecret_intent_and_requires_matching_runtime(host, platform):
    result = service.install_service(51122, '127.0.0.1', platform_name=platform)
    assert result['installed'] and result['active'] and not result['drift']
    assert not result['owned_ready']
    gateway = {'port': 51122, 'reachable': True}
    identity = {**result['expected_identity'], 'pid': 123}
    assert drift.observe(result, gateway, identity=identity)['owned_ready']
    assert drift.observe(result, gateway, identity={**identity, 'serviceId': 'f' * 32})['state'] == 'degraded'
    assert not drift.observe(result, gateway, identity={**identity, 'launchHash': 'f' * 64})['owned_ready']
    path = drift.manifest_path(51122)
    saved = json.loads(path.read_text())
    assert saved['state'] == 'applied'
    assert 'fixture-secret-value' not in path.read_text()
    assert '--service-id' in manifest.command_for(saved['settings'], saved['serviceId'])
    if os.name != 'nt': assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize('platform', ['macos', 'linux', 'windows'])
def test_repair_preview_is_read_only_and_write_updates_version_and_keeps_backup(host, monkeypatch, platform):
    service.install_service(51122, '127.0.0.1', platform_name=platform)
    path = drift.manifest_path(51122)
    original = path.read_bytes()
    previous = json.loads(original)
    monkeypatch.setattr(manifest, 'package_version', lambda: '2.4.3')
    count = len(host.calls)
    plan = service.repair_service(51122, platform_name=platform)
    assert plan['plan']['version_changed'] and not plan['changed']
    assert path.read_bytes() == original
    assert all('restart' not in row and '/Run' not in row and 'bootstrap' not in row for row in host.calls[count:])
    result = service.repair_service(51122, platform_name=platform, write=True)
    assert result['changed'] and not result['drift']
    record = json.loads(path.read_text())
    assert record['serviceId'] != previous['serviceId']
    backup = path.parent / 'backups' / record['backupId']
    assert (backup / 'intent.json').read_bytes() == original
    assert (backup / 'service-definition').is_file()
    assert record['settings']['packageVersion'] == '2.4.3'


@pytest.mark.parametrize('platform', ['macos', 'linux'])
def test_changed_flags_report_drift_and_repair_restores_only_owned_target(host, platform):
    service.install_service(51122, '127.0.0.1', platform_name=platform)
    target = drift.definition_path(51122, platform)
    original = target.read_bytes()
    target.write_bytes(original.replace(b'127.0.0.1', b'localhost'))
    info = service.service_status(51122, platform_name=platform)
    assert 'installed_definition_changed' in info['drift']
    with pytest.raises(ValueError, match='repair'):
        service.restart_service(51122, platform_name=platform, write=True)
    repaired = service.repair_service(51122, platform_name=platform, write=True)
    assert not repaired['drift']
    target.write_bytes(target.read_bytes().replace(b'codex_antigravity_auth.cli', b'unrelated.module'))
    before = len(host.calls)
    with pytest.raises(ValueError, match='ownership'):
        service.repair_service(51122, platform_name=platform, write=True)
    assert len(host.calls) == before


@pytest.mark.parametrize('platform', ['macos', 'linux', 'windows'])
def test_explicit_restart_uses_only_owned_service_name(host, platform):
    service.install_service(51122, '127.0.0.1', platform_name=platform)
    path = drift.manifest_path(51122)
    original = path.read_bytes()
    plan = service.restart_service(51122, platform_name=platform)
    assert not plan['changed']
    before = len(host.calls)
    result = service.restart_service(51122, platform_name=platform, write=True)
    assert result['changed'] and result['active'] and path.read_bytes() == original
    commands = host.calls[before:]
    assert all(row[0] in {'launchctl', 'systemctl', 'schtasks'} for row in commands)
    assert not any('/PID' in row or 'taskkill' in row or 'kill' in row for row in commands)


def test_deleted_interpreter_is_reported_and_repair_selects_current_python(host, tmp_path, monkeypatch):
    executable = tmp_path / 'old-python'
    executable.touch()
    current = sys.executable
    monkeypatch.setattr(sys, 'executable', str(executable))
    service.install_service(51122, '127.0.0.1', platform_name='macos')
    executable.unlink()
    monkeypatch.setattr(sys, 'executable', current)
    info = service.service_status(51122, platform_name='macos')
    assert set(info['drift']) >= {'interpreter_missing', 'desired_settings_changed'}
    result = service.repair_service(51122, platform_name='macos', write=True)
    assert not result['drift']


def test_reference_file_changes_are_detected_without_copying_contents(host, tmp_path, monkeypatch):
    path = tmp_path / 'fixture.env'
    path.write_text('API_KEY=fixture-secret-value\n')
    monkeypatch.setattr('codex_antigravity_auth.onepassword.shutil.which', lambda _: '/fixture/op')
    service.install_service(51122, '127.0.0.1', platform_name='macos', op_env_file=str(path))
    record = drift.manifest_path(51122).read_text()
    assert 'fixture-secret-value' not in record
    path.write_text('API_KEY=fixture-different-value\n')
    assert 'secret_reference_file_changed' in service.service_status(51122, platform_name='macos')['drift']
    service.repair_service(51122, platform_name='macos', write=True, op_environment='fixture-environment')
    saved = json.loads(drift.manifest_path(51122).read_text())
    assert saved['settings']['opEnvFile'] is None and saved['settings']['opEnvironment'] == 'fixture-environment'


@pytest.mark.parametrize('platform,failure', [('macos','bootstrap'), ('linux','restart'), ('windows','/Create')])
def test_failed_repair_retains_old_bytes_and_recovery_paths(host, platform, failure):
    service.install_service(51122, '127.0.0.1', platform_name=platform)
    path = drift.manifest_path(51122)
    original = path.read_bytes()
    host.fail = failure
    result = service.repair_service(51122, platform_name=platform, write=True, unified_model_picker=True)
    assert result['state'] == 'failed' and not result.get('owned_ready')
    assert result['recovery_paths']
    record = json.loads(path.read_text())
    assert record['state'] == 'prepared'
    backup = path.parent / 'backups' / record['backupId']
    assert (backup / 'intent.json').read_bytes() == original
    assert 'incomplete_registration' in service.service_status(51122, platform_name=platform)['drift']


@pytest.mark.parametrize('mutation', ['version','missing','shape','hash','incoherent'])
def test_corrupt_intent_is_preserved_and_cannot_authorize_repair(host, mutation):
    service.install_service(51122, '127.0.0.1', platform_name='macos')
    path = drift.manifest_path(51122)
    record = json.loads(path.read_text())
    if mutation == 'version': record['schemaVersion'] = 99
    elif mutation == 'missing': del record['settings']['module']
    elif mutation == 'shape': record['settings']['unified'] = 'false'
    elif mutation == 'hash': record['definitionHash'] = 'not-a-digest'
    elif mutation == 'incoherent': record['definitionHash'] = '0' * 64
    path.write_text(json.dumps(record))
    original = path.read_bytes()
    calls = len(host.calls)
    with pytest.raises(ValueError):
        service.repair_service(51122, platform_name='macos', write=True)
    assert path.read_bytes() == original and len(host.calls) == calls


def test_runtime_identity_uses_actual_launch_settings_without_paths_in_response(tmp_path, monkeypatch):
    monkeypatch.setenv('CODEX_HOME', str(tmp_path / 'client'))
    monkeypatch.setenv('ANTIGRAVITY_STATE_HOME', str(tmp_path / 'state'))
    monkeypatch.setattr(manifest, '_runtime', None)
    args = Namespace(service_id='a' * 32, port=51122, host='127.0.0.1', unified_model_picker=False, background=False)
    manifest.configure_runtime(args)
    identity = manifest.runtime_identity()
    assert identity['serviceId'] == 'a' * 32 and type(identity['pid']) is int
    assert str(tmp_path) not in json.dumps(identity)
    manifest.configure_runtime(Namespace(service_id=None))
    assert manifest.runtime_identity() is None


def test_runtime_endpoint_avoids_account_provider_and_catalog_access(monkeypatch):
    from fastapi.testclient import TestClient
    from codex_antigravity_auth import server
    monkeypatch.setattr(manifest, 'runtime_identity', lambda: {'serviceId': 'fixture'})
    monkeypatch.setattr(server, 'provider_health_catalog_fail_soft', Mock(side_effect=AssertionError('no catalog')))
    monkeypatch.setattr(server, 'account_health_summary', Mock(side_effect=AssertionError('no accounts')))
    response = TestClient(server.app, client=('127.0.0.1', 51199), base_url='http://localhost').get('/health/runtime')
    assert response.status_code == 200 and response.json()['service'] == {'serviceId': 'fixture'}


def test_loopback_runtime_probe_observes_only_its_synthetic_listener(tmp_path, monkeypatch):
    # Real bounded probe and HTTP policy, still under the credential-free runner.
    identity = {'serviceId': 'a'*32, 'launchHash': 'b'*64, 'packageVersion': '2.4.2', 'pid': 123}
    body = json.dumps({'ok': True, 'service': identity}).encode()
    original_getaddrinfo = socket.getaddrinfo
    monkeypatch.setenv('ANTIGRAVITY_GATEWAY_TOKEN', 'fixture-runtime-token')
    with upstream((200, {'Content-Type': 'application/json'}, body)) as (base, requests):
        port = urlparse(base).port

        def resolve_owned_localhost(name, destination_port, *args, **kwargs):
            if name == 'localhost' and destination_port == port:
                name = '127.0.0.1'
            return original_getaddrinfo(name, destination_port, *args, **kwargs)

        monkeypatch.setattr(socket, 'getaddrinfo', resolve_owned_localhost)
        assert drift.probe_runtime(port) == identity
        assert len(requests) == 1 and requests[0]['path'] == '/health/runtime'
        assert requests[0]['headers']['Authorization'] == 'Bearer fixture-runtime-token'
        assert requests[0]['body'] == ''


@pytest.mark.parametrize('phase', ['backup','intent','definition'])
def test_atomic_publication_failure_preserves_originals_and_reports_recovery(host, monkeypatch, phase):
    service.install_service(51122, '127.0.0.1', platform_name='macos')
    path, target = drift.manifest_path(51122), drift.definition_path(51122, 'macos')
    before_record, before_definition = path.read_bytes(), target.read_bytes()
    original = drift.SecureStore.atomic_write_bytes
    def fail(self, destination, value, **kwargs):
        if ((phase == 'backup' and destination.name == 'intent.json')
                or (phase == 'intent' and destination == path)
                or (phase == 'definition' and destination == target)):
            raise OSError('fixture write failure')
        return original(self, destination, value, **kwargs)
    monkeypatch.setattr(drift.SecureStore, 'atomic_write_bytes', fail)
    calls = len(host.calls)
    result = service.repair_service(51122, platform_name='macos', write=True)
    assert result['state'] == 'failed' and result['recovery_paths']
    assert target.read_bytes() == before_definition
    if phase in {'backup','intent'}:
        assert path.read_bytes() == before_record
    else:
        assert json.loads(path.read_text())['state'] == 'prepared'
    assert len(host.calls) == calls


def test_failed_manager_reload_does_not_restart_old_command(host):
    service.install_service(51122, '127.0.0.1', platform_name='linux')
    host.fail = 'daemon-reload'
    calls = len(host.calls)
    result = service.repair_service(51122, platform_name='linux', write=True)
    assert result['state'] == 'failed'
    assert not any('restart' in row for row in host.calls[calls:])


@pytest.mark.parametrize('target_kind', ['definition','intent'])
def test_symlinked_service_state_cannot_authorize_mutation(host, tmp_path, target_kind):
    service.install_service(51122, '127.0.0.1', platform_name='macos')
    target = drift.manifest_path(51122) if target_kind == 'intent' else drift.definition_path(51122, 'macos')
    raw = target.read_bytes()
    destination = tmp_path / 'unrelated'
    destination.write_bytes(raw)
    target.unlink(); target.symlink_to(destination)
    calls = len(host.calls)
    with pytest.raises((ValueError, OSError)):
        service.repair_service(51122, platform_name='macos', write=True)
    assert destination.read_bytes() == raw and len(host.calls) == calls


def test_cli_reports_foreign_healthy_port_as_degraded_and_failed_repair_keeps_paths(host, monkeypatch, capsys):
    monkeypatch.setattr(service, 'service_platform', lambda: 'macos')
    service.install_service(51122, '127.0.0.1', platform_name='macos')
    monkeypatch.setattr(cli, 'reachable_gateway_status_info', lambda *a, **kw: {
        'port': 51122, 'status': 'unmanaged', 'reachable': True, 'running': False,
        'reachable_model_count': 1, 'reachable_base_url': 'http://localhost:51122/v1'})
    monkeypatch.setattr(sys, 'argv', ['codex-antigravity', 'service', 'status', '--json'])
    with pytest.raises(SystemExit) as caught:
        cli.main()
    assert caught.value.code == 0
    result = json.loads(capsys.readouterr().out)
    assert result['status'] == 'degraded'
    assert result['data']['service']['identity_status'] == 'foreign_or_unverified'
    assert not result['data']['service']['owned_ready']
    host.fail = 'bootstrap'
    monkeypatch.setattr(sys, 'argv', ['codex-antigravity', 'service', 'repair', '--write', '--json'])
    with pytest.raises(SystemExit) as caught:
        cli.main()
    assert caught.value.code == 1
    result = json.loads(capsys.readouterr().out)
    assert result['status'] == 'failed'
    assert result['data']['service']['recovery_paths']


def test_cli_repair_preview_never_registers_or_changes_intent(host, monkeypatch, capsys):
    monkeypatch.setattr(service, 'service_platform', lambda: 'macos')
    service.install_service(51122, '127.0.0.1', platform_name='macos')
    path = drift.manifest_path(51122)
    original = path.read_bytes()
    monkeypatch.setattr(cli, 'reachable_gateway_status_info', lambda *a, **k: {'port':51122, 'reachable':False})
    calls = len(host.calls)
    monkeypatch.setattr(sys, 'argv', ['codex-antigravity', 'service', 'repair', '--unified-model-picker', '--json'])
    with pytest.raises(SystemExit) as caught:
        cli.main()
    assert caught.value.code == 0
    result = json.loads(capsys.readouterr().out)
    assert result['data']['service']['plan']['settings_changed']
    assert path.read_bytes() == original
    assert not any('bootstrap' in row or 'bootout' in row for row in host.calls[calls:])


@pytest.mark.parametrize('unified', [False, True])
def test_generated_start_command_reports_the_recorded_runtime_identity(host, monkeypatch, unified):
    service.install_service(51122, '127.0.0.1', platform_name='macos', unified_model_picker=unified)
    record = json.loads(drift.manifest_path(51122).read_text())
    command = manifest.command_for(record['settings'], record['serviceId'])
    captured = []
    monkeypatch.setattr('codex_antigravity_auth.process_logs.run_gateway', lambda *a, **k: captured.append(manifest.runtime_identity()))
    monkeypatch.setattr(manifest, '_runtime', None)
    monkeypatch.setenv('CODEX_HOME', record['settings']['clientHome'])
    monkeypatch.setenv('ANTIGRAVITY_STATE_HOME', record['settings']['stateHome'])
    monkeypatch.setenv('ANTIGRAVITY_UNIFIED_MODEL_PICKER', '0' if unified else '1')
    monkeypatch.setattr(sys, 'argv', ['codex-antigravity', *command[3:]])
    cli.main()
    assert captured == [{'serviceId': record['serviceId'], 'launchHash': manifest.runtime_digest(record['settings']),
                         'packageVersion': '2.4.2', 'pid': os.getpid()}]


@pytest.mark.parametrize('platform', ['macos', 'linux'])
@pytest.mark.parametrize('change', ['executable', 'wrapper', 'manager_identity', 'extra_command', 'unknown_argument'])
def test_repair_refuses_foreign_definition_even_when_known_tokens_remain(host, tmp_path, monkeypatch, platform, change):
    from codex_antigravity_auth.onepassword import shutil
    monkeypatch.setattr(shutil, 'which', lambda _: '/fixture/op')
    service.install_service(51122, '127.0.0.1', platform_name=platform, op_environment='fixture-environment')
    target = drift.definition_path(51122, platform)
    intent = drift.manifest_path(51122)
    record = json.loads(intent.read_text())
    command = manifest.command_for(record['settings'], record['serviceId'])
    if change == 'executable': command[command.index('-m') - 1] = '/fixture/unrelated-python'
    elif change == 'wrapper': command[0] = '/fixture/unrelated-wrapper'
    elif change == 'unknown_argument': command.append('--unrecognized-fixture')
    if platform == 'macos':
        changed = service.render_macos_launch_agent(51122, '127.0.0.1', _command=command).encode()
        if change in {'manager_identity', 'extra_command'}:
            assert changed == target.read_bytes()  # Refusal must target the edit, not reformatting.
        if change == 'manager_identity':
            changed = changed.replace(service.service_label(51122).encode(), b'fixture.unrelated.service')
        elif change == 'extra_command':
            changed = changed.replace(b'<key>ProgramArguments</key>', b'<key>Program</key><string>/fixture/unrelated-command</string><key>ProgramArguments</key>')
    else:
        import shlex
        raw = target.read_text()
        old_line = next(line for line in raw.splitlines() if line.startswith('ExecStart='))
        new_line = 'ExecStart=' + ' '.join(shlex.quote(part).replace('%', '%%') for part in command)
        raw = raw.replace(old_line, new_line)
        if change == 'manager_identity': raw = raw.replace('Type=simple', 'Type=simple\nUser=fixture-other')
        elif change == 'extra_command': raw = raw.replace('[Service]', '[Service]\nExecStartPre=/fixture/unrelated-command')
        changed = raw.encode()
    target.write_bytes(changed)
    before = intent.read_bytes()
    calls = len(host.calls)
    with pytest.raises(ValueError, match='ownership'):
        service.repair_service(51122, platform_name=platform, write=True)
    assert target.read_bytes() == changed and intent.read_bytes() == before
    assert len(host.calls) == calls


@pytest.mark.parametrize('platform', ['macos', 'linux'])
def test_only_recognized_canonical_flag_drift_can_be_repaired(host, platform):
    service.install_service(51122, '127.0.0.1', platform_name=platform)
    path = drift.manifest_path(51122)
    record = json.loads(path.read_text())
    target = drift.definition_path(51122, platform)
    target.write_bytes(drift.definition({**record['settings'], 'host':'::1', 'unified':True}, record['serviceId'], platform))
    result = service.repair_service(51122, platform_name=platform, write=True)
    assert not result['drift']
    assert json.loads(path.read_text())['settings']['unified'] is False


@pytest.mark.parametrize('code', [1, 124, 127, 0x80070005, -2147024891])
@pytest.mark.parametrize('existing', [False, True])
def test_failed_windows_xml_inspection_never_authorizes_create(host, monkeypatch, code, existing):
    if existing:
        service.install_service(51122, '127.0.0.1', platform_name='windows')
    path = drift.manifest_path(51122)
    before = path.read_bytes() if path.exists() else None
    previous_definition = host.windows_raw
    original = host.run
    attempts = []
    def run(command, **kwargs):
        attempts.append(command)
        if '/Query' in command and '/XML' in command:
            return subprocess.CompletedProcess(command, code, '', 'fixture query failure')
        return original(command, **kwargs)
    monkeypatch.setattr(service, '_run', run)
    with pytest.raises(ValueError, match='could not be inspected'):
        service.install_service(51122, '127.0.0.1', platform_name='windows')
    assert not any('/Create' in command or '/Run' in command or '/End' in command for command in attempts)
    assert (path.read_bytes() if path.exists() else None) == before
    assert host.windows_raw == previous_definition


@pytest.mark.parametrize('confirmation', [0, 1, 124, 0x80070005])
def test_windows_xml_missing_requires_an_independent_absence_confirmation(host, monkeypatch, confirmation):
    service.install_service(51122, '127.0.0.1', platform_name='windows')
    path = drift.manifest_path(51122)
    before = path.read_bytes()
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0x80070002 if '/XML' in command else confirmation, '', '')
    monkeypatch.setattr(service, '_run', run)
    with pytest.raises(ValueError, match='could not be inspected'):
        service.repair_service(51122, platform_name='windows', write=True)
    assert len(calls) == 2 and all('/Query' in command and '/HRESULT' in command for command in calls)
    assert path.read_bytes() == before


def test_confirmed_missing_windows_task_is_created_without_force(host):
    service.install_service(51122, '127.0.0.1', platform_name='windows')
    creates = [command for command in host.calls if '/Create' in command]
    assert len(creates) == 1 and '/F' not in creates[0]
    service.repair_service(51122, platform_name='windows', write=True)
    creates = [command for command in host.calls if '/Create' in command]
    assert len(creates) == 2 and '/F' in creates[1]


@pytest.mark.parametrize('peer,hostname,expected', [('127.0.0.1','localhost',200), ('::1','localhost',200),
    ('192.0.2.1','localhost',403), ('2001:db8::1','localhost',403), ('127.0.0.1','example.invalid',403), (None,'localhost',403)])
def test_runtime_endpoint_requires_loopback_peer_and_host_even_with_remote_token(monkeypatch, peer, hostname, expected):
    from fastapi.testclient import TestClient
    from codex_antigravity_auth import server
    monkeypatch.setenv('ANTIGRAVITY_ALLOW_REMOTE', '1')
    token = 'fixture-only-' + 'x' * 40
    monkeypatch.setenv('ANTIGRAVITY_GATEWAY_TOKEN', token)
    identity = {'serviceId':'a' * 32, 'pid':123}
    monkeypatch.setattr(manifest, 'runtime_identity', lambda: identity)
    response = TestClient(server.app, client=(peer, 51200) if peer is not None else None, base_url='http://' + hostname).get(
        '/health/runtime', headers={'Authorization':'Bearer ' + token})
    assert response.status_code == expected
    if expected != 200:
        assert 'serviceId' not in response.text and '123' not in response.text


def test_windows_task_appearing_after_absence_is_never_force_replaced(host, monkeypatch):
    original = host.run
    foreign = '<Task><Actions><Exec><Command>fixture-other</Command></Exec></Actions></Task>'
    attempts = []
    def run(command, **kwargs):
        attempts.append(command)
        if '/Create' in command:
            assert '/F' not in command
            host.windows_raw = foreign
            return subprocess.CompletedProcess(command, 1, '', 'fixture already exists')
        return original(command, **kwargs)
    monkeypatch.setattr(service, '_run', run)
    result = service.install_service(51122, '127.0.0.1', platform_name='windows')
    assert result['state'] == 'failed' and host.windows_raw == foreign
    assert not any('/Run' in command or '/End' in command for command in attempts)


def test_signed_hresult_absence_is_confirmed_without_localized_error_text(host, monkeypatch):
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, -2147024894, '', 'fixture localized text')
    monkeypatch.setattr(service, '_run', run)
    assert drift.installed_definition(51122, 'windows') is None
    assert len(calls) == 2 and all('/HRESULT' in command for command in calls)
