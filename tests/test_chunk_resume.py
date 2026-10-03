"""Immutable checkpoints use synthetic runs and owned temporary files only."""
import argparse
from copy import deepcopy
import json
from pathlib import Path

import pytest

from codex_antigravity_auth.skills.anti.scripts.anti_lib import checkpoints as cp
from codex_antigravity_auth.skills.anti.scripts.anti_lib.run_records import publish_unlocked

RECIPE='a'*64
CHUNKS=['b'*64,'c'*64,'d'*64]
ROUTE='e'*64


def write_record(root, run_id, *, status='running', checkpoint=None, mode='full', writer='1'*32):
    args=argparse.Namespace(run_id=run_id,command='review',_anti_writer_id=writer)
    metadata={'scope_status':'partial','run_control':{'scope':'process_local','attempts_started':3,'permits_acquired':3,'permits_released':3}}
    if checkpoint:metadata['checkpoint']=checkpoint.reference
    publish_unlocked(args,runs_dir=root,output_mode=mode,timestamp='2026-10-01T00:00:00Z',helper=None,
        output_preview_chars=100,verification_required_checks=[],mode='review',status=status,metadata=metadata)
    return args


def create(root, run_id='source', **kwargs):
    return cp.Checkpoint(root,run_id,'1'*32,recipe=kwargs.pop('recipe',RECIPE),chunks=CHUNKS,routes=[ROUTE],**kwargs)


def complete(checkpoint,index):
    checkpoint.record(index,status='success',output=f'Completed fixture chunk {index}.',model='fixture:model',
        generation={'actual_model':'fixture:model','usage':{'input_tokens':5,'output_tokens':10}},route=ROUTE)


def source_run(root):
    write_record(root,'source')
    source=create(root)
    complete(source,1);complete(source,2)
    source.record(3,status='failed',generation={'submitted':True})
    write_record(root,'source',status='error',checkpoint=source)
    return source


def test_resume_preserves_source_and_reuses_only_completed_chunks(tmp_path):
    source_run(tmp_path)
    before=(tmp_path/'source.json').read_bytes()
    originals={str(p):p.read_bytes() for p in (tmp_path/'source').rglob('*.json')}
    write_record(tmp_path,'target')
    target=create(tmp_path,'target',resume='source',rerun=[3])
    assert target.take(1)['output']=='Completed fixture chunk 1.'
    assert target.take(2)['status']=='success' and target.take(3) is None
    complete(target,3)
    assert target.reference['sourceRun']=='source'
    assert target.prior_runs['source']['run_control']['attempts_started']==3
    assert (tmp_path/'source.json').read_bytes()==before
    assert all(Path(path).read_bytes()==raw for path,raw in originals.items())
    assert len(target.events)==4 and len(target.refs)==4


def test_failed_chunk_requires_explicit_rerun_selection(tmp_path):
    source_run(tmp_path);write_record(tmp_path,'target')
    with pytest.raises(cp.CheckpointError,match='--rerun-chunk 3'):
        create(tmp_path,'target',resume='source')
    assert not (tmp_path/'target/checkpoints').exists()


def test_changed_recipe_never_reuses_prior_outputs(tmp_path):
    source_run(tmp_path);write_record(tmp_path,'target')
    with pytest.raises(cp.CheckpointError,match='changed'):
        create(tmp_path,'target',resume='source',rerun=[3],recipe='f'*64)


@pytest.mark.parametrize('mode',['never','summary'])
def test_retention_without_full_output_cannot_checkpoint(tmp_path,mode):
    write_record(tmp_path,'source',mode=mode)
    with pytest.raises(cp.CheckpointError,match='full-retention'):
        create(tmp_path)


def test_source_must_be_terminal_and_target_must_have_its_own_identity(tmp_path):
    write_record(tmp_path,'source');create(tmp_path)
    write_record(tmp_path,'target')
    with pytest.raises(cp.CheckpointError,match='terminal'):
        create(tmp_path,'target',resume='source')
    with pytest.raises(cp.CheckpointError,match='new run id'):
        create(tmp_path,'source',resume='source')


def test_modified_or_missing_completed_blob_refuses_all_reuse(tmp_path):
    source=source_run(tmp_path);write_record(tmp_path,'target')
    path=tmp_path/'source'/source.refs[0]['path']
    path.write_text('{"changed":true}')
    with pytest.raises(cp.CheckpointError,match='bytes changed'):
        create(tmp_path,'target',resume='source',rerun=[3])
    path.unlink()
    with pytest.raises(cp.CheckpointError,match='missing or invalid'):
        create(tmp_path,'target',resume='source',rerun=[3])


def test_redacted_output_and_unknown_actual_route_are_not_reusable(tmp_path):
    write_record(tmp_path,'source');source=create(tmp_path)
    source.record(1,status='success',output='password=fixture-sensitive-value',model='fixture:model',route=ROUTE)
    source.record(2,status='success',output='Safe fixture output.',model='fixture:model')
    assert not source.latest[1]['reusable'] and not source.latest[2]['reusable']
    assert 'fixture-sensitive-value' not in (tmp_path/'source'/source.refs[0]['path']).read_text()
    write_record(tmp_path,'source',status='partial',checkpoint=source);write_record(tmp_path,'target')
    with pytest.raises(cp.CheckpointError,match='--rerun-chunk 1'):
        create(tmp_path,'target',resume='source')


def test_writer_loss_stops_checkpoint_publication(tmp_path):
    write_record(tmp_path,'source');source=create(tmp_path)
    path=tmp_path/'source.json';record=json.loads(path.read_text());record['writerId']='2'*32;path.write_text(json.dumps(record))
    before=path.read_bytes()
    with pytest.raises(cp.CheckpointError,match='writer'):
        complete(source,1)
    assert path.read_bytes()==before


def test_invalid_source_id_is_rejected_before_lock_creation(tmp_path):
    write_record(tmp_path,'target')
    with pytest.raises(cp.CheckpointError,match='Invalid checkpoint run id'):
        create(tmp_path,'target',resume='../outside')
    assert not (tmp_path.parent/'.outside.json.lock').exists()


def test_checkpoints_store_prompt_hashes_without_raw_prompt(tmp_path):
    write_record(tmp_path,'source');source=create(tmp_path);complete(source,1)
    manifest=cp.read_ref(tmp_path/'source',source.reference['manifest'])
    assert manifest['chunks']==CHUNKS
    assert 'prompt' not in source.events[0] and source.events[0]['promptSha256']==CHUNKS[0]


def test_failed_atomic_index_update_leaves_the_old_checkpoint_readable(tmp_path,monkeypatch):
    write_record(tmp_path,'source');source=create(tmp_path)
    before=(tmp_path/'source.json').read_bytes();old=deepcopy(source.reference)
    def fail(*args):raise OSError('synthetic publication failure')
    monkeypatch.setattr(cp,'atomic_write_json',fail)
    with pytest.raises(OSError,match='synthetic'):
        complete(source,1)
    assert (tmp_path/'source.json').read_bytes()==before and source.reference==old
    assert cp.read_ref(tmp_path/'source',old['manifest'])['events']==[]


def test_cleanup_tombstone_prevents_new_checkpoint_writes(tmp_path):
    from codex_antigravity_auth.skills.anti.scripts.anti_lib.persistence import PersistenceError
    write_record(tmp_path,'source');source=create(tmp_path)
    before=(tmp_path/'source.json').read_bytes()
    (tmp_path/'.deleted').mkdir()
    (tmp_path/'.deleted/source.json').write_text('{}')
    with pytest.raises(PersistenceError,match='cleanup'):
        complete(source,1)
    assert (tmp_path/'source.json').read_bytes()==before


def test_symlinked_checkpoint_blob_never_reads_outside_artifacts(tmp_path):
    source=source_run(tmp_path);write_record(tmp_path,'target')
    path=tmp_path/'source'/source.refs[0]['path']
    path.unlink()
    outside=tmp_path/'outside.json';outside.write_text('{"private":"fixture"}')
    try:path.symlink_to(outside)
    except OSError:pytest.skip('symlinks unavailable')
    with pytest.raises(cp.CheckpointError,match='symlink'):
        create(tmp_path,'target',resume='source',rerun=[3])
    assert outside.read_text()=='{"private":"fixture"}'


@pytest.fixture
def anti_fixture(monkeypatch,tmp_path):
    import importlib.util
    import subprocess
    import codex_antigravity_auth
    from codex_antigravity_auth import server
    repo=tmp_path/'repo';repo.mkdir()
    (repo/'fixture.py').write_text('fixture_value = 1\n'*220)
    subprocess.run(['git','init',str(repo)],check=True,capture_output=True)
    subprocess.run(['git','add','fixture.py'],cwd=repo,check=True,capture_output=True)
    subprocess.run(['git','-c','user.name=Fixture','-c','user.email=fixture@example.invalid',
                    'commit','-m','synthetic source'],cwd=repo,check=True,capture_output=True)
    monkeypatch.chdir(repo)
    script=Path(codex_antigravity_auth.__file__).parent/'skills/anti/scripts/anti.py'
    spec=importlib.util.spec_from_file_location('anti_resume_fixture',script)
    anti=importlib.util.module_from_spec(spec);spec.loader.exec_module(anti)
    monkeypatch.setattr(anti,'RUNS_DIR',tmp_path/'runs')
    monkeypatch.setattr(anti,'ensure_helper_parity',lambda args:None)
    monkeypatch.setattr(anti,'_install_run_signal_handlers',lambda args:None)
    monkeypatch.setattr(anti,'helper_identity',lambda:{'treeHash':'f'*64,'parityStatus':'match'})
    monkeypatch.setenv('ANTIGRAVITY_LOCAL_ONLY','0')
    monkeypatch.setattr(server.app.state,'local_only_mode',None,raising=False)
    monkeypatch.setattr(server,'write_request_record',lambda record:None)
    providers={}
    monkeypatch.setattr(server,'all_provider_configs',lambda:providers)
    monkeypatch.setattr(server,'all_provider_configs_read_only',lambda:providers)
    return anti,repo,providers


def bridge_gateway(monkeypatch,anti):
    import io
    from urllib.parse import urlsplit
    from fastapi.testclient import TestClient
    from codex_antigravity_auth import server
    client=TestClient(server.app)
    def opened(request,*,timeout,payload=None,body=None):
        path=urlsplit(request.full_url).path
        assert path in {'/v1/models','/v1/responses','/v1/local/responses'}
        anti.transport_entry_timeout(request.get_method(),timeout,payload=payload,body=body,url=request.full_url)
        response=client.request(request.get_method(),path,content=body,headers={'Content-Type':'application/json'})
        wire=io.BytesIO(response.content);wire.status=response.status_code;wire.headers=response.headers
        return wire
    monkeypatch.setattr(anti,'open_gateway_request',opened)


def provider_fixture(base):
    from codex_antigravity_auth import byok
    raw=byok.normalize_provider_config({'providers':{'fixture':{'baseUrl':base+'/v1','apiKey':'synthetic-key',
                                                            'models':['resume-model']}}})['providers']['fixture']
    return byok.merged_provider_config('fixture',raw)


def response_fixture():
    body={'choices':[{'index':0,'message':{'role':'assistant','content':
        'Completed fixture review with concrete observations and no blockers.'},'finish_reason':'stop'}],
          'usage':{'prompt_tokens':10,'completion_tokens':10,'total_tokens':20}}
    return (200,{'Content-Type':'application/json'},json.dumps(body).encode())


def review_argv(run_id,*extra):
    return ['review','--scope','files','--file','fixture.py','--model','fixture:resume-model',
            '--checkpoint-chunks','--save-output','full','--run-id',run_id,'--retry','0','--fallback-policy','never',
            '--max-prompt-chars','1600','--max-review-chunks','100','--chunk-output-tokens','128',
            '--no-progress','--json',*extra]


from contextlib import contextmanager


@contextmanager
def failing_third_upstream():
    # A resume plans its remaining calls from the first run's saved manifest.
    # Keep one owned endpoint while selecting the third response dynamically.
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from fake_upstream import allow_listener, remove_listener
    seen=[]
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            seen.append({'body':json.loads(self.rfile.read(int(self.headers['Content-Length'])))})
            status,headers,body=(503,{'Content-Type':'application/json'},b'{"error":"synthetic busy"}') if len(seen)==3 else response_fixture()
            self.send_response(status)
            for key,value in headers.items():self.send_header(key,value)
            self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
        def log_message(self,*args):pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler,bind_and_activate=False)
    endpoint=allow_listener(server.socket);server.server_address=endpoint;server.server_activate()
    thread=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':0.01},daemon=True);thread.start()
    try:yield f'http://{endpoint[0]}:{endpoint[1]}',seen
    finally:
        server.shutdown();server.server_close();thread.join(timeout=2);remove_listener(endpoint)


def test_failed_third_chunk_resumes_only_missing_calls_and_keeps_lineage_accounting(anti_fixture,monkeypatch,capsys):
    anti,repo,providers=anti_fixture
    bridge_gateway(monkeypatch,anti)
    with failing_third_upstream() as (base,seen):
        providers['fixture']=provider_fixture(base)
        assert anti.main(review_argv('first'))==1
        initial_output=capsys.readouterr()
        source=anti.RUNS_DIR/'first.json';before=source.read_bytes()
        record=json.loads(before)
        assert 'checkpoint' in record['metadata'],initial_output.err
        chunk_count=len(record['metadata']['checkpoint']['chunks'])
        assert chunk_count>=3 and len(seen)==3
        assert anti.main(review_argv('second','--resume-from','first','--rerun-chunk','3'))==0
        result=json.loads(capsys.readouterr().out)
        assert len(seen)==chunk_count+2
    metadata=result['metadata']
    assert metadata['checkpoint']['reusedChunks']==[1,2]
    assert metadata['resume_accounting']['submitted_attempts']==len(seen)
    assert metadata['run_control']['attempts_started']==chunk_count-1
    assert metadata['scope_status']=='complete'
    assert source.read_bytes()==before
    first_prompt=seen[0]['body']['messages']
    assert sum(row['body']['messages']==first_prompt for row in seen)==1
    assert all('fixture_value = 1' not in path.read_text() for path in (anti.RUNS_DIR/'first/checkpoints').rglob('*.json'))


@pytest.mark.parametrize('change',['source','helper','policy','catalog'])
def test_changed_resume_identity_fails_before_any_new_generation(anti_fixture,monkeypatch,capsys,change):
    from fake_upstream import upstream
    anti,repo,providers=anti_fixture
    bridge_gateway(monkeypatch,anti)
    policy=repo/'policy.json'
    policy_value={'schemaVersion':1,'destinations':[{'baseUrl':'http://127.0.0.1:51122/v1','model':'fixture:resume-model',
        'stages':['primary','summary','fallback']}],'forbiddenPaths':[],'maxScanChars':100000}
    policy.write_text(json.dumps(policy_value))
    extra=['--data-policy',str(policy)] if change=='policy' else []
    with upstream(response_fixture(),response_fixture(),(503,{'Content-Type':'application/json'},b'{"error":"synthetic busy"}')) as (base,seen):
        providers['fixture']=provider_fixture(base)
        assert anti.main(review_argv('first',*extra))==1
        capsys.readouterr();before=(anti.RUNS_DIR/'first.json').read_bytes()
        assert len(seen)==3
        if change=='source':(repo/'fixture.py').write_text('fixture_value = 2\n'*220)
        elif change=='helper':monkeypatch.setattr(anti,'helper_identity',lambda:{'treeHash':'0'*64,'parityStatus':'match'})
        elif change=='policy':
            policy_value['maxScanChars']=99999;policy.write_text(json.dumps(policy_value))
        else:providers['fixture']['baseUrl']='http://127.0.0.1:12345/v1'
        assert anti.main(review_argv('second','--resume-from','first','--rerun-chunk','3',*extra))==1
        assert 'changed' in capsys.readouterr().err
        assert len(seen)==3 and (anti.RUNS_DIR/'first.json').read_bytes()==before


def test_gateway_receipt_matches_catalog_and_changes_with_actual_configuration(anti_fixture,monkeypatch):
    from fastapi.testclient import TestClient
    from fake_upstream import upstream
    from codex_antigravity_auth import server
    anti,repo,providers=anti_fixture
    client=TestClient(server.app)
    with upstream(response_fixture(),response_fixture()) as (base,seen):
        providers['fixture']=provider_fixture(base)
        first=next(item for item in client.get('/v1/models').json()['data'] if item['id']=='fixture:resume-model')
        response=client.post('/v1/responses',json={'model':'fixture:resume-model','input':'fixture'})
        assert response.headers['X-Antigravity-Route-Identity']=='v1:'+first['capabilities']['routing_identity']['sha256']
        providers['fixture']['headers']={'X-Fixture-Deployment':'changed'}
        response=client.post('/v1/responses',json={'model':'fixture:resume-model','input':'fixture'})
        assert response.headers['X-Antigravity-Route-Identity']!='v1:'+first['capabilities']['routing_identity']['sha256']


def test_plan_resume_uses_only_pending_chunks_and_fresh_synthesis(anti_fixture,monkeypatch,capsys):
    anti,repo,providers=anti_fixture
    bridge_gateway(monkeypatch,anti)
    common=['plan','--scope','none','--model','fixture:resume-model','--prompt','fixture planning goal '*250,
            '--checkpoint-chunks','--save-output','full','--max-prompt-chars','1600','--max-plan-chunks','100',
            '--chunk-output-tokens','128','--retry','0','--fallback-policy','never','--no-progress','--json']
    with failing_third_upstream() as (base,seen):
        providers['fixture']=provider_fixture(base)
        assert anti.main([*common,'--run-id','first'])==1
        capsys.readouterr();before=(anti.RUNS_DIR/'first.json').read_bytes()
        count=len(json.loads(before)['metadata']['checkpoint']['chunks'])
        assert len(seen)==3 and count>=3
        assert anti.main([*common,'--run-id','second','--resume-from','first','--rerun-chunk','3'])==0
        result=json.loads(capsys.readouterr().out)
        assert len(seen)==count+2
    assert result['metadata']['checkpoint']['reusedChunks']==[1,2]
    assert result['metadata']['resume_accounting']['submitted_attempts']==len(seen)
    assert (anti.RUNS_DIR/'first.json').read_bytes()==before


def test_resume_cannot_turn_omitted_scope_into_complete_scope(anti_fixture,monkeypatch,capsys):
    from fake_upstream import upstream
    anti,repo,providers=anti_fixture
    bridge_gateway(monkeypatch,anti)
    with upstream(response_fixture(),response_fixture(),response_fixture()) as (base,seen):
        providers['fixture']=provider_fixture(base)
        assert anti.main(review_argv('first','--max-review-chunks','1','--allow-partial'))==1
        capsys.readouterr()
        assert json.loads((anti.RUNS_DIR/'first.json').read_text())['scopeStatus']=='partial'
        assert anti.main(review_argv('second','--max-review-chunks','1','--allow-partial','--resume-from','first'))==1
        result=json.loads(capsys.readouterr().out)
        assert len(seen)==3 and result['metadata']['checkpoint']['reusedChunks']==[1]
    assert json.loads((anti.RUNS_DIR/'second.json').read_text())['scopeStatus']=='partial'


@pytest.mark.parametrize('options',[
    ['--checkpoint-chunks'],
    ['--checkpoint-chunks','--save-output','summary'],
    ['--checkpoint-chunks','--save-output','full','--chunked','off'],
    ['--checkpoint-chunks','--save-output','full','--dry-run'],
    ['--rerun-chunk','2'],
    ['--resume-from','../outside','--save-output','full'],
])
def test_incompatible_checkpoint_cli_flags_refuse_before_network(anti_fixture,monkeypatch,options):
    anti,repo,providers=anti_fixture
    monkeypatch.setattr(anti,'fetch_model_ids',lambda *a,**k:pytest.fail('no catalog lookup'))
    assert anti.main(['review','--scope','files','--file','fixture.py',*options])==1


def test_checkpoint_can_allocate_a_new_run_id(anti_fixture):
    anti,repo,providers=anti_fixture
    args=anti.build_parser().parse_args(['review','--checkpoint-chunks','--save-output','full'])
    anti.configure_checkpoint_args(args)
    assert args.chunked=='always' and args.run_id is None


def test_checkpoint_io_defers_signals_until_publication_is_settled(anti_fixture,monkeypatch):
    anti,repo,providers=anti_fixture
    events=[]
    args=argparse.Namespace()
    monkeypatch.setattr(anti,'_handle_run_signal',lambda *values:events.append('signal'))
    def publish():
        assert anti._RECORD_WRITES.depth>0
        anti._RECORD_WRITES.pending_signal=(args,15)
        events.append('published')
        return 'reference'
    assert anti.checkpoint_io(publish)=='reference'
    assert events==['published','signal'] and anti._RECORD_WRITES.depth==0


def test_post_replace_failure_does_not_regress_the_checkpoint_reference(tmp_path,monkeypatch):
    write_record(tmp_path,'source');source=create(tmp_path)
    original=cp.atomic_write_json
    def replaced(path,value):
        original(path,value)
        raise OSError('synthetic directory sync failure')
    monkeypatch.setattr(cp,'atomic_write_json',replaced)
    with pytest.raises(OSError,match='sync failure'):complete(source,1)
    record=json.loads((tmp_path/'source.json').read_text())
    assert source.reference==record['metadata']['checkpoint']
    assert cp.read_ref(tmp_path/'source',source.reference['manifest'])['events']


def test_checkpoint_filters_prompt_echoes_from_generation_diagnostics(tmp_path):
    write_record(tmp_path,'source');source=create(tmp_path)
    source.record(1,status='failed',generation={'primaryError':'RAW_FIXTURE_PROMPT_ECHO',
                                             'generation_failures':[{'error':'RAW_FIXTURE_PROMPT_ECHO'}], 'submitted':True})
    assert 'RAW_FIXTURE_PROMPT_ECHO' not in (tmp_path/'source'/source.refs[0]['path']).read_text()


def test_foreign_body_cannot_forge_gateway_route_receipts(anti_fixture,monkeypatch):
    import io
    anti,repo,providers=anti_fixture
    def opened(*a,**k):
        response=io.BytesIO(json.dumps({'_gateway_routing_identity':'a'*64}).encode())
        response.status=200;response.headers={};return response
    monkeypatch.setattr(anti,'open_gateway_request',opened)
    status,value=anti.request_json('POST','http://127.0.0.1:51122/v1/responses',payload={})
    assert status==200 and '_gateway_routing_identity' not in value


def test_changed_actual_route_after_lookup_cannot_be_claimed_as_an_unsent_chunk(anti_fixture,monkeypatch,capsys):
    from fake_upstream import upstream
    anti,repo,providers=anti_fixture
    bridge_gateway(monkeypatch,anti)
    original=anti.open_gateway_request
    def change_after_lookup(request,**kwargs):
        if request.get_method()=='POST':
            providers['fixture']['headers']={'X-Fixture-Deployment':'changed-after-lookup'}
        return original(request,**kwargs)
    monkeypatch.setattr(anti,'open_gateway_request',change_after_lookup)
    with upstream(response_fixture()) as (base,seen):
        providers['fixture']=provider_fixture(base)
        assert anti.main(review_argv('first'))==1
        assert len(seen)==1 and 'Actual generation route' in capsys.readouterr().err
    record=json.loads((anti.RUNS_DIR/'first.json').read_text())
    assert record['metadata']['run_control']['attempts_started']==1
    assert record['metadata']['chunk_generation'][0]['submitted'] is True
    assert record['metadata']['chunk_generation'][0]['status']=='failed'
    assert record['metadata']['checkpoint']['chunks'][0]['reusable'] is False


def test_google_preparation_receipt_matches_its_declared_backend_without_network(anti_fixture):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from codex_antigravity_auth.google_transport import GoogleTransport, AccountLease
    from codex_antigravity_auth.request_budget import RequestBudget
    from codex_antigravity_auth.route_identity import identity
    request=SimpleNamespace(scope={'state':{}})
    budget=RequestBudget(request,timeout=10,release_account=AsyncMock())
    model='gemini-3.8-flash'
    with budget.active():
        payload=GoogleTransport(timeout=10).build_request({'model':model,'input':'fixture'},
            AccountLease(email='fixture@example.invalid',project_id='fixture',access_token='synthetic-only'))
    assert request.scope['state']['routing_identity']==identity('antigravity',model)
    assert payload['model']=='gemini-3.8-flash-tiered'


def test_native_api_receipt_matches_catalog_for_the_actual_auth_endpoint(anti_fixture,monkeypatch):
    from fastapi.testclient import TestClient
    from fake_upstream import upstream
    from codex_antigravity_auth import server
    from codex_antigravity_auth.unified import OpenAIAuth
    anti,repo,providers=anti_fixture
    monkeypatch.setenv('ANTIGRAVITY_UNIFIED_MODEL_PICKER','1')
    body={'id':'resp_fixture','object':'response','status':'completed','model':'gpt-5.6',
          'output':[{'id':'msg_fixture','type':'message','role':'assistant','status':'completed',
                     'content':[{'type':'output_text','text':'Synthetic answer.','annotations':[]}]}]}
    with upstream((200,{'Content-Type':'application/json'},json.dumps(body).encode())) as (base,seen):
        auth=OpenAIAuth(kind='api_key',base_url=base+'/v1',api_key='synthetic-only')
        monkeypatch.setattr(server,'resolve_openai_auth',lambda:auth)
        client=TestClient(server.app)
        entry=next(row for row in client.get('/v1/models').json()['data'] if row['id']=='gpt-5.6')
        response=client.post('/v1/responses',json={'model':'gpt-5.6','input':'fixture'})
        assert response.status_code==200 and len(seen)==1
        assert response.headers['X-Antigravity-Route-Identity']=='v1:'+entry['capabilities']['routing_identity']['sha256']


def test_combined_accounting_retains_prior_and_current_currency_ceilings(anti_fixture):
    from types import SimpleNamespace
    anti,repo,providers=anti_fixture
    write_record(anti.RUNS_DIR,'target')
    session=anti.chunk_checkpoints.Checkpoint(anti.RUNS_DIR,'target','1'*32,recipe=RECIPE,chunks=CHUNKS,routes=[ROUTE])
    def receipt(calls,ceiling):
        return {'run_control':{'scope':'process_local','attempts_started':calls},
                'admission_controls':{'assumption_exceeded':False,'currency':{'currency':'USD','basis':'user_declared_complete_attempt_ceiling',
                                                  'provider_price_verified':False},
                    'currency_committed_ceiling':ceiling,'committed':{'calls':calls}}}
    session.prior_runs={'prior':session.accounting(receipt(3,'0.30'))}
    control=anti.RunControl(10,caps={})
    with control.attempt('http://127.0.0.1:51122/v1','fixture'):
        control.mark_submitted()
    control.spend_control=SimpleNamespace(snapshot=lambda:receipt(1,'0.10')['admission_controls'])
    args=argparse.Namespace(run_id='target',_chunk_checkpoint=session,_run_control=control)
    report=anti.checkpoint_metadata(args)['resume_accounting']
    assert report['submitted_attempts']==4
    assert report['currency_committed_ceilings']=={'USD':'0.40'}
    assert report['actual_billing_known'] is False and report['unknown_currency_runs']==[]
    assert report['scope']=='lineage_reporting_new_invocation_allowances'


def test_changed_source_commit_refuses_reuse_even_when_selected_bytes_match(anti_fixture,monkeypatch,capsys):
    import subprocess
    from fake_upstream import upstream
    anti,repo,providers=anti_fixture
    bridge_gateway(monkeypatch,anti)
    with upstream(response_fixture(),response_fixture(),(503,{'Content-Type':'application/json'},b'{"error":"synthetic busy"}')) as (base,seen):
        providers['fixture']=provider_fixture(base)
        assert anti.main(review_argv('first'))==1
        capsys.readouterr()
        subprocess.run(['git','-c','user.name=Fixture','-c','user.email=fixture@example.invalid',
                        'commit','--allow-empty','-m','changed source revision'],cwd=repo,check=True,capture_output=True)
        assert anti.main(review_argv('second','--resume-from','first','--rerun-chunk','3'))==1
        assert 'changed' in capsys.readouterr().err and len(seen)==3


def test_another_advertised_model_is_not_equivalent_to_the_checkpoint(anti_fixture,monkeypatch,capsys):
    from fake_upstream import upstream
    anti,repo,providers=anti_fixture
    bridge_gateway(monkeypatch,anti)
    with upstream(response_fixture(),response_fixture(),(503,{'Content-Type':'application/json'},b'{"error":"synthetic busy"}')) as (base,seen):
        providers['fixture']=provider_fixture(base)
        providers['fixture']['models'].append('other-model')
        assert anti.main(review_argv('first'))==1
        capsys.readouterr()
        assert anti.main(review_argv('second','--resume-from','first','--rerun-chunk','3','--model','fixture:other-model'))==1
        assert 'changed' in capsys.readouterr().err and len(seen)==3


@pytest.mark.parametrize('prior_count,allowed',[(62,True),(63,False)])
def test_lineage_bound_counts_prior_source_and_current_run(tmp_path,prior_count,allowed):
    write_record(tmp_path,'source');source=create(tmp_path)
    source.prior_runs={f'prior_{index}':{} for index in range(prior_count)}
    source.publish()
    write_record(tmp_path,'source',status='error',checkpoint=source)
    write_record(tmp_path,'target')
    if allowed:
        target=create(tmp_path,'target',resume='source')
        assert len(target.prior_runs)+1==cp.MAX_LINEAGE_RUNS==64
    else:
        with pytest.raises(cp.CheckpointError,match='lineage'):
            create(tmp_path,'target',resume='source')
        assert not (tmp_path/'target/checkpoints').exists()
