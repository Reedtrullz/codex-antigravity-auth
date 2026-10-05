import json
import pytest
from test_wav_audio import fixture, bridge, endpoint, response, upstream


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


def test_dry_run_has_no_gateway(fixture,monkeypatch,capsys):
    anti,_,_,first,_=fixture
    calls=bridge(monkeypatch,anti)
    assert anti.main(arguments(first,'--dry-run'))==0
    assert calls==[]
    assert 'untrusted data' in capsys.readouterr().out


@pytest.mark.parametrize('extra',[['--retry','1'],['--max-calls','2'],['--model','sonnet'],['--model','openrouter:google/gemini'],['--max-output-tokens','2049']])
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
