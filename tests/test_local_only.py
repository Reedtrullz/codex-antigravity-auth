"""Local-only policy with synthetic stores and owned HTTP fixtures only."""
import argparse
import importlib.util
import io
import json
from pathlib import Path
from unittest.mock import AsyncMock, Mock
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

from codex_antigravity_auth import byok, cli, server, endpoint_policy
from codex_antigravity_auth.skills.anti.scripts.anti_lib import local_policy
from fake_upstream import upstream


@pytest.fixture
def configurations(monkeypatch):
    monkeypatch.setenv('ANTIGRAVITY_LOCAL_ONLY','1')
    monkeypatch.setattr(server.app.state,'local_only_mode',None,raising=False)
    values={}
    monkeypatch.setattr(server,'all_provider_configs',lambda:values)
    monkeypatch.setattr(server,'all_provider_configs_read_only',lambda:values)
    monkeypatch.setattr(server,'is_unified_mode_enabled',lambda:True)
    monkeypatch.setattr(server,'write_request_record',lambda record:None)
    return values


def provider(identifier,base,models=('one','two')):
    raw=byok.normalize_provider_config({'providers':{identifier:{'baseUrl':base,'apiKeyOptional':True,
        'models':list(models),'apiKey':'fixture-only-key'}}})['providers'][identifier]
    return byok.merged_provider_config(identifier,raw)


def chat(text):
    return json.dumps({'choices':[{'index':0,'message':{'role':'assistant','content':text},'finish_reason':'stop'}]}).encode()


def test_local_catalog_filters_real_destinations_not_provider_names(configurations,monkeypatch):
    configurations.update({'local':provider('local','http://127.0.0.1:12345/v1'),
                           'ollama':provider('ollama','https://remote.example.invalid/v1')})
    monkeypatch.setattr(server,'openai_catalog',lambda:pytest.fail('no native OpenAI catalog'))
    catalog=TestClient(server.app).get('/v1/models').json()
    assert catalog['local_only_policy']=={'version':1,'enabled':True}
    assert {item['id'] for item in catalog['data']}=={'local:one','local:two'}
    assert all(item['capabilities']['destination_scope']=='loopback' for item in catalog['data'])
    assert catalog['provider_catalog_diagnostics']['status']=='complete'
    assert any(row['status']=='excluded_by_local_policy' for row in catalog['provider_catalog_diagnostics']['providers'])


@pytest.mark.parametrize('model',['gemini-3.8-flash','gpt-5.6','ollama:one'])
@pytest.mark.parametrize('stream',[False,True])
def test_gateway_blocks_remote_routes_before_auth_or_transport(configurations,monkeypatch,model,stream):
    configurations['ollama']=provider('ollama','https://remote.example.invalid/v1')
    monkeypatch.setattr(server,'resolve_openai_auth',lambda:pytest.fail('no OpenAI auth'))
    monkeypatch.setattr(server,'acquire_active_account_for_request',AsyncMock(side_effect=AssertionError('no Google account')))
    monkeypatch.setattr(server.httpx,'AsyncClient',lambda **kwargs:pytest.fail('no remote transport'))
    result=TestClient(server.app).post('/v1/local/responses',json={'model':model,'input':'fixture','stream':stream})
    assert result.status_code==403
    assert result.json()['detail']['code']=='local_only_route_forbidden'


def test_changed_provider_configuration_is_rechecked_at_dispatch(configurations,monkeypatch):
    configurations['local']=provider('local','http://127.0.0.1:12345/v1')
    client=TestClient(server.app)
    assert any(row['id']=='local:one' for row in client.get('/v1/models').json()['data'])
    configurations['local']=provider('local','https://remote.example.invalid/v1')
    monkeypatch.setattr(server.httpx,'AsyncClient',lambda **kwargs:pytest.fail('no changed remote endpoint'))
    assert client.post('/v1/local/responses',json={'model':'local:one','input':'fixture'}).status_code==403


def test_dedicated_endpoint_requires_enabled_gateway_mode(monkeypatch):
    monkeypatch.setenv('ANTIGRAVITY_LOCAL_ONLY','0')
    monkeypatch.setattr(server.app.state,'local_only_mode',None,raising=False)
    monkeypatch.setattr(server,'all_provider_configs',lambda:pytest.fail('no provider reads'))
    result=TestClient(server.app).post('/v1/local/responses',json={'model':'local:one','input':'fixture'})
    assert result.status_code==403 and 'requires gateway --local-only' in result.text


def test_local_lifespan_and_health_do_not_start_cloud_refresh_or_inspect_cloud_auth(configurations,monkeypatch):
    monkeypatch.setattr(server._RefreshAheadOwner,'start',lambda self:pytest.fail('no refresh worker'))
    monkeypatch.setattr(server,'account_health_summary',lambda:pytest.fail('no Google state inspection'))
    monkeypatch.setattr(server,'openai_auth_status',lambda:pytest.fail('no native auth inspection'))
    with TestClient(server.app) as client:
        health=client.get('/health').json()
        assert health['local_only_policy']['enabled'] is True
        assert health['accounts']['status']=='not_inspected_local_only'
        assert server._refresh_ahead_owner is None
    assert server._refresh_ahead_owner is None


def test_cli_start_policy_and_update_checks_are_explicit(monkeypatch):
    monkeypatch.setenv('ANTIGRAVITY_LOCAL_ONLY','0')
    monkeypatch.delenv('CODEX_ANTIGRAVITY_NO_UPDATE_CHECK',raising=False)
    cli.configure_local_gateway_environment(argparse.Namespace(local_only=True,host='127.0.0.1',allow_remote=False))
    monkeypatch.setattr(cli,'latest_pypi_version',lambda **kwargs:pytest.fail('no update lookup'))
    assert cli.version_check_result()['detail']=='version check disabled by local-only policy'
    with pytest.raises(SystemExit,match='loopback host'):
        cli.configure_local_gateway_environment(argparse.Namespace(local_only=True,host='0.0.0.0',allow_remote=True))


def test_local_mode_bypasses_https_proxy_environment(configurations,monkeypatch):
    monkeypatch.setenv('HTTPS_PROXY','http://remote-proxy.example.invalid:9999')
    options=endpoint_policy.httpx_client_options('https://127.0.0.1:12345/v1',timeout=1)
    assert options['trust_env'] is False and options['follow_redirects'] is False
    with pytest.raises(ValueError,match='loopback'):
        endpoint_policy.httpx_client_options('https://remote.example.invalid/v1',timeout=1)
    captured=[]
    def build(*handlers):
        captured.extend(handlers)
        return argparse.Namespace(open=lambda request,timeout:None)
    monkeypatch.setattr(endpoint_policy.shared.urllib.request,'build_opener',build)
    endpoint_policy.open_http_request('https://localhost:12345/v1/models')
    assert any(isinstance(handler,endpoint_policy.shared.urllib.request.ProxyHandler) and handler.proxies=={} for handler in captured)


@pytest.fixture
def anti(monkeypatch,tmp_path):
    import codex_antigravity_auth
    script=Path(codex_antigravity_auth.__file__).parent/'skills/anti/scripts/anti.py'
    spec=importlib.util.spec_from_file_location('anti_local_fixture',script)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    monkeypatch.setattr(module,'RUNS_DIR',tmp_path/'runs')
    monkeypatch.setattr(module,'ensure_helper_parity',lambda args:None)
    monkeypatch.setattr(module,'_install_run_signal_handlers',lambda args:None)
    return module


def bridge(monkeypatch,anti,client):
    requests=[]
    def opened(request,*,timeout,payload=None,body=None):
        path=urlsplit(request.full_url).path
        assert path in {'/v1/models','/v1/local/responses','/health'}
        timeout=anti.transport_entry_timeout(request.get_method(),timeout,payload=payload,body=body,url=request.full_url)
        requests.append((request.get_method(),path))
        result=client.request(request.get_method(),path,content=body,headers={'content-type':'application/json'})
        wire=io.BytesIO(result.content);wire.status=result.status_code;return wire
    monkeypatch.setattr(anti,'open_gateway_request',opened)
    return requests


@pytest.mark.parametrize('minimum_providers',[None,2])
def test_local_panel_uses_only_owned_provider_and_keeps_diversity_honest(configurations,anti,monkeypatch,capsys,minimum_providers):
    judgment=json.dumps({'summary':'Synthetic local synthesis.','findings':[],'disagreements':[],
                         'unverifiable':[],'recommended_next_actions':[],'caveats':[]})
    replies=[(200,{'Content-Type':'application/json'},chat('A complete synthetic local reviewer answer.'))]*2
    if minimum_providers is None:replies.append((200,{'Content-Type':'application/json'},chat(judgment)))
    with upstream(*replies) as (base,seen):
        configurations['local']=provider('local',base+'/v1')
        requests=bridge(monkeypatch,anti,TestClient(server.app))
        argv=['panel','--mode','ask','--local-only','--model','local:one','--model','local:two',
              '--judge','local:one','--prompt','fixture','--json','--no-progress','--no-verify']
        if minimum_providers is not None:argv+=['--min-providers',str(minimum_providers)]
        code=anti.main(argv)
    result=json.loads(capsys.readouterr().out)
    assert len(seen)==(3 if minimum_providers is None else 2)
    assert code==(0 if minimum_providers is None else 1)
    assert all(path!='/v1/responses' for method,path in requests if method=='POST')
    assert result['metadata']['distinct_actual_provider_count']==1
    if minimum_providers is None:assert result['panelStatus']=='same_provider_multi_model'


@pytest.mark.parametrize('stage',['judge','fallback'])
def test_missing_local_stage_refuses_before_any_generation(configurations,anti,monkeypatch,capsys,stage):
    configurations['local']=provider('local','http://127.0.0.1:12345/v1')
    requests=bridge(monkeypatch,anti,TestClient(server.app))
    argv=['panel','--mode','ask','--local-only','--model','local:one','--judge',
          'opus' if stage=='judge' else 'local:one','--prompt','fixture','--no-progress']
    if stage=='fallback':argv+=['--fallback-model','sonnet','--fallback-policy','on-retryable']
    assert anti.main(argv)==1
    assert stage in capsys.readouterr().err
    assert all(method=='GET' for method,path in requests)


def test_profile_export_and_import_are_nonsecret_and_explicit(anti,monkeypatch,tmp_path,capsys):
    monkeypatch.setattr(anti,'request_json',lambda *a,**k:pytest.fail('no profile network'))
    assert anti.main(['local-profile','--model','local:one','--judge','local:two'])==0
    exported=json.loads(capsys.readouterr().out)
    assert set(exported)=={'version','local_only','gateway','models','judge','fallback_model','fallback_policy'}
    path=tmp_path/'local.json';path.write_text(json.dumps(exported))
    args=anti.build_parser().parse_args(['panel','--mode','ask','--prompt','fixture','--local-profile',str(path)])
    prepared=local_policy.prepare_args(args)
    assert prepared['enabled'] and args.model==['local:one'] and args.judge=='local:two'
    assert anti.main(['consult','--local-profile',str(path),'--model','local:one','--prompt','fixture'])==1
    assert 'choose --local-profile' in capsys.readouterr().err


def test_remote_gateway_and_general_smoke_are_refused_without_lookup(anti,monkeypatch,capsys):
    monkeypatch.setattr(anti,'request_json',lambda *a,**k:pytest.fail('no lookup'))
    assert anti.main(['consult','--local-only','--base-url','https://remote.example.invalid/v1','--prompt','fixture'])==1
    assert 'non-loopback' in capsys.readouterr().err
    assert anti.main(['smoke','--local-only','--mode','full'])==1
    assert 'sidecar mode' in capsys.readouterr().err


def test_local_fallback_stays_on_declared_endpoint(configurations,anti,monkeypatch,capsys):
    with upstream((503,{'Content-Type':'application/json'},b'{"error":"synthetic unavailable"}'),
                  (200,{'Content-Type':'application/json'},chat('Complete synthetic fallback answer.'))) as (base,seen):
        configurations['local']=provider('local',base+'/v1')
        requests=bridge(monkeypatch,anti,TestClient(server.app))
        code=anti.main(['consult','--local-only','--model','local:one','--fallback-model','local:two',
                       '--fallback-policy','on-retryable','--retry','0','--prompt','fixture','--no-progress','--no-pre-read','--json'])
    result=json.loads(capsys.readouterr().out)
    assert code==0 and [row['body']['model'] for row in seen]==['one','two']
    assert result['metadata']['fallback_used'] is True
    assert all(path=='/v1/local/responses' for method,path in requests if method=='POST')


@pytest.mark.parametrize('stage',['review_chunk','review_synthesis','plan_chunk','plan_synthesis','judge'])
def test_each_generation_stage_uses_dedicated_local_path(configurations,anti,monkeypatch,stage):
    with upstream((200,{'Content-Type':'application/json'},chat('Complete synthetic local stage answer.'))) as (base,seen):
        configurations['local']=provider('local',base+'/v1')
        requests=bridge(monkeypatch,anti,TestClient(server.app))
        args=anti.build_parser().parse_args(['consult','--local-only','--model','local:one','--prompt','fixture','--no-progress'])
        text,model,_=anti.policy_generate(args,stage=stage,model='local:one',prompt='fixture',max_output_tokens=64,purpose=stage)
    assert model=='local:one' and text.startswith('Complete synthetic') and len(seen)==1
    assert ('POST','/v1/local/responses') in requests


def test_profile_single_model_is_reported_as_degraded(configurations,anti,monkeypatch,tmp_path,capsys):
    profile=local_policy.profile('http://127.0.0.1:51122/v1',['local:one'],'local:one')
    path=tmp_path/'local.json';path.write_text(json.dumps(profile))
    judgment=json.dumps({'summary':'Synthetic local synthesis.','findings':[],'disagreements':[],
                        'unverifiable':[],'recommended_next_actions':[],'caveats':[]})
    with upstream((200,{'Content-Type':'application/json'},chat('A complete synthetic local reviewer answer.')),
                  (200,{'Content-Type':'application/json'},chat(judgment))) as (base,seen):
        configurations['local']=provider('local',base+'/v1')
        bridge(monkeypatch,anti,TestClient(server.app))
        code=anti.main(['panel','--mode','ask','--local-profile',str(path),'--prompt','fixture','--json','--no-progress','--no-verify'])
    result=json.loads(capsys.readouterr().out)
    assert code==1 and len(seen)==2
    assert result['panelStatus']=='degraded_single_model' and result['runStatus']=='partial'
    assert result['metadata']['distinct_actual_model_count']==1


@pytest.mark.parametrize('contract',[None,{'version':1,'enabled':False},{'version':2,'enabled':True}])
def test_missing_or_disabled_gateway_contract_stops_before_post(anti,monkeypatch,capsys,contract):
    calls=[]
    catalog={'object':'list','capability_catalog_version':1,'data':[]}
    if contract is not None:catalog['local_only_policy']=contract
    def opened(request,**kwargs):
        calls.append(request.get_method())
        assert request.get_method()=='GET'
        body=io.BytesIO(json.dumps(catalog).encode());body.status=200;return body
    monkeypatch.setattr(anti,'open_gateway_request',opened)
    assert anti.main(['consult','--local-only','--model','local:one','--prompt','fixture','--no-progress','--no-pre-read'])==1
    assert calls==['GET'] and 'gateway' in capsys.readouterr().err


def test_per_request_policy_cannot_be_opted_out_of_in_gateway_mode(configurations,monkeypatch):
    configurations['local']=provider('local','https://remote.example.invalid/v1')
    monkeypatch.setattr(server.httpx,'AsyncClient',lambda **kwargs:pytest.fail('no remote transport'))
    result=TestClient(server.app).post('/v1/responses',json={'model':'local:one','input':'fixture',
                                                          'metadata':{'antigravity_local_only':False}})
    assert result.status_code==403
    monkeypatch.setenv('ANTIGRAVITY_LOCAL_ONLY','0')
    result=TestClient(server.app).post('/v1/responses',json={'model':'local:one','input':'fixture',
                                                          'metadata':{'antigravity_local_only':True}})
    assert result.status_code==403


def test_malformed_local_metadata_is_rejected_before_provider_reads(configurations,monkeypatch):
    monkeypatch.setattr(server,'all_provider_configs',lambda:pytest.fail('no provider read'))
    result=TestClient(server.app).post('/v1/responses',json={'model':'local:one','input':'fixture',
                                                          'metadata':{'antigravity_local_only':'true'}})
    assert result.status_code==400 and 'must be boolean' in result.text


def test_profile_rejects_credentials_and_remote_gateway(tmp_path):
    path=tmp_path/'local.json'
    settings=local_policy.profile('http://127.0.0.1:51122/v1',['local:one'],'local:one')
    settings['api_key']='synthetic-secret';path.write_text(json.dumps(settings))
    with pytest.raises(local_policy.LocalPolicyError,match='unsupported fields'):
        local_policy.prepare_args(argparse.Namespace(local_profile=str(path)))
    with pytest.raises(local_policy.LocalPolicyError,match='non-loopback'):
        local_policy.profile('https://remote.example.invalid/v1',['local:one'],'local:one')


def test_profile_rejects_ambiguous_duplicate_fields(tmp_path):
    path=tmp_path/'profile.json'
    path.write_text('{"version":1,"version":1}')
    with pytest.raises(local_policy.LocalPolicyError,match='duplicate field'):
        local_policy.prepare_args(argparse.Namespace(local_profile=str(path)))


def test_local_gateway_mode_does_not_accept_test_only_host_labels(monkeypatch):
    monkeypatch.setenv('ANTIGRAVITY_LOCAL_ONLY','0')
    with pytest.raises(SystemExit,match='loopback host'):
        cli.configure_local_gateway_environment(argparse.Namespace(local_only=True,host='testclient',allow_remote=False))


def test_multi_model_profile_does_not_silently_select_one_for_consult(tmp_path):
    path=tmp_path/'profile.json'
    path.write_text(json.dumps(local_policy.profile('http://127.0.0.1:51122/v1',['local:one','local:two'],'local:one')))
    with pytest.raises(local_policy.LocalPolicyError,match='exactly one'):
        local_policy.prepare_args(argparse.Namespace(command='consult',local_profile=str(path)))


def test_local_policy_receipt_is_content_free_and_idempotent(anti):
    from anti_lib.retention import lifecycle_metadata
    value={'local_policy':{'enabled':True,'destination_scope':'declared_loopback','profile_sha256':'a'*64,
                          'gateway':'private-fixture-gateway','models':['private-fixture-model']}}
    receipt=lifecycle_metadata(value)
    assert receipt==lifecycle_metadata(receipt)
    assert receipt['local_policy']['profile_sha256']=='a'*64
    assert 'private-fixture' not in json.dumps(receipt)


def test_local_catalog_omits_reserved_prefixes_that_dispatch_to_native_routes(configurations):
    configurations['openai'] = provider('openai', 'http://127.0.0.1:12345/v1', ('gpt-5.6', 'gemini-3.8-flash'))
    catalog = TestClient(server.app).get('/v1/models').json()
    assert catalog['data'] == []
    assert catalog['provider_catalog_diagnostics']['status'] == 'partial'
    assert catalog['provider_catalog_diagnostics']['scope'] == 'loopback_routes_only'
    assert catalog['provider_catalog_diagnostics']['providers'][0]['omitted_models'] == 2


def test_local_workflow_forwards_profile_models_and_policy(anti, monkeypatch, tmp_path):
    settings = local_policy.profile('http://127.0.0.1:51122/v1', ['local:one', 'local:two'], 'local:two')
    profile_path = tmp_path / 'local.json'
    profile_path.write_text(json.dumps(settings))
    captured = []
    def panel(args):
        captured.append(args)
        assert args.local_only and args._local_policy['enabled']
        assert args._run_control.local_policy is args._local_policy
        assert args.model == ['local:one', 'local:two']
        assert args.judge == 'local:two' and args.fallback_policy == 'never'
        return 0
    monkeypatch.setattr(anti, 'command_panel', panel)
    assert anti.main(['workflow', 'provider-compare', '--local-profile', str(profile_path), '--prompt', 'fixture', '--no-progress']) == 0
    assert len(captured) == 1


def test_local_mode_does_not_expand_repository_policy(anti, monkeypatch, tmp_path, capsys):
    path = tmp_path / 'policy.json'
    path.write_text(json.dumps({'schemaVersion': 1, 'destinations': [
        {'baseUrl': 'http://127.0.0.1:51122/v1', 'model': 'local:other', 'stages': ['primary']}],
        'forbiddenPaths': [], 'maxScanChars': 524288}))
    monkeypatch.setattr(anti, 'fetch_model_ids', lambda *a, **k: pytest.fail('policy must refuse before lookup'))
    assert anti.main(['consult', '--local-only', '--model', 'local:one', '--data-policy', str(path),
                      '--prompt', 'fixture', '--no-pre-read', '--no-progress']) == 1
    assert 'denied' in capsys.readouterr().err


@pytest.mark.parametrize('option', ['op_env_file', 'op_environment'])
def test_local_start_refuses_secret_network_wrapper(monkeypatch, option):
    monkeypatch.setenv('ANTIGRAVITY_LOCAL_ONLY', '0')
    args = argparse.Namespace(local_only=True, host='127.0.0.1', allow_remote=False)
    setattr(args, option, 'synthetic-fixture')
    with pytest.raises(SystemExit, match='1Password network wrapper'):
        cli.configure_local_gateway_environment(args)
