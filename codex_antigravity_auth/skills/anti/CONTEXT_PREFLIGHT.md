# Context preflight and uncertainty

Character packing limits protect helper resources. They cannot certify that a
complete model request fits a context window. Use the gateway's non-generating
`POST /v1/context/preflight` with the same Responses request body intended for
`POST /v1/responses`. It validates request shape and reads local declarations,
without refreshing credentials, calling a provider or counting through a hosted
API. It has the same JSON/Origin/Host/access guard and an owned request deadline.
The result assesses context only; it is not a route readiness or capability check.

```json
{
  "model": "configured-provider:configured-model",
  "instructions": "Evaluate the supplied design.",
  "input": "The full design goes here.",
  "tools": [],
  "max_output_tokens": 2048
}
```

The count-only response has `version: 1`, `status`, a reason, limit provenance,
per-component byte counts, output reservation and explicit unknown components.
It never returns the request text, model labels, URLs, schemas or source hashes.

| Status | Meaning |
| --- | --- |
| `fit` | A trusted route-bound evidence adapter counted the complete request, including translation/framing and nontext inputs, against a verified limit with an output cap that includes reasoning. |
| `unknown` | At least one required count, limit or reservation is unverified or unavailable. Estimates and declarations remain visible, and generation remains usable. |
| `reject` | The verified input count plus explicit output reservation, or that reservation alone, exceeds a verified limit. Generation refuses before provider dispatch with `context_limit_exceeded`. |

**All currently shipped limits are declarations. No verified route/counter is
registered initially.** Therefore ordinary requests currently report `unknown`,
even when a declared window is present. `context_limit.known` in the capability
catalog means a declaration exists, not that the provider's current limit has
been independently verified. An unknown provider ID is not assigned a limit from
its spelling. An estimated excess over a declaration is advisory, never a silent
source trim or an automatic rejection of that model.

## Estimate basis

The shared estimator serializes every supplied request field as compact JSON and
counts UTF-8 bytes. It reports groups for input/history, instructions, tools/tool
choice, output-format/schema and all remaining controls. Its input estimate is
one unit per serialized byte plus 2,048 framing units. This deliberately cautious
planning estimate is **not a proven upper bound or compatible tokenizer count**.
JSON encoding and protocol fields are not identical to the provider's model
context. Non-ASCII text is measured in UTF-8 bytes, not Python character count.

The explicit requested output cap is reported separately. Omitted output caps,
provider default reasoning, image/audio/video/file costs, opaque replay state and
server-side history remain unknown unless a compatible evidence adapter covers
them. No media URL is fetched and base64 size is not promoted to image tokens.
Extra request fields are counted in controls; tool/schema descriptions and long
instructions cannot disappear behind a prompt-only estimate. The request is
never rewritten or truncated by assessment.

## Generation and Anti

The gateway assesses the validated Responses request on each selected route,
before Google account selection, after read-only native OpenAI auth/endpoint
resolution, and after loading actual BYOK configuration but before transport. Any future compatible counter
must understand that route's complete translation and defaults; counting visible
input text alone does not qualify. The declaration binding is re-evaluated each
request. The standalone preflight endpoint does not resolve native OpenAI auth,
so its deployment-bound verification remains unknown even if a future adapter
is installed. `X-Antigravity-Context-Status` exposes the result on accepted generation
responses, with a second header explicitly labeling the estimate. Detailed
inspection uses the preflight endpoint. Its observation does not reserve a route
or promise configuration stays unchanged until a later generation call.

Anti independently assesses its complete assembled request after catalog/model
selection. Successful call metadata contains `context_preflight` and
`context_calibration`. Reported input usage is compared with the estimate as
calibration evidence only; absent usage stays unknown. These observations do not
verify a model limit, prove a tokenizer, or establish billing. Full retention
keeps this metadata; summary remains a bounded preview and never-mode does not
retain call histories. Currency and token-admission allowances remain governed
by [spend controls](SPEND_CONTROL.md).

## Adding verified evidence

The code-owned `context_preflight.VERIFIED_CONTEXTS` registry is deliberately
empty. A reviewed entry must bind `VerifiedContext` to the route/deployment
fingerprint and cite its evidence. Its compatible count adapter must handle the
full validated Responses request, translated framing, tools, media, hidden
history and provider defaults; return `None` whenever it cannot. A stale binding,
invalid count or failing counter falls back to unknown. Counts and limits cannot
be injected through request metadata, provider declarations or a model name.
The explicit output-cap semantics must cover reasoning before a fit can be
certified. Synthetic test oracles exercise this contract; they are not shipped
verification evidence. No custom tokenizer or new dependency is included.
