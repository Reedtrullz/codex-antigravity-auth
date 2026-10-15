import json
import pytest
import importlib.util
from pathlib import Path
# File-bound fixture loading also works in the installed importlib-mode suite.
_wav_spec = importlib.util.spec_from_file_location('anti_music_wav_fixture', Path(__file__).with_name('test_wav_audio.py'))
assert _wav_spec is not None and _wav_spec.loader is not None
_wav = importlib.util.module_from_spec(_wav_spec)
_wav_spec.loader.exec_module(_wav)
fixture, bridge, endpoint, response, upstream = (_wav.fixture, _wav.bridge, _wav.endpoint, _wav.response, _wav.upstream)


def arguments(first, *extra):
    return ['review-music','--model','gemini-3.8-flash','--audio',str(first),'--probe-unverified-audio','--prompt','Describe attacks','--json','--no-progress',*extra]


def result():
    return {'schemaVersion':1,'kind':'anti-music-review','comparisonStatus':'uncertain','findings':[], 'limitations':['Synthetic fixture, no listening'], 'musicalAcceptance':'not-established'}


def test_standalone_single_google_account_one_post(fixture, monkeypatch, capsys):
    anti,_,_,first,_=fixture
    bridge(monkeypatch,anti)
    with upstream(response(json.dumps(result()))) as (base,seen):
        endpoint(monkeypatch,base)
        assert anti.main(arguments(first)) == 0
        assert len(seen) == 1
    value=json.loads(capsys.readouterr().out)
    assert value['mode']=='review-music'
    assert value['metadata']['music_review']==result()
    assert value['metadata']['retry_disposition']=='disabled'


@pytest.mark.parametrize('silent_index', [0, 1])
def test_digital_silence_refuses_music_review_before_upload(fixture, monkeypatch, capsys, silent_index):
    anti, _, _, first, second = fixture
    [first, second][silent_index].write_bytes(_wav.wav(amplitude=0))
    calls = bridge(monkeypatch, anti)
    monkeypatch.setattr(_wav.server, 'acquire_active_account_for_request',
                        _wav.AsyncMock(side_effect=AssertionError('no account acquisition')))
    assert anti.main(arguments(first, '--audio', str(second))) == 1
    assert not any(method == 'POST' for method, _ in calls)
    assert f'clip {chr(65 + silent_index)} contains only zero PCM samples' in capsys.readouterr().err


def test_quiet_nonzero_pcm_is_not_rejected_as_digital_silence(fixture, monkeypatch, capsys):
    anti, _, _, first, _ = fixture
    first.write_bytes(_wav.wav(amplitude=1))
    bridge(monkeypatch, anti)
    with upstream(response(json.dumps(result()))) as (base, seen):
        endpoint(monkeypatch, base)
        assert anti.main(arguments(first)) == 0
        assert len(seen) == 1


def test_dry_run_has_no_gateway(fixture,monkeypatch,capsys):
    anti,_,_,first,_=fixture
    calls=bridge(monkeypatch,anti)
    assert anti.main(arguments(first,'--dry-run'))==0
    assert calls==[]
    assert 'untrusted data' in capsys.readouterr().out


@pytest.mark.parametrize('extra',[['--retry','1'],['--max-calls','2'],['--model','sonnet'],['--model','openrouter:google/gemini'],['--max-output-tokens','4097']])
def test_refusal_zero_network(fixture,monkeypatch,extra):
    anti,_,_,first,_=fixture
    calls=bridge(monkeypatch,anti)
    assert anti.main(arguments(first,*extra)) != 0
    assert calls==[]


@pytest.mark.parametrize('text',[ 'not json', json.dumps({**result(),'musicalAcceptance':'approved'}), json.dumps({**result(),'findings':[{'clipId':'bad'}]})])
def test_invalid_response_retained_once(fixture,monkeypatch,capsys,text):
    anti,_,_,first,_=fixture
    bridge(monkeypatch,anti)
    with upstream(response(text)) as (base,seen):
        endpoint(monkeypatch,base)
        assert anti.main(arguments(first))==1
        assert len(seen)==1
    value=json.loads(capsys.readouterr().out)
    assert value['metadata']['result_quality']=='incomplete'
    assert value['metadata']['music_validation_error']


def test_truncated_music_json_is_partial_without_retry(fixture, monkeypatch, capsys):
    anti, _, _, first, _ = fixture
    bridge(monkeypatch, anti)
    raw = json.loads(response(json.dumps(result()))[2])
    raw['response']['candidates'][0]['finishReason'] = 'MAX_TOKENS'
    with upstream((200, {'Content-Type': 'application/json'}, json.dumps(raw).encode())) as (base, seen):
        endpoint(monkeypatch, base)
        assert anti.main(arguments(first)) == 1
        assert len(seen) == 1
    value = json.loads(capsys.readouterr().out)
    assert value['runStatus'] == 'partial'
    assert value['metadata']['result_quality'] == 'incomplete'
    assert value['metadata']['retry_disposition'] == 'disabled'


def test_stale_evidence_refuses_before_gateway(fixture, monkeypatch, tmp_path):
    anti, _, _, first, _ = fixture
    calls = bridge(monkeypatch, anti)
    bundle = {'schemaVersion': 1, 'kind': 'anti-music-evidence',
              'clips': [{'id': 'A', 'sha256': '0'*64, 'durationSeconds': 1}],
              'claims': [], 'context': {'sourceAuthority': 'unknown', 'allowedDifferences': []}, 'limitations': []}
    path = tmp_path / 'evidence.json'
    path.write_text(json.dumps(bundle))
    assert anti.main(arguments(first, '--evidence-json', str(path))) != 0
    assert calls == []


@pytest.mark.parametrize('fence', ['```json', '```'])
def test_whole_fenced_review_is_validated_once_and_raw_output_retained(fixture, monkeypatch, capsys, fence):
    anti, _, _, first, _ = fixture
    bridge(monkeypatch, anti)
    text = fence + '\n' + json.dumps(result()) + '\n```'
    with upstream(response(text)) as (base, seen):
        endpoint(monkeypatch, base)
        assert anti.main(arguments(first)) == 0
        assert len(seen) == 1
    value = json.loads(capsys.readouterr().out)
    assert value['output_text'] == text
    assert value['metadata']['music_review'] == result()
    assert value['metadata']['music_response_encoding'] == 'markdown-json-fence'
    assert value['metadata']['musicalAcceptance'] == 'not-established'


@pytest.mark.parametrize('text', [
    'Here is the result:\n```json\n{}\n```',
    '```json\n{}\n```\n```json\n{}\n```',
    '```javascript\n{}\n```',
    '```json\n' + json.dumps({**result(), 'musicalAcceptance': 'approved'}) + '\n```',
    '```json\n{"kind":"anti-music-review","kind":"anti-music-review"}\n```',
])
def test_ambiguous_or_invalid_fenced_review_is_retained_without_retry(fixture, monkeypatch, capsys, text):
    anti, _, _, first, _ = fixture
    bridge(monkeypatch, anti)
    with upstream(response(text)) as (base, seen):
        endpoint(monkeypatch, base)
        assert anti.main(arguments(first)) == 1
        assert len(seen) == 1
    value = json.loads(capsys.readouterr().out)
    assert value['output_text'] == text
    assert 'music_review' not in value['metadata']


def test_truncated_fenced_review_cannot_be_normalized_to_success(fixture, monkeypatch, capsys):
    anti, _, _, first, _ = fixture
    bridge(monkeypatch, anti)
    text = '```json\n' + json.dumps(result()) + '\n```'
    raw = json.loads(response(text)[2])
    raw['response']['candidates'][0]['finishReason'] = 'MAX_TOKENS'
    with upstream((200, {'Content-Type': 'application/json'}, json.dumps(raw).encode())) as (base, seen):
        endpoint(monkeypatch, base)
        assert anti.main(arguments(first)) == 1
        assert len(seen) == 1
    value = json.loads(capsys.readouterr().out)
    assert value['output_text'] == text
    assert 'music_review' not in value['metadata']


def test_explicit_larger_music_cap_reaches_google_in_one_attempt(fixture, monkeypatch, capsys):
    anti, _, _, first, _ = fixture
    bridge(monkeypatch, anti)
    with upstream(response(json.dumps(result()))) as (base, seen):
        endpoint(monkeypatch, base)
        assert anti.main(arguments(first, '--max-output-tokens', '4096')) == 0
        assert len(seen) == 1
        assert seen[0]['body']['request']['generationConfig']['maxOutputTokens'] == 4096
    value = json.loads(capsys.readouterr().out)
    assert value['metadata']['retry_disposition'] == 'disabled'
    assert value['metadata']['musicalAcceptance'] == 'not-established'


@pytest.mark.parametrize('truncated', [False, True])
def test_extended_review_forwards_medium_64k_and_retains_actual_usage_once(fixture, monkeypatch, capsys, truncated):
    anti, _, _, first, second = fixture
    bridge(monkeypatch, anti)
    text = '```json\n' + json.dumps(result()) + '\n```'
    raw = json.loads(response(text)[2])
    raw['response']['usageMetadata'] = {'promptTokenCount': 8330, 'candidatesTokenCount': 1297,
                                       'thoughtsTokenCount': 2426, 'totalTokenCount': 12053}
    if truncated:
        raw['response']['candidates'][0]['finishReason'] = 'MAX_TOKENS'
    with upstream((200, {'Content-Type': 'application/json'}, json.dumps(raw).encode())) as (base, seen):
        endpoint(monkeypatch, base)
        assert anti.main(arguments(first, '--audio', str(second), '--model', 'gemini-3.8-flash-medium',
                                   '--extended-review', '--max-output-tokens', '65536',
                                   '--timeout', '600', '--run-timeout', '600', '--save-output', 'full',
                                   '--run-id', 'extended-review')) == (1 if truncated else 0)
        assert len(seen) == 1
        config = seen[0]['body']['request']['generationConfig']
        assert config['maxOutputTokens'] == 65536 and config['thinkingConfig']['thinkingLevel'] == 'medium'
    value = json.loads(capsys.readouterr().out)
    assert value['output_text'] == text
    assert value['metadata']['music_review_profile'] == 'extended-v1'
    assert value['metadata']['retry_disposition'] == 'disabled'
    assert value['metadata']['music_request_configuration']['reasoningEffort'] == 'medium'
    assert value['metadata']['music_request_configuration']['runTimeoutSeconds'] == 600
    assert value['metadata']['music_token_usage'] == {
        'inputTokens': 8330, 'outputTokensIncludingReasoning': 3723,
        'reasoningTokens': 2426, 'answerTokens': 1297, 'totalTokens': 12053}
    record = json.loads((anti.RUNS_DIR / 'extended-review.json').read_text())
    assert record['output_text'] == text and record['runStatus'] == ('partial' if truncated else 'success')
    assert ('music_review' in value['metadata']) is not truncated
    assert value['metadata']['musicalAcceptance'] == 'not-established'


@pytest.mark.parametrize('extra', [
    ['--max-output-tokens', '65537'], ['--timeout', '601'], ['--run-timeout', '601'],
    ['--retry', '1'], ['--max-calls', '2'], ['--fallback-model', 'flash'],
    ['--auto-route'], ['--model', 'sonnet'],
])
def test_extended_review_refuses_loose_or_ineligible_settings_before_http(fixture, monkeypatch, extra):
    anti, _, _, first, _ = fixture
    calls = bridge(monkeypatch, anti)
    assert anti.main(arguments(first, '--extended-review', *extra)) != 0
    assert calls == []


def test_extended_dry_run_no_dispatch_and_unknown_reasoning_stays_unknown(fixture, monkeypatch, capsys):
    anti, _, _, first, _ = fixture
    calls = bridge(monkeypatch, anti)
    assert anti.main(arguments(first, '--extended-review', '--max-output-tokens', '65536',
                               '--timeout', '600', '--run-timeout', '600', '--dry-run')) == 0
    assert calls == []
    capsys.readouterr()
    with upstream(response(json.dumps(result()))) as (base, seen):
        endpoint(monkeypatch, base)
        assert anti.main(arguments(first, '--extended-review')) == 0
        assert len(seen) == 1
    value = json.loads(capsys.readouterr().out)
    assert value['metadata']['music_token_usage']['reasoningTokens'] is None
    assert value['metadata']['music_token_usage']['answerTokens'] is None


def compact_evidence(first,tmp_path):
    import hashlib
    bundle={'schemaVersion':1,'kind':'anti-music-evidence','clips':[{'id':'A','sha256':hashlib.sha256(first.read_bytes()).hexdigest(),'durationSeconds':1}],
            'claims':[{'id':'c','clipId':'A','startSeconds':0,'endSeconds':1,'text':'Measured synthetic control','origin':'measurement','uncertainty':'No musical qualification'}],
            'context':{'sourceAuthority':'unknown','allowedDifferences':[]},'limitations':['No listening']}
    path=tmp_path/'compact.json';path.write_text(json.dumps(bundle));return path


def test_compact_dry_run_and_missing_claim_refusal_zero_dispatch(fixture,monkeypatch,capsys,tmp_path):
    anti,_,_,first,_=fixture;calls=bridge(monkeypatch,anti)
    path=compact_evidence(first,tmp_path)
    assert anti.main(arguments(first,'--compact-review','--evidence-json',str(path),'--dry-run'))==0
    assert calls==[]
    assert 'one finding' in capsys.readouterr().out
    assert anti.main(arguments(first,'--compact-review')) != 0
    assert calls==[]


@pytest.mark.parametrize('invalid', [False,True])
def test_compact_response_limits_enforced_one_attempt(fixture,monkeypatch,capsys,tmp_path,invalid):
    anti,_,_,first,_=fixture;bridge(monkeypatch,anti);path=compact_evidence(first,tmp_path)
    value={**result(),'findings':[{'clipId':'A','startSeconds':0,'endSeconds':1,'description':'x'*(501 if invalid else 10),'origin':'model-advisory','claimIds':['c'],'uncertainty':'Supplied evidence, not independently heard'}]}
    with upstream(response(json.dumps(value))) as (base,seen):
        endpoint(monkeypatch,base)
        assert anti.main(arguments(first,'--compact-review','--evidence-json',str(path))) == (1 if invalid else 0)
        assert len(seen)==1
    output=json.loads(capsys.readouterr().out)
    assert output['metadata']['music_review_profile']=='compact-v1'
    assert output['metadata']['retry_disposition']=='disabled'
    assert output['metadata']['music_request_configuration']['maxOutputTokens']==2048
    assert output['metadata']['music_request_configuration']['timeoutSeconds']==90
    assert output['metadata']['music_request_configuration']['helperSha256']
    if invalid: assert 'music_review' not in output['metadata']
