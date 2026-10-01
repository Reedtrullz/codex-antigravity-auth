"""Synthetic finalized-call contracts; no functions are executed by these tests."""
import asyncio
from copy import deepcopy
from decimal import Decimal
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from codex_antigravity_auth import server
from codex_antigravity_auth.google_transport import GoogleTransport, GoogleResponseAccumulator, GoogleStreamEventAdapter
from codex_antigravity_auth.openai_transport import OpenAICompatibleTransport, NativeResponsesStreamAdapter, ChatResponseAccumulator
from codex_antigravity_auth.response_protocol import TerminalKind
from codex_antigravity_auth.tool_calls import FunctionCallValidator, ToolCallError, native_response
from codex_antigravity_auth.transform import transform_chat_response, transform_request, clean_function_call_args
from codex_antigravity_auth.unified import OpenAIAuth
from fake_upstream import upstream, frames, split_bytes


def request(schema=None, *, name='lookup', namespace=None):
    tool = {'type':'function','name':name,'parameters':schema if schema is not None else {
        'type':'object','properties':{'q':{'type':'string'}},'required':['q']}}
    return {'input':'fixture', 'tools':[tool] if namespace is None else [{'type':'namespace','name':namespace,'tools':[tool]}]}


def text_item():
    return {'type':'message','id':'msg_fixture','role':'assistant','status':'completed',
            'content':[{'type':'output_text','text':'usable sibling hé🙂'}]}


def function_item(arguments='{"q":"fixture"}', *, name='lookup', call_id='call_fixture', namespace=None):
    item = {'type':'function_call','id':'fc_' + call_id,'call_id':call_id,'name':name,'arguments':arguments,'status':'completed'}
    if namespace is not None: item['namespace'] = namespace
    return item


def response(items, status='completed'):
    value = {'id':'resp_fixture','status':status,'model':'fixture','output':items,
             'usage':{'input_tokens':1,'output_tokens':2,'total_tokens':3}}
    if status == 'incomplete': value['incomplete_details'] = {'reason':'max_output_tokens'}
    return value


def events(items, *, status='completed'):
    result = [{'type':'response.created','response':{'id':'resp_fixture','status':'in_progress','output':[]}}]
    for index, item in enumerate(items):
        added = deepcopy(item)
        if item['type'] == 'function_call': added['arguments'] = ''
        result.append({'type':'response.output_item.added','output_index':index,'item':added})
        if item['type'] == 'function_call':
            result.append({'type':'response.function_call_arguments.done','output_index':index,
                           'item_id':item['id'],'arguments':item['arguments']})
        result.append({'type':'response.output_item.done','output_index':index,'item':item})
    result.append({'type':'response.' + status,'response':response(items, status)})
    return result


def collect_native(values, req=None):
    adapter = NativeResponsesStreamAdapter(display_model='fixture', request=req)
    emitted = []
    for chunk in split_bytes(frames(*values), seed=2):
        emitted += adapter.consume_bytes(chunk)
    emitted += adapter.finish()
    return emitted


@pytest.mark.parametrize('arguments', ['{', '[]', 'null', '42', 'true', '"text"', '', '{"q":NaN}', '{"q":1,"q":2}', {'q':'fixture'}])
def test_native_invalid_final_arguments_preserve_sibling_and_never_complete_bad_call(arguments):
    items = [function_item(arguments), text_item()]
    req = request()
    direct = native_response(response(items), display_model='fixture', validator=FunctionCallValidator(req))
    assert direct['status'] == 'failed' and direct['output'] == [text_item()]
    emitted = collect_native(events(items), req)
    assert emitted[-1]['type'] == 'response.failed'
    assert emitted[-1]['response']['output'] == [text_item()]
    assert not any(event['type'] == 'response.function_call_arguments.done' for event in emitted)
    assert not any(event['type'] == 'response.output_item.done' and event['item']['type'] == 'function_call' for event in emitted)


@pytest.mark.parametrize('arguments', ['{', '[]', '', '{"q":NaN}'])
def test_partial_function_arguments_remain_incomplete_with_explicit_error(arguments):
    items = [function_item(arguments), text_item()]
    direct = native_response(response(items, 'incomplete'), display_model='fixture', validator=FunctionCallValidator(request()))
    emitted = collect_native(events(items, status='incomplete'), request())
    assert direct['status'] == emitted[-1]['response']['status'] == 'incomplete'
    assert emitted[-1]['response']['output'] == [text_item()]
    assert emitted[-1]['response']['incomplete_details']['reason'] == 'max_output_tokens'
    assert emitted[-1]['response']['error']['code'] == 'incomplete_function_arguments'
    assert not any(event['type'] == 'response.function_call_arguments.done' for event in emitted)


def test_partial_deltas_are_not_validated_as_complete_json_and_valid_events_replay():
    item = function_item()
    values = events([item, text_item()])
    values[2:2] = [{'type':'response.function_call_arguments.delta','output_index':0,'item_id':item['id'],'delta':value}
                   for value in ('{', '"q":', '"fixture"', '}')]
    emitted = collect_native(values, request())
    assert emitted[-1]['response']['status'] == 'completed'
    assert emitted[-1]['response']['output'] == [item, text_item()]
    assert [event for event in emitted if event['type'].endswith('arguments.delta')] == values[2:6]


def test_bad_tool_followed_by_done_snapshots_without_terminal_output_keeps_sibling():
    items = [function_item('{'), text_item()]
    values = events(items)
    values[-1]['response']['output'] = []
    emitted = collect_native(values, request())
    assert emitted[-1]['response']['status'] == 'failed'
    assert emitted[-1]['response']['output'] == [text_item()]


def test_finalized_argument_snapshot_cannot_change_after_arguments_done():
    item = function_item()
    values = events([item, text_item()])
    values[2]['arguments'] = '{"q":"different"}'
    result = collect_native(values, request())[-1]['response']
    assert result['status'] == 'failed' and result['output'] == [text_item()]
    assert result['error']['code'] == 'conflicting_function_arguments'


def test_out_of_order_nonfunction_slot_is_held_so_failure_can_retain_coherent_siblings():
    bad, good = function_item('{'), text_item()
    values = [{'type':'response.output_item.added','output_index':1,'item':good},
              {'type':'response.output_item.done','output_index':1,'item':good},
              *events([bad])[1:-1], {'type':'response.completed','response':response([bad,good])}]
    emitted = collect_native(values, request())
    assert len(emitted) == 1 and emitted[0]['response']['output'] == [good]


@pytest.mark.parametrize('namespace', [None, 'crm'])
def test_declared_identity_schema_and_namespace_are_checked(namespace):
    validator = FunctionCallValidator(request(namespace=namespace))
    assert validator.arguments('lookup', '{"q":"fixture"}', namespace=namespace) == '{"q":"fixture"}'
    for name, args, ns, code in [('other','{}',namespace,'undeclared_function'),
                                  ('lookup','{"q":2}',namespace,'function_schema_mismatch'),
                                  ('lookup','{}',namespace,'function_schema_mismatch'),
                                  ('lookup','{"q":"fixture"}','other','undeclared_function')]:
        with pytest.raises(ToolCallError) as caught: validator.arguments(name,args,namespace=ns)
        assert caught.value.code == code


def test_schema_constraints_do_not_coerce_boolean_to_integer_or_ignore_nested_rules():
    schema = {'type':'object','required':['n','values'],'additionalProperties':False,'properties':{
        'n':{'type':'integer','minimum':1,'maximum':3},
        'values':{'type':'array','minItems':1,'maxItems':2,'items':{'type':'string','enum':['a','b']}}}}
    validator = FunctionCallValidator(request(schema))
    assert validator.arguments('lookup','{"n":2,"values":["a"]}')
    for value in ({'n':True,'values':['a']},{'n':0,'values':['a']},{'n':4,'values':['a']},
                  {'n':2,'values':[]},{'n':2,'values':['c']},{'n':2,'values':['a'],'extra':1}):
        with pytest.raises(ToolCallError, match='invalid or unsupported'):
            validator.arguments('lookup',json.dumps(value))


@pytest.mark.parametrize('bound,value,accepted', [
    ({'minimum':0.1}, '0.1', True), ({'maximum':0.3}, '0.3', True),
    ({'minimum':0.1}, '0.099999999999999999999', False),
    ({'maximum':0.3}, '0.300000000000000000001', False),
    ({'minimum':-0.3, 'maximum':-0.1}, '-0.3', True),
    ({'minimum':-0.3, 'maximum':-0.1}, '-0.1', True),
    ({'minimum':9007199254740993}, '9007199254740993', True),
    ({'minimum':9007199254740993}, '9007199254740992', False),
])
def test_numeric_bounds_compare_json_values_without_binary_float_rounding(bound, value, accepted):
    validator = FunctionCallValidator(request({'type':'object','properties':{'q':{'type':'number', **bound}},'required':['q']}))
    arguments = '{"q":' + value + '}'
    if accepted:
        assert validator.arguments('lookup', arguments) == arguments
    else:
        with pytest.raises(ToolCallError) as caught:
            validator.arguments('lookup', arguments)
        assert caught.value.code == 'function_schema_mismatch'


def test_local_refs_are_bounded_and_remote_refs_are_never_fetched():
    schema = {'type':'object','properties':{'q':{'$ref':'#/$defs/value'}},'$defs':{'value':{'type':'string','minLength':2}}}
    validator = FunctionCallValidator(request(schema))
    with pytest.raises(ToolCallError): validator.arguments('lookup','{"q":"x"}')
    assert validator.arguments('lookup','{"q":"xx"}')
    loop = FunctionCallValidator(request({'$ref':'#'}))
    with pytest.raises(ToolCallError) as caught: loop.arguments('lookup','{}')
    assert caught.value.code == 'tool_output_limit'
    remote = FunctionCallValidator(request({'$ref':'https://example.invalid/schema'}))
    assert remote.arguments('lookup','{}') == '{}'  # Documented unenforced remote reference, never a network fetch.


def test_placeholder_is_preserved_without_provenance_and_removed_only_for_injected_tool():
    ordinary = {'_placeholder':'legitimate','q':'fixture'}
    assert clean_function_call_args(ordinary) == ordinary
    schema = {'type':'object','properties':{'q':{'type':'number'}}}
    req = request(schema)
    google = FunctionCallValidator(req, route='google')
    raw = '{"_placeholder":true,"q":1.234567890123456789}'
    cleaned = google.arguments('lookup', raw)
    assert json.loads(cleaned, parse_float=Decimal) == {'q':Decimal('1.234567890123456789')}
    chat = FunctionCallValidator(req, route='byok')
    assert chat.arguments('lookup', raw) == raw
    explicit = request({'type':'object','properties':{'_placeholder':{'type':'string'}},'required':['_placeholder']})
    value = '{"_placeholder":"legitimate"}'
    assert FunctionCallValidator(explicit, route='google').arguments('lookup',value) == value


def test_placeholder_collision_is_rejected_before_account_lookup(monkeypatch):
    guard = Mock(side_effect=AssertionError('no account work'))
    monkeypatch.setattr(server.account_manager, 'acquire_account', guard)
    req = request({'type':'object','properties':{'_placeholder':{'type':'string'}}})
    result = TestClient(server.app).post('/v1/responses',json={**req,'model':'gemini-3.8-flash'})
    assert result.status_code == 400 and 'properties._placeholder' in result.json()['detail']
    guard.assert_not_called()


@pytest.mark.parametrize('bad', [None, [], 1, 'json', {'q':float('inf')}])
def test_google_bad_argument_shapes_fail_without_losing_text(bad):
    payload = {'candidates':[{'content':{'parts':[{'text':'usable sibling'},
        {'functionCall':{'name':'lookup','args':bad}}]},'finishReason':'STOP'}]}
    result = GoogleTransport(timeout=0).parse_response(payload, request=request())
    assert result.terminal.kind is TerminalKind.FAILED
    assert result.output[0]['content'][0]['text'] == 'usable sibling'
    assert not any(item['type'] == 'function_call' for item in result.output)


def test_google_partial_forms_never_become_completed_calls():
    accumulator = GoogleResponseAccumulator(tool_validator=FunctionCallValidator(request(), route='google'))
    for call in ({'name':'lookup','id':'fixture','args':{'q':'prefix'},'willContinue':True},
                 {'name':'lookup','id':'fixture','args':{'q':'suffix'}}):
        accumulator.consume({'candidates':[{'content':{'parts':[{'functionCall':call},{'text':'usable'}]},'finishReason':'STOP'}]})
    result = accumulator.finalize()
    assert result.terminal.kind is TerminalKind.FAILED
    assert result.terminal.error_code == 'unsupported_partial_function_call'
    assert not any(item['type'] == 'function_call' for item in result.output)


@pytest.mark.parametrize('partial', [[], [{'jsonPath':'$.q', 'stringValue':'prefix'}]])
@pytest.mark.parametrize('stream', [False, True])
def test_google_partial_args_presence_fails_and_preserves_independent_siblings(partial, stream):
    payload = {'candidates':[{'content':{'parts':[{'text':'usable'},
        {'functionCall':{'name':'lookup','id':'partial','args':{'q':'prefix'},'partialArgs':partial,'willContinue':False}},
        {'functionCall':{'name':'lookup','id':'partial','args':{'q':'suffix'}}},
        {'functionCall':{'name':'lookup','id':'good','args':{'q':'fixture'},'willContinue':False}}]},'finishReason':'STOP'}],
        'usageMetadata':{'promptTokenCount':1,'candidatesTokenCount':2,'totalTokenCount':3}}
    if stream:
        accumulator = GoogleResponseAccumulator(tool_validator=FunctionCallValidator(request(), route='google'))
        accumulator.consume(payload)
        result = accumulator.finalize()
    else:
        result = GoogleTransport(timeout=0).parse_response(payload, request=request())
    assert result.terminal.kind is TerminalKind.FAILED
    assert result.terminal.error_code == 'unsupported_partial_function_call'
    assert any(row['type'] == 'message' for row in result.output)
    assert [row['call_id'] for row in result.output if row['type'] == 'function_call'] == ['good']
    assert result.usage['total_tokens'] == 3


@pytest.mark.parametrize('separate_choices', [False, True])
@pytest.mark.parametrize('second_arguments', ['{"q":"other"}', '{'])
def test_chat_duplicate_call_ids_remove_all_ambiguous_calls_but_keep_siblings(separate_choices, second_arguments):
    calls = [{'id':'duplicate','function':{'name':'lookup','arguments':'{"q":"fixture"}'}},
             {'id':'duplicate','function':{'name':'lookup','arguments':second_arguments}},
             {'id':'good','function':{'name':'lookup','arguments':'{"q":"fixture"}'}}]
    groups = [[call] for call in calls] if separate_choices else [calls]
    payload = {'choices':[{'message':{'content':'usable','tool_calls':group},'finish_reason':'tool_calls'} for group in groups],
               'usage':{'prompt_tokens':1,'completion_tokens':2,'total_tokens':3}}
    result = OpenAICompatibleTransport(timeout=0).parse_chat_response(payload, request=request())
    assert result.terminal.kind is TerminalKind.FAILED
    assert result.terminal.error_code == 'conflicting_function_call'
    assert any(row['type'] == 'message' for row in result.output)
    assert [row['call_id'] for row in result.output if row['type'] == 'function_call'] == ['good']
    assert result.usage['total_tokens'] == 3


def test_chat_malformed_finished_arguments_do_not_erase_text_or_valid_sibling_calls():
    payload = {'choices':[{'message':{'content':'usable','tool_calls':[
        {'id':'bad','function':{'name':'lookup','arguments':'{'}},
        {'id':'good','function':{'name':'lookup','arguments':'{"q":"fixture"}'}}]},'finish_reason':'tool_calls'}]}
    result = OpenAICompatibleTransport(timeout=0).parse_chat_response(payload, request=request())
    assert result.terminal.kind is TerminalKind.FAILED
    assert result.output[0]['content'][0]['text'] == 'usable'
    assert [row['call_id'] for row in result.output if row['type'] == 'function_call'] == ['good']


def test_chat_unfinished_fragments_are_checked_only_at_terminal():
    accumulator = ChatResponseAccumulator(tool_validator=FunctionCallValidator(request()))
    for index, fragment in enumerate(('{"q":', '"fixture"', '}')):
        accumulator.consume({'choices':[{'delta':{'tool_calls':[{'index':0,'id':'fixture',
            'function':{'name':'lookup' if index == 0 else '', 'arguments':fragment}}]}}]})
    accumulator.consume({'choices':[{'delta':{},'finish_reason':'tool_calls'}]})
    result = accumulator.finalize()
    assert result.terminal.kind is TerminalKind.COMPLETED and result.output[0]['arguments'] == '{"q":"fixture"}'


def test_custom_native_tool_input_is_not_parsed_as_json():
    item = {'type':'custom_tool_call','id':'ct_fixture','call_id':'custom_fixture','name':'code_exec','input':'print("fixture")','status':'completed'}
    output = collect_native(events([item]), {'tools':[{'type':'custom','name':'code_exec'}]})[-1]['response']
    assert output['status'] == 'completed' and output['output'] == [item]


@pytest.mark.parametrize('route', ['google','byok','api_key','codex_oauth'])
@pytest.mark.parametrize('stream', [False,True])
@pytest.mark.parametrize('case', ['malformed','undeclared','schema','valid','minimum','maximum','outside_minimum','outside_maximum'])
def test_actual_routes_apply_original_function_contract_without_retries(monkeypatch, route, stream, case):
    from unittest.mock import AsyncMock
    req = request()
    name = 'other' if case == 'undeclared' else 'lookup'
    args = '{' if case == 'malformed' else '{"q":3}' if case == 'schema' else '{"q":"fixture"}'
    if case in {'minimum','maximum','outside_minimum','outside_maximum'}:
        req = request({'type':'object','properties':{'q':{'type':'number','minimum':0.1,'maximum':0.3}},'required':['q']})
        number = {'minimum':'0.1', 'maximum':'0.3', 'outside_minimum':'0.09', 'outside_maximum':'0.31'}[case]
        args = '{"q":' + number + '}'
    valid = case in {'valid','minimum','maximum'}
    native_items = [function_item(args,name=name),text_item()]
    if route == 'google':
        google_args = ['invalid'] if case == 'malformed' else json.loads(args)
        body = {'candidates':[{'content':{'parts':[{'text':'usable sibling'},
            {'functionCall':{'name':name,'args':google_args}}]},'finishReason':'STOP'}]}
        wire = frames(body,'[DONE]') if stream else json.dumps(body).encode()
    elif route == 'byok':
        if stream:
            body = {'choices':[{'index':0,'delta':{'content':'usable sibling','tool_calls':[{
                'index':0,'id':'fixture','function':{'name':name,'arguments':args}}]},'finish_reason':'tool_calls'}]}
            wire = frames(body,'[DONE]')
        else:
            body = {'choices':[{'message':{'content':'usable sibling','tool_calls':[{
                'id':'fixture','type':'function','function':{'name':name,'arguments':args}}]},'finish_reason':'tool_calls'}]}
            wire = json.dumps(body).encode()
    else:
        wire = frames(*events(native_items)) if stream or route == 'codex_oauth' else json.dumps(response(native_items)).encode()
    with upstream((200, {'Content-Type':'text/event-stream' if stream or route == 'codex_oauth' else 'application/json'}, wire)) as (base, captured):
        monkeypatch.setattr(server, 'record_attempt_outcome', AsyncMock())
        monkeypatch.setattr(server, 'release_account_for_request', AsyncMock())
        if route == 'google':
            real = GoogleTransport
            monkeypatch.setattr(server, 'GoogleTransport', lambda **kwargs: real(endpoint=base, **kwargs))
            monkeypatch.setattr(server, 'acquire_active_account_for_request', AsyncMock(return_value={
                'email':'fixture@example.invalid','accessToken':'fixture-only','projectId':'fixture'}))
            model = 'gemini-3.8-flash'
        elif route == 'byok':
            monkeypatch.setattr(server, 'all_provider_configs', lambda: {'fixture':{'id':'fixture','baseUrl':base,
                'kind':'openai_chat','apiKey':'fixture-only','models':['model']}})
            model = 'fixture:model'
        else:
            monkeypatch.setattr(server, 'is_unified_mode_enabled', lambda: True)
            monkeypatch.setattr(server, 'resolve_openai_auth', lambda: OpenAIAuth(kind=route,base_url=base,
                api_key='fixture-only',access_token='fixture-only'))
            monkeypatch.setattr(server, 'openai_responses_url', lambda auth: base + '/responses')
            model = 'gpt-5.6'
        result = TestClient(server.app).post('/v1/responses',json={**req,'model':model,'stream':stream})
    assert result.status_code == 200 and len(captured) == 1
    if stream:
        emitted = [json.loads(line[6:]) for line in result.text.splitlines() if line.startswith('data: ') and line != 'data: [DONE]']
        final = [event['response'] for event in emitted if event['type'] in {'response.completed','response.failed','response.incomplete'}][-1]
    else:
        final = result.json()
    assert final['status'] == ('completed' if valid else 'failed')
    assert any(item['type'] == 'message' for item in final['output'])
    calls = [item for item in final['output'] if item['type'] == 'function_call']
    assert bool(calls) == valid
    if stream and not valid:
        assert not any(event['type'] == 'response.function_call_arguments.done' for event in emitted)


def test_oversized_numeric_literals_and_pointer_expansion_are_bounded():
    validator = FunctionCallValidator()
    with pytest.raises(ToolCallError): validator.arguments('lookup','{"n":' + '9' * 1025 + '}')
    with pytest.raises(ToolCallError): validator.arguments('lookup',{'n':2**4000},object_allowed=True)
    reference = '#/' + '/'.join(['part'] * 100)
    validator = FunctionCallValidator(request({'$ref':reference}))
    with pytest.raises(ToolCallError) as caught: validator.arguments('lookup','{}')
    assert caught.value.code == 'tool_output_limit'


def test_native_namespace_choice_reaches_provider_unchanged(monkeypatch):
    req = request(namespace='crm')
    req['tool_choice'] = {'type':'function','name':'lookup','namespace':'crm'}
    native_items = [function_item(namespace='crm')]
    with upstream((200, {'Content-Type':'application/json'}, json.dumps(response(native_items)).encode())) as (base,captured):
        monkeypatch.setattr(server,'is_unified_mode_enabled',lambda: True)
        monkeypatch.setattr(server,'resolve_openai_auth',lambda:OpenAIAuth(kind='api_key',base_url=base,api_key='fixture-only'))
        result = TestClient(server.app).post('/v1/responses',json={**req,'model':'gpt-5.6'})
    assert result.status_code == 200 and result.json()['status'] == 'completed'
    assert captured[0]['body']['tool_choice'] == req['tool_choice']


@pytest.mark.parametrize('kind', ['tool_search_output','additional_tools'])
def test_explicit_native_history_declarations_are_checked_without_guessing_args(kind):
    tool = request()['tools'][0]
    item = {'type':kind,'tools':[tool],'status':'completed','execution':'client','call_id':'search_fixture'}
    if kind == 'additional_tools': item['role'] = 'developer'
    req = {'input':[item], 'tools':[tool]}
    validator = FunctionCallValidator(req)
    assert validator.arguments('lookup','{"q":"fixture"}')
    assert validator.arguments('lookup','{"q":"fixture"}',namespace='lookup')
    with pytest.raises(ToolCallError): validator.arguments('other','{}')
    with pytest.raises(ToolCallError): validator.arguments('lookup','{"q":1}',namespace='lookup')


def test_opaque_native_state_does_not_falsely_assert_catalog_completeness():
    validator = FunctionCallValidator({'previous_response_id':'resp_fixture'})
    assert validator.arguments('prior_tool','{"q":"fixture"}')
    with pytest.raises(ToolCallError): validator.arguments('prior_tool','not-json')
    explicit_none = FunctionCallValidator({'previous_response_id':'resp_fixture','tool_choice':'none'})
    with pytest.raises(ToolCallError) as caught: explicit_none.arguments('prior_tool','{}')
    assert caught.value.code == 'function_not_allowed'
    with pytest.raises(ToolCallError) as caught: FunctionCallValidator({}).arguments('prior_tool','{}')
    assert caught.value.code == 'undeclared_function'


def test_no_placeholder_provenance_is_assumed_for_colliding_unvalidated_schema():
    # The request boundary refuses this collision; a response helper receiving
    # it directly still cannot pretend the cleaner injected this user property.
    req = request({'type':'object','properties':{'_placeholder':{'type':'boolean'}}})
    assert FunctionCallValidator(req, route='google').arguments('lookup','{"_placeholder":true}') == '{"_placeholder":true}'


def test_native_item_execution_status_does_not_redefine_argument_json_shape():
    item = function_item()
    item.update(status='in_progress', **{'async':True})
    result = native_response(response([item]), display_model='fixture', validator=FunctionCallValidator(request()))
    assert result['status'] == 'completed' and result['output'] == [item]
    item['arguments'] = '{'
    result = native_response(response([item],'incomplete'), display_model='fixture', validator=FunctionCallValidator(request()))
    assert result['status'] == 'incomplete' and result['error']['code'] == 'incomplete_function_arguments'
