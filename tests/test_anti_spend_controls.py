"""Synthetic admission ledgers and HTTP fixtures; no real pricing or credentials."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta, datetime, timezone
from decimal import Decimal
import importlib.util
import io
import json
from pathlib import Path
import threading

import pytest


@pytest.fixture
def anti(monkeypatch, tmp_path):
    import codex_antigravity_auth
    spec=importlib.util.spec_from_file_location('anti_spend_fixture',Path(codex_antigravity_auth.__file__).parent/'skills/anti/scripts/anti.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    monkeypatch.setattr(module,'ensure_helper_parity',lambda args:None)
    monkeypatch.setattr(module,'RUNS_DIR',tmp_path/'runs')
    monkeypatch.setattr(module,'token_from_env',lambda name:None)
    return module


def settings(**overrides):
    args=argparse.Namespace(base_url='http://127.0.0.1:51122/v1',timeout=30,gateway_token_env='FIXTURE',
        retry=0,fallback_model=None,fallback_policy='on-retryable',progress=False,budget=None,run_timeout=10,
        max_parallel=4,currency_budget=None,pricing_file=None,max_calls=None,max_total_input_tokens=None,
        max_total_output_tokens=None)
    for key,value in overrides.items():setattr(args,key,value)
    return args


def profile(tmp_path, models=None, **overrides):
    day=datetime.now(timezone.utc).date()
    data={'version':1,'currency':'USD','gateway':'http://127.0.0.1:51122/v1','source':'synthetic operator ceiling; not a real provider quote',
          'as_of':day.isoformat(),'valid_until':(day+timedelta(days=1)).isoformat(),
          'models':models or {'fixture:model':{'max_charge_per_attempt':'0.02','max_request_bytes':100000,
              'max_output_tokens':100,'includes_reasoning_and_all_fees':True,'covers_all_gateway_attempts':True}}}
    data.update(overrides)
    path=tmp_path/'prices.json';path.write_text(json.dumps(data));return path


def open_sequence(monkeypatch,anti,responses):
    calls=[];iterator=iter(responses)
    def opened(request,*,timeout):
        calls.append(json.loads(request.data))
        status,payload=next(iterator)
        result=io.BytesIO(json.dumps(payload).encode());result.status=status;return result
    # Exercise endpoint_policy.open_http_request and its before_open callback;
    # only the final socket opener is synthetic.
    monkeypatch.setattr(anti.urllib.request,'build_opener',lambda *handlers:argparse.Namespace(open=opened))
    return calls


def forbid_open(monkeypatch,anti):
    def build_opener(*handlers):
        return argparse.Namespace(open=lambda *a,**k:pytest.fail('transport must not open'))
    monkeypatch.setattr(anti.urllib.request,'build_opener',build_opener)


def response(text='Synthetic completed answer.', usage=None):
    body={'status':'completed','output':[{'type':'message','role':'assistant','content':[{'type':'output_text','text':text}]}]}
    if usage is not None:body['usage']=usage
    return body


def generate(anti,args,model='fixture:model',cap=10,models=None):
    return anti.generate_with_fallback(args,model=model,prompt='Synthetic prompt 😀',max_output_tokens=cap,
                                       purpose='fixture attempt',model_ids=models or {model})


def test_call_limit_is_atomic_and_unsent_cancellation_releases_allowance(anti):
    policy=anti.SpendControl(max_calls=1)
    barrier=threading.Barrier(4)
    def reserve(_):
        barrier.wait(timeout=2)
        try:return policy.reserve('fixture:model',10,10)
        except Exception:return None
    with ThreadPoolExecutor(max_workers=4) as pool:tickets=list(pool.map(reserve,range(4)))
    accepted=[ticket for ticket in tickets if ticket is not None]
    assert len(accepted)==1
    policy.settle(accepted[0],submitted=False)
    ticket=policy.reserve('fixture:model',10,10)
    policy.settle(ticket,submitted=True,usage=None)
    with pytest.raises(Exception,match='calls admission limit'):policy.reserve('fixture:model',10,10)
    snapshot=policy.snapshot()
    assert snapshot['committed']['calls']==1 and snapshot['reserved']['calls']==0
    assert snapshot['missing_usage_attempts']=={'input_tokens':1,'output_tokens':1}


def test_input_reservation_counts_complete_serialized_unicode_request(anti,monkeypatch):
    calls=open_sequence(monkeypatch,anti,[(200,response())])
    args=settings(max_total_input_tokens=1)
    with pytest.raises(anti.SpendAdmissionError,match='input_tokens'):generate(anti,args)
    assert calls==[]
    snapshot=args._run_control.spend_control.snapshot()
    assert snapshot['committed']['calls']==0 and snapshot['token_limit_guarantee'] is False


def test_unknown_model_stays_usable_for_call_controls_without_currency_claim(anti,monkeypatch):
    calls=open_sequence(monkeypatch,anti,[(200,response())])
    args=settings(max_calls=1)
    generate(anti,args)
    with pytest.raises(anti.SpendAdmissionError):generate(anti,args)
    snapshot=args._run_control.spend_control.snapshot()
    assert len(calls)==1 and snapshot['currency'] is None and snapshot['billing_guarantee'] is False


@pytest.mark.parametrize('patch', [
    {'as_of':'2000-01-01','valid_until':'2099-01-01'},
    {'as_of':'2099-01-01','valid_until':'2099-01-02'},
    {'valid_until':'2000-01-01'}, {'currency':'usd'}, {'source':''}, {'version':True},
])
def test_stale_unknown_or_invalid_price_profiles_fail_before_http(anti,monkeypatch,tmp_path,patch):
    path=profile(tmp_path,**patch)
    forbid_open(monkeypatch,anti)
    with pytest.raises(anti.AntiError):generate(anti,settings(currency_budget='1',pricing_file=str(path)))


@pytest.mark.parametrize('value', ['-1','nan','Infinity','1e1000000','0.0000000001'])
def test_invalid_currency_amount_is_rejected(anti,value):
    with pytest.raises(ValueError):anti.SpendControl(currency_budget=value,pricing_file='unused')


def test_incomplete_price_scope_and_unpriced_fallback_refuse(anti,monkeypatch,tmp_path):
    path=profile(tmp_path)
    args=settings(currency_budget='1',pricing_file=str(path),fallback_model='other:model')
    calls=open_sequence(monkeypatch,anti,[(503,{'error':'fixture unavailable'})])
    with pytest.raises(anti.SpendAdmissionError,match='no complete price bound'):
        generate(anti,args,models={'fixture:model','other:model'})
    snapshot=args._run_control.spend_control.snapshot()
    assert len(calls)==1 and snapshot['currency_committed_ceiling']=='0.02'
    assert snapshot['refused_attempts']==1


def test_complete_price_bounds_charge_every_retry_and_hold_missing_usage(anti,monkeypatch,tmp_path):
    path=profile(tmp_path)
    args=settings(currency_budget='0.04',pricing_file=str(path),retry=1)
    calls=open_sequence(monkeypatch,anti,[(503,{'error':'fixture busy'}),(200,response())])
    monkeypatch.setattr(anti.time,'sleep',lambda seconds:None)
    generate(anti,args)
    with pytest.raises(anti.SpendAdmissionError,match='currency'):generate(anti,args)
    snapshot=args._run_control.spend_control.snapshot()
    assert len(calls)==2 and snapshot['currency_committed_ceiling']=='0.04'
    assert snapshot['currency']['basis']=='user_declared_complete_attempt_ceiling'
    assert snapshot['currency']['provider_price_verified'] is False
    assert snapshot['missing_usage_attempts']['output_tokens']==2


def test_larger_output_cap_retry_cannot_bypass_total_allowance(anti,monkeypatch):
    args=settings(max_total_output_tokens=10)
    calls=open_sequence(monkeypatch,anti,[(200,response(usage={'input_tokens':1,'output_tokens':2,'total_tokens':3}))])
    generate(anti,args,cap=6)
    with pytest.raises(anti.SpendAdmissionError,match='output_tokens'):generate(anti,args,cap=8)
    snapshot=args._run_control.spend_control.snapshot()
    assert len(calls)==1 and snapshot['committed']['output_tokens']==6
    assert snapshot['observed_tokens']['output_tokens']==2


def test_zero_price_local_route_still_obeys_call_count(anti,monkeypatch,tmp_path):
    path=profile(tmp_path,models={'ollama:fixture':{'max_charge_per_attempt':'0','max_request_bytes':100000,
        'max_output_tokens':100,'includes_reasoning_and_all_fees':True,'covers_all_gateway_attempts':True}})
    args=settings(currency_budget='0',pricing_file=str(path),max_calls=1)
    calls=open_sequence(monkeypatch,anti,[(200,response())])
    generate(anti,args,model='ollama:fixture')
    with pytest.raises(anti.SpendAdmissionError,match='calls'):generate(anti,args,model='ollama:fixture')
    assert len(calls)==1 and args._run_control.spend_control.snapshot()['currency_committed_ceiling']=='0'


def test_observed_overrun_is_reported_and_prevents_later_attempts(anti,monkeypatch):
    args=settings(max_total_output_tokens=100)
    calls=open_sequence(monkeypatch,anti,[(200,response(usage={'input_tokens':1,'output_tokens':11,'total_tokens':12}))])
    generate(anti,args,cap=10)
    with pytest.raises(anti.SpendAdmissionError,match='observed usage exceeded'):generate(anti,args,cap=10)
    snapshot=args._run_control.spend_control.snapshot()
    assert len(calls)==1 and snapshot['assumption_exceeded']
    assert snapshot['committed']['output_tokens']==snapshot['observed_tokens']['output_tokens']==11


def test_expiry_after_reservation_refunds_unsent_allowances(anti,monkeypatch,tmp_path):
    args=settings(max_calls=1,currency_budget='0.02',pricing_file=str(profile(tmp_path)))
    control=anti.run_control(args);clock=[0.0];control.clock=lambda:clock[0];control.deadline=1
    reserve=control.spend_control.reserve
    def expire(*values,**kwargs):ticket=reserve(*values,**kwargs);clock[0]=2;return ticket
    monkeypatch.setattr(control.spend_control,'reserve',expire)
    forbid_open(monkeypatch,anti)
    with pytest.raises(anti.RunDeadlineExceeded):generate(anti,args)
    snapshot=control.spend_control.snapshot()
    assert snapshot['committed']['calls']==snapshot['reserved']['calls']==0
    assert Decimal(snapshot['currency_reserved'])==Decimal(snapshot['currency_committed_ceiling'])==0
    assert snapshot['attempts'][0]['status']=='not_sent'


def test_profile_rechecked_on_each_attempt_without_reloading_file(anti,tmp_path):
    from anti_lib.spend_control import SpendControl
    day=date(2026,10,1);clock=[day]
    path=profile(tmp_path,as_of=day.isoformat(),valid_until=day.isoformat())
    policy=SpendControl(currency_budget='1',pricing_file=path,today=lambda:clock[0])
    ticket=policy.reserve('fixture:model',100,10,gateway='http://127.0.0.1:51122/v1');policy.settle(ticket,submitted=True)
    clock[0]+=timedelta(days=1)
    with pytest.raises(Exception,match='stale'):policy.reserve('fixture:model',100,10,gateway='http://127.0.0.1:51122/v1')


def test_complete_attempt_quote_requires_reasoning_fees_and_size_bounds(anti,tmp_path):
    from anti_lib.spend_control import SpendControl
    path=profile(tmp_path,models={'fixture:model':{'max_charge_per_attempt':'0.02','max_request_bytes':100,
        'max_output_tokens':10,'includes_reasoning_and_all_fees':False,'covers_all_gateway_attempts':True}})
    with pytest.raises(ValueError,match='all fees'):SpendControl(currency_budget='1',pricing_file=path)
    data=json.loads(path.read_text());data['models']['fixture:model']['includes_reasoning_and_all_fees']=True;path.write_text(json.dumps(data))
    policy=SpendControl(currency_budget='1',pricing_file=path)
    with pytest.raises(Exception,match='scope'):policy.reserve('fixture:model',101,10,gateway='http://127.0.0.1:51122/v1')
    with pytest.raises(Exception,match='scope'):policy.reserve('fixture:model',100,11,gateway='http://127.0.0.1:51122/v1')


def test_workflow_controls_survive_expansion_and_dry_run_has_units(anti):
    args=anti.build_parser().parse_args(['workflow','quick-check','--prompt','fixture','--max-calls','2',
        '--max-total-input-tokens','4096','--max-total-output-tokens','128','--dry-run','--json'])
    expanded=anti.build_parser().parse_args(anti.workflow_expansion(args))
    assert (expanded.max_calls,expanded.max_total_input_tokens,expanded.max_total_output_tokens)==(2,4096,128)
    assert anti.model_pricing_metadata('ollama:fixture')['provider_price_known'] is False
    assert anti.model_pricing_metadata('ollama:fixture')['units']=='heuristic_units'



def test_priced_fallback_reserves_each_actual_destination_without_usage_refund(anti,monkeypatch,tmp_path):
    data={name:{'max_charge_per_attempt':charge,'max_request_bytes':100000,'max_output_tokens':100,
                'includes_reasoning_and_all_fees':True,'covers_all_gateway_attempts':True} for name,charge in [('fixture:model','0.02'),('other:model','0.03')]}
    path=profile(tmp_path,models=data)
    args=settings(currency_budget='0.05',pricing_file=str(path),fallback_model='other:model',max_calls=2)
    calls=open_sequence(monkeypatch,anti,[(503,{'error':'fixture busy'}),
        (200,response(usage={'input_tokens':0,'output_tokens':0,'total_tokens':0}))])
    _,actual,_=generate(anti,args,models={'fixture:model','other:model'})
    state=args._run_control.spend_control.snapshot()
    assert actual=='other:model' and [call['model'] for call in calls]==['fixture:model','other:model']
    assert state['currency_committed_ceiling']=='0.05'
    assert [entry['currency_ceiling'] for entry in state['attempts']]==['0.02','0.03']


def test_cli_observed_overrun_returns_nonzero_and_partial_record(anti,monkeypatch,tmp_path,capsys):
    monkeypatch.setattr(anti,'fetch_model_ids',lambda *a,**k:{'fixture:model'})
    monkeypatch.setattr(anti,'_install_run_signal_handlers',lambda args:None)
    calls=open_sequence(monkeypatch,anti,[(200,response(usage={'input_tokens':1000000,'output_tokens':1,'total_tokens':1000001}))])
    code=anti.main(['consult','--model','fixture:model','--prompt','fixture','--max-calls','2','--max-output-tokens','10',
        '--run-id','usage-overrun','--save-output','summary','--no-progress','--no-pre-read','--json'])
    assert code==1 and len(calls)==1
    result=json.loads(capsys.readouterr().out)
    assert result['runStatus']=='partial'
    assert result['metadata']['admission_controls']['assumption_exceeded']
    record=json.loads((tmp_path/'runs/usage-overrun.json').read_text())
    assert record['status']=='partial' and record['scopeStatus']=='partial'


def test_disabled_controls_are_explicit_not_zero_usage_claims(anti):
    snapshot=anti.SpendControl().snapshot()
    assert snapshot['enabled'] is False
    assert snapshot['billing_guarantee'] is snapshot['token_limit_guarantee'] is False



def test_automatic_token_cap_retry_respects_output_allowance(anti,monkeypatch,tmp_path):
    monkeypatch.setattr(anti,'fetch_model_ids',lambda *a,**k:{'fixture:model'})
    monkeypatch.setattr(anti,'_install_run_signal_handlers',lambda args:None)
    partial=response('Synthetic partial output',{'input_tokens':1,'output_tokens':6,'total_tokens':7})
    partial.update(status='incomplete',incomplete_details={'reason':'max_output_tokens'})
    calls=open_sequence(monkeypatch,anti,[(200,partial)])
    assert anti.main(['consult','--model','fixture:model','--prompt','fixture','--max-output-tokens','6',
        '--max-total-output-tokens','6','--run-id','cap-retry','--no-progress','--no-pre-read'])==1
    assert len(calls)==1
    record=json.loads((tmp_path/'runs/cap-retry.json').read_text())
    admission=record['metadata']['admission_controls']
    assert admission['committed']['output_tokens']==6 and admission['refused_attempts']==1
    assert admission['attemptsRetained'] is False and 'attempts' not in admission
    assert 'fixture:model' not in json.dumps(record)


def test_currency_quote_must_cover_gateway_internal_attempts(anti,tmp_path):
    path=profile(tmp_path)
    data=json.loads(path.read_text());data['models']['fixture:model'].pop('covers_all_gateway_attempts')
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError,match='gateway attempt'):
        anti.SpendControl(currency_budget='1',pricing_file=path)



def test_settlement_is_idempotent_and_observed_usage_never_refunds_currency(anti,tmp_path):
    policy=anti.SpendControl(max_calls=2,currency_budget='0.04',pricing_file=profile(tmp_path))
    ticket=policy.reserve('fixture:model',100,10,gateway='http://127.0.0.1:51122/v1')
    policy.settle(ticket,submitted=True,usage={'input_tokens':0,'output_tokens':0})
    policy.settle(ticket,submitted=False)
    snapshot=policy.snapshot()
    assert snapshot['committed']['calls']==1 and snapshot['reserved']['calls']==0
    assert snapshot['currency_committed_ceiling']=='0.02'
    assert snapshot['observed_tokens']=={'input_tokens':0,'output_tokens':0}


@pytest.mark.parametrize('destination', ['http://127.0.0.1:51123/v1','https://127.0.0.1:51122/v1','http://127.0.0.1:51122/other'])
def test_quote_gateway_mismatch_refuses_before_transport(anti,monkeypatch,tmp_path,destination):
    path=profile(tmp_path)
    args=settings(currency_budget='1',pricing_file=str(path),base_url=destination)
    calls=open_sequence(monkeypatch,anti,[])
    with pytest.raises(anti.SpendAdmissionError,match='gateway.*pricing scope'):generate(anti,args)
    snapshot=args._run_control.spend_control.snapshot()
    assert calls==[] and snapshot['committed']['calls']==snapshot['reserved']['calls']==0
    assert snapshot['currency']['gateway']=='http://127.0.0.1:51122/v1'


def test_normalized_scope_matches_and_is_rechecked_after_gateway_change(anti,monkeypatch,tmp_path):
    path=profile(tmp_path,gateway='HTTPS://EXAMPLE.INVALID:443/v1/')
    args=settings(currency_budget='1',pricing_file=str(path),base_url='https://example.invalid/v1')
    calls=open_sequence(monkeypatch,anti,[(200,response())])
    generate(anti,args)
    assert args._run_control.spend_control.snapshot()['currency']['gateway']=='https://example.invalid/v1'
    args.base_url='https://different.invalid/v1'
    with pytest.raises(anti.SpendAdmissionError,match='gateway'):generate(anti,args)
    assert len(calls)==1


@pytest.mark.parametrize('kind,expected', [('expired','expired'),('fees','all fees'),('missing','not found'),('date','YYYY-MM-DD'),('json','valid UTF-8 JSON')])
def test_cli_pricing_refusal_gives_safe_actionable_reason(anti,monkeypatch,tmp_path,capsys,kind,expected):
    path=profile(tmp_path)
    data=json.loads(path.read_text())
    if kind=='expired':data['valid_until']='2000-01-01'
    if kind=='fees':data['models']['fixture:model']['includes_reasoning_and_all_fees']=False
    if kind=='date':data['as_of']='private-fixture-content'
    path.write_text(json.dumps(data))
    if kind=='missing':path.unlink()
    if kind=='json':path.write_text('private-fixture-content:invalid-json')
    forbid_open(monkeypatch,anti)
    assert anti.main(['consult','--model','fixture:model','--prompt','fixture','--currency-budget','1',
                      '--pricing-file',str(path),'--no-progress'])==1
    error=capsys.readouterr().err
    assert expected in error and 'private-fixture-content' not in error and str(path) not in error


def test_currency_gateway_scope_does_not_follow_redirects(anti,monkeypatch,tmp_path):
    from fake_upstream import upstream
    from http.server import BaseHTTPRequestHandler
    redirected=[]
    def get(handler):
        redirected.append(handler.path);handler.send_response(200);handler.end_headers()
    monkeypatch.setattr(BaseHTTPRequestHandler,'do_GET',get,raising=False)
    with upstream() as (target,target_requests):
        with upstream((302,{'Location':target+'/responses'},b'{}')) as (source,source_requests):
            path=profile(tmp_path,gateway=source)
            args=settings(currency_budget='1',pricing_file=str(path),base_url=source)
            with pytest.raises(anti.AntiError,match='HTTP 302'):generate(anti,args)
    assert len(source_requests)==1 and target_requests==[] and redirected==[]
    assert args._run_control.spend_control.snapshot()['committed']['calls']==1


def test_pricing_opener_preparation_cannot_move_dispatch_past_deadline(anti,monkeypatch,tmp_path):
    args=settings(currency_budget='1',pricing_file=str(profile(tmp_path)))
    control=anti.run_control(args);clock=[0.0];control.clock=lambda:clock[0];control.deadline=1
    def prepare(*handlers):
        clock[0]=2
        return argparse.Namespace(open=lambda *a,**k:pytest.fail('no late POST'))
    monkeypatch.setattr(anti.urllib.request,'build_opener',prepare)
    with pytest.raises(anti.RunDeadlineExceeded) as caught:generate(anti,args)
    assert caught.value.submitted is False
    assert control.snapshot()['attempts_started']==0
    assert control.spend_control.snapshot()['reserved']['calls']==0


def test_spend_lifecycle_projection_omits_attempt_and_quote_content(anti):
    projected=anti.lifecycle_metadata({'admission_controls':{
        'enabled':True,'billing_guarantee':False,'token_limit_guarantee':False,
        'refused_attempts':1,'attempts_omitted':False,
        'committed':{'calls':1,'input_tokens':True,'output_tokens':6},
        'currency':{'currency':'USD','sha256':'a'*64,'gateway':'private-fixture',
                    'source':'private-fixture','provider_price_verified':False,
                    'basis':'user_declared_complete_attempt_ceiling'},
        'attempts':[{'model':'private-fixture'}],
    }})['admission_controls']
    assert projected['committed']=={'calls':1,'output_tokens':6}
    assert projected['attemptsRetained'] is False and 'attempts' not in projected
    assert 'attempts_omitted' not in projected
    assert 'private-fixture' not in json.dumps(projected)
