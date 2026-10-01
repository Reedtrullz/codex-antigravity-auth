# Cost units, attempt allowances and declared currency ceilings

`--budget` retains its compatibility meaning: **heuristic units**, never USD or
another currency. Static free/quota/paid tiers and chars/4 reservations are local
heuristics, not current provider prices. Legacy JSON keys such as `estimated_cost`,
`observed_cost` and `known_prices` remain for compatibility; `cost_units`,
`heuristic_units`, `basis` and `heuristic_tier_rates` state their meaning. Even when
usage counters are observed, multiplying them by a heuristic rate does not observe
money. A static `free` label does not establish zero billing.

## Independent per-run controls

All generation commands and workflow expansion accept:

| Option | Admission basis |
| --- | --- |
| `--max-calls N` | Anti generation HTTP transport entries, including its retries, fallback and judge calls |
| `--max-total-input-tokens N` | Serialized request UTF-8 byte count plus1024 framing tokens per request, explicitly an estimate |
| `--max-total-output-tokens N` | Sum of each requested `max_output_tokens` cap, including larger token-cap retry requests |
| `--currency-budget AMOUNT --pricing-file PATH` | Sum of explicit complete-attempt charge ceilings described below |

Limits are independent; every supplied limit must admit the attempt. Zero permits
no positive reservation. Omitting an option leaves that allowance unlimited; the
[whole-run deadline](RUN_CONTROL.md) still applies. Admission is atomic across
parallel lanes, just before transport entry after the request is prepared. An
unsent reservation is released. Each retry/fallback reserves independently for
its actual model. Separate Anti processes have separate ledgers.

Input estimates include the serialized instructions, prompt, tool fields if
present, metadata, Unicode escaping and framing allowance. There is no custom
tokenizer and no universal tokenizer guarantee. Output caps also depend on the
provider's treatment of reasoning. Gateway-internal retries are not separate Anti
HTTP entries, so call/token allowances must not be presented as measured upstream
quota or billing limits. No source is silently trimmed to fit an allowance.

Returned input/output usage is recorded as observed counters, separately from
estimated reservations. Lower observed usage does not refund a reservation;
missing counters retain the full estimate and are labeled unknown. An observed
counter above its reservation updates the charged allowance, flags the assumption
failure and stops further admission. Commands return nonzero with partial
metadata if such an overrun is observed. A previous request cannot be undone.

## Explicit currency admission

Currency mode requires a local version1 JSON pricing file. It is loaded once and
its hash is recorded; every attempt rechecks dates. Dates must include today in
UTC, and `as_of` may be at most30 days old. Missing, stale, future, expired,
unbounded or unpriced entries refuse admission. No online price lookup happens. Refusal diagnostics identify stale dates, missing
files and incomplete bounds without echoing pricing-file paths or content.
The required `gateway` scope names one absolute HTTP(S) base URL. Scheme/host
case, default ports and trailing slashes normalize; path and nondefault port
remain part of the scope. Every reservation compares the actual request
destination with that scope, including after an in-process gateway change.
Currency-mode HTTP requests never follow redirects to another endpoint.
Model keys match the actual forwarded gateway model ID exactly, including fallback IDs.

This is a synthetic schema example. Replace IDs, source, dates and bounds with
your own complete declaration:

```json
{
  "version": 1,
  "currency": "USD",
  "gateway": "http://127.0.0.1:51122/v1",
  "source": "Synthetic schema example; replace with your pricing reference",
  "as_of": "2026-10-01",
  "valid_until": "2026-10-02",
  "models": {
    "fixture:model": {
      "max_charge_per_attempt": "0.02",
      "max_request_bytes": 100000,
      "max_output_tokens": 4096,
      "includes_reasoning_and_all_fees": true,
      "covers_all_gateway_attempts": true
    }
  }
}
```

The amount is an upper charge for **one complete Anti gateway request**, within
the declared body-byte/output-cap bounds, including reasoning, every gateway
upstream retry and all fees. A price per token alone cannot establish that bound.
The explicit coverage declarations are required; Anti cannot validate their
truth from local token counts. A zero-cost local route still needs an explicit
zero ceiling in currency mode and remains subject to any call/token allowances.

Amounts use nonnegative decimal strings with at most12 integer and9 fractional
digits; there is no float conversion. A submitted request consumes its declared
ceiling even when usage is missing or lower than expected. Unsent preparation or
deadline failures release it. No billing refund is inferred from local counters.

The report identifies `user_declared_complete_attempt_ceiling`, gateway scope, source/date/hash,
committed and pending ceiling totals, estimates, observed counts and missing
usage. `provider_price_verified`, `billing_guarantee` and `token_limit_guarantee`
remain false. The enforced currency invariant is the sum of admitted declared
ceilings, conditional on those declarations covering all actual charges; it is
not a measured invoice, verified provider price or unconditional spend guarantee.
If a complete ceiling is unknown, use the explicitly non-monetary controls.

```bash
python3 scripts/anti.py panel --mode ask --prompt "Compare these approaches" --max-calls 4 --max-total-output-tokens 12000
python3 scripts/anti.py consult --model fixture:model --prompt "Inspect this fixture" --currency-budget 0.04 --pricing-file ./my-pricing.json --max-calls 2
```

Dry-run JSON exposes these controls and their basis separately from heuristic
estimates. Synthetic tests cover concurrency, input/output caps, retry/fallback
reservations, unknown/stale price data, preparation expiry, missing usage,
observed overruns and zero-price fixtures. They do not establish live prices or
billing. The standalone helper ships the same admission module and documentation.
