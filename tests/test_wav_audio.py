"""Generated PCM fixtures and owned fake transport; no live listening claim."""
import asyncio
import base64
import importlib.util
import io
import json
from pathlib import Path
import struct
import time
from unittest.mock import AsyncMock
from urllib.parse import urlsplit
import wave

import pytest
from fastapi.testclient import TestClient

import codex_antigravity_auth
from codex_antigravity_auth import server,accounts,storage,google_transport
from codex_antigravity_auth.skills.anti.scripts.anti_lib import wav_audio as audio
from codex_antigravity_auth.skills.anti.scripts.anti_lib.data_policy import DataPolicy,PolicyError,content_digest
from fake_upstream import upstream


def wav(*,rate=8000,seconds=1,channels=1,width=2,amplitude=100):
    stream=io.BytesIO()
    with wave.open(stream,'wb') as handle:
        handle.setnchannels(channels);handle.setsampwidth(width);handle.setframerate(rate)
        sample=struct.pack('<h',amplitude) if width==2 else bytes([0])*width
        handle.writeframes(sample*(rate*seconds*channels))
    return stream.getvalue()


def part(raw=None):
    return {'type':'antigravity_audio','mime_type':'audio/wav','data':base64.b64encode(wav() if raw is None else raw).decode(),
            'probe_unverified':True}


def body(model='gemini-3.8-flash',*parts):
    return {'model':model,'stream':False,'max_output_tokens':128,'input':[{'role':'user','content':[
        {'type':'input_text','text':'Describe the supplied sound without assuming what it contains.'},*(parts or [part()])]}]}


@pytest.fixture
def fixture(monkeypatch,tmp_path):
    script=Path(codex_antigravity_auth.__file__).parent/'skills/anti/scripts/anti.py'
    spec=importlib.util.spec_from_file_location('anti_wav_fixture',script)
    anti=importlib.util.module_from_spec(spec);spec.loader.exec_module(anti)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(anti,'RUNS_DIR',tmp_path/'runs')
    monkeypatch.setattr(anti,'ensure_helper_parity',lambda args:None)
    monkeypatch.setattr(anti,'_install_run_signal_handlers',lambda args:None)
    monkeypatch.setattr(anti,'find_repo_root',lambda _:tmp_path)
    monkeypatch.setenv('ANTIGRAVITY_LOCAL_ONLY','0')
    monkeypatch.setattr(server.app.state,'local_only_mode',None,raising=False)
    monkeypatch.setattr(server,'all_provider_configs',lambda:{})
    monkeypatch.setattr(server,'all_provider_configs_read_only',lambda:{})
    records=[];monkeypatch.setattr(server,'write_request_record',records.append)
    manager=accounts.AccountManager();monkeypatch.setattr(server,'account_manager',manager)
    storage.save_accounts({'accounts':[{'email':'audio-fixture@example.invalid','accessToken':'synthetic-only',
                                       'expiresAt':time.time()+3600,'projectId':'audio-fixture-project'}]})
    first=tmp_path/'sensitive-source-name.wav';first.write_bytes(wav())
    second=tmp_path/'sensitive-render-name.wav';second.write_bytes(wav(amplitude=200))
    return anti,manager,records,first,second


def bridge(monkeypatch,anti):
    client=TestClient(server.app);seen=[]
    def opened(request,*,timeout,payload=None,body=None):
        path=urlsplit(request.full_url).path
        assert path in {'/v1/models','/v1/responses'}
        anti.transport_entry_timeout(request.get_method(),timeout,payload=payload,body=body,url=request.full_url)
        seen.append((request.get_method(),path))
        response=client.request(request.get_method(),path,content=body,headers={'Content-Type':'application/json'})
        wire=io.BytesIO(response.content);wire.status=response.status_code;wire.headers=response.headers;return wire
    monkeypatch.setattr(anti,'open_gateway_request',opened)
    return seen


def response(text='Synthetic fixture response; no actual listening is asserted.'):
    return (200,{'Content-Type':'application/json'},json.dumps({'response':{'candidates':[{'index':0,'finishReason':'STOP',
        'content':{'parts':[{'text':text}]}}]}}).encode())


def argv(path,*extra):
    return ['consult','--model','gemini-3.8-flash','--audio',str(path),'--probe-unverified-audio','--no-pre-read',
            '--prompt','Describe the supplied sound.','--json','--no-progress','--retry','0',*extra]


def listen_argv(path, *extra):
    return ['listen', '--model', 'gemini-3.8-flash', '--audio', str(path),
            '--probe-unverified-audio', '--prompt', 'Describe the supplied piano clips.',
            '--json', '--no-progress', *extra]


def test_listen_defaults_are_a_single_bounded_audio_attempt(fixture):
    anti, _, _, first, _ = fixture
    args = anti.build_parser().parse_args(listen_argv(first))
    assert args.max_calls == 1 and args.retry == 0
    assert args.max_output_tokens == 2048
    assert args.run_timeout == 90 and args.timeout == 90
    assert args.no_pre_read is True and args.fallback_policy == 'never'


@pytest.mark.parametrize('extra', [
    ['--max-calls', '2'], ['--retry', '1'], ['--max-output-tokens', '2049'],
    ['--run-timeout', '91'], ['--timeout', '91'],
    ['--fallback-model', 'gemini-3.1-pro'], ['--fallback-policy', 'on-retryable'],
    ['--auto-route'], ['--run-timeout', 'nan'], ['--timeout', 'inf'],
])
def test_listen_loose_limits_refuse_before_any_http(fixture, monkeypatch, capsys, extra):
    anti, _, _, first, _ = fixture
    monkeypatch.setattr(anti, 'request_json', lambda *a, **k: pytest.fail('no listen HTTP'))
    assert anti.main(listen_argv(first, *extra)) == 1
    assert 'listen' in capsys.readouterr().err.lower()


@pytest.mark.parametrize('missing', ['audio', 'model'])
def test_listen_requires_audio_and_explicit_model_before_http(fixture, monkeypatch, missing):
    anti, _, _, first, _ = fixture
    monkeypatch.setattr(anti, 'request_json', lambda *a, **k: pytest.fail('no listen HTTP'))
    args = listen_argv(first)
    index = args.index('--' + missing)
    del args[index:index + 2]
    if missing == 'audio':
        args.remove('--probe-unverified-audio')
    assert anti.main(args) == 1


def test_listen_cannot_select_first_model_from_local_profile(fixture, tmp_path):
    anti, _, _, first, _ = fixture
    profile = tmp_path / 'local-profile.json'
    profile.write_text(json.dumps({'version': 1, 'local_only': True,
        'gateway': 'http://127.0.0.1:51122/v1',
        'models': ['gemini-3.8-flash', 'gemini-3.1-pro'], 'judge': 'gemini-3.8-flash'}))
    raw = listen_argv(first, '--local-profile', str(profile))
    index = raw.index('--model')
    del raw[index:index + 2]
    args = anti.build_parser().parse_args(raw)
    with pytest.raises(anti.AntiError, match='listen requires explicit'):
        anti.run_control(args)


def test_listen_dry_run_never_contacts_catalog_and_has_no_retry(fixture, monkeypatch, capsys):
    anti, _, _, first, _ = fixture
    monkeypatch.setattr(anti, 'request_json', lambda *a, **k: pytest.fail('no dry-run HTTP'))
    args = listen_argv(first, '--dry-run')
    args.remove('--probe-unverified-audio')
    assert anti.main(args) == 0
    result, _ = json.JSONDecoder().raw_decode(capsys.readouterr().out)
    assert result['mode'] == 'listen'
    assert result['media_coverage']['status'] == 'not_sent'
    assert result['stages'][0]['possible_retries'] == 0


def test_listen_complete_output_pins_both_clips_without_pre_read(fixture, monkeypatch, capsys):
    anti, _, _, first, second = fixture
    bridge(monkeypatch, anti)
    monkeypatch.setattr(anti, 'build_consult_file_context',
                        lambda *a, **k: pytest.fail('listen must not pre-read source files'))
    with upstream(response('{"summary":"Synthetic music fixture","findings":[]}')) as (base, seen):
        endpoint(monkeypatch, base)
        assert anti.main(listen_argv(first, '--audio', str(second), '--save-output', 'full')) == 0
    result = json.loads(capsys.readouterr().out)
    assert len(seen) == 1 and result['mode'] == 'listen'
    assert result['metadata']['result_quality'] == 'complete'
    media = result['metadata']['media_coverage']
    assert media['gateway_attempts'] == 1 and media['captured_count'] == 2
    assert [x['sha256'] for x in media['audio']] == [audio.inspect_wav(p.read_bytes())['sha256'] for p in (first, second)]
    assert media['provider_audio_acceptance'] == 'unverified'
    assert media['listening_verification'] == 'not_run'


def test_listen_truncated_output_is_retained_without_second_post(fixture, monkeypatch, capsys):
    anti, _, _, first, _ = fixture
    bridge(monkeypatch, anti)
    raw = json.loads(response('Retained partial musical observation.')[2])
    raw['response']['candidates'][0]['finishReason'] = 'MAX_TOKENS'
    with upstream((200, {'Content-Type': 'application/json'}, json.dumps(raw).encode())) as (base, seen):
        endpoint(monkeypatch, base)
        assert anti.main(listen_argv(first)) == 1
    result = json.loads(capsys.readouterr().out)
    assert len(seen) == 1 and result['runStatus'] == 'partial'
    assert result['output_text'] == 'Retained partial musical observation.'
    assert result['metadata']['result_quality'] == 'incomplete'
    assert result['metadata']['retry_disposition'] == 'disabled'


def test_listen_retryable_transport_error_makes_one_post(fixture, monkeypatch):
    anti, _, _, first, _ = fixture
    bridge(monkeypatch, anti)
    with upstream((503, {'Content-Type': 'application/json'}, b'{"error":"synthetic"}')) as (base, seen):
        endpoint(monkeypatch, base)
        assert anti.main(listen_argv(first)) == 1
    assert len(seen) == 1


@pytest.mark.parametrize('status', [401, 403, 429])
def test_audio_backend_auth_or_quota_failure_never_rotates(fixture, monkeypatch, status):
    _, manager, _, _, _ = fixture
    storage.save_accounts({'accounts': [
        {'email': f'audio-{index}@example.invalid', 'accessToken': 'synthetic-only',
         'expiresAt': time.time() + 3600, 'projectId': 'fixture'} for index in range(2)
    ]})
    with upstream((status, {'Content-Type': 'application/json'}, b'{"error":"synthetic"}')) as (base, seen):
        endpoint(monkeypatch, base)
        result = TestClient(server.app).post('/v1/responses', json=body())
    assert len(seen) == 1
    assert result.status_code in {status, 503}
    assert not manager._in_flight


def test_audio_backend_connection_failure_never_rotates(fixture, monkeypatch):
    _, manager, _, _, _ = fixture
    attempts = []
    async def failed(self, request, lease):
        attempts.append(request)
        raise OSError('synthetic transport failure')
    monkeypatch.setattr(google_transport.GoogleTransport, 'post', failed)
    result = TestClient(server.app).post('/v1/responses', json=body())
    assert result.status_code == 502 and len(attempts) == 1
    assert 'after rotation' not in json.dumps(result.json())
    assert not manager._in_flight


@pytest.mark.parametrize('limit', [None, 0, 2, True])
def test_listen_refuses_gateway_without_exact_backend_attempt_bound(fixture, monkeypatch, limit):
    anti, _, _, first, _ = fixture
    catalog_for_helper(anti)
    contract = anti.CAPABILITY_REGISTRY.entries['gemini-3.8-flash']['audio_input']
    if limit is None:
        contract.pop('backend_attempt_limit', None)
    else:
        contract['backend_attempt_limit'] = limit
    args = anti.build_parser().parse_args(listen_argv(first))
    anti.run_control(args)
    with pytest.raises(anti.wav_audio.AudioError, match='single backend attempt'):
        anti.captured_media(args).require(anti.CAPABILITY_REGISTRY, args.model, 'primary')


def endpoint(monkeypatch,base):
    monkeypatch.setattr(server,'GoogleTransport',lambda **kw:google_transport.GoogleTransport(endpoint=base,**kw))


@pytest.mark.parametrize('channels,rate',[(1,8000),(2,16000),(1,24000),(2,44100),(1,48000)])
def test_classic_pcm_parser_reports_original_frames_and_bytes(channels,rate):
    raw=wav(channels=channels,rate=rate)
    info=audio.inspect_wav(raw)
    assert info['frames']==rate and info['channels']==channels and info['sample_rate']==rate
    assert info['duration_ms']==1000 and info['bytes']==len(raw) and info['mime']=='audio/wav'
    assert audio.decode_part(part(raw))[0]==raw


@pytest.mark.parametrize('mode',['empty','truncated','riff_size','compressed','alignment','byte_rate','stereo24','duration','oversize','missing_data'])
def test_malformed_or_unsupported_wav_rejected(mode):
    raw=bytearray(wav())
    if mode=='empty':raw=bytearray(wav(seconds=0))
    elif mode=='truncated':raw=raw[:-1]
    elif mode=='riff_size':struct.pack_into('<I',raw,4,1)
    elif mode=='compressed':struct.pack_into('<H',raw,20,3)
    elif mode=='alignment':struct.pack_into('<H',raw,32,9)
    elif mode=='byte_rate':struct.pack_into('<I',raw,28,99)
    elif mode=='stereo24':raw=bytearray(wav(channels=2,width=3))
    elif mode=='duration':raw=bytearray(wav(seconds=31))
    elif mode=='oversize':raw.extend(b'\0'*audio.MAX_FILE_BYTES)
    elif mode=='missing_data':raw[36:40]=b'JUNK'
    with pytest.raises(audio.AudioError):audio.inspect_wav(bytes(raw))


@pytest.mark.parametrize('change',[{'mime_type':'audio/mp3'},{'probe_unverified':False},{'probe_unverified':1},
                                 {'data':'!!!'},{'data':'UklGR'}, {'extra':'unrecognized'}])
def test_private_extension_rejects_mime_base64_flags_and_unknown_fields(change):
    value=part();value.update(change)
    with pytest.raises(audio.AudioError):audio.decode_part(value)


def test_gateway_rejects_unsupported_routes_before_any_auth(fixture,monkeypatch):
    monkeypatch.setattr(server,'resolve_openai_auth',lambda:pytest.fail('no OpenAI auth'))
    monkeypatch.setattr(server,'acquire_active_account_for_request',AsyncMock(side_effect=AssertionError('no account acquisition')))
    monkeypatch.setattr(server,'all_provider_configs',lambda:pytest.fail('no BYOK credential store'))
    client=TestClient(server.app)
    for model in ('claude-sonnet-4-6','gpt-oss-120b-medium','gpt-5.6','fixture:vision','gemini-3.1-flash-image'):
        result=client.post('/v1/responses',json=body(model))
        assert result.status_code in {400,404}
        assert 'Audio' in result.text or 'audio' in result.text or 'unsupported' in result.text or 'disabled' in result.text


@pytest.mark.parametrize('change',['assistant','system','tool','stream','missing_intent','too_many','mixed','large_request'])
def test_invalid_audio_request_rejected_before_provider(fixture,monkeypatch,change):
    monkeypatch.setattr(server,'acquire_active_account_for_request',AsyncMock(side_effect=AssertionError('no account acquisition')))
    value=body()
    if change in {'assistant','system'}:value['input'][0]['role']=change
    elif change=='tool':value['input']=[{'type':'function_call_output','call_id':'synthetic','output':[part()]}]
    elif change=='stream':value['stream']=True
    elif change=='missing_intent':value['input'][0]['content'][1]['probe_unverified']=False
    elif change=='too_many':value['input'][0]['content']=[part()]*3
    elif change=='mixed':value['input'][0]['content'].append({'type':'input_image','image_url':'data:image/png;base64,AAAA'})
    else:value['input'][0]['content'][0]['text']='x'*audio.MAX_REQUEST_BYTES
    result=TestClient(server.app).post('/v1/responses',json=value)
    assert result.status_code==400


def test_catalog_separates_encoder_from_backend_listening_acceptance(fixture):
    catalog=TestClient(server.app).get('/v1/models').json()
    gemini=next(row for row in catalog['data'] if row['id']=='gemini-3.8-flash')['capabilities']
    claude=next(row for row in catalog['data'] if row['id']=='claude-sonnet-4-6')['capabilities']
    assert gemini['audio_input']['transport_supported'] is True
    assert gemini['audio_input']['backend_acceptance']=='unverified' and gemini['audio_input']['requires_probe_opt_in'] is True
    assert 'audio' not in gemini['effective']['input_modalities']
    assert claude['audio_input']['transport_supported'] is False


@pytest.mark.parametrize('mode',['never','summary','full'])
def test_helper_gateway_fake_upstream_preserve_order_and_omit_payloads_from_records(fixture,monkeypatch,capsys,mode):
    anti,manager,records,first,second=fixture;bridge(monkeypatch,anti)
    originals=[first.read_bytes(),second.read_bytes()]
    with upstream(response()) as (base,seen):
        endpoint(monkeypatch,base)
        assert anti.main(argv(first,'--audio',str(second),'--save-output',mode,'--run-id','wav-fixture'))==0
    output=capsys.readouterr();result=json.loads(output.out)
    sent=seen[0]['body']['request']['contents'][0]['parts']
    media=[p['inlineData'] for p in sent if 'inlineData' in p]
    assert [base64.b64decode(p['data']) for p in media]==originals
    assert [p['mimeType'] for p in media]==['audio/wav','audio/wav']
    assert 'sensitive-source-name' not in json.dumps(seen[0]['body']) and 'sensitive-render-name' not in json.dumps(seen[0]['body'])
    receipt=result['metadata']['media_coverage']
    assert receipt['kind']=='audio' and receipt['gateway_attempts']==1 and receipt['captured_duration_ms']==2000
    assert receipt['provider_audio_acceptance']=='unverified' and receipt['listening_verification']=='not_run'
    assert result['metadata']['context_preflight']['status']=='unknown'
    assert result['metadata']['gateway_routing_identity']
    assert not manager._in_flight
    stored=json.loads((anti.RUNS_DIR/'wav-fixture.json').read_text())['metadata']['media_coverage']
    assert ('audio' in stored)==(mode!='never')
    assert stored['captured_count']==2
    captures=output.out+output.err+json.dumps(records)+''.join(path.read_text() for path in anti.RUNS_DIR.rglob('*.json'))
    for original,path in zip(originals,(first,second)):
        assert base64.b64encode(original).decode() not in captures and str(path) not in captures


def test_dry_run_never_contacts_catalog_and_requires_no_probe_intent(fixture,monkeypatch,capsys):
    anti,_,_,first,_=fixture
    monkeypatch.setattr(anti,'request_json',lambda *a,**k:pytest.fail('no dry-run HTTP'))
    args=[arg for arg in argv(first) if arg!='--probe-unverified-audio']+['--dry-run']
    assert anti.main(args)==0
    report,_=json.JSONDecoder().raw_decode(capsys.readouterr().out)
    assert report['media_coverage']['probe_upload_intent'] is False
    assert report['media_coverage']['status']=='not_sent'
    assert any('audio costs' in value for value in report['unknowns'])


def test_missing_explicit_intent_and_text_only_fallback_refuse_before_generation(fixture,monkeypatch,capsys):
    anti,_,_,first,_=fixture
    original_request=anti.request_json
    monkeypatch.setattr(anti,'request_json',lambda *a,**k:pytest.fail('no request without intent'))
    assert anti.main([arg for arg in argv(first) if arg!='--probe-unverified-audio'])==1
    assert '--probe-unverified-audio' in capsys.readouterr().err
    monkeypatch.setattr(anti,'request_json',original_request)
    seen=bridge(monkeypatch,anti)
    assert anti.main(argv(first,'--fallback-model','claude-sonnet-4-6','--fallback-policy','on-retryable'))==1
    assert all(method=='GET' for method,path in seen)


def test_policy_paths_and_text_audio_acknowledgements_are_bound(fixture,tmp_path,monkeypatch):
    anti,_,_,first,_=fixture
    value={'schemaVersion':1,'destinations':[{'baseUrl':'http://127.0.0.1:51122/v1','model':'gemini-3.8-flash','stages':['primary','fallback']}],
           'forbiddenPaths':['*.wav'],'maxScanChars':100000}
    path=tmp_path/'policy.json';path.write_text(json.dumps(value));policy=DataPolicy(path,root=tmp_path)
    with pytest.raises(PolicyError,match='denied'):audio.capture([str(first)],policy=policy,probe=True)
    value['forbiddenPaths']=[];path.write_text(json.dumps(value));capture=audio.capture([str(first)],probe=True)
    secret='sk-fixtureabcdefghijklmnopqrstuvwxyz'
    policy=DataPolicy(path,root=tmp_path,acknowledgements=[content_digest(secret)])
    with pytest.raises(PolicyError,match='acknowledge'):
        policy.check(prompt=secret,model='gemini-3.8-flash',base_url='http://127.0.0.1:51122/v1',stage='primary',media=capture.identity())
    policy=DataPolicy(path,root=tmp_path,acknowledgements=[content_digest(secret,capture.identity())])
    assert policy.check(prompt=secret,model='gemini-3.8-flash',base_url='http://127.0.0.1:51122/v1',stage='primary',media=capture.identity())['reason']=='acknowledged'


def test_backend_echoes_are_withheld_and_audio_base64_is_redacted(fixture,monkeypatch,capsys):
    anti,manager,records,first,_=fixture;bridge(monkeypatch,anti);encoded=base64.b64encode(first.read_bytes()).decode()
    with upstream((400,{'Content-Type':'application/json'},json.dumps({'error':{'message':'echo '+encoded}}).encode())) as (base,seen):
        endpoint(monkeypatch,base)
        assert anti.main(argv(first,'--save-output','full','--run-id','failed-probe'))==1
    captured=capsys.readouterr();all_text=captured.out+captured.err+json.dumps(records)+''.join(p.read_text() for p in anti.RUNS_DIR.rglob('*.json'))
    assert encoded not in all_text and encoded[:100] not in all_text
    assert not manager._in_flight
    assert 'unverified' in all_text
    from codex_antigravity_auth.redaction import redact_secret_text
    for value in (encoded,'data:audio/wav;base64,'+encoded,encoded[:100],json.dumps({'data':encoded})):
        assert encoded[:40] not in redact_secret_text(value)


def catalog_for_helper(anti):
    anti.CAPABILITY_REGISTRY.consume(TestClient(server.app).get('/v1/models').json())


def responses_answer(model='gemini-3.8-flash'):
    return (200,{'Content-Type':'application/json'},json.dumps({'status':'completed','model':model,'output':[
        {'type':'message','role':'assistant','content':[{'type':'output_text','text':'Synthetic gateway answer.'}]}]}).encode())


def test_helper_retry_and_explicit_gemini_fallback_keep_exact_audio(fixture,capsys):
    anti,_,_,first,_=fixture;catalog_for_helper(anti)
    # The owned listener stands in for the gateway to isolate helper retry rules.
    with upstream((503,{'Content-Type':'application/json'},b'{"detail":"synthetic unavailable"}'),responses_answer()) as (base,seen):
        args=anti.build_parser().parse_args(argv(first,'--base-url',base+'/v1','--retry','1'))
        result=anti.post_response(base_url=args.base_url,model=args.model,prompt=args.prompt,max_output_tokens=128,timeout=3,
            token_env=args.gateway_token_env,retries=1,model_ids={args.model},budget_args=args)
        assert result.response_metadata['attempts']==2
        assert seen[0]['body']['input']==seen[1]['body']['input']
        assert anti.captured_media(args).report()['gateway_attempts']==2
    with upstream((503,{'Content-Type':'application/json'},b'{"detail":"synthetic unavailable"}'),responses_answer('gemini-3.1-pro')) as (base,seen):
        args=anti.build_parser().parse_args(argv(first,'--base-url',base+'/v1','--fallback-model','gemini-3.1-pro','--fallback-policy','on-retryable'))
        text,used,_=anti.generate_with_fallback(args,model=args.model,prompt=args.prompt,max_output_tokens=128,purpose='audio',model_ids={'gemini-3.8-flash','gemini-3.1-pro'})
        assert used=='gemini-3.1-pro'
        assert seen[0]['body']['input']==seen[1]['body']['input']
        assert [row['fallback'] for row in anti.captured_media(args).report()['attempts']]==[False,True]


def test_audio_error_suppression_preserves_retry_after_deferral(fixture):
    anti,_,_,first,_=fixture;catalog_for_helper(anti)
    encoded=base64.b64encode(first.read_bytes()).decode()
    with upstream((503,{'Content-Type':'application/json','Retry-After':'1000'},json.dumps({'detail':encoded}).encode())) as (base,seen):
        args=anti.build_parser().parse_args(argv(first,'--base-url',base+'/v1','--retry','1'))
        with pytest.raises(anti.AntiError,match='retry deferred') as error:
            anti.post_response(base_url=args.base_url,model=args.model,prompt=args.prompt,max_output_tokens=128,timeout=3,
                token_env=args.gateway_token_env,retries=1,model_ids={args.model},budget_args=args)
        assert len(seen)==1 and encoded[:100] not in str(error.value)
        assert anti.captured_media(args).report()['gateway_attempts']==1


def test_audio_attempt_allowance_blocks_retry_before_another_post(fixture):
    anti,_,_,first,_=fixture;catalog_for_helper(anti)
    with upstream((503,{'Content-Type':'application/json'},b'{"detail":"synthetic unavailable"}')) as (base,seen):
        args=anti.build_parser().parse_args(argv(first,'--base-url',base+'/v1','--max-calls','1'))
        with pytest.raises(anti.SpendAdmissionError):
            anti.post_response(base_url=args.base_url,model=args.model,prompt=args.prompt,max_output_tokens=128,timeout=3,
                token_env=args.gateway_token_env,retries=1,model_ids={args.model},budget_args=args)
        assert len(seen)==1 and anti.captured_media(args).report()['gateway_attempts']==1
        assert args._run_control.snapshot()['permits_acquired']==args._run_control.snapshot()['permits_released']


def test_audio_cancellation_releases_acquired_account(fixture,monkeypatch):
    from starlette.requests import Request
    _,manager,records,first,_=fixture
    async def scenario():
        started=asyncio.Event();never=asyncio.Event();prepared=[]
        async def blocked_post(self,request,lease):
            prepared.append(self.build_request(request,lease));started.set();await never.wait()
        monkeypatch.setattr(google_transport.GoogleTransport,'post',blocked_post)
        raw=json.dumps(body()).encode();delivered=False
        async def receive():
            nonlocal delivered
            if not delivered:delivered=True;return {'type':'http.request','body':raw,'more_body':False}
            await never.wait()
        scope={'type':'http','method':'POST','path':'/v1/responses','headers':[],'query_string':b'',
               'client':('127.0.0.1',1),'server':('127.0.0.1',80),'scheme':'http'}
        task=asyncio.create_task(server.create_response(Request(scope,receive)))
        await asyncio.wait_for(started.wait(),3);task.cancel()
        with pytest.raises(asyncio.CancelledError):await task
        assert len(prepared)==1 and not manager._in_flight
    asyncio.run(scenario())
    assert any(row.get('cancelled') is True for row in records)
    assert base64.b64encode(first.read_bytes()).decode() not in json.dumps(records)


def test_capture_preserves_metadata_and_rejects_symlink_parents(fixture,tmp_path):
    _,_,_,first,_=fixture
    raw=first.read_bytes();chunk=b'JUNK'+struct.pack('<I',8)+b'fixture!'
    changed=bytearray(raw[:36]+chunk+raw[36:]);struct.pack_into('<I',changed,4,len(changed)-8)
    first.write_bytes(changed)
    captured=audio.capture([str(first)],probe=True)
    assert base64.b64decode(captured.images[0].encoded)==changed
    first.write_bytes(b'replaced after capture')
    assert base64.b64decode(captured.images[0].encoded)==changed
    directory=tmp_path/'actual';directory.mkdir();(directory/'part.wav').write_bytes(wav())
    link=tmp_path/'linked';link.symlink_to(directory,target_is_directory=True)
    with pytest.raises(audio.AudioError):audio.capture([str(link/'part.wav')],probe=True)
    with pytest.raises(TypeError):captured.images[0].descriptor['bytes']=0


def test_pcm_probe_requires_explicit_model_and_refuses_auto_routing(fixture,monkeypatch,capsys):
    anti,_,_,first,_=fixture
    monkeypatch.setattr(anti,'request_json',lambda *a,**k:pytest.fail('no implicit route'))
    assert anti.main(['consult','--audio',str(first),'--probe-unverified-audio','--prompt','fixture'])==1
    assert 'explicit --model' in capsys.readouterr().err
    assert anti.main(argv(first,'--auto-route'))==1
    assert 'automatic routing' in capsys.readouterr().err


def test_audio_api_cannot_use_an_implicit_default_model(fixture,monkeypatch):
    monkeypatch.setattr(server,'acquire_active_account_for_request',AsyncMock(side_effect=AssertionError('no implicit account')))
    request=body();request.pop('model')
    response=TestClient(server.app).post('/v1/responses',json=request)
    assert response.status_code==400 and 'explicit model' in response.text


def test_standalone_audio_dry_run_needs_no_gateway_package(fixture,tmp_path):
    import shutil
    import subprocess
    import sys
    from standalone import without_installed_packages
    anti,_,_,first,_=fixture
    skill=tmp_path/'standalone';shutil.copytree(Path(codex_antigravity_auth.__file__).parent/'skills/anti',skill,ignore=shutil.ignore_patterns('__pycache__'))
    args=[v for v in argv(first) if v!='--probe-unverified-audio']+['--dry-run']
    code=without_installed_packages('import sys,runpy\nsys.argv='+repr([str(skill/'scripts/anti.py'),*args])+'\nrunpy.run_path(sys.argv[0],run_name="__main__")\n')
    result=subprocess.run([sys.executable,'-c',code],cwd=tmp_path,capture_output=True,text=True,timeout=30)
    assert result.returncode==0,result.stderr
    value,_=json.JSONDecoder().raw_decode(result.stdout)
    assert value['media_coverage']['kind']=='audio' and value['media_coverage']['gateway_attempts']==0
    assert str(first) not in result.stdout


@pytest.mark.parametrize('value',['https://example.invalid/clip.wav','data:audio/wav;base64,AAAA'])
def test_no_audio_url_fetch_or_inline_cli_payload(value):
    with pytest.raises(audio.AudioError):audio.capture([value],probe=True)


def test_unrelated_audio_controls_have_different_hashes_without_filename_cues(fixture):
    _,_,_,first,second=fixture
    one=audio.capture([str(first)],probe=True);two=audio.capture([str(second)],probe=True)
    assert one.identity()[0]['sha256']!=two.identity()[0]['sha256']
    assert one.input('Same neutral prompt.')[0]['content'][0]==two.input('Same neutral prompt.')[0]['content'][0]
    assert first.name not in json.dumps(one.input('Same neutral prompt.'))
    # This checks fixture discrimination/transport only; it performs no listening.


def test_changed_audio_cannot_reuse_text_audio_policy_acknowledgement(fixture,tmp_path):
    _,_,_,first,second=fixture;one=audio.capture([str(first)],probe=True);two=audio.capture([str(second)],probe=True)
    prompt='sk-fixtureabcdefghijklmnopqrstuvwxyz'
    config={'schemaVersion':1,'destinations':[{'baseUrl':'http://127.0.0.1:51122/v1','model':'gemini-3.8-flash','stages':['primary']}],
            'forbiddenPaths':[],'maxScanChars':100000}
    path=tmp_path/'changed-policy.json';path.write_text(json.dumps(config))
    policy=DataPolicy(path,root=tmp_path,acknowledgements=[content_digest(prompt,one.identity())])
    with pytest.raises(PolicyError,match='acknowledge'):
        policy.check(prompt=prompt,model='gemini-3.8-flash',base_url='http://127.0.0.1:51122/v1',stage='primary',media=two.identity())


def test_source_assembly_refuses_mixed_image_audio_and_nonconsult_args(fixture,monkeypatch):
    anti,_,_,first,_=fixture
    monkeypatch.setattr(anti,'request_json',lambda *a,**k:pytest.fail('no mixed-media dispatch'))
    assert anti.main(argv(first,'--image',str(first)))==1
    with pytest.raises(SystemExit):anti.main(['panel','--audio',str(first),'--prompt','fixture'])


@pytest.mark.parametrize('field,value',[('max_files',0),('max_file_bytes',1),('max_total_bytes',1),('max_duration_seconds',0),
    ('max_files',True),('content_type','input_audio'),('streaming',True)])
def test_helper_honors_the_selected_gateway_audio_contract_bounds(fixture,field,value):
    anti,_,_,first,_=fixture;catalog_for_helper(anti)
    session=audio.capture([str(first)],probe=True)
    anti.CAPABILITY_REGISTRY.entries['gemini-3.8-flash']['audio_input'][field]=value
    with pytest.raises(audio.AudioError,match='advertised'):
        session.require(anti.CAPABILITY_REGISTRY,'gemini-3.8-flash','primary')
