"""Generated PCM fixtures and owned fake transport; no live listening claim."""
import asyncio
import base64
import importlib.util
import io
import json
import math
import re
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
        parsed=urlsplit(request.full_url)
        path=parsed.path
        assert path in {'/v1/models','/v1/responses','/v1/account-bindings'}
        anti.transport_entry_timeout(request.get_method(),timeout,payload=payload,body=body,url=request.full_url)
        seen.append((request.get_method(),path))
        target=path + (f'?{parsed.query}' if parsed.query else '')
        headers=dict(request.headers)
        headers['Content-Type']='application/json'
        response=client.request(request.get_method(),target,content=body,headers=headers)
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
    ['--max-calls', '2'], ['--retry', '1'], ['--max-output-tokens', '4097'],
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


@pytest.mark.parametrize('clip_count', [1, 2])
@pytest.mark.parametrize('output_json', [True, False])
def test_listen_all_silent_dry_run_plans_no_calls_or_cost(fixture, monkeypatch, capsys, tmp_path, clip_count, output_json):
    anti, _, _, _, _ = fixture
    silent = tmp_path / 'room.wav';silent.write_bytes(wav(amplitude=0))
    monkeypatch.setattr(anti, 'open_gateway_request', lambda *a, **k: pytest.fail('no dry-run HTTP'))
    args = listen_argv(silent, '--dry-run')
    args.remove('--probe-unverified-audio')
    if clip_count == 2:
        args.extend(['--audio', str(silent)])
    if not output_json:
        args.remove('--json')
    assert anti.main(args) == 0
    output = capsys.readouterr().out
    if output_json:
        result, _ = json.JSONDecoder().raw_decode(output)
        stages = result['stages']
        assert result['estimated_cost_total'] == 0
        assert result['token_ceilings']['listen'] == 0
        assert result['estimates'][0]['estimated_total_tokens'] == 0
        media = result['media_coverage']
        assert media['submitted_count'] == 0 and media['measured_silent_skipped_count'] == clip_count
        assert media['model_observation']['status'] == 'not_requested'
    else:
        stages = json.loads(next(line.split('stages: ', 1)[1] for line in output.splitlines() if 'stages: ' in line))
        assert '~0 total tokens' in output
    assert len(stages) == 1
    stage = stages[0]
    assert stage['calls'] == stage['planned_calls'] == stage['max_attempts'] == 0
    assert stage['possible_retries'] == stage['estimated_cost'] == stage['max_output_tokens'] == 0


@pytest.mark.parametrize('kind', ['mixed', 'quiet', 'send_silence', 'consult'])
def test_audio_dry_run_keeps_call_when_audio_will_be_sent(fixture, monkeypatch, capsys, tmp_path, kind):
    anti, _, _, first, _ = fixture
    silent = tmp_path / 'room.wav';silent.write_bytes(wav(amplitude=0))
    quiet = tmp_path / 'quiet.wav';quiet.write_bytes(wav(amplitude=1))
    monkeypatch.setattr(anti, 'open_gateway_request', lambda *a, **k: pytest.fail('no dry-run HTTP'))
    args = listen_argv(quiet if kind == 'quiet' else silent, '--dry-run')
    if kind == 'mixed':
        args.extend(['--audio', str(first)])
    elif kind == 'send_silence':
        args.append('--send-digital-silence')
    elif kind == 'consult':
        args[0] = 'consult'
    assert anti.main(args) == 0
    result, _ = json.JSONDecoder().raw_decode(capsys.readouterr().out)
    assert result['stages'][0]['calls'] == result['stages'][0]['planned_calls'] == 1
    assert result['estimated_cost_total'] > 0 and result['estimates'][0]['estimated_total_tokens'] > 0
    assert result['media_coverage']['submitted_count'] == 1


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


def test_listen_explicit_schema_and_thought_usage_reach_owned_boundaries(fixture, monkeypatch, capsys):
    anti, _, _, first, _ = fixture
    bridge(monkeypatch, anti)
    schema = {'type': 'object', 'properties': {'content': {'type': 'string'}},
              'required': ['content'], 'additionalProperties': False}
    raw = json.loads(response('{"content":"unknown"}')[2])
    raw['response']['usageMetadata'] = {'promptTokenCount': 3, 'candidatesTokenCount': 4,
        'totalTokenCount': 15, 'thoughtsTokenCount': 8}
    with upstream((200, {'Content-Type': 'application/json'}, json.dumps(raw).encode())) as (base, seen):
        endpoint(monkeypatch, base)
        assert anti.main(listen_argv(first, '--response-schema', json.dumps(schema))) == 0
    result = json.loads(capsys.readouterr().out)
    assert len(seen) == 1
    assert seen[0]['body']['request']['generationConfig']['responseJsonSchema'] == schema
    assert result['metadata']['usage']['output_tokens_details'] == {'reasoning_tokens': 8}
    assert result['metadata']['response_schema_sha256']


@pytest.mark.parametrize('schema', ['[]', '{"type":"object","type":"string"}', '{"minimum":NaN}'])
def test_listen_bad_schema_refuses_before_gateway_lookup(fixture, monkeypatch, schema):
    anti, _, _, first, _ = fixture
    monkeypatch.setattr(anti, 'open_gateway_request', lambda *a, **kw: pytest.fail('no gateway lookup'))
    assert anti.main(listen_argv(first, '--response-schema', schema)) == 1


def test_listen_retryable_transport_error_makes_one_post(fixture, monkeypatch):
    anti, _, _, first, _ = fixture
    bridge(monkeypatch, anti)
    with upstream((503, {'Content-Type': 'application/json'}, b'{"error":"synthetic"}')) as (base, seen):
        endpoint(monkeypatch, base)
        assert anti.main(listen_argv(first)) == 1
    assert len(seen) == 1


def test_audio_consult_retains_truncation_without_automatic_retry(fixture, monkeypatch, capsys):
    anti, _, _, first, _ = fixture
    bridge(monkeypatch, anti)
    partial = 'Synthetic incomplete music proposal.'
    payload = {'response': {'candidates': [{'index': 0, 'finishReason': 'MAX_TOKENS',
                'content': {'parts': [{'text': partial}]}}],
                'usageMetadata': {'promptTokenCount': 20, 'candidatesTokenCount': 10,
                                  'thoughtsTokenCount': 65526, 'totalTokenCount': 65556}}}
    with upstream((200, {'Content-Type': 'application/json'}, json.dumps(payload).encode())) as (base, seen):
        endpoint(monkeypatch, base)
        assert anti.main(argv(first, '--max-output-tokens', '65536', '--max-calls', '1',
                              '--fallback-policy', 'never', '--run-id', 'audio-retained',
                              '--save-output', 'full')) == 1
    assert len(seen) == 1
    result = json.loads(capsys.readouterr().out)
    assert result['output_text'] == partial
    assert result['metadata']['retry_disposition'] == 'disabled'
    assert result['metadata']['consult_attempt_count'] == 1
    record = json.loads((anti.RUNS_DIR / 'audio-retained.json').read_text())
    assert record['output_text'] == partial and record['runStatus'] == 'partial'
    assert record['metadata']['usage']['output_tokens_details']['reasoning_tokens'] == 65526


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


def mono_pcm(samples, rate=8000, channels=1):
    stream = io.BytesIO()
    with wave.open(stream, 'wb') as handle:
        handle.setnchannels(channels);handle.setsampwidth(2);handle.setframerate(rate)
        handle.writeframes(struct.pack('<%dh' % len(samples), *samples))
    return stream.getvalue()


def test_silence_map_measures_zero_quiet_and_sound_windows():
    window = 800
    measured = audio.measure_pcm(mono_pcm([1000] * (2 * window) + [0] * (3 * window) + [10] * window + [-1000] * window))
    assert measured['wholly_digital_silence'] is False
    assert measured['window_ms'] == 100 and measured['quiet_peak_dbfs'] == -60.0
    assert measured['windows'] == {'zero': 3, 'quiet': 1, 'sound': 3}
    assert measured['segments'] == [[0, 200, 'sound'], [200, 500, 'zero'], [500, 600, 'quiet'], [600, 700, 'sound']]
    assert measured['segments_omitted'] == 0
    assert measured['peak_dbfs'] == round(20 * math.log10(1000 / 32768), 2)
    silent = audio.measure_pcm(mono_pcm([0] * 8000))
    assert silent['wholly_digital_silence'] is True and silent['peak_dbfs'] is None and silent['rms_dbfs'] is None
    assert silent['segments'] == [[0, 1000, 'zero']]
    stereo = audio.measure_pcm(mono_pcm([0, 0] * 800 + [0, 500] * 800, channels=2))
    assert stereo['segments'] == [[0, 100, 'zero'], [100, 200, 'sound']]
    assert audio.measure_pcm(wav(channels=2, amplitude=0))['wholly_digital_silence'] is True


@pytest.mark.parametrize('width', [1, 3, 4])
def test_silence_map_refuses_non_pcm16_widths(width):
    with pytest.raises(audio.AudioError, match='requires 16-bit PCM'):
        audio.measure_pcm(wav(width=width))


def test_silence_map_refuses_empty_frames():
    with pytest.raises(audio.AudioError, match='requires nonempty frames'):
        audio.measure_pcm(mono_pcm([]))


def test_response_model_id_does_not_mutate_the_request():
    request = {'model': 'gemini-3.8-flash-high', 'input': 'x'}
    assert server.response_model_id(request) == 'gemini-3.8-flash'
    assert request == {'model': 'gemini-3.8-flash-high', 'input': 'x'}
    server.apply_response_model(request)
    assert request['model'] == 'gemini-3.8-flash' and request['reasoning'] == {'effort': 'high'}


def test_all_skipped_session_needs_no_upload_capacity(fixture, tmp_path):
    anti, _, _, _, _ = fixture;catalog_for_helper(anti)
    silent = tmp_path / 'room.wav';silent.write_bytes(wav(amplitude=0))
    session = audio.capture([str(silent)], probe=True, silence_policy='skipped')
    anti.CAPABILITY_REGISTRY.entries['gemini-3.8-flash']['audio_input']['max_files'] = 0
    assert session.all_skipped and session.supports(anti.CAPABILITY_REGISTRY, 'gemini-3.8-flash') is True
    sent = audio.capture([str(silent)], probe=True, silence_policy='sent')
    assert sent.supports(anti.CAPABILITY_REGISTRY, 'gemini-3.8-flash') is False


def test_consult_audio_still_uploads_wholly_silent_clip(fixture, monkeypatch, capsys, tmp_path):
    anti, _, _, _, _ = fixture
    silent = tmp_path / 'room.wav';silent.write_bytes(wav(amplitude=0))
    bridge(monkeypatch, anti)
    with upstream(response()) as (base, seen):
        endpoint(monkeypatch, base)
        assert anti.main(argv(silent)) == 0
    result = json.loads(capsys.readouterr().out)
    parts = seen[0]['body']['request']['contents'][0]['parts']
    assert [base64.b64decode(p['inlineData']['data']) for p in parts if 'inlineData' in p] == [silent.read_bytes()]
    media = result['metadata']['media_coverage']
    assert media['measurement']['digital_silence_policy'] == 'sent'
    assert media['measurement']['clips'][0]['measured_result'] == 'exact_digital_silence'
    assert media['measurement']['clips'][0]['submitted_to_model'] is True


def test_listen_wholly_silent_clip_returns_measured_result_without_gateway_contact(fixture, monkeypatch, capsys, tmp_path):
    anti, _, _, _, _ = fixture
    silent = tmp_path / 'room.wav';silent.write_bytes(wav(amplitude=0))
    monkeypatch.setattr(anti, 'open_gateway_request', lambda *a, **k: pytest.fail('no gateway contact for measured silence'))
    assert anti.main(listen_argv(silent, '--save-output', 'full', '--run-id', 'silence-measured')) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['output_text'] == '' and result['runStatus'] == 'success'
    assert any('not a model observation' in caveat for caveat in result['caveats'])
    media = result['metadata']['media_coverage']
    assert media['gateway_attempts'] == 0 and media['status'] == 'not_sent'
    assert media['measurement']['basis'] == 'local_deterministic_pcm16'
    assert media['measurement']['digital_silence_policy'] == 'skipped'
    clip = media['measurement']['clips'][0]
    assert clip['label'] == 'A' and clip['wholly_digital_silence'] is True
    assert clip['submitted_to_model'] is False and clip['measured_result'] == 'exact_digital_silence'
    assert media['model_observation']['status'] == 'not_requested' and media['model_observation']['clips'] == []
    stored = json.loads((anti.RUNS_DIR / 'silence-measured.json').read_text())['metadata']['media_coverage']
    assert stored['measurement']['clips'][0]['measured_result'] == 'exact_digital_silence'
    assert stored['model_observation']['status'] == 'not_requested'


def test_listen_never_retention_keeps_only_measurement_counts(fixture, monkeypatch, capsys, tmp_path):
    anti, _, _, _, _ = fixture
    silent = tmp_path / 'room.wav';silent.write_bytes(wav(amplitude=0))
    monkeypatch.setattr(anti, 'open_gateway_request', lambda *a, **k: pytest.fail('no gateway contact'))
    assert anti.main(listen_argv(silent, '--save-output', 'never', '--run-id', 'silence-never')) == 0
    capsys.readouterr()
    stored = json.loads((anti.RUNS_DIR / 'silence-never.json').read_text())['metadata']['media_coverage']
    assert stored['measured_silent_skipped_count'] == 1 and stored['submitted_count'] == 0
    assert 'measurement' not in stored and 'audio' not in stored


def test_listen_sends_only_sounding_clip_and_preserves_identity(fixture, monkeypatch, capsys, tmp_path):
    anti, _, _, first, _ = fixture
    silent = tmp_path / 'room.wav';silent.write_bytes(wav(amplitude=0))
    bridge(monkeypatch, anti)
    with upstream(response('{"clips":[]}')) as (base, seen):
        endpoint(monkeypatch, base)
        assert anti.main(listen_argv(silent, '--audio', str(first))) == 0
    result = json.loads(capsys.readouterr().out)
    assert len(seen) == 1
    parts = seen[0]['body']['request']['contents'][0]['parts']
    sent = [base64.b64decode(p['inlineData']['data']) for p in parts if 'inlineData' in p]
    assert sent == [first.read_bytes()]
    texts = [p['text'] for p in parts if 'text' in p]
    assert texts[1:] == ['Audio clip A (attachment 1 of 2) was not sent: Anti measured every sample as zero (exact digital silence).',
                         'Audio clip B (attachment 2 of 2):']
    assert 'inlineData' in parts[-1]
    media = result['metadata']['media_coverage']
    assert media['gateway_attempts'] == 1 and media['submitted_count'] == 1 and media['measured_silent_skipped_count'] == 1
    assert [c['submitted_to_model'] for c in media['measurement']['clips']] == [False, True]
    assert [c['measured_result'] for c in media['measurement']['clips']] == ['exact_digital_silence', None]
    assert media['model_observation']['status'] == 'requested' and media['model_observation']['clips'] == ['B']


def test_listen_send_digital_silence_uploads_silent_clip_for_perception_probes(fixture, monkeypatch, capsys, tmp_path):
    anti, _, _, _, _ = fixture
    silent = tmp_path / 'room.wav';silent.write_bytes(wav(amplitude=0))
    bridge(monkeypatch, anti)
    with upstream(response('{"clips":[]}')) as (base, seen):
        endpoint(monkeypatch, base)
        assert anti.main(listen_argv(silent, '--send-digital-silence')) == 0
    result = json.loads(capsys.readouterr().out)
    parts = seen[0]['body']['request']['contents'][0]['parts']
    assert [base64.b64decode(p['inlineData']['data']) for p in parts if 'inlineData' in p] == [silent.read_bytes()]
    media = result['metadata']['media_coverage']
    assert media['measurement']['digital_silence_policy'] == 'sent_by_explicit_request'
    assert media['measurement']['clips'][0]['submitted_to_model'] is True
    assert media['measurement']['clips'][0]['measured_result'] == 'exact_digital_silence'
    assert media['model_observation']['clips'] == ['A']


def test_send_digital_silence_is_listen_only(fixture):
    anti, _, _, first, _ = fixture
    with pytest.raises(SystemExit):
        anti.build_parser().parse_args(argv(first, '--send-digital-silence'))


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


def test_explicit_pro_high_audio_route_reaches_its_backend(fixture, monkeypatch):
    with upstream(response()) as (base, seen):
        endpoint(monkeypatch, base)
        result = TestClient(server.app).post('/v1/responses', json=body('gemini-3.1-pro-high'))
    assert result.status_code == 200
    assert len(seen) == 1
    assert seen[0]['body']['model'] == 'gemini-pro-agent'


@pytest.mark.parametrize('model,reasoning,level', [
    ('gemini-3.8-flash-high', None, 'high'), ('gemini-3.8-flash-low', None, 'low'),
    ('gemini-3.7-flash-high', None, 'high'), ('gemini-3.8-flash-high', {'effort': 'low'}, 'low')])
def test_effort_suffixed_alias_keeps_its_thinking_level_through_gateway(fixture, monkeypatch, model, reasoning, level):
    request = body(model)
    if reasoning is not None:
        request['reasoning'] = reasoning
    with upstream(response()) as (base, seen):
        endpoint(monkeypatch, base)
        result = TestClient(server.app).post('/v1/responses', json=request)
    assert result.status_code == 200
    assert len(seen) == 1
    assert seen[0]['body']['request']['generationConfig']['thinkingConfig'] == {'thinkingLevel': level}


def test_fixed_model_id_does_not_infer_an_unsupported_reasoning_parameter(fixture, monkeypatch):
    anti, _, _, _, _ = fixture
    bridge(monkeypatch, anti)
    request_json = anti.request_json
    sent = []
    def capture(method, url, **kwargs):
        if method != 'POST':
            return request_json(method, url, **kwargs)
        sent.append(kwargs['payload'])
        return 200, {'status': 'completed', 'model': 'gemini-3.5-flash-high',
                     'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': 'fixture'}]}]}
    monkeypatch.setattr(anti, 'request_json', capture)
    anti.post_response(base_url='http://127.0.0.1:51123/v1', model='gemini-3.5-flash-high',
                       prompt='fixture', max_output_tokens=32, timeout=5, token_env=anti.DEFAULT_TOKEN_ENV)
    assert len(sent) == 1 and sent[0]['model'] == 'gemini-3.5-flash-high'
    assert 'reasoning' not in sent[0]


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
    for index, label in enumerate(('A', 'B')):
        position = next(i for i, p in enumerate(sent) if p.get('inlineData', {}).get('data') == media[index]['data'])
        boundary = sent[position - 1]['text']
        assert re.findall(r'\b[A-B]\b', boundary) == [label]
        assert re.findall(r'\d+', boundary) == [str(index + 1), str(len(media))]
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
