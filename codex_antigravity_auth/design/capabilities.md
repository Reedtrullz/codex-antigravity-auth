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
only text. Audio, video, files, unresolved image file IDs and unknown content types return a
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
