import json
from types import SimpleNamespace

import httpx
import pytest

from tests.test_wav_audio import fixture, bridge, endpoint, response, upstream, server
from tests.test_gemini_music_review import arguments, result
from tests.test_wav_audio import listen_argv


def test_bound_listen_verifies_inventory_and_posts_once(fixture, monkeypatch, capsys, tmp_path):
    anti, _, _, first, _ = fixture
    inventory = __import__('fastapi.testclient', fromlist=['TestClient']).TestClient(server.app).get('/v1/account-bindings?model=gemini-3.8-flash').json()
    row = inventory['accounts'][0]
    path = tmp_path / 'binding.json'
    path.write_text(json.dumps({'schemaVersion': 1, 'gatewayInstance': inventory['gatewayInstance'], 'accountRef': row['accountRef'], 'inventorySha256': inventory['inventorySha256']}))
    calls = bridge(monkeypatch, anti)
    with upstream(response('A piano observation.')) as (base, seen):
        endpoint(monkeypatch, base)
        assert anti.main(listen_argv(first, '--account-binding-json', str(path))) == 0
    assert calls[0] == ('GET', '/v1/account-bindings')
    assert len(seen) == 1
    value = json.loads(capsys.readouterr().out)
    assert value['metadata']['account_binding_verified_before_attempt'] is True
    assert value['metadata']['account_binding_gateway_instance'] == inventory['gatewayInstance']
    assert row['accountRef'] not in json.dumps(value)


def binding_file(tmp_path, *, account_ref='acct_' + 'b' * 12, inventory='c' * 64):
    path = tmp_path / 'binding.json'
    path.write_text(json.dumps({
        'schemaVersion': 1,
        'gatewayInstance': 'a' * 32,
        'accountRef': account_ref,
        'inventorySha256': inventory,
    }))
    return path


def test_helper_binding_file_is_local_redacted_and_dry_run_has_no_dispatch(fixture, monkeypatch, capsys, tmp_path):
    anti, _, _, first, _ = fixture
    path = binding_file(tmp_path)
    calls = bridge(monkeypatch, anti)
    assert anti.main(arguments(first, '--account-binding-json', str(path), '--dry-run')) == 0
    assert calls == []
    output = capsys.readouterr().out
    assert 'b' * 12 not in output
    assert 'account-binding-json' in output or 'binding' in output.lower()


def test_helper_binding_file_rejects_stale_or_oversized_data(fixture, tmp_path):
    anti, _, _, _, _ = fixture
    path = binding_file(tmp_path)
    valid = json.loads(path.read_text())
    path.write_text(json.dumps({**valid, 'email': 'primary@example.invalid'}))
    with pytest.raises(anti.AntiError, match='fields'):
        anti.load_account_binding_file(str(path.resolve()))


def test_binding_requires_a_native_gemini_model_even_in_dry_run(fixture, monkeypatch, capsys, tmp_path):
    anti, _, _, first, _ = fixture
    path = binding_file(tmp_path)
    calls = bridge(monkeypatch, anti)
    assert anti.main(arguments(first, '--model', 'sonnet', '--account-binding-json', str(path), '--dry-run')) == 1
    assert calls == []
    output = capsys.readouterr()
    assert 'eligible Antigravity Gemini route' in output.err
    assert 'b' * 12 not in output.out + output.err
    path.write_text('x' * 2049)
    with pytest.raises(anti.AntiError, match='bounded'):
        anti.load_account_binding_file(str(path.resolve()))


def test_request_json_forwards_only_the_private_binding_header(fixture, monkeypatch):
    anti, _, _, _, _ = fixture
    captured = {}

    class Response:
        status = 200
        headers = {}

        def read(self):
            return b'{"ok":true}'

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def opened(request, **kwargs):
        captured.update(request.headers)
        return Response()

    monkeypatch.setattr(anti, 'open_gateway_request', opened)
    status, payload = anti.request_json('POST', 'http://127.0.0.1:51122/v1/responses', payload={'model': 'gemini-3.8-flash'}, extra_headers={'X-Anti-Account-Binding': 'private-binding'})
    assert status == 200 and payload == {'ok': True}
    assert captured.get('X-anti-account-binding') == 'private-binding'


def test_gateway_binding_verification_refuses_stale_or_busy_inventory(fixture, monkeypatch):
    anti, _, _, _, _ = fixture
    binding = json.dumps({'schemaVersion': 1, 'gatewayInstance': 'a' * 32, 'accountRef': 'acct_' + 'b' * 12, 'inventorySha256': 'c' * 64}, separators=(',', ':'))
    inventory = {'gatewayInstance': 'a' * 32, 'inventorySha256': 'c' * 64, 'accounts': [{'accountRef': 'acct_' + 'b' * 12, 'eligible': True, 'inFlight': 0}]}
    monkeypatch.setattr(anti, 'request_json', lambda *args, **kwargs: (200, inventory))
    assert anti.verify_gateway_binding(SimpleNamespace(base_url='http://127.0.0.1:51122/v1', timeout=1, gateway_token_env='TOKEN'), 'gemini-3.8-flash', binding)['inventorySha256'] == 'c' * 64
    monkeypatch.setattr(anti, 'request_json', lambda *args, **kwargs: (200, {**inventory, 'inventorySha256': 'd' * 64}))
    with pytest.raises(anti.AntiError, match='stale'):
        anti.verify_gateway_binding(SimpleNamespace(base_url='http://127.0.0.1:51122/v1', timeout=1, gateway_token_env='TOKEN'), 'gemini-3.8-flash', binding)
    monkeypatch.setattr(anti, 'request_json', lambda *args, **kwargs: (200, {**inventory, 'gatewayInstance': 'd' * 32}))
    with pytest.raises(anti.AntiError, match='stale gateway'):
        anti.verify_gateway_binding(SimpleNamespace(base_url='http://127.0.0.1:51122/v1', timeout=1, gateway_token_env='TOKEN'), 'gemini-3.8-flash', binding)


def test_bound_review_reads_eligibility_then_posts_once(fixture, monkeypatch, capsys, tmp_path):
    anti, _, _, first, _ = fixture
    inventory = __import__('fastapi.testclient', fromlist=['TestClient']).TestClient(server.app).get('/v1/account-bindings?model=gemini-3.8-flash').json()
    row = inventory['accounts'][0]
    path = tmp_path / 'binding.json'
    path.write_text(json.dumps({'schemaVersion': 1, 'gatewayInstance': inventory['gatewayInstance'], 'accountRef': row['accountRef'], 'inventorySha256': inventory['inventorySha256']}))
    calls = bridge(monkeypatch, anti)
    with upstream(response(json.dumps(result()))) as (base, seen):
        endpoint(monkeypatch, base)
        assert anti.main(arguments(first, '--account-binding-json', str(path))) == 0
    assert ('GET', '/v1/account-bindings') in calls
    assert len(seen) == 1
    value = json.loads(capsys.readouterr().out)
    assert value['metadata']['account_binding_config_sha256']
    assert value['metadata']['account_binding_gateway_instance'] == inventory['gatewayInstance']
    assert row['accountRef'] not in json.dumps(value)
    assert inventory['inventorySha256'] not in json.dumps(value)


@pytest.mark.parametrize('failure', ['429', '503', 'transport'])
def test_bound_gateway_failure_is_one_exact_attempt_without_rotation(fixture, monkeypatch, capsys, tmp_path, failure):
    anti, _, _, first, _ = fixture
    selected = {
        'email': 'selected@example.invalid',
        'accessToken': 'synthetic-selected',
        'expiresAt': 4102444800,
        'projectId': 'fixture-project',
    }
    inventory = {
        'schemaVersion': 1,
        'gatewayInstance': 'a' * 32,
        'family': 'gemini',
        'accounts': [
            {'accountRef': 'acct_' + 'a' * 12, 'eligible': True, 'tokenExpiresAt': 4102444800, 'inFlight': 0, 'exclusionReasons': []},
            {'accountRef': 'acct_' + 'b' * 12, 'eligible': True, 'tokenExpiresAt': 4102444800, 'inFlight': 0, 'exclusionReasons': []},
        ],
        'inventorySha256': 'c' * 64,
    }
    path = tmp_path / 'binding.json'
    path.write_text(json.dumps({
        'schemaVersion': 1,
        'gatewayInstance': inventory['gatewayInstance'],
        'accountRef': inventory['accounts'][1]['accountRef'],
        'inventorySha256': inventory['inventorySha256'],
    }))
    calls = bridge(monkeypatch, anti)
    captured = []
    released = []

    async def post(self, payload, lease):
        captured.append((payload, lease.email))
        if failure == 'transport':
            raise httpx.ConnectError('synthetic transport failure', request=httpx.Request('POST', 'https://example.invalid'))
        return httpx.Response(int(failure), json={'error': {'code': failure, 'message': 'synthetic bound failure'}})

    monkeypatch.setattr(server.GoogleTransport, 'post', post)
    monkeypatch.setattr(server.account_manager, 'binding_inventory', lambda model: inventory)
    monkeypatch.setattr(server.account_manager, 'acquire_bound_account', lambda model, binding: dict(selected))
    monkeypatch.setattr(server.account_manager, 'release_account', released.append)
    monkeypatch.setattr(server.account_manager, 'acquire_account', lambda *args, **kwargs: pytest.fail('automatic rotation'))
    monkeypatch.setattr(server, 'schedule_refresh_accounts_ahead', lambda *args, **kwargs: pytest.fail('background refresh'))

    assert anti.main(arguments(first, '--account-binding-json', str(path))) != 0
    assert calls[0] == ('GET', '/v1/account-bindings')
    assert sum(1 for method, path in calls if method == 'POST' and path == '/v1/responses') == 1
    assert len(captured) == 1
    payload, lease_email = captured[0]
    assert lease_email == selected['email']
    serialized = json.dumps(payload)
    assert 'X-Anti-Account-Binding' not in serialized
    assert inventory['gatewayInstance'] not in serialized
    assert inventory['accounts'][1]['accountRef'] not in serialized
    assert released == [selected['email']]
    captured_output = capsys.readouterr()
    diagnostics = captured_output.out + captured_output.err
    assert inventory['accounts'][1]['accountRef'] not in diagnostics
    assert inventory['gatewayInstance'] not in diagnostics
    assert inventory['inventorySha256'] not in diagnostics
