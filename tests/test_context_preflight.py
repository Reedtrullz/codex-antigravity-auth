"""Context evidence is synthetic; no real tokenizer/limit/provider is certified."""
from copy import deepcopy
import asyncio
import argparse
import io
from urllib.parse import urlsplit
import importlib.util
import json
from pathlib import Path
from unittest.mock import AsyncMock
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from codex_antigravity_auth import byok, context_preflight, server
from codex_antigravity_auth.skills.anti.scripts.anti_lib.context_budget import (
    assess, calibration, VerifiedContext,
)
from codex_antigravity_auth.resource_limits import Admission, ResourceLimits
from fake_upstream import upstream


def proof(counter=lambda request: 20, **overrides):
    values = dict(binding='fixture-binding', source='synthetic exact-count oracle', context_tokens=100,
                  count_input=counter, output_includes_reasoning=True)
    values.update(overrides)
    return VerifiedContext(**values)


def test_whole_request_inventory_counts_tools_instructions_format_and_unicode_without_mutation():
    request = {'model':'fixture:x', 'input':'😀' * 10, 'instructions':'long ' * 100,
               'tools':[{'type':'function', 'name':'fixture', 'parameters':{'type':'object', 'properties':{
                   'arg':{'type':['string','null'], 'description':'wide ' * 500}}}}],
               'text':{'format':{'type':'json_schema', 'schema':{'type':'object'}}},
               'max_output_tokens':70, 'reasoning':{'effort':'high'}}
    before = deepcopy(request)
    report = assess(request, declared_tokens=64)
    assert request == before
    assert all(report['input']['component_bytes'][group] > 0 for group in ('input','instructions','tools','format','controls'))
    assert report['input']['serialized_utf8_bytes'] > len(json.dumps(request, ensure_ascii=False, separators=(',', ':')))
    assert report['status'] == 'unknown' and report['declared_comparison'] == 'estimate_exceeds_declaration'
    assert report['input']['exact_tokens'] is None and report['input']['estimate_is_upper_bound'] is False
    assert 'reasoning_reservation' in report['unknown_components']
    assert 'wide' not in json.dumps(report) and '😀' not in json.dumps(report)


@pytest.mark.parametrize('item', [
    {'type':'image','source':{'type':'base64','media_type':'image/png','data':'fixture'}},
    {'type':'image','image_url':'https://fixture.invalid/image.png'},
    {'type':'input_image','image_url':'data:image/png;base64,fixture'},
    {'type':'input_image','image_url':'https://fixture.invalid/image.png'},
    {'type':'reasoning','encrypted_content':'fixture'},
    {'type':'input_audio','data':'fixture'},
])
def test_media_and_opaque_items_remain_unknown_without_compatible_counter(item):
    report = assess({'input':[item], 'max_output_tokens':20}, declared_tokens=100000)
    assert report['status'] == 'unknown'
    assert any('token_cost' in reason for reason in report['unknown_components'])


@pytest.mark.parametrize('output,status', [(80,'fit'), (81,'reject')])
def test_verified_complete_count_and_output_reservation(output,status):
    report = assess({'input':'fixture','max_output_tokens':output}, binding='fixture-binding', evidence=proof())
    assert report['status'] == status and report['input']['exact_tokens'] == 20
    assert report['limit']['verified_tokens'] == 100


def test_output_alone_can_exceed_verified_limit_despite_unknown_input():
    report = assess({'input':'fixture','max_output_tokens':101}, binding='fixture-binding', evidence=proof(counter=None))
    assert report['status'] == 'reject' and 'compatible_tokenizer' in report['unknown_components']


@pytest.mark.parametrize('counter', [None, lambda request: None, lambda request: -1, lambda request: True,
                                    lambda request: 1/0])
def test_missing_incompatible_or_failed_counter_never_certifies_fit(counter):
    report = assess({'input':'fixture','max_output_tokens':10}, binding='fixture-binding', evidence=proof(counter=counter))
    assert report['status'] == 'unknown' and report['input']['exact_tokens'] is None


def test_stale_binding_and_declared_values_do_not_become_verified_limits():
    report = assess({'input':'fixture','max_output_tokens':101}, declared_tokens=1,
                    binding='changed-deployment', evidence=proof())
    assert report['status'] == 'unknown' and report['limit']['verified_tokens'] is None
    assert 'verified_context_limit' in report['unknown_components']


def test_no_output_or_reasoning_bound_stays_unknown():
    assert assess({'input':'fixture'}, binding='fixture-binding', evidence=proof())['status'] == 'unknown'
    report = assess({'input':'fixture','max_output_tokens':10}, binding='fixture-binding',
                    evidence=proof(output_includes_reasoning=False))
    assert report['status'] == 'unknown' and 'reasoning_reservation' in report['unknown_components']


def test_exact_adapter_accounts_for_media_and_server_history_only_when_it_returns_a_count():
    request = {'input':[{'type':'input_image','image_url':'fixture'}], 'previous_response_id':'fixture', 'max_output_tokens':10}
    assert assess(request, binding='fixture-binding', evidence=proof())['status'] == 'fit'
    report = assess(request, binding='fixture-binding', evidence=proof(counter=lambda _:None))
    assert 'server_side_history' in report['unknown_components'] and report['status'] == 'unknown'


def test_counter_cannot_modify_the_dispatched_request():
    request = {'input':'must remain','max_output_tokens':10}
    def counter(value):
        value['input'] = 'mutated'
        return 20
    assess(request, binding='fixture-binding', evidence=proof(counter=counter))
    assert request['input'] == 'must remain'


def test_calibration_is_observation_without_billing_or_limit_authority():
    report = assess({'input':'fixture','max_output_tokens':10})
    observed = calibration(report, {'input_tokens':13})
    assert observed['observed_minus_estimated'] == 13-report['input']['estimated_tokens']
    assert observed['verifies_limit'] is False and observed['billing_authority'] is False
    assert calibration(report, {})['observed_input_tokens'] is None


@pytest.fixture
def gateway(monkeypatch):
    values = {}
    monkeypatch.setenv('ANTIGRAVITY_LOCAL_ONLY','0')
    monkeypatch.setenv('ANTIGRAVITY_UNIFIED_MODEL_PICKER','1')
    monkeypatch.setattr(server.app.state, 'local_only_mode', None, raising=False)
    monkeypatch.setattr(server, 'all_provider_configs', lambda:values)
    monkeypatch.setattr(server, 'all_provider_configs_read_only', lambda:values)
    monkeypatch.setattr(server, 'write_request_record', lambda record:None)
    monkeypatch.setattr(context_preflight, 'VERIFIED_CONTEXTS', {})
    return values


def provider(base):
    value = byok.normalize_provider_config({'providers':{'fixture':{'baseUrl':base,'apiKey':'synthetic-key',
        'models':[{'id':'arbitrary-unknown','context_window':50}]}}})['providers']['fixture']
    return byok.merged_provider_config('fixture', value)


class RawRequest:
    def __init__(self, body, *, started=None):
        self.body = body
        self.headers = {'content-length': str(len(body)), 'content-type': 'application/json'}
        self.state = SimpleNamespace()
        self.read_chunks = 0
        self.started = started

    async def stream(self):
        self.read_chunks += 1
        if self.started is not None:
            self.started.set()
        yield self.body

    async def is_disconnected(self):
        return False


def test_nongenerating_preflight_reads_declarations_without_provider_or_auth_activity(gateway,monkeypatch):
    gateway['fixture'] = provider('http://127.0.0.1:12345/v1')
    monkeypatch.setattr(server, 'resolve_openai_auth', lambda:pytest.fail('no auth'))
    monkeypatch.setattr(server.httpx, 'AsyncClient', lambda **kwargs:pytest.fail('no provider'))
    request = {'model':'fixture:arbitrary-unknown','input':'long fixture ' * 100, 'max_output_tokens':100}
    response = TestClient(server.app).post('/v1/context/preflight', json=request)
    assert response.status_code == 200
    report = response.json()
    assert report['status'] == 'unknown' and report['limit']['declared_tokens'] == 50
    assert report['declared_comparison'] == 'estimate_exceeds_declaration'
    assert 'long fixture' not in response.text


@pytest.mark.parametrize('kind', ['body', 'depth'])
def test_preflight_applies_current_json_limits_before_reading_provider_configuration(gateway,monkeypatch,kind):
    gateway['fixture'] = provider('http://127.0.0.1:12345/v1')
    provider_reads = []
    monkeypatch.setattr(server, 'all_provider_configs_read_only', lambda: provider_reads.append(True) or gateway)
    if kind == 'body':
        monkeypatch.setenv('ANTIGRAVITY_MAX_BODY_BYTES', '1024')
        body = b'{"model":"fixture:arbitrary-unknown","input":"' + b'x' * 1024 + b'"}'
        code = 'request_body_limit'
    else:
        monkeypatch.setenv('ANTIGRAVITY_MAX_JSON_DEPTH', '8')
        body = b'{"model":"fixture:arbitrary-unknown","input":' + b'[' * 9 + b'"x"' + b']' * 9 + b'}'
        code = 'json_depth_limit'
    raw = RawRequest(body)
    with pytest.raises(HTTPException) as caught:
        asyncio.run(server.context_preflight(raw))
    assert caught.value.status_code == 413
    assert caught.value.detail['code'] == code
    assert raw.read_chunks == (0 if kind == 'body' else 1)
    assert provider_reads == []


def test_preflight_malformed_json_stays_400_and_invalid_limit_policy_stays_500(gateway,monkeypatch):
    admission = Admission()
    monkeypatch.setattr(server, 'ADMISSION', admission)
    malformed = RawRequest(b'{"input":')
    with pytest.raises(HTTPException) as caught:
        asyncio.run(server.context_preflight(malformed))
    assert caught.value.status_code == 400
    assert admission.total == 0

    monkeypatch.setenv('ANTIGRAVITY_MAX_BODY_BYTES', '0')
    invalid_policy = RawRequest(b'{"model":"fixture:arbitrary-unknown","input":"x"}')
    with pytest.raises(HTTPException) as caught:
        asyncio.run(server.context_preflight(invalid_policy))
    assert caught.value.status_code == 500
    assert invalid_policy.read_chunks == 0
    assert admission.total == 0


def test_preflight_shares_global_startup_admission_and_releases_on_completion_and_cancel(gateway,monkeypatch):
    gateway['fixture'] = provider('http://127.0.0.1:12345/v1')
    configured = ResourceLimits(inflight=2, route_inflight=1)
    monkeypatch.setattr(ResourceLimits, 'from_env', classmethod(lambda cls: configured))
    admission = Admission()
    admission.set_startup_ceiling(1)
    monkeypatch.setattr(server, 'ADMISSION', admission)
    entered = asyncio.Event()

    async def block_generation(_request, _budget):
        entered.set()
        await asyncio.Future()

    monkeypatch.setattr(server, '_create_response', block_generation)

    async def scenario():
        generation = RawRequest(b'{"model":"fixture:arbitrary-unknown","input":"x"}')
        pending = asyncio.create_task(server.create_response(generation))
        await entered.wait()
        assert admission.total == 1 and admission.startup_ceiling == 1

        rejected = RawRequest(b'{"model":"fixture:arbitrary-unknown","input":"x"}')
        with pytest.raises(HTTPException) as caught:
            await server.context_preflight(rejected)
        assert caught.value.status_code == 503
        assert caught.value.detail['code'] == 'gateway_overloaded'
        assert caught.value.headers['Retry-After'] == '1'
        assert rejected.read_chunks == 0 and admission.total == 1

        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert admission.total == 0 and admission.startup_ceiling == 1

        accepted = RawRequest(b'{"model":"fixture:arbitrary-unknown","input":"x"}')
        report = await server.context_preflight(accepted)
        assert report['status'] == 'unknown'
        assert accepted.read_chunks == 1 and admission.total == 0
        assert admission.startup_ceiling == 1

    try:
        asyncio.run(scenario())
    finally:
        admission.set_startup_ceiling(None)


def test_preflight_body_read_cancellation_releases_global_admission(gateway,monkeypatch):
    admission = Admission()
    monkeypatch.setattr(server, 'ADMISSION', admission)
    entered = asyncio.Event()

    class StalledRawRequest(RawRequest):
        async def stream(self):
            self.read_chunks += 1
            entered.set()
            await asyncio.Future()
            yield b''

    async def scenario():
        request = StalledRawRequest(b'')
        pending = asyncio.create_task(server.context_preflight(request))
        await entered.wait()
        assert admission.total == 1
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert admission.total == 0

    asyncio.run(scenario())


@pytest.mark.parametrize('model',['gpt-5.6','gemini-3.8-flash','fixture:arbitrary-unknown'])
@pytest.mark.parametrize('stream',[False,True])
def test_verified_overlimit_rejects_before_account_or_transport(gateway,monkeypatch,model,stream):
    gateway['fixture'] = provider('http://127.0.0.1:12345/v1')
    request = {'model':model,'input':'fixture','max_output_tokens':81,'stream':stream}
    auth = SimpleNamespace(kind='api_key',base_url='https://fixture.invalid/v1',account_id=None)
    deployment = {'kind':auth.kind,'endpoint':server.openai_responses_url(auth),'account':None} if model == 'gpt-5.6' else None
    _, binding = context_preflight.declaration(request, gateway, deployment=deployment)
    context_preflight.VERIFIED_CONTEXTS[binding] = proof(binding=binding)
    monkeypatch.setattr(server, 'resolve_openai_auth', lambda:auth)
    monkeypatch.setattr(server, 'acquire_active_account_for_request', AsyncMock(side_effect=AssertionError('no Google account')))
    monkeypatch.setattr(server.httpx, 'AsyncClient', lambda **kwargs:pytest.fail('no provider'))
    result = TestClient(server.app).post('/v1/responses', json=request)
    assert result.status_code == 400 and result.json()['detail']['code'] == 'context_limit_exceeded'


def test_unknown_provider_stays_usable_and_request_is_not_trimmed(gateway):
    body = {'choices':[{'index':0,'message':{'role':'assistant','content':'Synthetic completed answer.'},'finish_reason':'stop'}]}
    prompt = 'complete fixture ' * 1000
    with upstream((200, {'Content-Type':'application/json'}, json.dumps(body).encode())) as (base,seen):
        gateway['fixture'] = provider(base+'/v1')
        result = TestClient(server.app).post('/v1/responses', json={
            'model':'fixture:arbitrary-unknown','input':prompt,'max_output_tokens':100})
    assert result.status_code == 200 and result.headers['X-Antigravity-Context-Status'] == 'unknown'
    assert seen[0]['body']['messages'][0]['content'] == prompt


@pytest.mark.parametrize('headers,status', [({'Content-Type':'text/plain'},415),
    ({'Content-Type':'application/json','Origin':'https://fixture.invalid'},403)])
def test_preflight_endpoint_keeps_mutating_request_guards(gateway,headers,status):
    result = TestClient(server.app).post('/v1/context/preflight', content='{"input":"fixture"}', headers=headers)
    assert result.status_code == status


def test_anti_carries_count_only_assessment_and_usage_calibration(monkeypatch,tmp_path):
    import codex_antigravity_auth
    script = Path(codex_antigravity_auth.__file__).parent/'skills/anti/scripts/anti.py'
    spec = importlib.util.spec_from_file_location('anti_context_fixture',script)
    anti = importlib.util.module_from_spec(spec);spec.loader.exec_module(anti)
    monkeypatch.setattr(anti, 'RUNS_DIR', tmp_path/'runs')
    monkeypatch.setattr(anti, 'request_json', lambda *a,**k:(200,{'status':'completed',
        'output':[{'type':'message','content':[{'type':'output_text','text':'Synthetic answer.'}]}],
        'usage':{'input_tokens':15,'output_tokens':4}}))
    result = anti.post_response(base_url='http://127.0.0.1:51122/v1', model='fixture:x', prompt='fixture prompt',
        max_output_tokens=20, timeout=5, token_env='FIXTURE', model_ids={'fixture:x'})
    report = result.response_metadata['context_preflight']
    assert report['status'] == 'unknown' and report['output']['requested_tokens'] == 20
    assert 'fixture prompt' not in json.dumps(report)
    assert result.response_metadata['context_calibration']['observed_input_tokens'] == 15


def test_changed_provider_endpoint_invalidates_verified_binding(gateway):
    gateway['fixture'] = provider('http://127.0.0.1:12345/v1')
    request = {'model':'fixture:arbitrary-unknown','input':'fixture','max_output_tokens':81}
    _, binding = context_preflight.declaration(request, gateway)
    context_preflight.VERIFIED_CONTEXTS[binding] = proof(binding=binding)
    assert context_preflight.inspect(request, gateway)['status'] == 'reject'
    gateway['fixture']['baseUrl'] = 'http://127.0.0.1:12346/v1'
    assert context_preflight.inspect(request, gateway)['status'] == 'unknown'


def test_preflight_slow_configuration_read_obeys_owned_deadline(gateway,monkeypatch):
    import time
    monkeypatch.setattr(server, 'GOOGLE_BACKEND_TIMEOUT_SECONDS', 0.03)
    def slow():
        time.sleep(0.15)
        return {}
    monkeypatch.setattr(server, 'all_provider_configs_read_only', slow)
    response = TestClient(server.app).post('/v1/context/preflight', json={'input':'fixture'})
    assert response.status_code == 504
    assert server.ADMISSION.total == 0


def test_native_openai_evidence_needs_the_actual_endpoint_and_auth_kind(gateway):
    request = {'model':'gpt-5.6','input':'fixture','max_output_tokens':81}
    deployment = {'kind':'api_key','endpoint':'https://fixture.invalid/v1/responses','account':None}
    _, binding = context_preflight.declaration(request, gateway, deployment=deployment)
    context_preflight.VERIFIED_CONTEXTS[binding] = proof(binding=binding)
    assert context_preflight.inspect(request, gateway)['status'] == 'unknown'
    assert context_preflight.inspect(request, gateway, deployment=deployment)['status'] == 'reject'
    changed = {**deployment, 'endpoint':'https://other.invalid/v1/responses'}
    assert context_preflight.inspect(request, gateway, deployment=changed)['status'] == 'unknown'


def test_generation_binding_uses_the_already_selected_route(gateway,monkeypatch):
    request = {'model':'gpt-5.6','input':'fixture','max_output_tokens':81}
    deployment = {'kind':'api_key','endpoint':'https://fixture.invalid/v1/responses','account':None}
    _, binding = context_preflight.declaration(request, gateway, deployment=deployment, route='openai')
    context_preflight.VERIFIED_CONTEXTS[binding] = proof(binding=binding)
    monkeypatch.setattr(context_preflight, 'classify_route', lambda *a,**k:pytest.fail('do not reclassify a selected route'))
    assert context_preflight.inspect(request, gateway, deployment=deployment, route='openai')['status'] == 'reject'


def test_default_reasoning_remains_unknown_until_verified_cap_semantics_clear_it():
    request = {'input':'fixture','max_output_tokens':10}
    assert 'reasoning_reservation' in assess(request)['unknown_components']
    report = assess(request, binding='fixture-binding', evidence=proof())
    assert report['status'] == 'fit' and 'reasoning_reservation' not in report['unknown_components']


def test_metadata_projection_preserves_the_original_request_and_counter_view():
    base = {'input':'fixture','max_output_tokens':10}
    value = {**base,'metadata':{'run_id':'private-fixture','antigravity_backend_timeout_seconds':30}}
    before = deepcopy(value)
    assert assess(value) == assess(base)
    assert value == before
    def counter(request):
        assert request == base and 'metadata' not in request
        return 20
    assert assess(value, binding='fixture-binding', evidence=proof(counter=counter))['status'] == 'fit'


def test_preflight_generation_and_anti_retry_reports_use_one_context_projection(gateway,monkeypatch,tmp_path):
    import codex_antigravity_auth
    script = Path(codex_antigravity_auth.__file__).parent/'skills/anti/scripts/anti.py'
    spec = importlib.util.spec_from_file_location('anti_context_parity_fixture',script)
    anti = importlib.util.module_from_spec(spec);spec.loader.exec_module(anti)
    monkeypatch.setattr(anti, 'RUNS_DIR', tmp_path/'runs')
    reports = []
    enforce = context_preflight.enforce
    def capture(request,providers,**kwargs):
        result = enforce(request,providers,**kwargs)
        reports.append(result)
        return result
    monkeypatch.setattr(context_preflight,'enforce',capture)
    client = TestClient(server.app)
    sent = []
    def opened(request,*,timeout,payload=None,body=None):
        path = urlsplit(request.full_url).path
        timeout = anti.transport_entry_timeout(request.get_method(), timeout, payload=payload, body=body, url=request.full_url)
        if request.get_method() == 'POST':sent.append(json.loads(body))
        result = client.request(request.get_method(),path,content=body,headers={'Content-Type':'application/json'})
        wire = io.BytesIO(result.content);wire.status=result.status_code;wire.headers=result.headers
        return wire
    monkeypatch.setattr(anti,'open_gateway_request',opened)
    clock = [0.0]
    control = anti.RunControl(20,caps={},clock=lambda:clock[0],error_type=anti.RunDeadlineExceeded)
    control.sleep = lambda delay:clock.__setitem__(0,clock[0]+3)
    args = argparse.Namespace(timeout=30, _run_control=control)
    reply = {'choices':[{'index':0,'message':{'role':'assistant','content':'Synthetic answer.'},'finish_reason':'stop'}]}
    with upstream((503,{'Content-Type':'application/json'},b'{"error":"synthetic busy"}'),
                  (200,{'Content-Type':'application/json'},json.dumps(reply).encode())) as (base,seen):
        gateway['fixture'] = provider(base+'/v1')
        result = anti.post_response(base_url='http://127.0.0.1:51122/v1',model='fixture:arbitrary-unknown',
            prompt='fixture prompt',max_output_tokens=20,timeout=30,token_env='FIXTURE',retries=1,
            run_id='fixture-run',budget_args=args)
        expected = client.post('/v1/context/preflight',json=sent[-1]).json()
    assert len(sent) == len(reports) == len(seen) == 2
    assert sent[0]['metadata'] != sent[1]['metadata']
    assert reports[0] == reports[1] == expected == result.response_metadata['context_preflight']
    assert expected['measurement_boundary'] == 'validated_responses_without_gateway_metadata'
    assert 'fixture-run' not in json.dumps(expected)
