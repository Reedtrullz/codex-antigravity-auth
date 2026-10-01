# Google output parts and generated-media rejection

The Google adapter represents text, reasoning and finalized function calls.
Both buffered responses and streaming events use one part normalizer. Supported
fields within a mixed part are processed independently, so text or thought flags
do not hide a valid function call. Function validation remains owned by
[tool-calls.md](tool-calls.md). `thoughtSignature` is opaque metadata: it never
turns ordinary text into reasoning and is not exposed as an invented replay field.

## Unsupported output

Generated image, audio and video output has no client-compatible bridge in this
adapter. The presence of `inlineData`, `fileData`, `videoMetadata` or
`mediaResolution` produces `unsupported_output_modality`, even if its value is
empty or malformed. The adapter does not decode, fetch, copy into output or save
media payloads, MIME values or file URIs. Other unrepresented active fields,
including executable code and function responses, produce
`unsupported_output_part`; malformed supported fields produce
`malformed_output_part`, including a supplied non-list `parts` container. Media
errors take precedence over malformed parts, then other unsupported parts,
independent of part/candidate/chunk ordering. Error messages are fixed and contain no provider values.

Usable sibling text, reasoning, valid calls and aggregate usage are retained. A
nominally completed response becomes failed. A provider's incomplete result keeps
its incomplete reason and also reports the output error. The stream does not
reset or replay an attempt after observing unsupported output. A failed result
must not be treated as authorization to execute its sibling calls.

The known `gemini-3.1-flash-image` backend is recognized for an explicit HTTP 400
before account selection or provider dispatch, but is excluded from the gateway
and standalone Anti advertised catalogs. Aliases and overlays targeting that same
backend inherit this restriction. The central capability contract reports that
its generation requires an image output bridge. Other models retain image *input*
support. An unknown backend that unexpectedly returns media reaches the same
explicit output failure; a model name alone is not proof of text-only behavior.

## Compatibility and evidence

Google's [Part reference](https://docs.cloud.google.com/gemini-enterprise-agent-platform/reference/rest/v1/Content)
(checked 2026-10-01) separates the data union from thought/signature and media
metadata, and describes inline bytes and file URIs. The public data union allows
one active member. Mixed-field synthetic fixtures exercise loss-aware handling of
existing adapter inputs; they do not claim that the live provider emits those
combinations. Legacy `type: thinking` / `thinking` fixtures remain accepted.

Synthetic tests cover mixed fields, malformed and unsupported parts, signature
handling, preserved usage and incomplete reasons, pre-dispatch model rejection,
and streaming/buffered HTTP routes with an owned fake upstream. The same contract
tests run against installed wheel and rebuilt-sdist artifacts. No live provider,
media decoding, file download, gallery or credentialed test is part of this work.
