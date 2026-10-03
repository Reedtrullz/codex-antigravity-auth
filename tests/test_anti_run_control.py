"""Synthetic monotonic-clock and transport tests; no real credentials or gateway."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import json
from pathlib import Path
import threading
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def anti(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location('anti_deadline_fixture', Path(__import__('codex_antigravity_auth').__file__).parent/'skills/anti/scripts/anti.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, 'ensure_helper_parity', lambda args: None)
    monkeypatch.setattr(module, 'RUNS_DIR', tmp_path/'runs')
    return module


def args(anti, **overrides):
    result = argparse.Namespace(base_url='http://127.0.0.1:51122/v1', timeout=30, gateway_token_env='FIXTURE_ONLY_TOKEN',
        retry=0, fallback_model='openrouter:fixture', fallback_policy='on-retryable', progress=False, budget=None,
        run_timeout=10, max_parallel=8, run_id='fixture-run', save_output='never')
    for key,value in overrides.items(): setattr(result,key,value)
    return result


def success(text='A complete synthetic answer with retained evidence.'):
    return {'status':'completed','output':[{'type':'message','role':'assistant','content':[{'type':'output_text','text':text}]}]}


def set_transport(monkeypatch, anti, transport):
    # These fakes replace request_json, so explicitly simulate its final HTTP
    # entry boundary. Preparation-expiry regressions below use real request_json.
    def entered(method, url, **kwargs):
        kwargs['timeout'] = anti.transport_entry_timeout(method, kwargs['timeout'])
        return transport(method, url, **kwargs)
    monkeypatch.setattr(anti, 'request_json', entered)


@pytest.mark.parametrize('value', [0,-1,float('inf'),float('nan'),86401,True])
def test_invalid_run_duration_is_rejected_before_transport(anti, value):
    with pytest.raises(anti.AntiError): anti.run_control(args(anti,run_timeout=value))


def test_multiple_primaries_fallback_to_one_destination_under_shared_cap(anti, monkeypatch):
    settings=args(anti)
    primaries=[f'fixture{i}:model' for i in range(6)]
    models=set(primaries+['openrouter:fixture'])
    barrier=threading.Barrier(len(primaries))
    lock=threading.Lock(); active=peak=0
    def transport(method,url,*,payload,**kwargs):
        nonlocal active,peak
        if payload['model'] in primaries:
            barrier.wait(timeout=3)
            return 503, {'error':'synthetic busy'}
        with lock:
            active+=1; peak=max(peak,active)
        time.sleep(.015)
        with lock: active-=1
        return 200, success()
    set_transport(monkeypatch,anti,transport)
    control=anti.run_control(settings)
    def lane(model):
        return anti.generate_with_fallback(settings,model=model,prompt='fixture',max_output_tokens=32,purpose='lane',model_ids=models)
    with ThreadPoolExecutor(max_workers=6) as pool:
        results=list(pool.map(lane,primaries))
    assert all(row[1]=='openrouter:fixture' for row in results)
    assert peak==2 and active==0
    snapshot=control.snapshot()
    assert snapshot['attempts_started']==12
    assert snapshot['permits_acquired']==snapshot['permits_released']==12


@pytest.mark.parametrize('delay', [5,999])
def test_retry_after_beyond_remaining_defers_without_sleep_or_fallback(anti, monkeypatch, delay):
    settings=args(anti,retry=2)
    clock=[0.0]
    settings._run_control=anti.RunControl(1,caps=anti.PROVIDER_PARALLEL_CAPS,clock=lambda:clock[0],
        sleeper=lambda seconds:pytest.fail('must not sleep'),error_type=anti.RunDeadlineExceeded)
    calls=[]
    def transport(method,url,**kwargs):
        calls.append(kwargs['payload']['model'])
        return 429, {'error':'synthetic busy','_retry_after_seconds':delay}
    set_transport(monkeypatch,anti,transport)
    with pytest.raises(anti.RunDeadlineExceeded):
        anti.generate_with_fallback(settings,model='fixture:primary',prompt='fixture',max_output_tokens=32,purpose='lane',model_ids={'fixture:primary','openrouter:fixture'})
    assert calls==['fixture:primary']
    assert settings._run_control.snapshot()['deferred_calls']==1
    assert settings._run_control.snapshot()['permits_released']==1


def test_deadline_is_not_reset_by_retry_and_each_attempt_releases_permit(anti, monkeypatch):
    settings=args(anti,retry=1,fallback_model=None)
    clock=[0.0]; timeouts=[]
    settings._run_control=anti.RunControl(5,caps={},clock=lambda:clock[0],sleeper=lambda seconds:clock.__setitem__(0,clock[0]+seconds),error_type=anti.RunDeadlineExceeded)
    def transport(method,url,**kwargs):
        timeouts.append(kwargs['timeout']);clock[0]+=1
        return (503,{'error':'fixture'}) if len(timeouts)==1 else (200,success())
    set_transport(monkeypatch,anti,transport)
    anti.generate_with_fallback(settings,model='fixture:model',prompt='fixture',max_output_tokens=32,purpose='lane',model_ids={'fixture:model'})
    assert timeouts==[5,3.25]
    assert settings._run_control.snapshot()['permits_released']==2


def test_expired_admission_does_not_reserve_money_or_make_http_call(anti, monkeypatch):
    settings=args(anti)
    clock=[0.0]
    settings._run_control=anti.RunControl(1,caps={},clock=lambda:clock[0],error_type=anti.RunDeadlineExceeded)
    clock[0]=2
    monkeypatch.setattr(anti,'request_json',lambda *a,**k:pytest.fail('no HTTP'))
    monkeypatch.setattr(anti,'reserve_budget_call',lambda *a,**k:pytest.fail('no reservation'))
    with pytest.raises(anti.RunDeadlineExceeded) as caught:
        anti.post_response(base_url=settings.base_url,model='fixture:model',prompt='fixture',max_output_tokens=32,
                           timeout=30,token_env='FIXTURE',model_ids={'fixture:model'},budget_args=settings)
    assert caught.value.submitted is False
    assert settings._run_control.snapshot()['attempts_started']==0


def test_waiting_destination_expires_and_permit_is_reusable(anti):
    held=anti.RunControl(2,caps={'fixture':1})
    waiting=anti.RunControl(.03,caps={'fixture':1},error_type=anti.RunDeadlineExceeded)
    with held.attempt('fixture-gateway','fixture'):
        with ThreadPoolExecutor(max_workers=1) as pool:
            def wait():
                with waiting.attempt('fixture-gateway','fixture'): pytest.fail('must not acquire')
            future=pool.submit(wait)
            with pytest.raises(anti.RunDeadlineExceeded): future.result(timeout=1)
    with anti.RunControl(1,caps={'fixture':1}).attempt('fixture-gateway','fixture'): pass
    assert waiting.snapshot()['permits_acquired']==0


def test_exception_releases_destination_permit(anti):
    control=anti.RunControl(1,caps={'fixture':1})
    with pytest.raises(RuntimeError):
        with control.attempt('fixture-gateway','fixture'): raise RuntimeError('synthetic')
    with control.attempt('fixture-gateway','fixture'): pass
    assert control.snapshot()['permits_acquired']==control.snapshot()['permits_released']==2


def test_catalog_route_and_family_own_destination_aliases(anti):
    anti.CAPABILITY_REGISTRY.entries={'alias':{'canonical_id':'canonical','route':'byok','family':'openrouter'},
                                     'canonical':{'canonical_id':'canonical','route':'byok','family':'openrouter'},
                                     'gpt-fixture':{'canonical_id':'gpt-fixture','route':'openai'}}
    anti.CAPABILITY_REGISTRY.aliases={'other':'canonical'}
    assert anti.destination_for_model('alias')==anti.destination_for_model('other')=='openrouter'
    assert anti.destination_for_model('gpt-fixture')=='openai'
    assert anti.destination_for_model('unknown/slash')=='unknown-route'


def test_body_drip_uses_remaining_time_and_fails_after_deadline(anti):
    clock=[0.0]; timeouts=[]
    control=anti.RunControl(1,caps={},clock=lambda:clock[0],error_type=anti.RunDeadlineExceeded)
    class Sock:
        def settimeout(self,value): timeouts.append(value)
    class Body:
        fp=argparse.Namespace(raw=argparse.Namespace(_sock=Sock()))
        def read1(self, size): clock[0]+=.6;return b'x'
    with control.bind(),pytest.raises(anti.RunDeadlineExceeded) as caught:
        anti.read_response_body(Body(),10)
    assert timeouts==[1,.4] and caught.value.submitted


def test_workflow_expansion_shares_existing_deadline(anti, monkeypatch):
    settings=anti.build_parser().parse_args(['workflow','quick-check','--prompt','fixture','--run-timeout','7','--no-progress'])
    seen=[]
    monkeypatch.setattr(anti,'command_panel',lambda expanded:seen.append(expanded._run_control) or 0)
    monkeypatch.setattr(anti,'_install_run_signal_handlers',lambda args:None)
    anti.command_workflow(settings)
    assert seen==[settings._run_control] and seen[0].limit==7


def test_panel_judge_expiry_preserves_partial_lane_record_without_raw_never_data(anti, monkeypatch, tmp_path):
    models={'claude-sonnet-4-6','claude-opus-4-6-thinking'}
    monkeypatch.setattr(anti,'fetch_model_ids',lambda *a,**k:models)
    monkeypatch.setattr(anti,'_install_run_signal_handlers',lambda args:None)
    clock=[0.0]
    control=anti.RunControl(1,caps=anti.PROVIDER_PARALLEL_CAPS,clock=lambda:clock[0],error_type=anti.RunDeadlineExceeded)
    def get(args=None):
        if args is not None:args._run_control=control
        return control
    monkeypatch.setattr(anti,'run_control',get)
    def transport(method,url,**kwargs):
        return 200,success('private-fixture-lane-output')
    set_transport(monkeypatch,anti,transport)
    build=anti.build_panel_synthesis_prompt
    def expire(**kwargs):
        result=build(**kwargs);clock[0]=2;return result
    monkeypatch.setattr(anti,'build_panel_synthesis_prompt',expire)
    rc=anti.main(['panel','--mode','ask','--prompt','private-fixture-prompt','--run-id','fixture-run','--save-output','never','--no-progress'])
    assert rc==1
    record=json.loads((tmp_path/'runs/fixture-run.json').read_text())
    assert record['runStatus']!='success'
    assert record['metadata']['scope_status']=='partial'
    assert record['metadata']['panel_lane_count']==2
    assert 'panel_results' not in record['metadata']
    assert record['metadata']['run_control']['attempts_started']==2
    assert 'private-fixture' not in json.dumps(record)


def test_lifecycle_controls_retain_only_bounded_content_free_fields(anti):
    projected = anti.lifecycle_metadata({
        'run_control': {'attempts_started': 3, 'deferred_calls': True,
                        'elapsed_seconds': float('nan'), 'remaining_seconds': 0,
                        'scope': 'process_local', 'events': [{'model': 'private-fixture'}]},
        'panel_lane_count': 2, 'judge_attempt_count': 1, 'synthesis_status': 'not_sent',
        'panel_results': [{'output': 'private-fixture'}],
    })
    assert projected == {
        'run_control': {'eventsRetained': False, 'attempts_started': 3,
                        'remaining_seconds': 0, 'scope': 'process_local'},
        'panel_lane_count': 2, 'judge_attempt_count': 1, 'synthesis_status': 'not_sent',
    }



def test_chunk_deadline_preserves_completed_coverage_and_counts_unsent_chunk(anti, monkeypatch):
    settings=anti.build_parser().parse_args(['review','--scope','files','--max-prompt-chars','1200',
        '--max-review-chunks','0','--chunked','always','--no-progress'])
    clock=[0.0]
    settings._run_control=anti.RunControl(1,caps={},clock=lambda:clock[0],error_type=anti.RunDeadlineExceeded)
    model='claude-opus-4-6-thinking'
    monkeypatch.setattr(anti,'fetch_model_ids',lambda *a,**k:{model})
    calls=[]
    def transport(*a,**kwargs):
        calls.append(kwargs['payload'])
        return 200,success()
    set_transport(monkeypatch,anti,transport)
    generate=anti.generate_with_fallback
    def expire(*a,**kwargs):
        result=generate(*a,**kwargs);clock[0]=2;return result
    monkeypatch.setattr(anti,'generate_with_fallback',expire)
    context={'scope_line':'files','diff':'','file_texts':[('fixture.py','x'*4000)],
        'file_records':[{'path':'fixture.py'}],'paths':['fixture.py'],'excluded':[],'caveats':[]}
    chunks,metadata=anti.build_review_chunk_prompts(context,max_prompt_chars=1200,max_chunks=0)
    with pytest.raises(anti.RunDeadlineExceeded) as caught:
        anti.run_chunked_review(args=settings,context=context,model=model,base_metadata={},max_prompt_chars=1200,
                                chunks=chunks,chunk_metadata=metadata)
    assert len(calls)==1
    evidence=caught.value.run_metadata
    assert evidence['completed_chunk_count']==1 and evidence['failed_chunk_count']==0
    assert evidence['not_sent_chunk_count']==len(chunks)-1
    assert evidence['scope_status']=='partial'
    assert evidence['chunk_generation'][1]['status']=='not_sent'
    assert len(evidence['_execution_ledger'])==1


def test_owned_http_response_is_read_with_deadline_and_no_credential_dependency(anti):
    from fake_upstream import upstream
    body=json.dumps(success()).encode()
    with upstream((200,{'Content-Type':'application/json'},body)) as (base,seen):
        settings=args(anti,base_url=base,fallback_model=None)
        text,model,metadata=anti.generate_with_fallback(settings,model='fixture:model',prompt='fixture',
            max_output_tokens=32,purpose='synthetic HTTP',model_ids={'fixture:model'})
    assert text.startswith('A complete') and len(seen)==1
    assert settings._run_control.snapshot()['permits_released']==1


def test_queue_deadline_records_deferred_rows_and_never_claims_complete_scope(anti, monkeypatch, tmp_path):
    models={'claude-sonnet-4-6','claude-opus-4-6-thinking'}
    monkeypatch.setattr(anti,'fetch_model_ids',lambda *a,**k:models)
    monkeypatch.setattr(anti,'_install_run_signal_handlers',lambda args:None)
    clock=[0.0]
    control=anti.RunControl(1,caps={},clock=lambda:clock[0],error_type=anti.RunDeadlineExceeded)
    def get(args=None):
        if args is not None:args._run_control=control
        return control
    monkeypatch.setattr(anti,'run_control',get)
    calls=[]
    def transport(*a,**kwargs):calls.append(1);return 200,success()
    set_transport(monkeypatch,anti,transport)
    generate=anti.generate_with_fallback
    def expire(*a,**kwargs):
        result=generate(*a,**kwargs);clock[0]=2;return result
    monkeypatch.setattr(anti,'generate_with_fallback',expire)
    assert anti.main(['panel','--mode','ask','--prompt','fixture','--run-id','fixture-run',
                      '--max-parallel','1','--min-successes','1','--save-output','summary','--no-progress'])==1
    assert len(calls)==1
    record=json.loads((tmp_path/'runs/fixture-run.json').read_text())
    assert record['scopeStatus']=='partial'
    assert any(row['status']=='deferred' for row in record['metadata']['panel_results'])


@pytest.mark.parametrize('status,body', [(200,{'status':'failed','error':{'message':'fixture refusal'}}),
                                       (403,{'error':'fixture denial'}), (503,{'error':'fixture unavailable'})])
def test_terminal_failures_preserve_submitted_attempt_evidence(anti,monkeypatch,status,body):
    settings=args(anti,fallback_model=None)
    set_transport(monkeypatch,anti,lambda *a,**k:(status,body))
    with pytest.raises(anti.AntiError) as caught:
        anti.generate_with_fallback(settings,model='fixture:model',prompt='fixture',max_output_tokens=32,
                                    purpose='lane',model_ids={'fixture:model'})
    assert caught.value.submitted is True
    assert caught.value.generation_metadata['submitted'] is True
    assert settings._run_control.snapshot()['permits_released']==1


@pytest.mark.parametrize('preparation', ['json','request','opener'])
def test_preparation_expiry_never_enters_transport_or_marks_submitted(anti,monkeypatch,preparation):
    settings=args(anti, fallback_model=None, budget=1)
    clock=[0.0]
    settings._run_control=anti.RunControl(1,caps={},clock=lambda:clock[0],error_type=anti.RunDeadlineExceeded)
    if preparation=='context':
        assess=anti.assess_context
        def expire(*values,**kwargs):
            result=assess(*values,**kwargs);clock[0]=2;return result
        monkeypatch.setattr(anti,'assess_context',expire)
    elif preparation=='json':
        encode=anti.json.dumps
        def expire(value,*values,**kwargs):
            result=encode(value,*values,**kwargs)
            if isinstance(value,dict) and value.get('input')=='fixture':clock[0]=2
            return result
        monkeypatch.setattr(anti.json,'dumps',expire)
    elif preparation=='request':
        request=anti.urllib.request.Request
        def expire(*values,**kwargs):
            result=request(*values,**kwargs);clock[0]=2;return result
        monkeypatch.setattr(anti.urllib.request,'Request',expire)
    else:
        def expire(*handlers):
            clock[0]=2
            return argparse.Namespace(open=lambda *a,**k:pytest.fail('no late POST'))
        monkeypatch.setattr(anti.urllib.request,'build_opener',expire)
    monkeypatch.setattr(anti.urllib.request,'urlopen',lambda *a,**k:pytest.fail('no late POST'))
    with pytest.raises(anti.RunDeadlineExceeded) as caught:
        anti.generate_with_fallback(settings,model='claude-sonnet-4-6',prompt='fixture',max_output_tokens=32,
                                    purpose='preparation',model_ids={'claude-sonnet-4-6'})
    assert caught.value.submitted is False and caught.value.generation_metadata['submitted'] is False
    snapshot=settings._run_control.snapshot()
    assert snapshot['attempts_started']==0
    assert snapshot['permits_acquired']==snapshot['permits_released']==(0 if preparation=='context' else 1)
    state=anti.budget_metadata(settings)
    if preparation=='context':
        assert not hasattr(settings,'_anti_budget_state')
        assert 'budget_attempts' not in state
    else:
        assert state['budget_reserved']==state['budget_committed']==0
        assert state['budget_attempts'][-1]['status']=='not_sent'


def test_transport_timeout_is_rechecked_after_preparation(anti,monkeypatch):
    import io
    settings=args(anti,fallback_model=None)
    clock=[0.0];timeouts=[]
    settings._run_control=anti.RunControl(1,caps={},clock=lambda:clock[0],error_type=anti.RunDeadlineExceeded)
    body=json.dumps(success()).encode()
    request=anti.urllib.request.Request
    def prepare(*values,**kwargs):
        result=request(*values,**kwargs);clock[0]=.25;return result
    monkeypatch.setattr(anti.urllib.request,'Request',prepare)
    def opened(req,*,timeout):
        timeouts.append(timeout)
        response=io.BytesIO(body);response.status=200;return response
    monkeypatch.setattr(anti.urllib.request,'urlopen',opened)
    monkeypatch.setattr(anti.urllib.request,'build_opener',lambda *handlers:argparse.Namespace(open=opened))
    anti.generate_with_fallback(settings,model='fixture:model',prompt='fixture',max_output_tokens=32,
                                purpose='preparation',model_ids={'fixture:model'})
    assert timeouts==[.75]
    assert settings._run_control.snapshot()['attempts_started']==1


@pytest.mark.parametrize('retention', ['full','summary','never'])
def test_deferred_judge_retry_retains_first_judge_evidence_per_policy(anti,monkeypatch,tmp_path,retention):
    models={'claude-sonnet-4-6','claude-opus-4-6-thinking'}
    monkeypatch.setattr(anti,'fetch_model_ids',lambda *a,**k:models)
    monkeypatch.setattr(anti,'_install_run_signal_handlers',lambda args:None)
    clock=[0.0]
    control=anti.RunControl(1,caps=anti.PROVIDER_PARALLEL_CAPS,clock=lambda:clock[0],error_type=anti.RunDeadlineExceeded)
    def get(settings=None):
        if settings is not None:settings._run_control=control
        return control
    monkeypatch.setattr(anti,'run_control',get)
    calls=[]
    def transport(*a,**kwargs):
        calls.append(kwargs['payload'])
        if 'You are synthesizing an Antigravity multi-model advisory panel' in kwargs['payload']['input']:
            return 200,success('first-judge-private-fixture malformed output')
        return 200,success()
    set_transport(monkeypatch,anti,transport)
    parse=anti.parse_panel_findings
    def expire(text):
        result=parse(text)
        if text.startswith('first-judge-private-fixture'):clock[0]=2
        return result
    monkeypatch.setattr(anti,'parse_panel_findings',expire)
    assert anti.main(['panel','--mode','ask','--prompt','fixture','--run-id','judge-retry',
                      '--save-output',retention,'--no-progress'])==1
    assert len(calls)==3
    record=json.loads((tmp_path/'runs/judge-retry.json').read_text())
    assert record['metadata']['judge_attempt_count']==1
    assert record['metadata']['synthesis_status']=='not_sent'
    if retention=='full':
        assert len(record['metadata']['judge_attempts'])==1
        judges=[entry for entry in record['execution_ledger'] if entry['stage']=='panel_judge_1']
        assert len(judges)==1 and judges[0]['output'].startswith('first-judge-private-fixture')
        assert judges[0]['generation']['submitted'] is True
    else:
        assert 'first-judge-private-fixture' not in json.dumps(record)


def test_plan_deferred_chunk_has_disjoint_failed_and_not_sent_counts(anti,monkeypatch):
    settings=anti.build_parser().parse_args(['plan','--prompt','fixture','--chunked','always',
        '--max-plan-chunks','20','--no-progress'])
    clock=[0.0]
    settings._run_control=anti.RunControl(1,caps={},clock=lambda:clock[0],error_type=anti.RunDeadlineExceeded)
    model='claude-opus-4-6-thinking'
    monkeypatch.setattr(anti,'fetch_model_ids',lambda *a,**k:{model})
    calls=[]
    def transport(*a,**kwargs):calls.append(1);return 200,success()
    set_transport(monkeypatch,anti,transport)
    generate=anti.generate_with_fallback
    def expire(*a,**kwargs):
        result=generate(*a,**kwargs);clock[0]=2;return result
    monkeypatch.setattr(anti,'generate_with_fallback',expire)
    with pytest.raises(anti.RunDeadlineExceeded) as caught:
        anti.run_chunked_plan(args=settings,prompt='x'*6000,model=model,caveats=[],max_prompt_chars=1200)
    evidence=caught.value.run_metadata
    assert len(calls)==1 and evidence['completed_chunk_count']==1
    assert evidence['failed_chunk_count']==0
    assert evidence['not_sent_chunk_count']==evidence['planned_chunk_count']-1
    assert evidence['chunk_generation'][1]['status']=='not_sent'
    assert evidence['chunk_generation'][1]['submitted'] is False


def test_compare_stops_and_labels_remaining_models_deferred(anti,monkeypatch,tmp_path,capsys):
    models={'fixture:a','fixture:b','fixture:c'}
    monkeypatch.setattr(anti,'fetch_model_ids',lambda *a,**k:models)
    monkeypatch.setattr(anti,'ensure_models_available',lambda **kwargs:None)
    monkeypatch.setattr(anti,'_install_run_signal_handlers',lambda args:None)
    clock=[0.0]
    control=anti.RunControl(1,caps={},clock=lambda:clock[0],error_type=anti.RunDeadlineExceeded)
    def get(settings=None):
        if settings is not None:settings._run_control=control
        return control
    monkeypatch.setattr(anti,'run_control',get)
    calls=[]
    def transport(*a,**kwargs):calls.append(1);return 200,success()
    set_transport(monkeypatch,anti,transport)
    generate=anti.generate_with_fallback;generation_models=[]
    def expire(*a,**kwargs):
        generation_models.append(kwargs['model'])
        result=generate(*a,**kwargs);clock[0]=2;return result
    monkeypatch.setattr(anti,'generate_with_fallback',expire)
    assert anti.main(['compare','--model','fixture:a','--model','fixture:b','--model','fixture:c',
                      '--prompt','fixture','--run-id','compare-fixture','--json','--no-progress'])==1
    result=json.loads(capsys.readouterr().out)
    assert len(calls)==1 and generation_models==['fixture:a','fixture:b']
    assert [row['status'] for row in result['results']]==['success','deferred','deferred']
    assert [row['submitted'] for row in result['results']]==[True,False,False]
    assert result['metadata']['scopeStatus']=='partial'
