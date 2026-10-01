"""Synthetic images and owned fake endpoints; no visual quality or live acceptance claim."""
import argparse
import base64
import binascii
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import struct
from urllib.parse import urlsplit
import zlib

import pytest
from fastapi.testclient import TestClient

import codex_antigravity_auth
from codex_antigravity_auth import byok, server
from codex_antigravity_auth.skills.anti.scripts.anti_lib import media
from codex_antigravity_auth.skills.anti.scripts.anti_lib.data_policy import DataPolicy, PolicyError, content_digest, audit_projection
from codex_antigravity_auth.skills.anti.scripts.anti_lib.run_records import publish_unlocked
from fake_upstream import upstream


def png():
    def chunk(kind, data):
        return struct.pack('!I',len(data))+kind+data+struct.pack('!I',binascii.crc32(kind+data)&0xffffffff)
    return b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('!IIBBBBB',1,1,8,2,0,0,0))+chunk(b'IDAT',zlib.compress(b'\0\xff\0\0'))+chunk(b'IEND',b'')


@pytest.fixture
def image_path(tmp_path):
    path=tmp_path/'synthetic.png';path.write_bytes(png());return path


@pytest.fixture
def anti(monkeypatch,tmp_path):
    script=Path(codex_antigravity_auth.__file__).parent/'skills/anti/scripts/anti.py'
    spec=importlib.util.spec_from_file_location('anti_images_fixture',script)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(module,'RUNS_DIR',tmp_path/'runs')
    monkeypatch.setattr(module,'ensure_helper_parity',lambda args:None)
    monkeypatch.setattr(module,'_install_run_signal_handlers',lambda args:None)
    monkeypatch.setattr(module,'find_repo_root',lambda _:tmp_path)
    monkeypatch.setenv('ANTIGRAVITY_LOCAL_ONLY','0')
    monkeypatch.setattr(server.app.state,'local_only_mode',None,raising=False)
    monkeypatch.setattr(server,'write_request_record',lambda record:None)
    values={}
    monkeypatch.setattr(server,'all_provider_configs',lambda:values)
    monkeypatch.setattr(server,'all_provider_configs_read_only',lambda:values)
    return module,values


def provider(base, *, images=True, models=('vision','judge','fallback')):
    raw=byok.normalize_provider_config({'providers':{'fixture':{'baseUrl':base+'/v1','apiKey':'synthetic-only',
        'capabilities':{'input_modalities':['text','image'] if images else ['text'],'image_forms':['data_url']},
        'models':list(models)}}})['providers']['fixture']
    return byok.merged_provider_config('fixture',raw)


def bridge(monkeypatch,anti):
    client=TestClient(server.app);seen=[]
    def opened(request,*,timeout,payload=None,body=None):
        path=urlsplit(request.full_url).path
        assert path in {'/v1/models','/v1/responses','/v1/local/responses'}
        anti.transport_entry_timeout(request.get_method(),timeout,payload=payload,body=body,url=request.full_url)
        seen.append((request.get_method(),path))
        result=client.request(request.get_method(),path,content=body,headers={'Content-Type':'application/json'})
        wire=io.BytesIO(result.content);wire.status=result.status_code;wire.headers=result.headers;return wire
    monkeypatch.setattr(anti,'open_gateway_request',opened)
    return seen


def response(text='Complete synthetic image analysis; no real visual acceptance is asserted.'):
    return (200,{'Content-Type':'application/json'},json.dumps({'choices':[{'index':0,'message':{'role':'assistant','content':text},'finish_reason':'stop'}]}).encode())


def argv(path,*extra):
    return ['consult','--no-pre-read','--prompt','Describe the supplied fixture.','--model','fixture:vision','--image',str(path),'--json','--no-progress',*extra]


def test_capture_keeps_original_bytes_after_source_changes(image_path):
    captured=media.capture([str(image_path)])
    image_path.write_bytes(b'changed after capture')
    assert base64.b64decode(captured.images[0].data_url.partition(',')[2])==png()
    report=captured.report()
    assert report['images']==[{'index':1,'mime':'image/png','bytes':len(png()),'sha256':hashlib.sha256(png()).hexdigest()}]
    assert report['status']=='not_sent' and report['format_validation']=='signature_only'
    assert str(image_path) not in json.dumps(report) and 'base64' not in json.dumps(report)


@pytest.mark.parametrize('value',[b'',b'plain text',b'ID3audio',b'\0\0\0\x18ftypmp42'])
def test_non_images_are_not_reinterpreted_as_text(tmp_path,value):
    path=tmp_path/'input.bin';path.write_bytes(value)
    with pytest.raises(media.MediaError):media.capture([str(path)])


@pytest.mark.parametrize('value',['https://example.invalid/x.png','data:image/png;base64,AAAA','bad\nname'])
def test_remote_and_inline_attachments_refused(value):
    with pytest.raises(media.MediaError):media.capture([value])


def test_limits_missing_directories_and_symlinks_refuse(tmp_path,image_path):
    for values in ([str(image_path)]*5,[str(tmp_path/'missing')],[str(tmp_path)]):
        with pytest.raises(media.MediaError):media.capture(values)
    link=tmp_path/'link.png';link.symlink_to(image_path)
    with pytest.raises(media.MediaError):media.capture([str(link)])
    huge=tmp_path/'large.png';huge.write_bytes(b'\x89PNG\r\n\x1a\n'+b'\0'*(2*1024*1024))
    with pytest.raises(media.MediaError):media.capture([str(huge)])
    huge.write_bytes(b'\x89PNG\r\n\x1a\n'+b'\0'*(2*1024*1024-8))
    assert len(media.capture([str(huge),str(huge)]).images)==2
    with pytest.raises(media.MediaError):media.capture([str(huge)]*3)


def policy(tmp_path,*,ack=(),forbidden=()):
    path=tmp_path/'policy.json';path.write_text(json.dumps({'schemaVersion':1,'destinations':[
        {'baseUrl':'http://127.0.0.1:51122/v1','model':'fixture:vision','stages':['primary','summary','judge','fallback']}],
        'forbiddenPaths':list(forbidden),'maxScanChars':100000}))
    return DataPolicy(path,root=tmp_path,acknowledgements=ack)


def test_policy_paths_checked_before_capture(monkeypatch,tmp_path,image_path):
    monkeypatch.setattr(media,'read_file',lambda *a,**k:pytest.fail('no forbidden image read'))
    with pytest.raises(PolicyError,match='denied'):media.capture([str(image_path)],policy=policy(tmp_path,forbidden=['*.png']))


def test_text_ack_does_not_authorize_changed_images(tmp_path,image_path):
    prompt='sk-fixtureabcdefghijklmnopqrstuvwxyz'
    images=media.capture([str(image_path)]).identity()
    kwargs=dict(prompt=prompt,base_url='http://127.0.0.1:51122/v1',model='fixture:vision',stage='primary',media=images)
    with pytest.raises(PolicyError,match='acknowledge'):policy(tmp_path,ack=[content_digest(prompt)]).check(**kwargs)
    session=policy(tmp_path,ack=[content_digest(prompt,images)]);session.check(**kwargs)
    audit=session.audit();assert audit_projection(audit)==audit and audit['schemaVersion']==2
    assert audit['decisions'][0]['unscannedMediaCount']==1
    images[0]['sha256']='f'*64
    with pytest.raises(PolicyError,match='acknowledge'):session.check(**kwargs)


@pytest.mark.parametrize('mode',['never','summary','full'])
def test_retention_separates_media_from_code_scope_and_omits_bytes(tmp_path,image_path,mode):
    captured=media.capture([str(image_path)]);captured.submitted('fixture:vision','primary')
    metadata={'huge':'x'*50000,'media_coverage':captured.report()}
    args=argparse.Namespace(run_id='media',command='consult',_anti_writer_id='1'*32)
    publish_unlocked(args,runs_dir=tmp_path/'runs',output_mode=mode,timestamp='2026-10-01T00:00:00Z',helper=None,
        output_preview_chars=100,verification_required_checks=[],mode='consult',status='success',metadata=metadata,output_text='Fixture answer.')
    record=json.loads((tmp_path/'runs/media.json').read_text())
    receipt=record['metadata']['media_coverage']
    assert receipt['captured_count']==1 and receipt['gateway_attempts']==1
    if mode=='never':assert 'images' not in receipt and receipt['image_hashes_retained'] is False
    else:assert receipt['images']==captured.identity()
    for path in (tmp_path/'runs').rglob('*.json'):
        raw=path.read_text();assert str(image_path) not in raw and captured.images[0].data_url not in raw
    assert mode=='never' or record['scopeStatus']=='complete'  # independent of unverified image acceptance


def test_consult_reaches_fake_chat_with_exact_image_bytes(anti,image_path,monkeypatch,capsys):
    module,providers=anti;bridge(monkeypatch,module)
    with upstream(response()) as (base,seen):
        providers['fixture']=provider(base)
        assert module.main(argv(image_path))==0
    parts=seen[0]['body']['messages'][-1]['content']
    assert any(part.get('type')=='text' for part in parts)
    image=next(part for part in parts if part['type']=='image_url')['image_url']['url']
    assert base64.b64decode(image.partition(',')[2])==png()
    result=json.loads(capsys.readouterr().out)
    assert result['metadata']['media_coverage']['gateway_attempts']==1
    assert result['metadata']['context_preflight']['status']=='unknown'


@pytest.mark.parametrize('stage',['reviewer','judge','fallback'])
def test_panel_rejects_any_text_only_selected_stage_before_generation(anti,image_path,monkeypatch,capsys,stage):
    module,providers=anti;seen=bridge(monkeypatch,module)
    models=[{'id':name,'capabilities':{'input_modalities':['text'] if name==stage else ['text','image']}}
            for name in ('reviewer','judge','fallback')]
    providers['fixture']=provider('http://127.0.0.1:12345',models=models)
    args=['panel','--mode','ask','--model','fixture:reviewer','--judge','fixture:judge','--prompt','fixture',
          '--image',str(image_path),'--fallback-model','fixture:fallback','--fallback-policy','on-retryable','--no-progress']
    assert module.main(args)==1
    assert 'Image attachments require' in capsys.readouterr().err
    assert not any(method=='POST' for method,path in seen)


def test_panel_reviewers_and_judge_each_receive_same_images(anti,image_path,monkeypatch,capsys):
    module,providers=anti;bridge(monkeypatch,module)
    judgment=json.dumps({'summary':'Synthetic judgment.','findings':[],'disagreements':[],'unverifiable':[],
                         'recommended_next_actions':[],'caveats':[]})
    with upstream(response(),response(),response(judgment)) as (base,seen):
        providers['fixture']=provider(base)
        assert module.main(['panel','--mode','ask','--model','fixture:vision','--model','fixture:fallback','--judge','fixture:judge',
                            '--prompt','fixture','--image',str(image_path),'--no-verify','--no-progress','--json'])==0
    for request in seen:
        urls=[part['image_url']['url'] for message in request['body']['messages'] for part in message.get('content',[]) if isinstance(part,dict) and part.get('type')=='image_url']
        assert len(urls)==1 and base64.b64decode(urls[0].partition(',')[2])==png()
    result=json.loads(capsys.readouterr().out)
    assert result['metadata']['media_coverage']['gateway_attempts']==3
    assert {item['stage'] for item in result['metadata']['media_coverage']['attempts']}=={'primary','judge'}


def test_fallback_keeps_images_and_records_actual_attempt(anti,image_path,monkeypatch,capsys):
    module,providers=anti;bridge(monkeypatch,module)
    with upstream((503,{'Content-Type':'application/json'},b'{"error":"synthetic busy"}'),response()) as (base,seen):
        providers['fixture']=provider(base)
        assert module.main(argv(image_path,'--fallback-model','fixture:fallback','--fallback-policy','on-retryable','--retry','0'))==0
    result=json.loads(capsys.readouterr().out)
    assert [row['fallback'] for row in result['metadata']['media_coverage']['attempts']]==[False,True]
    assert len(seen)==2
    for request in seen:assert 'data:image/png;base64,'+base64.b64encode(png()).decode() in json.dumps(request['body'])


def test_dry_run_captures_without_catalog_or_generation(anti,image_path,monkeypatch,capsys):
    module,_=anti
    monkeypatch.setattr(module,'request_json',lambda *a,**k:pytest.fail('no network in preview'))
    assert module.main(argv(image_path,'--dry-run'))==0
    result,_=json.JSONDecoder().raw_decode(capsys.readouterr().out)
    assert result['media_coverage']['gateway_attempts']==0
    assert any('image costs' in value for value in result['unknowns'])


def test_missing_image_payload_fails_before_submission_or_spend(anti,image_path):
    module,_=anti
    args=module.build_parser().parse_args(argv(image_path,'--max-calls','1'))
    control=module.run_control(args)
    with control.bind():
        token=module._CALL_SUBMITTED.set(0)
        try:
            with pytest.raises(module.image_attachments.MediaError):
                module.transport_entry_timeout('POST',1,payload={'model':'fixture:vision','input':'text','max_output_tokens':10},body=b'{}')
        finally:module._CALL_SUBMITTED.reset(token)
    assert control.snapshot()['attempts_started']==0 and control.media.report()['gateway_attempts']==0
    assert control.spend_control.snapshot()['committed']['calls']==0


def test_google_translation_preserves_mime_and_exact_image_payload(image_path):
    from codex_antigravity_auth.transform import transform_request
    session=media.capture([str(image_path)])
    result=transform_request({'model':'gemini-3.8-flash','input':session.input('fixture')},project_id='synthetic-project')
    assert base64.b64encode(png()).decode() in json.dumps(result)
    assert 'image/png' in json.dumps(result)


def test_native_responses_preserves_original_data_url(anti,image_path,monkeypatch,capsys):
    from codex_antigravity_auth.unified import OpenAIAuth
    module,_=anti;bridge(monkeypatch,module)
    monkeypatch.setattr(server,'is_unified_mode_enabled',lambda:True)
    body={'id':'resp_fixture','status':'completed','model':'gpt-5.6','output':[{'type':'message','role':'assistant',
          'content':[{'type':'output_text','text':'Synthetic native image response.'}]}]}
    with upstream((200,{'Content-Type':'application/json'},json.dumps(body).encode())) as (base,seen):
        monkeypatch.setattr(server,'resolve_openai_auth',lambda:OpenAIAuth(kind='api_key',base_url=base,api_key='synthetic-only'))
        assert module.main(argv(image_path,'--model','gpt-5.6'))==0
    parts=seen[0]['body']['input'][0]['content']
    assert parts[1]['image_url']=='data:image/png;base64,'+base64.b64encode(png()).decode()
    assert json.loads(capsys.readouterr().out)['metadata']['media_coverage']['gateway_attempts']==1


def test_unknown_gateway_capabilities_never_use_name_inference(anti,image_path,monkeypatch,capsys):
    module,_=anti
    def request(method,*a,**k):
        assert method=='GET'
        return 200,{'data':[{'id':'fixture:vision'}]}
    monkeypatch.setattr(module,'request_json',request)
    assert module.main(argv(image_path))==1
    assert 'gateway declaration' in capsys.readouterr().err


@pytest.mark.parametrize('stage',['primary','summary','judge','fallback'])
def test_image_policy_enforced_at_each_submission_stage(anti,image_path,tmp_path,stage):
    module,_=anti
    session=policy(tmp_path)
    session.routes.remove(('http://127.0.0.1:51122/v1','fixture:vision',stage))
    args=module.build_parser().parse_args(argv(image_path));args._data_policy_session=session
    module.run_control(args)
    token=module._POLICY_STAGE.set('primary' if stage=='fallback' else stage)
    try:
        with pytest.raises(PolicyError,match='denied'):
            module.policy_submit(args,model='fixture:vision',prompt='safe',base_url='http://127.0.0.1:51122/v1',fallback=stage=='fallback')
    finally:module._POLICY_STAGE.reset(token)
    row=session.audit()['decisions'][-1]
    assert row['unscannedMediaCount']==1 and row['promptSha256']==content_digest('safe',module.captured_media(args).identity())


def test_workflow_reuses_frozen_capture_after_file_changes(anti,image_path,monkeypatch,capsys,tmp_path):
    module,providers=anti;bridge(monkeypatch,module)
    monkeypatch.setenv('ANTIGRAVITY_LOCAL_ONLY','1')
    (tmp_path/'fixture.py').write_text('synthetic = True\n')
    original=module.image_attachments.capture
    captures=[]
    def capture(*a,**k):
        result=original(*a,**k);captures.append(result);image_path.write_bytes(b'changed');return result
    monkeypatch.setattr(module.image_attachments,'capture',capture)
    judgment=json.dumps({'summary':'Synthetic judgment.','findings':[],'disagreements':[],'unverifiable':[],
                         'recommended_next_actions':[],'caveats':[]})
    with upstream(response(),response(judgment)) as (base,seen):
        providers['fixture']=provider(base)
        assert module.main(['workflow','quick-check','--model','fixture:vision','--judge','fixture:judge',
            '--prompt','fixture','--scope','files','--file',str(tmp_path/'fixture.py'),'--image',str(image_path),'--no-progress','--json','--no-verify','--local-only'])==1
    assert len(captures)==1
    for request in seen:assert captures[0].images[0].data_url in json.dumps(request['body'])
    result=json.loads(capsys.readouterr().out)
    assert result['panelStatus']=='degraded_single_model'
    assert result['metadata']['media_coverage']['gateway_attempts']==2


def test_jpeg_signature_preserves_metadata_without_decoding(tmp_path):
    raw=b'\xff\xd8\xff\xe1fixture-exif-with-embedded-metadata\xff\xd9'
    path=tmp_path/'fixture.jpg';path.write_bytes(raw)
    captured=media.capture([str(path)])
    assert captured.images[0].mime=='image/jpeg'
    assert base64.b64decode(captured.images[0].data_url.partition(',')[2])==raw
    assert captured.report()['format_validation']=='signature_only'


@pytest.mark.parametrize('bad',[None,{},[],True,3])
def test_untrusted_media_descriptors_cannot_crash_projection(image_path,bad):
    report=media.capture([str(image_path)]).report();report['images'][0]['mime']=bad
    assert media.projection(report,hashes=True) is None
    with pytest.raises(PolicyError):content_digest('fixture',report['images'])


def test_transport_retry_and_summary_both_preserve_attachment(anti,image_path,monkeypatch):
    module,providers=anti;bridge(monkeypatch,module)
    args=module.build_parser().parse_args(argv(image_path,'--retry','1'))
    with upstream((503,{'Content-Type':'application/json'},b'{"error":"synthetic busy"}'),response()) as (base,seen):
        providers['fixture']=provider(base)
        text,model,details=module.policy_generate(args,stage='summary',model='fixture:vision',prompt='fixture summary',
            max_output_tokens=100,purpose='summary')
    assert model=='fixture:vision' and details['attempts']==2
    report=module.captured_media(args).report()
    assert report['gateway_attempts']==2 and all(row['stage']=='summary' for row in report['attempts'])
    assert all('data:image/png;base64,'+base64.b64encode(png()).decode() in json.dumps(request['body']) for request in seen)
