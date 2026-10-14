"""Boundary shape/linkage failures and retained native wire payloads; synthetic only."""
from copy import deepcopy
import json
from unittest.mock import Mock

from fastapi.testclient import TestClient
import pytest

from codex_antigravity_auth import server
from codex_antigravity_auth.request_shapes import validate_request_shapes
from codex_antigravity_auth.transform import transform_request, transform_request_to_chat
from codex_antigravity_auth.unified import OpenAIAuth
from fake_upstream import upstream


BAD_COMMON = [
    ({'input': 12}, 'input'),
    ({'input': ['fixture']}, 'input[0]'),
    ({'input': [{'role':'user'}]}, 'input[0].content'),
    ({'input': [{'role':'user','content':None}]}, 'input[0].content'),
    ({'input': [{'role':'tool','content':'fixture'}]}, 'input[0].role'),
    ({'input': [{'role':{},'content':'fixture'}]}, 'input[0].role'),
    ({'input': [{'role':'user','content':[42]}]}, 'input[0].content[0]'),
    ({'input': [{'role':'user','content':[{'type':'input_text'}]}]}, 'input[0].content[0].text'),
    ({'input': [{'role':'user','content':[{'type':'input_text','text':7}]}]}, 'input[0].content[0].text'),
    ({'input': [{'type':'function_call','name':'fixture','arguments':'{}'}]}, 'input[0].call_id'),
    ({'input': [{'type':'function_call','call_id':'fixture','arguments':'{}'}]}, 'input[0].name'),
    ({'input': [{'type':'function_call_output','call_id':'fixture'}]}, 'input[0].output'),
    ({'tools': {}}, 'tools'),
    ({'tools': None}, 'tools'),
    ({'tools': ['fixture']}, 'tools[0]'),
    ({'tools': [{'type':'function','name':'bad name'}]}, 'tools[0].name'),
    ({'tools': [{'type':'function','function':[]}]}, 'tools[0].function'),
    ({'tools': [{'type':'function','name':'fixture','parameters':[]}]}, 'tools[0].parameters'),
    ({'tools': [{'type':'function','name':'fixture','description':12}]}, 'tools[0].description'),
    ({'tools': [{'type':'function','name':'fixture','strict':'true'}]}, 'tools[0].strict'),
    ({'tools': [{'type':'function','name':'fixture','parameters':{'required':'q'}}]}, 'required'),
    ({'tools': [{'type':'function','name':'fixture','parameters':{'properties':[]}}]}, 'properties'),
    ({'tools': [{'type':'function','name':'fixture','parameters':{'allOf':{}}}]}, 'allOf'),
]


@pytest.mark.parametrize('model', ['gemini-3.8-flash','fixture:model','gpt-5.6'])
@pytest.mark.parametrize('fields,path', BAD_COMMON)
def test_malformed_shapes_fail_before_any_account_provider_or_auth_work(monkeypatch, model, fields, path):
    monkeypatch.setattr(server, 'is_unified_mode_enabled', lambda: True)
    guards = []
    for owner, name in [(server.account_manager,'acquire_account'), (server,'all_provider_configs'), (server,'resolve_openai_auth')]:
        guard = Mock(side_effect=AssertionError('account/config/auth must not run'))
        guards.append(guard); monkeypatch.setattr(owner, name, guard)
    response = TestClient(server.app).post('/v1/responses', json={'model':model, **fields})
    assert response.status_code == 400 and path in response.json()['detail']
    for guard in guards: guard.assert_not_called()


@pytest.mark.parametrize('model', ['gemini-3.8-flash','fixture:model'])
@pytest.mark.parametrize('fields,path', [
    ({'tools':[{'type':'web_search_preview'}]}, 'tools[0].type'),
    ({'tools':[{'type':'custom','name':'fixture'}]}, 'tools[0].type'),
    ({'input':[{'type':'function_call_output','call_id':'orphan','output':'fixture'}]}, 'input[0].call_id'),
    ({'input':[{'type':'message','role':'user','content':[{'type':'tool_result','tool_use_id':'orphan','content':'fixture'}]}]}, 'content[0].call_id'),
    ({'input':[{'type':'function_call','call_id':'fixture','name':'fixture','arguments':'not-json'}]}, 'arguments'),
    ({'input':[{'type':'function_call','call_id':'fixture','name':'fixture','arguments':'[]'}]}, 'arguments'),
    ({'input':[{'type':'function_call','call_id':'fixture','name':'fixture','arguments':'{"q":1,"q":2}'}]}, 'arguments'),
])
def test_translated_routes_reject_unhonored_tools_and_orphans_before_config(monkeypatch, model, fields, path):
    guard = Mock(side_effect=AssertionError('no account/config lookup'))
    monkeypatch.setattr(server.account_manager, 'acquire_account', guard)
    monkeypatch.setattr(server, 'all_provider_configs', guard)
    response = TestClient(server.app).post('/v1/responses', json={'model':model, **fields})
    assert response.status_code == 400 and path in response.json()['detail']
    guard.assert_not_called()


def conversation():
    return [
        {'type':'function_call','name':'first','call_id':'call_first','arguments':'{"q":"fixture"}'},
        {'type':'function_call','name':'second','call_id':'call_second','arguments':{'n':2}},
        {'type':'function_call_output','call_id':'call_first','output':'first result'},
        {'type':'function_call_output','call_id':'call_second','output':{'value':2}},
    ]


def test_valid_parallel_tool_history_preserves_call_names_ids_and_results():
    request = {'model':'gemini-3.8-flash','input':conversation(), 'tools':[
        {'type':'function','name':'first','parameters':{'type':'object','properties':{'q':{'type':'string'}},'required':['q']}},
        {'type':'function','function':{'name':'second','parameters':{'type':'object','properties':{'n':{'type':'integer'}},'required':['n']}}},
    ]}
    before = deepcopy(request)
    google = transform_request(request)['request']
    parts = [part for item in google['contents'] for part in item['parts']]
    assert [part['functionCall']['name'] for part in parts if 'functionCall' in part] == ['first','second']
    assert [part['functionResponse']['name'] for part in parts if 'functionResponse' in part] == ['first','second']
    chat = transform_request_to_chat(request, 'fixture')['messages']
    assert [call['id'] for call in chat[0]['tool_calls']] == ['call_first','call_second']
    assert [row['tool_call_id'] for row in chat[1:]] == ['call_first','call_second']
    assert request == before


@pytest.mark.parametrize('change,path', [('duplicate_call','input[1].call_id'), ('duplicate_result','input[4].call_id'),
                                        ('name_mismatch','input[2].name')])
def test_ambiguous_tool_history_is_rejected(change, path):
    items = conversation()
    if change == 'duplicate_call': items[1]['call_id'] = 'call_first'
    elif change == 'duplicate_result': items.append(dict(items[2]))
    else: items[2]['name'] = 'second'
    for route in ('google','byok'):
        with pytest.raises(ValueError) as caught:
            validate_request_shapes({'input':items}, route=route)
        assert path in str(caught.value)


@pytest.mark.parametrize('keyword,value', [('additionalProperties',False), ('minLength',2), ('pattern','fixture'),
    ('const','fixture'), ('$ref','#/$defs/fixture'), ('format','email')])
def test_google_schema_weakening_is_an_explicit_pre_account_error(monkeypatch, keyword, value):
    request = {'model':'gemini-3.8-flash','input':'fixture','tools':[{'type':'function','name':'fixture',
               'parameters':{'type':'object','properties':{'q':{'type':'string',keyword:value}},'required':['q']}}]}
    guard = Mock(side_effect=AssertionError('no account'))
    monkeypatch.setattr(server.account_manager, 'acquire_account', guard)
    response = TestClient(server.app).post('/v1/responses', json=request)
    assert response.status_code == 400
    assert keyword in response.json()['detail'] and 'translation_loss' in response.json()['detail']
    guard.assert_not_called()


def test_google_strict_is_not_misrepresented_as_implemented(monkeypatch):
    guard = Mock(side_effect=AssertionError('no account'))
    monkeypatch.setattr(server.account_manager, 'acquire_account', guard)
    response = TestClient(server.app).post('/v1/responses', json={'model':'gemini-3.8-flash','input':'fixture',
        'tools':[{'type':'function','name':'fixture','strict':True,'parameters':{}}]})
    assert response.status_code == 400 and 'tools[0].strict: translation_loss' in response.json()['detail']
    guard.assert_not_called()


def test_native_builtin_items_tools_choices_and_linked_continuation_reach_wire_unchanged(monkeypatch):
    request = {'model':'gpt-5.6','previous_response_id':'resp_previous_fixture', 'input':[
        {'type':'item_reference','id':'item_fixture'},
        {'type':'web_search_call','id':'search_fixture','status':'completed','action':{'type':'search','query':'fixture'}},
        {'type':'custom_tool_call','id':'custom_fixture','call_id':'custom_call','name':'fixture','input':'opaque syntax'},
        {'type':'function_call_output','call_id':'prior_fixture_call','output':'result from prior response'},
        {'role':'assistant','content':[{'type':'refusal','refusal':'fixture refusal'}]},
    ], 'tools':[{'type':'web_search_preview'}], 'tool_choice':{'type':'web_search_preview'}}
    result = {'id':'resp_fixture','status':'completed','output':[{'type':'message','role':'assistant','content':[{'type':'output_text','text':'fixture'}]}]}
    with upstream((200, {'Content-Type':'application/json'}, json.dumps(result).encode())) as (base, requests):
        monkeypatch.setattr(server, 'is_unified_mode_enabled', lambda: True)
        monkeypatch.setattr(server, 'resolve_openai_auth', lambda: OpenAIAuth(kind='api_key', base_url=base, api_key='fixture-only'))
        response = TestClient(server.app).post('/v1/responses', json=request)
    assert response.status_code == 200
    assert requests[0]['body'] == {**request, 'stream':False}


def test_byok_preserves_constraint_schema_and_strict_flag_on_wire(monkeypatch):
    request = {'model':'fixture:model', 'input':conversation(), 'tools':[{'type':'function','name':'fixture','strict':True,
        'parameters':{'type':'object','properties':{'q':{'type':'string','minLength':2}},'additionalProperties':False}}]}
    result = {'choices':[{'message':{'content':'fixture'},'finish_reason':'stop'}]}
    with upstream((200, {'Content-Type':'application/json'}, json.dumps(result).encode())) as (base, requests):
        provider = {'id':'fixture','kind':'openai_chat','baseUrl':base,'apiKey':'fixture-only','models':['model']}
        monkeypatch.setattr(server, 'all_provider_configs', lambda: {'fixture':provider})
        response = TestClient(server.app).post('/v1/responses', json=request)
    assert response.status_code == 200
    sent = requests[0]['body']['tools'][0]['function']
    assert sent['strict'] is True and sent['parameters'] == request['tools'][0]['parameters']


def test_nested_boolean_schema_is_preserved_for_native_and_byok():
    request = {'tools':[{'type':'function','name':'fixture','parameters':{'type':'object','properties':{'denied':False}}}]}
    validate_request_shapes(request, route='native')
    validate_request_shapes(request, route='byok')
    with pytest.raises(ValueError, match='boolean schemas'):
        validate_request_shapes(request, route='google')


def test_bounded_shape_validation_rejects_cycles_and_excessive_depth():
    value = {}; value['self'] = value
    with pytest.raises(ValueError, match='depth/node'):
        validate_request_shapes({'input':value})


@pytest.mark.parametrize('part,path', [
    ({'type':'tool_use','id':'fixture','call_id':'different','name':'fixture','input':{}}, 'call_id'),
    ({'type':'tool_result','tool_use_id':'fixture','call_id':'different','content':'fixture'}, 'call_id'),
    ({'type':'tool_result','tool_use_id':'fixture','content':'first','output':'second'}, 'output'),
])
def test_conflicting_legacy_tool_aliases_cannot_select_different_meanings(part, path):
    role = 'assistant' if part['type'] == 'tool_use' else 'user'
    with pytest.raises(ValueError) as caught:
        validate_request_shapes({'input':[{'role':role, 'content':[part]}]}, route='google')
    assert path in str(caught.value) and 'conflicting' in str(caught.value)


def test_conflicting_tool_choice_spellings_are_rejected():
    with pytest.raises(ValueError, match='conflicting'):
        validate_request_shapes({'tool_choice':{'type':'function','name':'one','function':{'name':'two'}}})


def test_native_optional_function_fields_are_carried_without_translation():
    request = {'tools':[{'type':'function','name':'fixture','description':None,'strict':None,'parameters':None}]}
    before = deepcopy(request)
    validate_request_shapes(request, route='native')
    assert request == before


@pytest.mark.parametrize('nested', [False, True])
def test_google_empty_name_requirement_is_not_dropped_before_dispatch(monkeypatch, nested):
    schema = {'type':'object','properties':{'':{'type':'string'}},'required':['']}
    if nested:
        schema = {'type':'object','properties':{'outer':schema},'required':['outer']}
    request = {'model':'gemini-3.8-flash','input':'fixture','tools':[
        {'type':'function','name':'fixture','parameters':schema}]}
    guard = Mock(side_effect=AssertionError('no account work'))
    monkeypatch.setattr(server.account_manager, 'acquire_account', guard)
    response = TestClient(server.app).post('/v1/responses', json=request)
    assert response.status_code == 400 and 'required[0]: translation_loss' in response.json()['detail']
    if nested:
        assert 'properties["outer"]' in response.json()['detail']
    guard.assert_not_called()
    for route in ('native','byok'):
        before = deepcopy(request)
        validate_request_shapes(request, route=route)
        assert request == before


@pytest.mark.parametrize('model', ['gemini-3.8-flash', 'fixture:model'])
@pytest.mark.parametrize('choice,path', [
    ({'type':'function','function':{'name':'lookup','extra':'fixture'}}, 'tool_choice.function'),
    ({'type':'function','function':{'name':'lookup'},'extra':'fixture'}, 'tool_choice'),
    ({'type':'function','name':'lookup','extra':'fixture'}, 'tool_choice'),
])
def test_translated_tool_choices_cannot_drop_extra_fields_before_account_work(monkeypatch, model, choice, path):
    guard = Mock(side_effect=AssertionError('no account/config work'))
    monkeypatch.setattr(server.account_manager, 'acquire_account', guard)
    monkeypatch.setattr(server, 'all_provider_configs', guard)
    request = {'model':model,'input':'fixture','tools':[{'type':'function','name':'lookup'}],'tool_choice':choice}
    response = TestClient(server.app).post('/v1/responses', json=request)
    assert response.status_code == 400 and path in response.json()['detail']
    guard.assert_not_called()
    before = deepcopy(request)
    validate_request_shapes(request, route='native')
    assert request == before


@pytest.mark.parametrize('choice', [{'type':'function','name':'lookup'}, {'type':'function','function':{'name':'lookup'}}])
def test_supported_flat_and_nested_choices_still_translate(choice):
    request = {'model':'gemini-3.8-flash','input':'fixture','tools':[{'type':'function','name':'lookup'}],'tool_choice':choice}
    google = transform_request(request)['request']['toolConfig']['functionCallingConfig']
    chat = transform_request_to_chat(request, 'fixture')['tool_choice']
    assert google['allowedFunctionNames'] == ['lookup']
    assert chat == {'type':'function','function':{'name':'lookup'}}
