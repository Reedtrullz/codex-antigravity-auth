"""Synthetic Google output fixtures; unsupported media is never decoded or sent."""
import asyncio
import base64
from copy import deepcopy
import json
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi.testclient import TestClient

from codex_antigravity_auth import server, models
from codex_antigravity_auth.capability_catalog import native_contract, standalone_snapshot
from codex_antigravity_auth.google_transport import GoogleTransport, GoogleStreamEventAdapter, AccountLease
from codex_antigravity_auth.response_protocol import TerminalKind
from fake_upstream import upstream, frames


def request():
    return {'model':'gemini-3.8-flash','input':'synthetic request', 'tools':[{'type':'function','name':'lookup',
        'parameters':{'type':'object','properties':{'q':{'type':'string'}},'required':['q']}}]}


def call():
    return {'id':'fixture-call','name':'lookup','args':{'q':'fixture'}}


def payload(parts, finish='STOP'):
    return {'candidates':[{'finishReason':finish, 'content':{'role':'model','parts':parts}}],
            'usageMetadata':{'promptTokenCount':2,'candidatesTokenCount':3,'totalTokenCount':5}}


def outcomes(parts, finish='STOP'):
    body = payload(parts, finish)
    direct = GoogleTransport(timeout=1).parse_response(body, request=request())
    adapter = GoogleStreamEventAdapter(response_id='resp_fixture',display_model='fixture',request=request())
    events = [adapter.created(), *adapter.consume(body)]
    adapter.mark_done()
    events.extend(adapter.finish())
    final = next(event['response'] for event in events if isinstance(event,dict) and event.get('type') in
                 {'response.completed','response.failed','response.incomplete'})
    return direct, final, events, adapter


def semantic_output(items):
    text, reasoning, functions = [], [], []
    for item in items:
        if item['type'] == 'message': text.extend(part.get('text','') for part in item['content'])
        elif item['type'] == 'reasoning':
            reasoning.append(item.get('step_by_step_summary') or ''.join(part.get('text','') for part in item.get('summary',[])))
        elif item['type'] == 'function_call': functions.append((item['name'],json.loads(item['arguments']),item['call_id']))
    return ''.join(text), ''.join(reasoning), functions


@pytest.mark.parametrize('part,text,reasoning', [
    ({'text':'visible'},'visible',''),
    ({'text':'visible','thoughtSignature':'opaque-fixture-signature'},'visible',''),
    ({'thought':True,'text':'reasoning'},'','reasoning'),
    ({'type':'thinking','thinking':'legacy reasoning'},'','legacy reasoning'),
])
def test_mixed_text_or_thought_and_call_have_identical_semantics(part, text, reasoning):
    part['functionCall'] = call()
    direct, streamed, events, _ = outcomes([part])
    expected = (text,reasoning,[('lookup',{'q':'fixture'},'fixture-call')])
    assert direct.terminal.kind is TerminalKind.COMPLETED and streamed['status'] == 'completed'
    assert semantic_output(direct.output) == semantic_output(streamed['output']) == expected
    assert direct.usage == streamed['usage'] == {'input_tokens':2,'output_tokens':3,'total_tokens':5}
    assert 'opaque-fixture-signature' not in json.dumps(streamed)
    calls = [event for event in events if isinstance(event,dict) and event.get('type') == 'response.output_item.done'
             and event['item']['type'] == 'function_call']
    assert len(calls) == 1


@pytest.mark.parametrize('media', [
    {'inlineData':{'mimeType':'image/png','data':'fixture-base64-content'}},
    {'inlineData':{'mimeType':'audio/wav','data':'fixture-base64-content'}},
    {'fileData':{'mimeType':'video/mp4','fileUri':'https://example.invalid/private-output'}},
    {'inlineData':None}, {'inlineData':{}}, {'videoMetadata':{}}, {'mediaResolution':{}},
])
@pytest.mark.parametrize('mixed', [False, True])
def test_unsupported_media_fails_specifically_and_preserves_supported_fields(media, mixed, monkeypatch):
    decode = Mock(side_effect=AssertionError('media must not be decoded'))
    monkeypatch.setattr(base64, 'b64decode', decode)
    part = {**media, **({'text':'usable','functionCall':call()} if mixed else {})}
    original = deepcopy(part)
    direct, streamed, events, adapter = outcomes([part])
    assert direct.terminal.kind is TerminalKind.FAILED and streamed['status'] == 'failed'
    assert direct.terminal.error_code == streamed['error']['code'] == 'unsupported_output_modality'
    expected = ('usable','',[('lookup',{'q':'fixture'},'fixture-call')]) if mixed else ('','',[])
    assert semantic_output(direct.output) == semantic_output(streamed['output']) == expected
    assert 'fixture-base64-content' not in json.dumps(streamed) and 'private-output' not in json.dumps(streamed)
    assert part == original and adapter.visible_output_started
    with pytest.raises(RuntimeError, match='cannot reset'): adapter.reset_attempt()
    decode.assert_not_called()


def test_unsupported_media_keeps_provider_incomplete_reason():
    direct, streamed, _, _ = outcomes([{'text':'usable','inlineData':{}}], finish='MAX_TOKENS')
    assert direct.terminal.kind is TerminalKind.INCOMPLETE and streamed['status'] == 'incomplete'
    assert streamed['error']['code'] == 'unsupported_output_modality'
    assert streamed['incomplete_details']['reason'] == 'max_output_tokens'
    assert semantic_output(streamed['output'])[0] == 'usable'


@pytest.mark.parametrize('extra,code', [
    ({'text':None},'malformed_output_part'), ({'text':['bad']},'malformed_output_part'),
    ({'text':'\ud800'},'malformed_output_part'), ({'unknownOutput':{'fixture':'private'}},'unsupported_output_part'),
    ({'executableCode':{'code':'must not execute'}},'unsupported_output_part'),
    ({'functionResponse':{'name':'lookup','response':{}}},'unsupported_output_part'),
])
def test_bad_or_unmapped_fields_do_not_silently_erase_valid_calls(extra, code):
    direct, streamed, _, _ = outcomes([{**extra,'functionCall':call()}])
    assert direct.terminal.error_code == streamed['error']['code'] == code
    assert semantic_output(direct.output)[2] == semantic_output(streamed['output'])[2] == [('lookup',{'q':'fixture'},'fixture-call')]


def test_stream_normalizes_each_part_once_and_preserves_function_identity(monkeypatch):
    from codex_antigravity_auth import google_transport
    original = google_transport.normalize_google_part
    seen = []
    def normalize(part, validator):
        seen.append(part)
        return original(part, validator)
    monkeypatch.setattr(google_transport,'normalize_google_part',normalize)
    adapter = GoogleStreamEventAdapter(response_id='resp_fixture',display_model='fixture',request=request())
    adapter.created()
    adapter.consume(payload([{'text':'visible','functionCall':call()}]))
    adapter.mark_done()
    adapter.finish()
    assert len(seen) == 1


@pytest.mark.parametrize('stream', [False,True])
@pytest.mark.parametrize('model', ['gemini-3.1-flash-image','openai:gemini-3.1-flash-image','overlay-image'])
def test_known_image_generation_is_rejected_before_accounts_and_http(model, stream, monkeypatch):
    if model == 'overlay-image':
        models.save_model_overlays([models.NativeModel('overlay-image','gemini-3.1-flash-image','Fixture',1000,'gemini')])
    acquire = Mock(side_effect=AssertionError('no account acquisition'))
    client = Mock(side_effect=AssertionError('no HTTP client'))
    monkeypatch.setattr(server.account_manager,'acquire_account',acquire)
    monkeypatch.setattr(server.httpx,'AsyncClient',client)
    result = TestClient(server.app).post('/v1/responses',json={'model':model,'input':'fixture','stream':stream})
    assert result.status_code == 400 and result.json()['detail']['code'] == 'unsupported_output_modality'
    acquire.assert_not_called(); client.assert_not_called()


def test_direct_transport_also_refuses_known_image_generation_before_client():
    factory = Mock(side_effect=AssertionError('no HTTP client'))
    transport = GoogleTransport(timeout=1,client_factory=factory)
    lease = AccountLease('fixture@example.invalid','fixture-project','fixture-only-token')
    async def run():
        with pytest.raises(ValueError, match='unsupported_output_modality'):
            await transport.post({'model':'gemini-3.1-flash-image'},lease)
        with pytest.raises(ValueError, match='unsupported_output_modality'):
            async with transport.stream({'model':'gemini-3.1-flash-image'},lease): pass
    asyncio.run(run())
    factory.assert_not_called()


def test_unsupported_generation_is_absent_from_gateway_and_standalone_catalogs(monkeypatch):
    monkeypatch.setattr(server,'all_provider_configs_read_only',lambda:{})
    model = models.native_model_definition('gemini-3.1-flash-image')
    assert model is not None
    description = native_contract(model)
    assert description['gateway_generation'] == {'supported':False,'required_output_bridge':'image'}
    assert description['effective']['output_types'] == []
    assert model.id not in {entry['id'] for entry in TestClient(server.app).get('/v1/models').json()['data']}
    assert model.id not in {entry['id'] for entry in standalone_snapshot()['data']}
    assert models.native_model_capabilities('gemini-3.8-flash').input_modalities == frozenset({'text','image'})


@pytest.mark.parametrize('stream', [False,True])
def test_actual_google_route_reports_media_failure_without_retry(stream, monkeypatch):
    body = payload([{'text':'usable','functionCall':call(),'inlineData':{'mimeType':'image/png','data':'private-fixture-image'}}])
    wire = frames(body,'[DONE]') if stream else json.dumps(body).encode()
    with upstream((200, {'Content-Type':'text/event-stream' if stream else 'application/json'},wire)) as (base, seen):
        real = GoogleTransport
        monkeypatch.setattr(server,'GoogleTransport',lambda **kwargs:real(endpoint=base,**kwargs))
        monkeypatch.setattr(server,'acquire_active_account_for_request',AsyncMock(return_value={
            'email':'fixture@example.invalid','accessToken':'fixture-only','projectId':'fixture'}))
        monkeypatch.setattr(server,'record_attempt_outcome',AsyncMock())
        monkeypatch.setattr(server,'release_account_for_request',AsyncMock())
        response = TestClient(server.app).post('/v1/responses',json={**request(),'stream':stream})
    assert response.status_code == 200 and len(seen) == 1
    if stream:
        events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ') and line != 'data: [DONE]']
        result = next(event['response'] for event in events if event['type'] == 'response.failed')
    else: result = response.json()
    assert result['status'] == 'failed' and result['error']['code'] == 'unsupported_output_modality'
    assert semantic_output(result['output']) == ('usable','',[('lookup',{'q':'fixture'},'fixture-call')])
    assert 'private-fixture-image' not in response.text


@pytest.mark.parametrize('reverse', [False, True])
@pytest.mark.parametrize('first', [{'unknownOutput': {}}, {'text': None}])
def test_media_error_has_stable_precedence_across_parts_and_chunks(first, reverse):
    parts = [first, {'text': 'usable', 'inlineData': {}}]
    if reverse: parts.reverse()
    direct, streamed, _, _ = outcomes(parts)
    assert direct.terminal.error_code == streamed['error']['code'] == 'unsupported_output_modality'
    bodies = [payload([part]) for part in parts]
    adapter = GoogleStreamEventAdapter(response_id='resp_fixture', display_model='fixture', request=request())
    adapter.created()
    for body in bodies: adapter.consume(body)
    adapter.mark_done()
    final = next(event['response'] for event in adapter.finish() if isinstance(event, dict) and event.get('type') == 'response.failed')
    assert direct.terminal.error_code == final['error']['code'] == 'unsupported_output_modality'
    assert semantic_output(direct.output)[0] == semantic_output(final['output'])[0] == 'usable'


@pytest.mark.parametrize('bad', [None, {}, 'invalid', 17])
@pytest.mark.parametrize('reverse', [False, True])
def test_malformed_parts_container_preserves_siblings_and_fails(bad, reverse):
    bodies = [payload([{'text':'usable'}]), payload(bad)]
    if reverse: bodies.reverse()
    direct = GoogleTransport(timeout=1).parse_response(payload(bad), request=request())
    adapter = GoogleStreamEventAdapter(response_id='resp_fixture',display_model='fixture',request=request())
    adapter.created()
    for body in bodies: adapter.consume(body)
    adapter.mark_done()
    final = next(event['response'] for event in adapter.finish() if isinstance(event,dict) and event.get('type') == 'response.failed')
    assert direct.terminal.error_code == final['error']['code'] == 'malformed_output_part'
    assert semantic_output(direct.output)[0] == ''
    assert semantic_output(final['output'])[0] == 'usable'
    with pytest.raises(RuntimeError, match='cannot reset'): adapter.reset_attempt()


@pytest.mark.parametrize('nonprimary', [[{'inlineData': {}}], None])
def test_unsupported_nonprimary_alternative_cannot_contaminate_selected_output(nonprimary):
    body = payload([{'text': 'selected'}])
    body['candidates'][0]['index'] = 0
    body['candidates'].append({'index': 1, 'finishReason': 'STOP', 'content': {'parts': nonprimary}})
    direct = GoogleTransport(timeout=1).parse_response(body, request=request())
    adapter = GoogleStreamEventAdapter(response_id='resp_fixture', display_model='fixture', request=request())
    adapter.created()
    adapter.consume(body)
    adapter.mark_done()
    streamed = next(event['response'] for event in adapter.finish()
                    if isinstance(event, dict) and event.get('type') == 'response.completed')
    assert direct.terminal.kind is TerminalKind.COMPLETED
    assert semantic_output(direct.output) == semantic_output(streamed['output']) == ('selected', '', [])
