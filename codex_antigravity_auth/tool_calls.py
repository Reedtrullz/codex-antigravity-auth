"""Bounded finalized-function validation and request-scoped placeholder provenance."""
from __future__ import annotations

from copy import deepcopy
from decimal import Decimal, InvalidOperation
import json
import math

MAX_CHARS = 8 * 1024 * 1024
MAX_NODES = 100_000
MAX_DEPTH = 48
MAX_NUMBER_CHARS = 1024


class ToolCallError(ValueError):
    def __init__(self, code='invalid_function_arguments'):
        self.code = code
        super().__init__('The provider returned an invalid or unsupported function call.')


def _tick(budget, depth):
    budget[0] -= 1
    if budget[0] < 0 or depth > MAX_DEPTH:
        raise ToolCallError('tool_output_limit')


def _number(value):
    return type(value) in (int, float, Decimal)


def _comparison_number(value):
    # JSON schema floats and argument Decimals must share JSON decimal semantics,
    # rather than comparing against a float's exact binary approximation.
    return Decimal(str(value)) if type(value) is float else value


def _check(value):
    pending = [(value, 0)]
    budget = [MAX_NODES]
    chars = 0
    while pending:
        node, depth = pending.pop()
        _tick(budget, depth)
        if isinstance(node, (dict, list)):
            if len(node) + len(pending) > budget[0]:
                raise ToolCallError('tool_output_limit')
            if isinstance(node, dict):
                if any(type(key) is not str for key in node):
                    raise ToolCallError()
                try:
                    for key in node: key.encode('utf-8')
                except UnicodeError:
                    raise ToolCallError() from None
                chars += sum(len(key) for key in node)
                pending.extend((child, depth + 1) for child in node.values())
            else:
                pending.extend((child, depth + 1) for child in node)
        elif type(node) is str:
            chars += len(node)
            try:
                node.encode('utf-8')
            except UnicodeError:
                raise ToolCallError() from None
        elif type(node) is Decimal:
            if not node.is_finite(): raise ToolCallError()
            if len(str(node)) > MAX_NUMBER_CHARS: raise ToolCallError('tool_output_limit')
        elif type(node) is float:
            if not math.isfinite(node): raise ToolCallError()
        elif type(node) is int and node.bit_length() > 3400:
            raise ToolCallError('tool_output_limit')
        elif node is not None and type(node) not in (int, bool):
            raise ToolCallError()
        if chars > MAX_CHARS:
            raise ToolCallError('tool_output_limit')


def _pairs(values):
    result = {}
    for key, value in values:
        if key in result:
            raise ToolCallError()
        result[key] = value
    return result


def _number_literal(value, parser):
    if len(value) > MAX_NUMBER_CHARS:
        raise ToolCallError('tool_output_limit')
    return parser(value)


def parse_arguments(value, *, object_allowed=False):
    if type(value) is str:
        if not value or len(value) > MAX_CHARS:
            raise ToolCallError('tool_output_limit' if value else 'invalid_function_arguments')
        try:
            parsed = json.loads(value, object_pairs_hook=_pairs, parse_float=lambda value: _number_literal(value, Decimal),
                                parse_int=lambda value: _number_literal(value, int),
                                parse_constant=lambda _: (_ for _ in ()).throw(ToolCallError()))
        except ToolCallError:
            raise
        except (ValueError, RecursionError, InvalidOperation):
            raise ToolCallError() from None
    elif object_allowed and type(value) is dict:
        parsed = value
    else:
        raise ToolCallError()
    if type(parsed) is not dict:
        raise ToolCallError()
    _check(parsed)
    return parsed


def dump_arguments(value):
    """Keep decimal numeric values exact when removing a proven internal key."""
    if type(value) is Decimal:
        return str(value)
    if type(value) is dict:
        return '{' + ', '.join(json.dumps(key, ensure_ascii=True) + ': ' + dump_arguments(child)
                              for key, child in value.items()) + '}'
    if type(value) is list:
        return '[' + ', '.join(dump_arguments(child) for child in value) + ']'
    return json.dumps(value, ensure_ascii=True, allow_nan=False)


def _equal(left, right, budget, depth=0):
    _tick(budget, depth)
    if _number(left) and _number(right):
        return Decimal(str(left)) == Decimal(str(right))
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(_equal(left[k], right[k], budget, depth+1) for k in left)
    if isinstance(left, list):
        return len(left) == len(right) and all(_equal(a,b,budget,depth+1) for a,b in zip(left,right))
    return left == right


def _schema(value, schema, root, budget, depth=0, *, google=False):
    _tick(budget, depth)
    if schema is False:
        raise ToolCallError('function_schema_mismatch')
    if not isinstance(schema, dict):
        return
    ref = schema.get('$ref')
    if isinstance(ref, str) and ref.startswith('#'):
        if ref.count('/') > MAX_DEPTH:
            raise ToolCallError('tool_output_limit')
        target = root
        try:
            if ref != '#':
                if not ref.startswith('#/'): raise KeyError
                for part in ref[2:].split('/'):
                    target = target[part.replace('~1','/').replace('~0','~')]
            _schema(value, target, root, budget, depth+1, google=google)
        except (KeyError, TypeError):
            raise ToolCallError('unsupported_tool_schema') from None
    kind = schema.get('type')
    def matches(kind):
        return ((kind == 'object' and type(value) is dict) or (kind == 'array' and type(value) is list)
                or (kind == 'string' and type(value) is str) or (kind == 'boolean' and type(value) is bool)
                or (kind == 'null' and value is None) or (kind == 'number' and _number(value))
                or (kind == 'integer' and _number(value) and Decimal(str(value)) == Decimal(str(value)).to_integral_value()))
    nullable = google and value is None and schema.get('nullable') is True
    if kind is not None and not nullable and not any(matches(k) for k in (kind if isinstance(kind,list) else [kind])):
        raise ToolCallError('function_schema_mismatch')
    if 'enum' in schema and not any(_equal(value, candidate, budget) for candidate in schema['enum']):
        raise ToolCallError('function_schema_mismatch')
    if 'const' in schema and not _equal(value, schema['const'], budget):
        raise ToolCallError('function_schema_mismatch')
    for key in ('allOf','anyOf','oneOf'):
        if key in schema:
            successes = 0
            for option in schema[key]:
                try:
                    _schema(value, option, root, budget, depth+1, google=google)
                    successes += 1
                except ToolCallError as exc:
                    if exc.code != 'function_schema_mismatch': raise
            if (key == 'allOf' and successes != len(schema[key])) or (key == 'anyOf' and not successes) or (key == 'oneOf' and successes != 1):
                raise ToolCallError('function_schema_mismatch')
    if isinstance(value, dict):
        if any(key not in value for key in schema.get('required', [])):
            raise ToolCallError('function_schema_mismatch')
        properties = schema.get('properties', {})
        for key, child in value.items():
            if key in properties:
                _schema(child, properties[key], root, budget, depth+1, google=google)
            elif not schema.get('patternProperties'):
                # Regex-based properties are deliberately not executed locally.
                _schema(child, schema.get('additionalProperties', True), root, budget, depth+1, google=google)
    if isinstance(value, list):
        prefix = schema.get('prefixItems', [])
        for index, child in enumerate(value):
            _schema(child, prefix[index] if index < len(prefix) else schema.get('items', True), root, budget, depth+1, google=google)
    for low, high, measured in (('minLength','maxLength',len(value) if type(value) is str else None),
                               ('minItems','maxItems',len(value) if type(value) is list else None),
                               ('minProperties','maxProperties',len(value) if type(value) is dict else None),
                               ('minimum','maximum',value if _number(value) else None)):
        if measured is not None:
            measured = _comparison_number(measured)
            if low in schema and measured < _comparison_number(schema[low]): raise ToolCallError('function_schema_mismatch')
            if high in schema and measured > _comparison_number(schema[high]): raise ToolCallError('function_schema_mismatch')


def declaration_sources(request, *, native=False):
    tools = request.get('tools', [])
    if not isinstance(tools, list): raise ToolCallError('unsupported_tool_schema')
    if len(tools) > MAX_NODES: raise ToolCallError('tool_output_limit')
    sources = [(tool, False) for tool in tools]
    history = request.get('input')
    if native and isinstance(history, list):
        if len(history) > MAX_NODES: raise ToolCallError('tool_output_limit')
        for item in history:
            if not isinstance(item, dict): continue
            kind = item.get('type')
            if kind not in {'tool_search_output', 'additional_tools'}: continue
            if kind == 'additional_tools' and item.get('role') != 'developer':
                raise ToolCallError('unsupported_tool_schema')
            if kind == 'tool_search_output' and item.get('status', 'completed') != 'completed':
                continue
            loaded = item.get('tools')
            if not isinstance(loaded, list): raise ToolCallError('unsupported_tool_schema')
            if len(loaded) + len(sources) > MAX_NODES: raise ToolCallError('tool_output_limit')
            sources.extend((tool, True) for tool in loaded)
    return sources


class FunctionCallValidator:
    def __init__(self, request=None, *, route='native'):
        self.rules = None if request is None else {}
        self._rule_loaded = {}
        self.route = route
        self.choice = request.get('tool_choice') if isinstance(request, dict) else None
        self.catalog_complete = request is not None
        if route == 'native' and isinstance(request, dict):
            state = request.get('previous_response_id')
            conversation = request.get('conversation')
            conversation = conversation.get('id') if isinstance(conversation, dict) else conversation
            if (isinstance(state, str) and state) or (isinstance(conversation, str) and conversation):
                self.catalog_complete = False  # Opaque earlier declarations are not locally available.
        if request is None:
            return
        pending = [(tool, None, 0, loaded) for tool, loaded in declaration_sources(request, native=route == 'native')]
        budget = MAX_NODES
        while pending:
            tool, namespace, depth, loaded = pending.pop()
            budget -= 1
            if budget < 0 or depth > MAX_DEPTH: raise ToolCallError('tool_output_limit')
            if not isinstance(tool, dict): continue
            if tool.get('type') == 'namespace':
                children = tool.get('tools', [])
                if not isinstance(children, list) or not isinstance(tool.get('name'), str):
                    raise ToolCallError('unsupported_tool_schema')
                if len(children) + len(pending) > budget: raise ToolCallError('tool_output_limit')
                pending.extend((child, tool.get('name'), depth+1, loaded) for child in children)
                continue
            if tool.get('type') != 'function': continue
            fn = tool.get('function', tool)
            if not isinstance(fn, dict) or not isinstance(fn.get('name'), str): continue
            schema = fn.get('parameters')
            if schema is None: schema = {}
            if not isinstance(schema, (dict, bool)): raise ToolCallError('unsupported_tool_schema')
            from .request_shapes import _bounded, _schema as check_schema
            try:
                _check(schema)
                _bounded(schema, 'tools')
                check_schema(schema, 'tools')
            except ValueError:
                raise ToolCallError('unsupported_tool_schema') from None
            injected = (route == 'google' and isinstance(schema, dict) and schema.get('type') == 'object'
                        and not schema.get('required') and '_placeholder' not in schema.get('properties', {}))
            keys = [(namespace, fn['name'])]
            if route == 'native' and namespace is None and (loaded or fn.get('defer_loading') is True
                    or self._rule_loaded.get((fn['name'], fn['name'])) is True):
                # The documented client-loaded unscoped function example uses
                # its own name as namespace. Do not invent one on the wire.
                keys.append((fn['name'], fn['name']))
            for key in keys:
                if key in self.rules:
                    if loaded and not self._rule_loaded[key]:
                        continue  # Explicit current tools override historical declarations.
                    previous, _ = self.rules[key]
                    if loaded == self._rule_loaded[key] and json.dumps(previous, sort_keys=True) != json.dumps(schema, sort_keys=True):
                        raise ToolCallError('ambiguous_function_schema')
                self.rules[key] = (deepcopy(schema), injected)
                self._rule_loaded[key] = loaded

    def arguments(self, name, value, *, namespace=None, object_allowed=False):
        from .transform import valid_function_name
        if not valid_function_name(name):
            raise ToolCallError('invalid_function_name')
        if namespace is not None and not isinstance(namespace, str):
            raise ToolCallError('invalid_function_call')
        namespace = namespace or None
        parsed = parse_arguments(value, object_allowed=object_allowed)
        if self.choice == 'none':
            raise ToolCallError('function_not_allowed')
        if isinstance(self.choice, dict) and self.choice.get('type') == 'function':
            selected = self.choice.get('function', self.choice)
            if isinstance(selected, dict):
                selected_namespace = self.choice.get('namespace', selected.get('namespace'))
                if name != selected.get('name') or (selected_namespace is not None and namespace != selected_namespace):
                    raise ToolCallError('function_not_allowed')
        rule = None
        if self.rules is not None:
            rule = self.rules.get((namespace, name))
            if rule is None and self.catalog_complete:
                raise ToolCallError('undeclared_function')
        changed = False
        if rule is not None:
            schema, injected = rule
            if injected and '_placeholder' in parsed:
                if type(parsed['_placeholder']) is not bool:
                    raise ToolCallError()
                parsed = {key: child for key,child in parsed.items() if key != '_placeholder'}
                changed = True
            try:
                _schema(parsed, schema, schema, [MAX_NODES], google=self.route == "google")
            except (TypeError, ValueError, InvalidOperation, RecursionError) as exc:
                if isinstance(exc, ToolCallError): raise
                raise ToolCallError('unsupported_tool_schema') from None
        encoded = value if type(value) is str and not changed else dump_arguments(parsed)
        if len(encoded) > MAX_CHARS: raise ToolCallError('tool_output_limit')
        return encoded


def tool_terminal(terminal, code):
    from .response_protocol import ProviderTerminal, TerminalKind
    if terminal.kind is TerminalKind.INCOMPLETE:
        if code == 'invalid_function_arguments': code = 'incomplete_function_arguments'
        return ProviderTerminal(terminal.kind, terminal.reason, incomplete_reason=terminal.incomplete_reason,
                                error_code=code, error_message='Function-call validation failed.')
    return ProviderTerminal(TerminalKind.FAILED, code, error_code=code, error_message='Function-call validation failed.')


def google_arguments(function, validator):
    if type(function) is not dict:
        raise ToolCallError('invalid_function_call')
    if 'willContinue' in function and type(function['willContinue']) is not bool:
        raise ToolCallError('invalid_function_call')
    if function.get('willContinue') or 'partialArgs' in function:
        raise ToolCallError('unsupported_partial_function_call')
    from .transform import _valid_tool_call_id
    call_id = function.get('id')
    if call_id and (not _valid_tool_call_id(call_id) or len(call_id) > 1024):
        raise ToolCallError('invalid_function_call')
    value = function.get('args', {})
    if type(value) is not dict:
        raise ToolCallError()
    return validator.arguments(function.get('name'), value, object_allowed=True)


def checked_native_call(item, validator, *, finalized_arguments=None):
    from .native_output import validate_item
    from .transform import _valid_tool_call_id
    validate_item(item)
    if not _valid_tool_call_id(item.get('call_id')) or len(item['call_id']) > 1024:
        raise ToolCallError('invalid_function_call')
    arguments = validator.arguments(item['name'], item['arguments'], namespace=item.get('namespace'))
    if finalized_arguments is not None and not _equal(parse_arguments(arguments), parse_arguments(finalized_arguments), [MAX_NODES]):
        raise ToolCallError('conflicting_function_arguments')
    return {**item, 'arguments': arguments}


def native_response(payload, *, display_model, validator, bad_indices=(), error_code=None, finalized_arguments=None, errors=None):
    from collections import Counter
    from .native_output import validate_response, NativeOutputError, check_json
    if not isinstance(payload, dict) or not isinstance(payload.get('output'), list):
        return validate_response(payload, display_model=display_model)
    # Keep the native contract's decoded budget before filtering anything.
    try:
        check_json(payload)
    except NativeOutputError:
        return validate_response(payload, display_model=display_model)
    ids = [item['id'] for item in payload['output'] if isinstance(item, dict) and isinstance(item.get('id'), str)]
    if len(set(ids)) != len(ids):
        return validate_response(payload, display_model=display_model)
    output = []
    function_ids = {item.get('call_id') for item in payload['output'] if isinstance(item, dict)
                    and item.get('type') == 'function_call' and isinstance(item.get('call_id'), str)}
    identifiers = Counter(item.get('call_id') for item in payload['output'] if isinstance(item, dict)
                          and item.get('type') in {'function_call','custom_tool_call'} and isinstance(item.get('call_id'), str))
    for index, item in enumerate(payload['output']):
        if not isinstance(item, dict) or item.get('type') != 'function_call':
            if (isinstance(item, dict) and item.get('type') == 'custom_tool_call'
                    and isinstance(item.get('call_id'), str) and item['call_id'] in function_ids and identifiers[item['call_id']] > 1):
                error_code = error_code or 'conflicting_function_call'
                continue
            output.append(item)
            continue
        try:
            if index in bad_indices:
                raise ToolCallError(error_code or 'invalid_function_arguments')
            checked = checked_native_call(item, validator, finalized_arguments=(finalized_arguments or {}).get(index))
            if identifiers[checked['call_id']] > 1:
                raise ToolCallError('conflicting_function_call')
            output.append(checked)
        except (ToolCallError, NativeOutputError) as exc:
            error_code = error_code or exc.code
    response = {**payload, 'output': output}
    if error_code:
        if errors is not None: errors.append(error_code)
        response['status'] = 'incomplete' if payload.get('status') == 'incomplete' else 'failed'
        if response['status'] == 'incomplete' and error_code == 'invalid_function_arguments':
            error_code = 'incomplete_function_arguments'
        response['error'] = {'code':error_code, 'message':'Function-call validation failed.'}
    return validate_response(response, display_model=display_model)
