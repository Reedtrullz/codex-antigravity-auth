"""Pure pre-dispatch shape/linkage checks for the adapters' existing contracts."""
from __future__ import annotations

import json
import math

from .schema import UNSUPPORTED_KEYWORDS

MAX_DEPTH = 48
MAX_NODES = 100_000
ANNOTATIONS = {'$schema', '$id', '$comment', 'title', 'description', 'default', 'examples'}
SCHEMA_TYPES = {'object', 'array', 'string', 'number', 'integer', 'boolean', 'null'}


class RequestShapeError(ValueError):
    pass


def reject(path, message):
    raise RequestShapeError(f'{path}: {message}')


def _name(value, path):
    from .transform import valid_function_name
    if not valid_function_name(value):
        reject(path, 'expected a 1-64 character function name using letters, numbers, underscores or hyphens')


def _call_id(value, path):
    from .transform import _valid_tool_call_id
    if not _valid_tool_call_id(value) or len(value) > 1024:
        reject(path, 'expected a nonempty bounded call ID without whitespace or controls')
    return value


def _bounded(value, path):
    # This also protects direct helper calls from cycles/deep mutable objects.
    pending = [(value, path, 0)]
    count = 0
    while pending:
        item, where, depth = pending.pop()
        count += 1
        if depth > MAX_DEPTH or count > MAX_NODES:
            reject(path, 'request structure exceeds validation depth/node limits')
        if isinstance(item, (dict, list)) and count + len(pending) + len(item) > MAX_NODES:
            reject(path, "request structure exceeds validation depth/node limits")
        if isinstance(item, dict):
            if any(not isinstance(key, str) for key in item):
                reject(where, 'object keys must be strings')
            pending.extend((child, where, depth + 1) for child in item.values())
        elif isinstance(item, list):
            pending.extend((child, where, depth + 1) for child in item)
        elif isinstance(item, float) and not math.isfinite(item):
            reject(where, 'numbers must be finite')
        elif item is not None and not isinstance(item, (str, int, float, bool)):
            reject(where, 'expected a JSON value')


def _schema(schema, path, *, google=False):
    pending = [(schema, path)]
    while pending:
        node, where = pending.pop()
        if isinstance(node, bool):
            if google:
                reject(where, 'translation_loss: Google schema translation cannot preserve boolean schemas')
            continue
        if not isinstance(node, dict):
            reject(where, 'schema must be an object or boolean')
        if google:
            for key in node:
                if key in UNSUPPORTED_KEYWORDS and key not in ANNOTATIONS:
                    reject(f'{where}.{key}', 'translation_loss: Google schema translation would omit this construct')
        if '$ref' in node and not isinstance(node['$ref'], str):
            reject(where + '.$ref', 'expected a reference string')
        kind = node.get('type')
        if kind is not None:
            kinds = kind if isinstance(kind, list) else [kind]
            if not kinds or any(not isinstance(value, str) or value not in SCHEMA_TYPES for value in kinds):
                reject(where + '.type', 'expected JSON Schema type names')
        if 'required' in node:
            required = node['required']
            if not isinstance(required, list) or any(not isinstance(name, str) for name in required):
                reject(where + '.required', 'expected an array of property names')
            if len(set(required)) != len(required):
                reject(where + '.required', 'property names must be unique')
        if 'enum' in node and (not isinstance(node['enum'], list) or not node['enum']):
            reject(where + '.enum', 'expected a nonempty array')
        for key in ('properties', '$defs', 'definitions', 'patternProperties', 'dependentSchemas'):
            if key in node:
                if not isinstance(node[key], dict):
                    reject(where + '.' + key, 'expected an object of schemas')
                for name, child in node[key].items():
                    label = json.dumps(name, ensure_ascii=True)
                    if len(label) > 128:
                        label = '"<long-property-name>"'
                    pending.append((child, f'{where}.{key}[{label}]'))
        for key in ('items', 'additionalProperties', 'propertyNames', 'contains', 'not', 'if', 'then', 'else'):
            if key in node:
                pending.append((node[key], where + '.' + key))
        for key in ('anyOf', 'oneOf', 'allOf', 'prefixItems'):
            if key in node:
                children = node[key]
                if not isinstance(children, list) or not children:
                    reject(where + '.' + key, 'expected a nonempty array of schemas')
                pending.extend((child, f'{where}.{key}[{index}]') for index, child in enumerate(children))


def _arguments(value, path):
    def object_pairs(pairs):
        result = {}
        for name, child in pairs:
            if name in result:
                raise ValueError('duplicate argument key')
            result[name] = child
        return result
    if isinstance(value, str):
        if len(value) > 8 * 1024 * 1024:
            reject(path, 'function arguments exceed the 8 Mi-character parsing limit')
        try:
            value = json.loads(value, object_pairs_hook=object_pairs, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        except (ValueError, RecursionError):
            reject(path, 'expected a JSON object or a JSON string encoding an object')
    if not isinstance(value, dict):
        reject(path, 'expected function arguments as an object')
    _bounded(value, path)


def validate_request_shapes(request, *, route=None):
    """route=None/native validates common shapes without banning native unions.

    Translation checks run again after route classification and before account/
    key resolution. Unknown native provider-specific item/tool unions are passed
    through intact for upstream validation, never normalized by this module.
    """
    translated = route in {'google', 'byok'}
    _bounded(request, 'request')
    tools = request.get('tools', [])
    if not isinstance(tools, list):
        reject('tools', 'expected an array of tool definitions')
    names = set()
    for index, tool in enumerate(tools):
        path = f'tools[{index}]'
        if not isinstance(tool, dict):
            reject(path, 'expected a tool object')
        kind = tool.get('type')
        if not isinstance(kind, str) or not kind:
            reject(path + '.type', 'expected a tool type string')
        if kind != 'function':
            if translated:
                reject(path + '.type', 'this route implements function tools only')
            continue
        if 'function' in tool:
            fn, path = tool['function'], path + '.function'
            if not isinstance(fn, dict):
                reject(path, 'expected a function definition object')
            if translated and set(tool) - {'type', 'function'}:
                reject(f'tools[{index}]', 'nested function wrapper has unsupported fields')
            if any(key in tool for key in ('name', 'parameters', 'description', 'strict')):
                reject(f'tools[{index}]', 'conflicting flat and nested function definitions')
        else:
            fn = tool
        _name(fn.get('name'), path + '.name')
        if fn['name'] in names:
            reject(path + '.name', 'duplicate function definition')
        names.add(fn['name'])
        if 'description' in fn and not isinstance(fn['description'], str) and (translated or fn['description'] is not None):
            reject(path + '.description', 'expected text')
        if 'strict' in fn and not isinstance(fn['strict'], bool) and (translated or fn['strict'] is not None):
            reject(path + '.strict', 'expected a boolean')
        if route == 'google' and fn.get('strict') is True:
            reject(path + '.strict', 'translation_loss: Google strict-schema guarantees are not implemented')
        if translated:
            allowed = {'type','name','parameters','description','strict'} if fn is tool else {'name','parameters','description','strict'}
            if set(fn) - allowed:
                reject(path, 'function definition has unsupported fields for this route')
        parameters = fn.get('parameters', {})
        if parameters is None and not translated:
            continue
        if not isinstance(parameters, dict):
            reject(path + '.parameters', 'expected a schema object')
        _schema(parameters, path + '.parameters', google=route == 'google')
    choice = request.get('tool_choice')
    if isinstance(choice, dict) and 'name' in choice and 'function' in choice:
        reject('tool_choice', 'conflicting flat and nested function choices')
    if isinstance(choice, dict) and translated and choice.get('type') != 'function':
        reject('tool_choice.type', 'this route implements function choices only')

    if translated and request.get('previous_response_id'):
        reject('previous_response_id', 'translated routes require the full conversation in input')
    value = request.get('input')
    if value is None:
        if translated and 'input' in request:
            reject('input', 'expected text or an array of input objects')
        return
    if isinstance(value, str):
        return
    if not isinstance(value, list):
        reject('input', 'expected text or an array of input objects')
    calls, answered = {}, set()

    def call(item, path, *, nested=False):
        _name(item.get('name'), path + '.name')
        if nested and 'id' in item and 'call_id' in item and item['id'] != item['call_id']:
            reject(path + '.call_id', 'conflicting tool-use identifiers')
        identifier = _call_id(item.get('call_id') or item.get('id'), path + '.call_id')
        arguments = item.get('input', {}) if nested else item.get('arguments')
        if translated:
            _arguments(arguments, path + ('.input' if nested else '.arguments'))
        elif not isinstance(arguments, (str, dict)):
            reject(path + '.arguments', 'expected function arguments')
        if translated and identifier in calls:
            reject(path + '.call_id', 'duplicate function-call identity')
        calls[identifier] = item['name']

    def result(item, path, *, nested=False):
        if nested and 'tool_use_id' in item and 'call_id' in item and item['tool_use_id'] != item['call_id']:
            reject(path + '.call_id', 'conflicting tool-result identifiers')
        if nested and 'content' in item and 'output' in item and item['content'] != item['output']:
            reject(path + '.output', 'conflicting tool-result content')
        identifier = _call_id(item.get('tool_use_id') or item.get('call_id'), path + '.call_id')
        if translated:
            if identifier not in calls:
                reject(path + '.call_id', 'orphan tool output has no preceding function call')
            if identifier in answered:
                reject(path + '.call_id', 'duplicate tool output')
            if item.get('name') is not None and item['name'] != calls[identifier]:
                reject(path + '.name', 'tool output name does not match its call')
            answered.add(identifier)
        key = 'content' if nested and 'content' in item else 'output'
        if key not in item:
            reject(path + '.' + key, 'tool output must be explicit')
        output = item[key]
        if not isinstance(output, (str, dict, list)):
            reject(path + '.' + key, 'expected text, structured JSON, or text content parts')
        if isinstance(output, list):
            content(output, path + '.' + key, 'tool', tool_output=True)

    def content(parts, path, role, *, tool_output=False):
        if isinstance(parts, str):
            return
        if not isinstance(parts, list):
            reject(path, 'expected text or an array of content objects')
        for index, part in enumerate(parts):
            where = f'{path}[{index}]'
            if not isinstance(part, dict):
                reject(where, 'expected a content object')
            kind = part.get('type')
            if not isinstance(kind, str) or not kind:
                reject(where + '.type', 'expected a content type string')
            if kind in {'text','input_text','output_text'}:
                if not isinstance(part.get('text'), str):
                    reject(where + '.text', 'expected text')
            elif tool_output:
                reject(where + '.type', 'tool output content must be text')
            elif kind == 'tool_use':
                if route == 'byok':
                    reject(where + '.type', 'use a top-level function_call on this route')
                if role != 'assistant':
                    reject(where, 'tool use must appear in an assistant message')
                call(part, where, nested=True)
            elif kind in {'tool_result','function_call_output'}:
                if role != 'user':
                    reject(where, 'tool results must appear in a user message')
                result(part, where, nested=True)
            elif kind in {'image','input_image'}:
                pass  # Existing input_fidelity owns media and role capabilities.
            elif translated:
                reject(where + '.type', 'unsupported content type for this route')

    for index, item in enumerate(value):
        path = f'input[{index}]'
        if not isinstance(item, dict):
            reject(path, 'expected an input object')
        kind = item.get('type', 'message')
        if not isinstance(kind, str) or not kind:
            reject(path + '.type', 'expected an input type string')
        if kind == 'message':
            role = item.get('role', 'user')
            if role not in ('user','assistant','system','developer'):
                reject(path + '.role', 'expected user, assistant, system or developer')
            if 'content' not in item:
                reject(path + '.content', 'message content must be explicit')
            content(item['content'], path + '.content', role)
        elif kind == 'function_call':
            call(item, path)
        elif kind == 'function_call_output':
            result(item, path)
        elif kind == 'reasoning':
            pass  # Existing route capability owns replay/opaque policy.
        elif translated:
            reject(path + '.type', 'unsupported input item type for this route')
