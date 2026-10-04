# Capability and fidelity contract

Capability declarations describe what this gateway can carry faithfully. They do
not establish current provider availability or successful live acceptance.

## Input attachments

The gateway validates input before account acquisition or outbound requests. It
accepts text and images where the selected model explicitly supports them. Known
native aliases inherit their canonical definition. Unknown native backends and
user overlays default to text; an overlay may declare
`input_modalities = ["text", "image"]`. Unknown backends retain text passthrough.

Image sources are HTTP(S) URLs without embedded credentials, or canonical base64
data URLs with MIME image/png, image/jpeg, image/gif or image/webp. Each inline
image is limited to 20 MiB decoded, checked before allocation. These are bounded
syntax/MIME checks, not an image decoder. Accepted URLs and encoded bytes are
forwarded unchanged; the gateway never downloads them. Chat image detail is
preserved; Google rejects low/high detail because that control is not mapped.

BYOK routes default to text. Provider or per-model `capabilities` may declare
`input_modalities: ["text", "image"]` and `image_forms: ["url", "data_url"]`.
Use only forms supported by that provider/model. Per-model declarations override
provider declarations. The picker and dispatch use the same contract.

Images in system/developer roles are rejected because those adapter roles carry
only text. Ordinary audio, video, files, unresolved image file IDs and unknown content types return a
400 with an input field path. A mixed request is rejected in full; no unsupported
attachment is converted into a text label or silently discarded. No implicit
text-reference mode, media downloader or transcoder is provided. Tool-result
arrays containing media are rejected because their current translation is text.

Google output text carrying `thoughtSignature` remains ordinary text unless the
provider explicitly marks it as a thought. This contract does not claim that
opaque Google continuation signatures are preserved or that they are required by
an upstream model. Those are separate from input image support.

## BYOK reasoning

A provider/model must explicitly declare its request-effort mapping. A legacy
`reasoning: true` boolean alone is insufficient and advertises no effort levels.
Absent reasoning settings remain compatible; unsupported requested settings fail
before key resolution or HTTP. The currently implemented mapping follows
[OpenRouter's reasoning contract](https://openrouter.ai/docs/guides/best-practices/reasoning-tokens)
(accessed 2026-10-01): Responses `reasoning.effort` becomes the nested Chat
`reasoning.effort` field. Support must be declared for each configured model or
inherited from its provider, never inferred from the model's name.

Example per-model capability declaration:

```json
{
  "id": "vendor/model",
  "capabilities": {
    "reasoning_effort": {
      "parameter": "reasoning.effort",
      "levels": ["low", "medium", "high"]
    },
    "reasoning_replay": true
  }
}
```

Declare only documented levels for that provider/model. The picker lists exactly
those levels; unsupported summary, token-budget or other options are rejected.
`reasoning: false` disables the inherited mapping; `reasoning_effort: null` clears
it. Other wire mappings remain unsupported until implemented with evidence.

`reasoning_replay` is a separate, opt-in BYOK capability for the existing plaintext
summary/tool-continuation mapping. Effort support does not imply replay support.
Opaque encrypted reasoning or structured reasoning-details replay is rejected on
translated routes, whose adapter cannot preserve it. Native Responses can carry
those fields without translation. This is a transport contract, not a claim that
an arbitrary backend accepts every form of historical reasoning.

Google reasoning-history replay is explicitly unsupported until a preserving
mapping exists. Translated reasoning summaries contain no fabricated encrypted
content fields. BYOK effort configuration must select an effort; an empty object
is rejected instead of being erased.

## Versioned route catalog and standalone Anti

Native structured text output is encoded only for eligible Gemini text routes.
Responses `text.format` accepts `text`, `json_object`, or `json_schema`; JSON uses
`responseMimeType` and schemas use `responseJsonSchema` intact. The output bridge
supports types, properties, required names, additionalProperties, items, string
or numeric enums, numeric/item-count bounds, anyOf, and text annotations. It
rejects other constraints (including references, oneOf exclusivity and patterns)
instead of weakening them through the function-parameter sanitizer. `strict:true`
is rejected because OpenAI strict guarantees have not been established here.
Malformed or unsupported requests fail before account acquisition. Claude,
GPT-OSS, unknown native definitions and image-output routes do not advertise
effective structured output. Public Gemini schema fields establish an encoding
contract; private OAuth acceptance and semantic correctness require separate
verification. Explicit plain-text format keeps the ordinary request unchanged.

`/v1/models` retains the `data` and `models` picker lists and adds
`capability_catalog_version: 1`. Each entry carries `canonical_id`, `alias_of`
and a versioned `capabilities` object: route, backend identity, family, aliases,
transport-supported input/output types, effective declarations, context-limit
provenance, and explicitly unknown availability. Backend support is distinct from
what an adapter can encode. Missing BYOK declarations for tools/structured output
remain null in the contract; a client must not treat null as supported.

Known context limits mean **explicitly declared**, not measured provider limits.
Unknown BYOK and user-extended OpenAI limits are null; the former assumed 128K/400K
values are no longer fabricated. Image/audio/video output is not listed merely
because an upstream model offers it. Registry membership never establishes health.

Canonical native IDs precede aliases, and native definitions precede the OpenAI
registry on overlaps, preserving the router's existing priority. The picker emits
one identity and `shadowed_routes` explains the suppressed OpenAI declaration.
Account selection and cooldowns call the same canonical family resolver, including
Claude-family overlays whose IDs do not contain “claude”. Classic unknown-backend
text passthrough remains available.

Anti consumes this contract during its existing catalog fetch; no second network
request is added. Its standalone snapshot is generated from pure built-in native
definitions by `scripts/generate_capability_snapshot.py`. A test checks exact
snapshot parity; the package asset manifest covers both the consumer and JSON.
Local shorthand/effort aliases remain client conveniences; capability matching
uses the canonical catalog aliases. Unknown BYOK models are conservative in the
snapshot. Missing catalog versions use that explicit fallback; unsupported or
malformed versioned entries cannot retain stale capability support.

Only the tracked bundled skill is maintained in this repository; no user-installed
skill copy or personal configuration is modified by these changes. The generated
snapshot is parity-gated; the surrounding skill documentation is hand-maintained.

Native effort declarations are tied to explicit `thinking_level` or
`thinking_budget` mappings on the model definition. A model without a mapping
advertises no effort control and rejects requested reasoning instead of silently
ignoring it (including 3.1 Pro, GPT-OSS and undeclared overlays). An overlay can
explicitly set `reasoning_mapping` to one of those implemented mappings. Thinking
budgets require an output cap above 1024 when explicitly requested. The native
picker, validator and wire payload are covered together for every published level.
Adapter output types remain listed separately from effective types: undeclared
BYOK reasoning/function-call outputs are not promoted to effective support.

## Experimental WAV probe extension

A separate version1 `audio_input` object describes bounded PCM-WAV encoding for
Gemini backends and explicitly unverified OAuth backend acceptance. It does not
add audio to ordinary effective modalities. The private `antigravity_audio` part
requires explicit probe intent, an explicit model, and a non-streaming Gemini
user message; other routes refuse it before auth/dispatch. No field is advertised
as verified listening. See the owning [WAV contract](../skills/anti/WAV_AUDIO.md).
