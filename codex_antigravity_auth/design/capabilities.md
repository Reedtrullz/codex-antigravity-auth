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

Audio, video, files, unresolved image file IDs and unknown content types return a
400 with an input field path. A mixed request is rejected in full; no unsupported
attachment is converted into a text label or silently discarded. No implicit
text-reference mode, media downloader or transcoder is provided. Tool-result
arrays containing media are rejected because their current translation is text.

Google output text carrying `thoughtSignature` remains ordinary text unless the
provider explicitly marks it as a thought. This contract does not claim that
opaque Google continuation signatures are preserved or that they are required by
an upstream model. Those are separate from input image support.
