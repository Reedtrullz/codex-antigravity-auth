"""One loss-aware Google part normalizer shared by buffered and streamed output."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any
import uuid

from .tool_calls import ToolCallError, google_arguments
from .response_protocol import ProviderTerminal, TerminalKind


@dataclass(frozen=True)
class GooglePart:
    text: str = ''
    reasoning: str = ''
    function: dict[str, Any] | None = None
    has_function: bool = False
    tool_error: str | None = None
    partial_id: str | None = None
    partial_name: str | None = None
    output_error: str | None = None


_KNOWN_FIELDS = frozenset({'text','thinking','type','thought','thoughtSignature','functionCall'})
_MEDIA_FIELDS = ('inlineData','fileData','videoMetadata','mediaResolution')
_UNSUPPORTED_FIELDS = ('functionResponse','executableCode','codeExecutionResult')


def normalize_google_part(part, validator) -> GooglePart:
    if not isinstance(part, dict):
        return GooglePart(output_error='malformed_output_part')
    output_error = None
    if any(field in part for field in _MEDIA_FIELDS):
        # Do not inspect/decode/copy bytes, URIs, filenames or MIME values.
        output_error = 'unsupported_output_modality'
    elif any(field in part for field in _UNSUPPORTED_FIELDS) or any(field not in _KNOWN_FIELDS for field in part):
        output_error = 'unsupported_output_part'
    kind = part.get('type')
    if kind in ('image','audio','video'):
        output_error = output_error or 'unsupported_output_modality'
    elif kind is not None and kind not in ('text','thinking'):
        output_error = output_error or 'unsupported_output_part'
    if 'thought' in part and type(part['thought']) is not bool:
        output_error = output_error or 'malformed_output_part'
    thought = part.get('thought') is True or kind == 'thinking'
    text, thinking = part.get('text'), part.get('thinking')
    if ('text' in part and not isinstance(text, str)) or ('thinking' in part and not isinstance(thinking, str)):
        output_error = output_error or 'malformed_output_part'
    text = text if isinstance(text, str) else ''
    thinking = thinking if isinstance(thinking, str) else ''
    try: text.encode('utf-8')
    except UnicodeError:
        text = ''
        output_error = output_error or 'malformed_output_part'
    try: thinking.encode('utf-8')
    except UnicodeError:
        thinking = ''
        output_error = output_error or 'malformed_output_part'
    if thinking and (not thought or (text and text != thinking)):
        output_error = output_error or 'malformed_output_part'
    reasoning = (text or thinking) if thought else ''
    visible = '' if thought else text
    function = None
    tool_error = partial_id = partial_name = None
    has_function = 'functionCall' in part
    if has_function:
        call = part['functionCall']
        try:
            arguments = google_arguments(call, validator)
            call_id = call.get('id')
            if not isinstance(call_id, str) or not call_id: call_id = 'call_' + uuid.uuid4().hex[:8]
            function = {'type':'function_call','id':'fc_' + uuid.uuid4().hex[:8],
                        'call_id':call_id,'name':call['name'],'arguments':arguments}
        except ToolCallError as exc:
            tool_error = exc.code
            if isinstance(call, dict) and exc.code == 'unsupported_partial_function_call':
                from .transform import valid_function_name
                if isinstance(call.get('id'), str) and call['id']:
                    partial_id = call['id']
                elif valid_function_name(call.get('name')):
                    partial_name = call['name']
    # thoughtSignature is intentionally not reasoning and is not exposed or
    # invented as an opaque-replay field in the Responses representation.
    return GooglePart(visible, reasoning, function, has_function, tool_error, partial_id, partial_name, output_error)


def merge_output_error(previous, current):
    """Stable failure selection, independent of part/candidate/chunk order."""
    priority = {None: 0, 'unsupported_output_part': 1, 'malformed_output_part': 2,
                'unsupported_output_modality': 3}
    return current if priority[current] > priority[previous] else previous


def output_failure(terminal, code):
    message = ('Generated media output is not supported by the Google adapter.'
               if code == 'unsupported_output_modality' else 'The Google adapter cannot represent an output part.')
    if terminal.kind is TerminalKind.INCOMPLETE:
        return ProviderTerminal(terminal.kind, terminal.reason, incomplete_reason=terminal.incomplete_reason,
                                error_code=code, error_message=message)
    return ProviderTerminal(TerminalKind.FAILED, code, error_code=code, error_message=message)
