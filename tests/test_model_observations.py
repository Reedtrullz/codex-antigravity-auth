"""Only owned fixture HTTP and synthetic credentials exercise discovery/probes."""
import asyncio
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import threading
import time
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from _test_isolation import allow_listener, remove_listener
from codex_antigravity_auth import byok, cli, models, server, unified
from codex_antigravity_auth import model_observations as obs


@pytest.fixture(autouse=True)
def state(tmp_path, monkeypatch):
    monkeypatch.setattr(models, 'model_overlay_path', lambda: tmp_path / 'models.toml')
    monkeypatch.setattr(byok, 'PROVIDERS_FILE', str(tmp_path / 'providers.json'))
    monkeypatch.setattr(byok, 'get_providers_json_path', lambda: tmp_path / 'providers.json')
    models.invalidate_model_overlay_cache()
    yield tmp_path
    models.invalidate_model_overlay_cache()


@contextmanager
def fixture_http(responses):
    seen, errors = [], []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append({'path':self.path, 'headers':dict(self.headers),
                         'body':self.rfile.read(int(self.headers.get('Content-Length', '0')))})
            if len(seen) > len(responses):
                errors.append('unexpected request'); self.send_error(500); return
            status, payload, headers = responses[len(seen)-1]
            body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
            self.send_response(status)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Content-Type', 'application/json')
            for key,value in headers.items(): self.send_header(key, value)
            self.end_headers()
            try: self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError): pass
        do_POST = do_GET
        def log_message(self, *args): pass
    http = ThreadingHTTPServer(('127.0.0.1', 0), Handler, bind_and_activate=False)
    address = allow_listener(http.socket)
    http.server_address = address
    http.server_activate()
    thread = threading.Thread(target=http.serve_forever, kwargs={'poll_interval':0.01}, daemon=True)
    thread.start()
    try: yield f'http://{address[0]}:{address[1]}/v1', seen
    finally:
        http.shutdown(); http.server_close(); thread.join(2); remove_listener(address)
    assert not thread.is_alive() and not errors


def provider(base='https://example.invalid/v1', **extra):
    return {'id':'fixture', 'kind':'openai_chat', 'baseUrl':base, 'apiKey':'fixture-provider-credential', 'models':['declared'], **extra}


def ready(status='completed', **extra):
    return {'status':status, 'output':[{'type':'message','role':'assistant','status':'completed',
                                      'content':[{'type':'output_text','text':'fixture-generated-private-text'}]}], **extra}


def test_offline_discovery_never_constructs_network_client_or_mutates_state(state, monkeypatch):
    guard = Mock(side_effect=AssertionError('no network'))
    monkeypatch.setattr(obs.httpx, 'AsyncClient', guard)
    before = list(state.rglob('*'))
    assert obs.discover('fixture', configs={'fixture':provider()}) == {'state':'missing','record':None}
    assert list(state.rglob('*')) == before
    guard.assert_not_called()


def test_paginated_discovery_caches_ids_only_and_never_promotes_features(state):
    responses = [(200, {'data':[{'id':'first','api_key':'fixture-upstream-secret','capabilities':{'tools':True}}],
                        'has_more':True,'last_id':'first'}, {}),
                 (200, {'data':[{'id':'second'}], 'has_more':False}, {})]
    with fixture_http(responses) as (base, seen):
        config = provider(base)
        result = obs.discover('fixture', network=True, configs={'fixture':config})
        assert [request['path'] for request in seen] == ['/v1/models','/v1/models?after=first']
        assert all(request['headers']['Authorization'] == 'Bearer fixture-provider-credential' for request in seen)
    record = result['record']
    assert record['status'] == 'complete' and record['models'] == ['first','second'] and record['pages'] == 2
    cached = obs.cache_path('discovery','fixture').read_text()
    assert 'fixture-provider-credential' not in cached and 'fixture-upstream-secret' not in cached
    assert 'capabilities' not in cached and 'Authorization' not in cached and base not in cached
    assert config['models'] == ['declared']
    assert obs.discover('fixture', configs={'fixture':config}) == result


@pytest.mark.parametrize('payload,code,models_seen', [
    ({'data':[{'id':'good'}, {'id':'sk-fixtureabcdefghijklmnopqrstuvwxyz'}, {}, 'bad']}, 'invalid_records', ['good']),
    ({'data':'bad'}, 'invalid_catalog', []),
    ({'data':[{'id':'good'}], 'next_url':'https://other.invalid/credential-destination'}, 'pagination_invalid', ['good']),
])
def test_partial_and_malformed_catalogs_have_fixed_diagnostics(payload, code, models_seen):
    with fixture_http([(200,payload,{})]) as (base, seen):
        record = obs.discover('fixture', network=True, configs={'fixture':provider(base)})['record']
    assert record['models'] == models_seen and record['diagnostics'] == [code]
    assert record['status'] == ('partial' if models_seen else 'error')
    assert len(seen) == 1


@pytest.mark.parametrize('status,diagnostic,state', [(404,'discovery_unavailable','unsupported'), (405,'discovery_unavailable','unsupported'),
    (401,'http_error','error'), (302,'redirect_refused','error')])
def test_absent_discovery_and_http_failures_do_not_erase_declarations(status, diagnostic, state):
    with fixture_http([(status, b'fixture-private-error', {'Location':'https://other.invalid/target'})]) as (base, seen):
        config = provider(base)
        result = obs.discover('fixture', network=True, configs={'fixture':config})['record']
    assert result['status'] == state and result['diagnostics'] == [diagnostic]
    assert config['models'] == ['declared'] and len(seen) == 1
    assert 'fixture-private-error' not in json.dumps(result)


def test_pagination_cycle_page_and_byte_caps_are_explicit(monkeypatch):
    with fixture_http([(200, {'data':[{'id':'same'}], 'next_cursor':'repeat'}, {})]*2) as (base, seen):
        result = obs.discover('fixture', network=True, configs={'fixture':provider(base)})['record']
    assert len(seen) == 2 and result['diagnostics'] == ['pagination_cycle']
    with fixture_http([(200, {'data':[{'id':f'model-{i}'}], 'next_cursor':str(i)}, {}) for i in range(obs.MAX_PAGES)]) as (base, seen):
        result = obs.discover('fixture', network=True, configs={'fixture':provider(base)})['record']
    assert len(seen) == obs.MAX_PAGES and result['diagnostics'] == ['page_limit']
    with fixture_http([(200, b'x' * (obs.MAX_BYTES+1), {})]) as (base, seen):
        result = obs.discover('fixture', network=True, configs={'fixture':provider(base)})['record']
    assert result['diagnostics'] == ['byte_limit']


def test_timeout_is_bounded_and_never_caches_exception_details(monkeypatch):
    async def pending(*args, **kwargs): await asyncio.sleep(2)
    monkeypatch.setattr(obs, '_response_json', pending)
    result = obs.discover('fixture', network=True, timeout=.01, configs={'fixture':provider()})['record']
    assert result['status'] == 'error' and result['diagnostics'] == ['timeout']


def test_stale_mismatched_and_malformed_cache_never_become_generation_evidence(state):
    config = provider()
    identity = obs.provider_identity('fixture', config)
    record = {**obs._record('discovery', identity, 60), 'models':['fixture-model'],'pages':1}
    obs.save_observation(record, 'fixture')
    assert obs.read_observation('discovery','fixture',identity,now=record['expiresAt'])['state'] == 'stale'
    assert obs.read_observation('discovery','fixture',{**identity,'configuration':'0'*64})['state'] == 'mismatch'
    path = obs.cache_path('discovery','fixture')
    record['schemaVersion'] = 99
    path.write_text(json.dumps(record))
    before = path.read_bytes()
    assert obs.read_observation('discovery','fixture',identity)['state'] == 'invalid'
    assert path.read_bytes() == before


def test_explain_uses_canonical_local_route_without_network_or_state_writes(state, monkeypatch):
    guard = Mock(side_effect=AssertionError('no network'))
    monkeypatch.setattr(obs.httpx,'AsyncClient',guard)
    monkeypatch.setenv('ANTIGRAVITY_UNIFIED_MODEL_PICKER','1')
    before = list(state.rglob('*'))
    google = obs.describe_model('openai:sonnet', configs={})
    byok_result = obs.describe_model('fixture/declared', configs={'fixture':provider()})
    native = obs.describe_model('gpt-5.6', configs={})
    assert google['canonical_id'] == 'claude-sonnet-4-6' and google['route'] == 'antigravity'
    assert byok_result['canonical_id'] == 'fixture:declared' and byok_result['route'] == 'byok'
    assert native['route'] == 'openai'
    assert not any(value['recent_generation_ok'] for value in (google,byok_result,native))
    assert all(value['availability'] == 'unknown' for value in (google,byok_result,native))
    assert list(state.rglob('*')) == before
    guard.assert_not_called()


@pytest.mark.parametrize('payload,terminal,ok', [(ready(),'completed',True), (ready('incomplete'),'incomplete',False),
    (ready('failed'),'failed',False), ({'status':'completed','output':[]},'empty',False),
    ({'status':'completed','output_text':'fake ready'},'malformed',False)])
def test_probe_records_only_matching_expiring_terminal_evidence(state, monkeypatch, payload, terminal, ok):
    monkeypatch.setenv('FIXTURE_GATEWAY_TOKEN','fixture-gateway-credential')
    with fixture_http([(200,payload,{})]) as (base, seen):
        result = obs.probe('sonnet', network=True, base_url=base, token_env='FIXTURE_GATEWAY_TOKEN', configs={})
        report = obs.describe_model('claude-sonnet-4-6', base_url=base, configs={})
        assert report['recent_generation_ok'] is ok
        assert json.loads(seen[0]['body'])['max_output_tokens'] == 16
        assert json.loads(seen[0]['body'])['model'] == 'claude-sonnet-4-6'
        assert seen[0]['headers']['Authorization'] == 'Bearer fixture-gateway-credential'
        assert len(seen) == 1
    assert result['terminal'] == terminal and result['generationOk'] is ok
    text = obs.cache_path('probe','claude-sonnet-4-6').read_text()
    assert 'fixture-generated-private-text' not in text and 'fixture-gateway-credential' not in text
    assert result['identity']['route'] == 'antigravity' and result['identity']['capability'] == 'text_generation'
    different = obs.describe_model('sonnet', base_url='http://127.0.0.1:1/v1', configs={})
    assert different['probe']['state'] == 'mismatch' and not different['recent_generation_ok']
    monkeypatch.setattr(obs.time, 'time', lambda: result['expiresAt'])
    assert not obs.describe_model('sonnet', base_url=base, configs={})['recent_generation_ok']


def test_probe_refuses_without_explicit_network_permission():
    with pytest.raises(obs.ObservationError, match='--network'): obs.probe('sonnet', configs={})


def test_provider_import_is_preview_then_digest_gated_and_preserves_capabilities(state, monkeypatch):
    stored = provider(models=[{'id':'declared','capabilities':{'input_modalities':['text','image']}}])
    byok.save_provider_config({'providers':{'fixture':stored}})
    observed = {**obs._record('discovery',obs.provider_identity('fixture',stored),60), 'models':['added'], 'pages':1}
    obs.save_observation(observed,'fixture')
    before = byok.get_providers_json_path().read_bytes()
    preview = obs.import_discovered('fixture',['added'])
    assert not preview['saved'] and preview['add'] == ['added']
    assert byok.get_providers_json_path().read_bytes() == before
    with pytest.raises(obs.ObservationError, match='digest'): obs.import_discovered('fixture',['added'],write=True)
    result = obs.import_discovered('fixture',['added'],write=True,accept_digest=preview['digest'])
    assert result['saved']
    models_after = byok.load_provider_config_read_only()['providers']['fixture']['models']
    assert models_after[0]['capabilities'] == stored['models'][0]['capabilities'] and models_after[1] == 'added'
    assert obs.discover('fixture')['state'] == 'fresh'


def test_changed_provider_model_selection_invalidates_import_preview():
    stored = provider()
    byok.save_provider_config({'providers':{'fixture':stored}})
    obs.save_observation({**obs._record('discovery',obs.provider_identity('fixture',stored),60), 'models':['added'], 'pages':1},'fixture')
    preview = obs.import_discovered('fixture',['added'])
    byok.set_provider_config('fixture', models=['declared','intervening'])
    before = byok.get_providers_json_path().read_bytes()
    with pytest.raises(obs.ObservationError, match='digest'):
        obs.import_discovered('fixture',['added'],write=True,accept_digest=preview['digest'])
    assert byok.get_providers_json_path().read_bytes() == before


def test_overlay_import_requires_review_of_exact_bytes_and_existing_declarations(state):
    source = state / 'incoming.toml'
    source.write_text('[[models]]\nid="fixture-model"\nbackend_id="fixture-backend"\ndisplay_name="Fixture"\nfamily="gemini"\ncontext_window=1000\n')
    preview = obs.import_overlay(source)
    assert preview['definitions'][0]['backend_id'] == 'fixture-backend' and not models.model_overlay_path().exists()
    source.write_text(source.read_text().replace('context_window=1000','context_window=2000'))
    with pytest.raises(obs.ObservationError, match='digest'): obs.import_overlay(source,write=True,accept_digest=preview['digest'])
    assert not models.model_overlay_path().exists()
    preview = obs.import_overlay(source)
    assert obs.import_overlay(source,write=True,accept_digest=preview['digest'])['saved']
    assert models.native_model_definition('fixture-model').context_window == 2000


def test_read_only_normalization_does_not_echo_private_provider_labels(state, capsys):
    byok.normalize_provider_entry({'displayName':'fixture@example.invalid','apiKey':'bad\nkey'}, quiet=True)
    assert capsys.readouterr().err == ''


def test_catalog_diagnostics_show_omitted_configurations_without_hiding_local_models(monkeypatch):
    monkeypatch.setattr(server,'all_provider_configs_read_only',lambda:{'bad':provider(kind='future_transport'), 'fixture':provider()})
    result = TestClient(server.app).get('/v1/models').json()
    assert any(item['id'] == 'fixture:declared' for item in result['data'])
    assert any(item['id'] == 'gemini-3.8-flash' for item in result['data'])
    assert result['provider_catalog_diagnostics']['status'] == 'partial'
    assert {'provider':'bad','status':'unsupported_route','omitted_models':1} in result['provider_catalog_diagnostics']['providers']
    assert 'fixture-provider-credential' not in json.dumps(result)


@pytest.mark.parametrize('failure', ['timeout','error'])
def test_catalog_timeout_and_error_are_visible(monkeypatch, failure):
    def failed(created, **kwargs):
        if failure == 'error': raise RuntimeError('fixture-private-error')
        time.sleep(.1)
        return []
    monkeypatch.setattr(server,'provider_model_catalog',failed)
    monkeypatch.setattr(server,'MODEL_CATALOG_PROVIDER_TIMEOUT_SECONDS',.005)
    result = TestClient(server.app).get('/v1/models').json()
    assert result['provider_catalog_diagnostics']['status'] == failure
    assert result['data'] and 'fixture-private-error' not in json.dumps(result)


def test_cli_explain_json_is_offline_and_probe_requires_network(monkeypatch, capsys):
    monkeypatch.setattr(obs,'providers_read_only',lambda:{})
    monkeypatch.setattr(sys,'argv',['codex-antigravity','models','explain','sonnet','--json'])
    cli.main()
    assert json.loads(capsys.readouterr().out)['recent_generation_ok'] is False
    monkeypatch.setattr(sys,'argv',['codex-antigravity','models','probe','sonnet','--json'])
    with pytest.raises(SystemExit) as exc: cli.main()
    assert exc.value.code == 1 and '--network' in json.loads(capsys.readouterr().out)['message']


def test_preloaded_route_classification_preserves_preset_short_circuit(monkeypatch):
    guard = Mock(side_effect=AssertionError('no mutable config lookup'))
    monkeypatch.setattr(byok, 'all_provider_configs', guard)
    assert unified.classify_route('openrouter/vendor/model', unified_enabled=True) == 'byok'
    assert unified.classify_route('fixture/declared', unified_enabled=True, provider_configs={'fixture':provider()}) == 'byok'
    guard.assert_not_called()


@pytest.mark.parametrize('time_limit', [True, 0, -1, float('inf'), float('nan'), 10**400])
def test_invalid_time_limits_refuse_before_network(time_limit, monkeypatch):
    guard = Mock(side_effect=AssertionError('no network'))
    monkeypatch.setattr(obs.httpx, 'AsyncClient', guard)
    with pytest.raises(obs.ObservationError): obs.discover('fixture',network=True,timeout=time_limit,configs={'fixture':provider()})
    guard.assert_not_called()


def test_contradictory_pagination_cannot_be_treated_as_complete():
    with fixture_http([(200, {'data':[{'id':'good'}], 'has_more':False, 'next_cursor':'more'}, {})]) as (base, seen):
        result = obs.discover('fixture', network=True, configs={'fixture':provider(base)})['record']
    assert result['status'] == 'partial' and result['diagnostics'] == ['pagination_invalid'] and len(seen) == 1


def test_cache_cannot_claim_success_with_inconsistent_http_status(monkeypatch):
    report = obs.describe_model('sonnet',configs={})
    record = {**obs._record('probe',report['probe_identity'],60), 'terminal':'completed','httpStatus':401,'generationOk':True}
    path = obs.cache_path('probe',report['canonical_id'])
    path.parent.mkdir()
    path.write_text(json.dumps(record))
    result = obs.describe_model('sonnet',configs={})
    assert result['probe']['state'] == 'invalid' and not result['recent_generation_ok']


def test_overlay_preview_detects_comment_only_changes(state):
    original = models.validate_overlay_model({'id':'existing-fixture','backend_id':'existing-fixture','family':'gemini','context_window':1000})
    models.save_model_overlays([original])
    source = state / 'incoming.toml'
    source.write_text('[[models]]\nid="new-fixture"\nbackend_id="new-fixture"\nfamily="gemini"\ncontext_window=1000\n')
    preview = obs.import_overlay(source)
    target = models.model_overlay_path()
    target.write_text(target.read_text() + '\n# intervening user note\n')
    before = target.read_bytes()
    with pytest.raises(obs.ObservationError, match='digest'):
        obs.import_overlay(source,write=True,accept_digest=preview['digest'])
    assert target.read_bytes() == before


def test_model_count_limit_is_explicit_and_keeps_only_bounded_ids(monkeypatch):
    monkeypatch.setattr(obs,'MAX_MODELS',2)
    with fixture_http([(200, {'data':[{'id':'one'},{'id':'two'},{'id':'three'}]}, {})]) as (base, _):
        result = obs.discover('fixture',network=True,configs={'fixture':provider(base)})['record']
    assert result['models'] == ['one','two'] and result['diagnostics'] == ['model_limit'] and result['status'] == 'partial'


def test_catalog_ids_cannot_echo_known_transport_credentials():
    with fixture_http([(200, {'data':[{'id':'good'},{'id':'fixture-provider-credential'},
                                    {'id':'prefix-fixture-header-credential-suffix'}]}, {})]) as (base, _):
        result = obs.discover('fixture',network=True,configs={'fixture':provider(base, headers={'X-Fixture-Key':'fixture-header-credential'})})
    assert result['record']['models'] == ['good'] and result['record']['status'] == 'partial'
    saved = obs.cache_path('discovery','fixture').read_text()
    assert 'fixture-provider-credential' not in saved and 'fixture-header-credential' not in saved


def test_provider_import_refuses_hidden_case_collisions():
    stored = provider()
    byok.save_provider_config({'providers':{'fixture':stored}})
    obs.save_observation({**obs._record('discovery',obs.provider_identity('fixture',stored),60),
                          'models':['DECLARED'], 'pages':1},'fixture')
    before = byok.get_providers_json_path().read_bytes()
    with pytest.raises(obs.ObservationError, match='case policy'):
        obs.import_discovered('fixture',['DECLARED'])
    assert byok.get_providers_json_path().read_bytes() == before


@pytest.mark.parametrize('provider_id,url', [('custom-fixture',None), ('custom-fixture','http://remote.invalid/v1'),
                                           ('deepseek','https://example.invalid/v1?private-fixture')])
def test_invalid_stored_endpoints_remain_visible_without_secret_labels(state, monkeypatch, capsys, provider_id, url):
    raw = {'providers':{provider_id:{'models':['fixture-model'], 'apiKey':'fixture-secret-value',
                                    'displayName':'fixture-private-label', **({'baseUrl':url} if url is not None else {})}}}
    # Preserve the malformed fixture on disk; the normal writer intentionally
    # normalizes inputs, which would erase the condition under test.
    from codex_antigravity_auth.storage import save_secure_json_file
    path = byok.get_providers_json_path()
    save_secure_json_file(path, raw, error_label='synthetic provider fixture')
    before = path.read_bytes()
    result = TestClient(server.app).get('/v1/models').json()
    rows = result['provider_catalog_diagnostics']['providers']
    assert {'provider':provider_id, 'status':'invalid_configuration', 'omitted_models':1} in rows
    assert result['provider_catalog_diagnostics']['status'] == 'partial'
    assert not any(item['id'] == provider_id + ':fixture-model' for item in result['data'])
    assert any(item['id'] == 'gemini-3.8-flash' for item in result['data'])
    assert 'fixture-secret-value' not in json.dumps(result) and 'fixture-private-label' not in json.dumps(rows)
    assert path.read_bytes() == before and capsys.readouterr().err == ''


def test_discovery_import_digest_binds_the_complete_observation():
    stored = provider()
    byok.save_provider_config({'providers':{'fixture':stored}})
    record = {**obs._record('discovery',obs.provider_identity('fixture',stored),60), 'models':['added'], 'pages':1}
    obs.save_observation(record,'fixture')
    preview = obs.import_discovered('fixture',['added'])
    obs.save_observation({**record,'models':['added','different-catalog-entry']},'fixture')
    before = byok.get_providers_json_path().read_bytes()
    with pytest.raises(obs.ObservationError, match='digest'):
        obs.import_discovered('fixture',['added'],write=True,accept_digest=preview['digest'])
    assert byok.get_providers_json_path().read_bytes() == before


def test_import_rechecks_source_while_holding_observation_and_store_locks(monkeypatch):
    from contextlib import contextmanager
    from codex_antigravity_auth import storage
    stored = provider()
    byok.save_provider_config({'providers':{'fixture':stored}})
    record = {**obs._record('discovery',obs.provider_identity('fixture',stored),60), 'models':['added'], 'pages':1}
    obs.save_observation(record,'fixture')
    preview = obs.import_discovered('fixture',['added'])
    before = byok.get_providers_json_path().read_bytes()
    original_update, original_read, original_lock = storage.update_secure_json_file, obs.read_observation, obs.file_lock
    held = {'cache':False,'store':False,'checked':False}
    @contextmanager
    def cache_lock(path):
        assert path == obs.cache_path('discovery','fixture')
        with original_lock(path):
            held['cache'] = True
            try: yield
            finally: held['cache'] = False
    def update(path, default, mutate, **kwargs):
        def under_store_lock(data):
            held['store'] = True
            try: return mutate(data)
            finally: held['store'] = False
        return original_update(path, default, under_store_lock, **kwargs)
    def read(*args, **kwargs):
        if held['store']:
            assert held['cache']
            held['checked'] = True
            # Inject expiry at the last recheck without altering store bytes.
            return {'state':'stale','record':record}
        return original_read(*args, **kwargs)
    monkeypatch.setattr(obs,'file_lock',cache_lock)
    monkeypatch.setattr(storage,'update_secure_json_file',update)
    monkeypatch.setattr(obs,'read_observation',read)
    with pytest.raises(obs.ObservationError, match='observation'):
        obs.import_discovered('fixture',['added'],write=True,accept_digest=preview['digest'])
    assert held['checked'] and byok.get_providers_json_path().read_bytes() == before


def test_unconfigured_byok_explanation_is_structured_offline_and_not_ready(state, monkeypatch, capsys):
    monkeypatch.setattr(obs,'providers_read_only',lambda:{})
    monkeypatch.setattr(obs.httpx,'AsyncClient',Mock(side_effect=AssertionError('no network')))
    before = list(state.rglob('*'))
    monkeypatch.setattr(sys,'argv',['codex-antigravity','models','explain','missing:vendor/model','--json'])
    cli.main()
    result = json.loads(capsys.readouterr().out)
    assert result['route'] == 'byok' and result['canonical_id'] == 'missing:vendor/model'
    assert result['configuration_diagnostics'] == ['provider_not_configured']
    assert result['declaration'] is None and result['discovery']['state'] == result['probe']['state'] == 'missing'
    assert result['recent_generation_ok'] is False and result['availability'] == 'unknown'
    assert list(state.rglob('*')) == before


def test_refresh_between_preview_and_cache_lock_cannot_change_import_source(monkeypatch):
    stored = provider()
    byok.save_provider_config({'providers':{'fixture':stored}})
    record = {**obs._record('discovery',obs.provider_identity('fixture',stored),60),'models':['added'],'pages':1}
    obs.save_observation(record,'fixture')
    preview = obs.import_discovered('fixture',['added'])
    before = byok.get_providers_json_path().read_bytes()
    original_lock = obs.file_lock
    @contextmanager
    def refreshed_lock(path):
        obs.save_observation({**record,'models':['replacement']},'fixture')
        with original_lock(path): yield
    monkeypatch.setattr(obs,'file_lock',refreshed_lock)
    with pytest.raises(obs.ObservationError, match='observation changed'):
        obs.import_discovered('fixture',['added'],write=True,accept_digest=preview['digest'])
    assert byok.get_providers_json_path().read_bytes() == before


def test_refresh_waits_until_the_reviewed_import_transaction_finishes(monkeypatch):
    from codex_antigravity_auth import storage
    stored = provider()
    byok.save_provider_config({'providers':{'fixture':stored}})
    record = {**obs._record('discovery',obs.provider_identity('fixture',stored),60),'models':['added'],'pages':1}
    obs.save_observation(record,'fixture')
    preview = obs.import_discovered('fixture',['added'])
    before_update = storage.update_secure_json_file
    started, finished = threading.Event(), threading.Event()
    workers, errors = [], []
    def refresh():
        started.set()
        try: obs.save_observation({**record,'models':['replacement']},'fixture')
        except Exception as exc: errors.append(type(exc).__name__)
        finally: finished.set()
    def update(path, default, mutate, **kwargs):
        def under_lock(data):
            worker = threading.Thread(target=refresh, daemon=True)
            workers.append(worker); worker.start()
            assert started.wait(1)
            assert not finished.wait(.03)
            return mutate(data)
        return before_update(path,default,under_lock,**kwargs)
    monkeypatch.setattr(storage,'update_secure_json_file',update)
    try:
        assert obs.import_discovered('fixture',['added'],write=True,accept_digest=preview['digest'])['saved']
    finally:
        for worker in workers: worker.join(2)
    assert finished.is_set() and not errors and not any(worker.is_alive() for worker in workers)
    assert 'added' in byok.load_provider_config_read_only()['providers']['fixture']['models']
    assert obs.discover('fixture')['record']['models'] == ['replacement']


def test_builtin_preset_can_supply_its_default_endpoint():
    from codex_antigravity_auth.storage import save_secure_json_file
    save_secure_json_file(byok.get_providers_json_path(), {'providers':{'custom':{'models':['fixture-model']}}}, error_label='fixture')
    result = TestClient(server.app).get('/v1/models').json()
    assert {'provider':'custom','status':'complete','omitted_models':0} in result['provider_catalog_diagnostics']['providers']


@pytest.mark.parametrize('providers', [None, ['invalid'], {'bad': 'fixture-secret-value'}])
def test_malformed_provider_structure_is_reported_without_echoing_values(providers):
    from codex_antigravity_auth.storage import save_secure_json_file
    save_secure_json_file(byok.get_providers_json_path(), {'providers':providers}, error_label='fixture')
    result = TestClient(server.app).get('/v1/models').json()
    assert result['provider_catalog_diagnostics']['status'] == 'partial'
    assert result['provider_catalog_diagnostics']['providers'] == [{'provider':None,'status':'configuration_unreadable','omitted_models':None}]
    assert result['data'] and 'fixture-secret-value' not in json.dumps(result)
