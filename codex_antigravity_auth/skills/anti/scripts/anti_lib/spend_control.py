"""Atomic attempt admission with explicit estimates and user-declared price bounds."""
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import re
import threading


class SpendRefused(Exception):
    submitted = False


def amount(value):
    if not isinstance(value, str) or not re.fullmatch(r'(?:0|[1-9][0-9]{0,11})(?:\.[0-9]{1,9})?', value):
        raise ValueError('currency amounts require a nonnegative decimal string with at most nine fractional digits')
    return Decimal(value)


def positive_bound(value, *, zero=False):
    if type(value) is not int or value < (0 if zero else 1) or value > 1_000_000_000:
        raise ValueError('invalid integer admission bound')
    return value


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate pricing field')
        result[key] = value
    return result


def utc_day():
    return datetime.now(timezone.utc).date()


class Pricing:
    """An operator's dated complete-attempt quote, not a price discovered by Anti."""
    def __init__(self, path, *, today=utc_day):
        self.today = today
        with Path(path).expanduser().open('rb') as handle:
            raw = handle.read(262145)
        if len(raw) > 262144:
            raise ValueError('pricing file exceeds 256 KiB')
        data = json.loads(raw, object_pairs_hook=unique_object)
        if not isinstance(data, dict) or type(data.get('version')) is not int or data['version'] != 1:
            raise ValueError('unsupported pricing file version')
        if not isinstance(data.get('currency'), str) or not re.fullmatch('[A-Z]{3}', data['currency']):
            raise ValueError('pricing currency must be a three-letter uppercase code')
        if not isinstance(data.get('source'), str) or not data['source'].strip() or len(data['source']) > 1024:
            raise ValueError('pricing requires a source reference')
        self.as_of = date.fromisoformat(data['as_of'])
        self.valid_until = date.fromisoformat(data['valid_until'])
        self.currency, self.source = data['currency'], data['source']
        self.sha256 = hashlib.sha256(raw).hexdigest()
        models = data.get('models')
        if not isinstance(models, dict) or not models or len(models) > 2048:
            raise ValueError('pricing requires exact model entries')
        self.models = {}
        for model, entry in models.items():
            if not isinstance(model, str) or not model or len(model) > 512 or not isinstance(entry, dict):
                raise ValueError('invalid pricing model entry')
            if entry.get('includes_reasoning_and_all_fees') is not True or entry.get('covers_all_gateway_attempts') is not True:
                raise ValueError('pricing must bound reasoning, all fees and every gateway attempt')
            self.models[model] = {
                'charge': amount(entry['max_charge_per_attempt']),
                'bytes': positive_bound(entry['max_request_bytes']),
                'output': positive_bound(entry['max_output_tokens'], zero=True),
            }
        self.check_date()

    def check_date(self):
        today = self.today()
        if not self.as_of <= today <= self.valid_until or (today - self.as_of).days > 30:
            raise ValueError('pricing is stale, future-dated or expired; refresh the explicit declaration')

    def quote(self, model, request_bytes, output_tokens):
        self.check_date()
        entry = self.models.get(model)
        if entry is None:
            raise ValueError('no complete price bound for the actual model')
        if request_bytes > entry['bytes'] or output_tokens > entry['output']:
            raise ValueError('request exceeds the declared price-bound scope')
        return entry['charge']

    def metadata(self):
        return {'currency': self.currency, 'source': self.source, 'as_of': self.as_of.isoformat(),
                'valid_until': self.valid_until.isoformat(), 'sha256': self.sha256,
                'basis': 'user_declared_complete_attempt_ceiling', 'provider_price_verified': False}


class SpendControl:
    def __init__(self, *, max_calls=None, max_input_tokens=None, max_output_tokens=None,
                 currency_budget=None, pricing_file=None, error_type=SpendRefused, today=utc_day):
        self.error_type = error_type
        self.limits = {'calls': max_calls, 'input_tokens': max_input_tokens, 'output_tokens': max_output_tokens}
        for value in self.limits.values():
            if value is not None: positive_bound(value, zero=True)
        if (currency_budget is None) != (pricing_file is None):
            raise ValueError('currency budget and pricing file must be supplied together')
        self.currency_limit = amount(currency_budget) if currency_budget is not None else None
        self.pricing = Pricing(pricing_file, today=today) if pricing_file is not None else None
        self.enabled = any(value is not None for value in self.limits.values()) or self.pricing is not None
        self.lock = threading.Lock()
        self.reserved = dict.fromkeys(self.limits, 0)
        self.committed = dict.fromkeys(self.limits, 0)
        self.observed = {'input_tokens': 0, 'output_tokens': 0}
        self.missing = {'input_tokens': 0, 'output_tokens': 0}
        self.currency_reserved = self.currency_committed = Decimal(0)
        self.assumption_exceeded = False
        self.refused = 0
        self.history = []
        self.history_omitted = 0

    def _refuse(self, reason):
        self.refused += 1
        error = self.error_type('admission refused: ' + reason)
        error.reason = reason
        raise error

    def reserve(self, model, request_bytes, output_tokens):
        if not self.enabled: return None
        positive_bound(request_bytes, zero=True)
        positive_bound(output_tokens, zero=True)
        # A byte-based estimate includes instructions, tool schemas and metadata
        # in the serialized request. The framing margin is explicit, not a claim
        # of tokenizer precision or a universal provider-side token bound.
        estimate = {'calls': 1, 'input_tokens': request_bytes + 1024, 'output_tokens': output_tokens}
        with self.lock:
            if self.assumption_exceeded:
                self._refuse('observed usage exceeded a reserved token estimate; further admission stopped')
            try:
                charge = self.pricing.quote(model, request_bytes, output_tokens) if self.pricing else Decimal(0)
            except ValueError as error:
                self._refuse(str(error))
            for key, limit in self.limits.items():
                if limit is not None and self.committed[key] + self.reserved[key] + estimate[key] > limit:
                    self._refuse(key + ' admission limit exhausted')
            if self.currency_limit is not None and self.currency_committed + self.currency_reserved + charge > self.currency_limit:
                self._refuse('declared currency attempt ceilings exceed budget')
            ticket = {'estimate': estimate, 'charge': charge, 'settled': False,
                      'entry': {'model': model, 'status': 'reserved', 'estimated_tokens': {key:value for key,value in estimate.items() if key != 'calls'},
                                'currency_ceiling': str(charge) if self.pricing else None}}
            for key, value in estimate.items(): self.reserved[key] += value
            self.currency_reserved += charge
            if len(self.history) < 256: self.history.append(ticket['entry'])
            else: self.history_omitted += 1
            return ticket

    def settle(self, ticket, *, submitted, usage=None):
        if ticket is None: return
        with self.lock:
            if ticket['settled']: return
            ticket['settled'] = True
            estimate = ticket['estimate']
            for key, value in estimate.items(): self.reserved[key] -= value
            self.currency_reserved -= ticket['charge']
            entry = ticket['entry']
            entry['submitted'] = submitted
            if not submitted:
                entry['status'] = 'not_sent'
                return
            self.committed['calls'] += 1
            observed = {}
            for key in self.observed:
                value = usage.get(key) if isinstance(usage, dict) else None
                known = type(value) is int and value >= 0
                observed[key] = value if known else None
                self.committed[key] += max(estimate[key], value if known else 0)
                if known:
                    self.observed[key] += value
                    if value > estimate[key]: self.assumption_exceeded = True
                else: self.missing[key] += 1
            # Local usage is not an invoice. A submitted currency reservation is
            # never refunded merely because returned token counters are lower.
            self.currency_committed += ticket['charge']
            entry.update(status='observed' if all(value is not None for value in observed.values()) else 'unknown_usage',
                         observed_tokens=observed)

    def snapshot(self):
        with self.lock:
            return {'enabled': self.enabled, 'limits': dict(self.limits), 'reserved': dict(self.reserved), 'committed': dict(self.committed),
                    'observed_tokens': dict(self.observed), 'missing_usage_attempts': dict(self.missing),
                    'input_basis': 'serialized_utf8_bytes_plus_1024_framing_estimate',
                    'output_basis': 'requested_output_cap per Anti HTTP request; gateway-internal retries and reasoning inclusion are not independently measured',
                    'call_basis': 'Anti generation HTTP transport entries; not gateway-internal provider attempts',
                    'token_limit_guarantee': False, 'billing_guarantee': False,
                    'assumption_exceeded': self.assumption_exceeded, 'refused_attempts': self.refused,
                    'currency': self.pricing.metadata() if self.pricing else None,
                    'currency_budget': str(self.currency_limit) if self.currency_limit is not None else None,
                    'currency_reserved': str(self.currency_reserved), 'currency_committed_ceiling': str(self.currency_committed),
                    'attempts': [dict(entry) for entry in self.history], 'attempts_omitted': self.history_omitted}
