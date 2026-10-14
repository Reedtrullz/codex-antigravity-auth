"""Count-only context evidence. UTF-8 estimates never certify tokenizer fit."""
from __future__ import annotations

from dataclasses import dataclass
from copy import deepcopy
import hashlib
import json
from typing import Callable

VERSION = 1
FRAMING_ESTIMATE = 2048


def positive_int(value):
    return value if type(value) is int and value > 0 else None


def encoded(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode('utf-8')


def binding_key(contract):
    """Bind trusted evidence to the complete route/deployment declaration."""
    return hashlib.sha256(encoded(contract)).hexdigest()


@dataclass(frozen=True)
class VerifiedContext:
    """Code-owned evidence; never constructed from request/provider JSON.

    count_input must count the complete Responses request after this exact route's
    translation/framing, including tools, instructions, media and replay state.
    None means the compatible counter cannot account for this request. The output
    cap must bound *all* output plus reasoning for the verified route.
    """
    binding: str
    source: str
    context_tokens: int
    count_input: Callable[[dict], int | None] | None = None
    output_includes_reasoning: bool = False


class ContextLimitExceeded(ValueError):
    def __init__(self, report):
        super().__init__('context_limit_exceeded: request exceeds the verified context budget; reduce explicit input or output reservation')
        self.report = report


def components(request):
    groups = {'input': {}, 'instructions': {}, 'tools': {}, 'format': {}, 'controls': {}}
    for key, value in request.items():
        group = ('input' if key in {'input', 'messages', 'previous_response_id', 'conversation'} else
                 'instructions' if key in {'instructions', 'system', 'developer'} else
                 'tools' if key in {'tools', 'tool_choice', 'parallel_tool_calls'} else
                 'format' if key in {'text', 'response_format'} else 'controls')
        groups[group][key] = value
    return {name: len(encoded(value)) if value else 0 for name, value in groups.items()}


def unknown_inputs(request):
    reasons = set()
    pending = [request]
    while pending:
        value = pending.pop()
        if isinstance(value, dict):
            kind = value.get('type')
            if isinstance(kind, str) and kind in {'image', 'input_image', 'image_url', 'antigravity_audio', 'input_audio', 'input_video', 'input_file'}:
                reasons.add('media_token_cost')
            if any(key in value for key in ('inlineData', 'fileData', 'encrypted_content')):
                reasons.add('media_or_opaque_token_cost')
            pending.extend(value.values())
        elif isinstance(value, list):
            pending.extend(value)
    if request.get('previous_response_id') or request.get('conversation'):
        reasons.add('server_side_history')
    return reasons


def assess(request, *, declared_tokens=None, binding=None, evidence=None):
    """Assess the model-context request view; metadata is gateway-only control.

    Use this same projection before or after the gateway removes metadata and
    before or after Anti updates per-attempt timeout hints. Preserve the caller's
    complete request and let compatible counters handle provider translation.
    """
    request = {key: value for key, value in request.items() if key != 'metadata'}
    sizes = components(request)
    wire_bytes = len(encoded(request))
    estimated_input = wire_bytes + FRAMING_ESTIMATE
    output = positive_int(request.get('max_output_tokens'))
    unknown = unknown_inputs(request)
    unknown.update({'compatible_tokenizer', 'adapter_framing', 'verified_context_limit', 'reasoning_reservation'})
    if output is None:
        unknown.add('output_reservation')
    declared = positive_int(declared_tokens)
    verified = (isinstance(evidence, VerifiedContext) and isinstance(binding, str)
                and evidence.binding == binding and isinstance(evidence.source, str) and bool(evidence.source.strip())
                and positive_int(evidence.context_tokens) is not None)
    limit = evidence.context_tokens if verified else None
    exact_input = None
    source = 'none'
    if verified:
        unknown.discard('verified_context_limit')
        source = 'verified_route_adapter'
        if evidence.output_includes_reasoning is True:
            unknown.discard('reasoning_reservation')
        else:
            # Even an absent effort selector may leave a provider default active.
            unknown.add('reasoning_reservation')
        if evidence.count_input is not None:
            try:
                count = evidence.count_input(deepcopy(request))
                if type(count) is int and count >= 0:
                    exact_input = count
                    unknown.difference_update({'compatible_tokenizer', 'adapter_framing', 'media_token_cost',
                                               'media_or_opaque_token_cost', 'server_side_history'})
                else:
                    unknown.add('compatible_counter_unavailable')
            except Exception:
                unknown.add('compatible_counter_unavailable')
    known_minimum = (exact_input or 0) + (output or 0)
    status = 'unknown'
    reason = 'insufficient_verified_evidence'
    if verified and known_minimum > limit:
        status, reason = 'reject', 'known_reservation_exceeds_verified_limit'
    elif verified and not unknown:
        status, reason = 'fit', 'complete_verified_count_with_output_reservation'
    comparison = 'unknown'
    if declared is not None and output is not None:
        comparison = 'estimate_exceeds_declaration' if estimated_input + output > declared else 'estimate_within_declaration'
    return {'version': VERSION, 'status': status, 'reason': reason,
            'measurement_boundary': 'validated_responses_without_gateway_metadata',
            'limit': {'verified_tokens': limit, 'declared_tokens': declared, 'basis': source},
            'input': {'exact_tokens': exact_input, 'estimated_tokens': estimated_input,
                      'estimate_basis': 'one_unit_per_serialized_utf8_byte_plus_2048_framing_units',
                      'estimate_is_upper_bound': False, 'serialized_utf8_bytes': wire_bytes, 'component_bytes': sizes},
            'output': {'requested_tokens': output, 'includes_reasoning': bool(verified and evidence.output_includes_reasoning is True)},
            'unknown_components': sorted(unknown), 'declared_comparison': comparison,
            'request_unchanged': True}


def calibration(report, usage):
    """Provider-reported counts are observations, not ground truth or billing."""
    value = usage.get('input_tokens') if isinstance(usage, dict) else None
    observed = value if type(value) is int and value >= 0 else None
    estimate = report['input']['estimated_tokens']
    return {'basis': 'provider_reported_usage', 'observed_input_tokens': observed,
            'estimated_input_tokens': estimate,
            'observed_minus_estimated': observed - estimate if observed is not None else None,
            'billing_authority': False, 'verifies_limit': False}
