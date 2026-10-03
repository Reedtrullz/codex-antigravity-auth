# Experimental PCM WAV consult input

Anti can capture explicit local WAV files for a bounded advisory consult. The
encoder and fake-upstream transport are tested; **audio acceptance/listening on
the Antigravity OAuth backend is unverified**. A successful text answer is not
proof that a model heard the supplied recording. This feature does not certify
musical accuracy, arrangement fidelity or keyboard suitability.

```sh
# Offline capture/format/size preview: no model catalog or upload.
python scripts/anti.py consult --model gemini-3.8-flash --no-pre-read \
  --audio excerpt.wav --prompt 'Describe the supplied sound.' --dry-run --json

# Explicit upload intent to an unverified backend; run only with your approval.
python scripts/anti.py consult --model gemini-3.8-flash --no-pre-read \
  --audio first.wav --audio second.wav --probe-unverified-audio \
  --prompt 'Compare these recordings in the supplied order.' \
  --max-calls 1 --retry 0 --run-timeout 60 --max-output-tokens 512 --json
```

Use an explicitly selected advertised Gemini model. Automatic routing is refused.
The second command intentionally sends the captured audio through the configured
gateway and its existing Google account. It does not require a new provider API
key, install anything, or verify that the OAuth route supports audio. Without
`--probe-unverified-audio`, non-dry audio calls fail before catalog lookup/upload.
The flag requires `--audio` and does not bypass privacy or capability checks.

## Supported first slice

| Property | Bound |
| --- | --- |
| Commands | `consult` / `ask`; no panel, judge, review, plan, workflow or streaming audio |
| Files | One or two explicit local regular files; no symlinks in any path component |
| Encoding | Classic RIFF/WAVE, uncompressed PCM format1,16-bit samples, mono or stereo |
| Sample rates | 8,000 /16,000 /22,050 /24,000 /32,000 /44,100 /48,000Hz |
| Bytes | At most2MiB per file and4MiB total |
| Duration | At most30seconds per file and60seconds total; byte limits also apply |
| Request | At most8MiB serialized UTF-8 JSON including prompt and encoded attachments |

RIFF length, chunk boundaries, unique format/data chunks, PCM byte rate, block
alignment, frame count and duration are validated. Python's PCM reader must agree
with the chunk metadata. Empty/truncated/compressed/extended-format WAV, unsupported
sample widths/rates, MP3 and other media fail explicitly; there is no transcoding.
URLs, inline command-line data, directories and mixtures with `--image` are refused.
Large/high-rate clips may hit the byte bound before30seconds; select a smaller
excerpt yourself. No microphone capture, implicit screenshot capture, URL fetch,
cloud file service or automatic upload occurs during dry-run.

Capture happens once through the bounded no-follow reader. Original bytes and
embedded WAV metadata are preserved unchanged, including during retries and an
explicitly configured capable fallback. The input order is stable; source filenames
and transcripts are not automatically included as listening clues. Metadata uses
ordinal indices, MIME, bytes, frames, duration, sample rate/channels and SHA-256.

## Gateway extension and capability evidence

The gateway's private content type is **not a standard OpenAI Responses audio
field**. The helper sends it only after a compatible gateway catalog advertises
its versioned `capabilities.audio_input` contract:

```json
{
  "model": "gemini-3.8-flash",
  "stream": false,
  "input": [{"role":"user","content":[
    {"type":"input_text","text":"Describe the supplied sound."},
    {"type":"antigravity_audio","mime_type":"audio/wav",
     "data":"CANONICAL_BASE64_PCM_WAV","probe_unverified":true}
  ]}]
}
```

Every audio part requires explicit boolean probe intent. The extension is limited
to user-message input on eligible Gemini backends. Native OpenAI, BYOK Chat,
Claude, GPT-OSS, generated-image routes, tool/system/assistant audio and streaming
are rejected before provider/account acquisition. Unknown fields and unsupported
formats cannot be dropped into a text-only request. Direct transform/capability
calls enforce the same route/content rules.

The optional `audio_input` catalog object has version1, `format: pcm_wav`,
`transport_supported`, `backend_acceptance: unverified` (or `unsupported`),
`requires_probe_opt_in: true`, the private content type and bounds, and
`listening_verification: not_run`. Existing ordinary `effective.input_modalities`
are unchanged; an encoder/probe contract is not verified model modality support.
Older gateways/snapshots cannot authorize an audio submission. Each selected
primary/fallback route is checked before calls and again at actual submission.

The encoder maps the captured bytes to Google's `inlineData`/`mimeType`/base64
structure. [Gemini audio documentation](https://ai.google.dev/gemini-api/docs/audio)
describes inline WAV input, and the [Part/Blob contract](https://ai.google.dev/api/generate-content)
defines the binary fields. These public API documents establish the wire format,
not acceptance by the internal Antigravity OAuth endpoint. [Python3.10 wave](https://docs.python.org/3.10/library/wave.html)
is the compatibility floor; WAVE_FORMAT_EXTENSIBLE remains intentionally rejected.

## Privacy, outcome and retention

[Data policy](DATA_POLICY.md) checks all paths before reading and authorizes each
primary/fallback submission. Text scanning cannot inspect speech, music or embedded
WAV metadata. An audio acknowledgement identity is SHA-256 of compact sorted JSON
containing `version:2`, the UTF-8 `promptSha256`, and ordered `audio` descriptors.
Changing a recording changes that identity. Existing image/text-only identities
are unchanged. Probe intent never substitutes for a denied policy route/path or
an unacknowledged text-secret match.

`metadata.media_coverage.kind: audio` records capture/submission counts and bounded
attempt traces separately from code scope. `probe_upload_intent` records explicit
intent; `provider_audio_acceptance: unverified` and `listening_verification: not_run`
remain honest even after a text response. Existing generation metadata preserves
requested/actual model/provider and gateway route identity. A dispatch attempt is
not proof of delivery, understanding or verified listening.

Never mode retains only fixed count/duration/intent/limitation receipts; summary
adds bounded descriptor hashes; full retains detailed redacted output and attempt
metadata. Attachment paths, raw WAV and base64 are not stored in these receipts.
Provider error details for audio requests are replaced with fixed diagnostics,
while HTTP status/retry deferral is preserved. Canonical WAV base64 echoes are
redacted before diagnostic/output clipping. Model-generated text can itself reveal
private spoken content; its normal output-retention policy still applies. No OCR,
transcription or audio-content secret scan is claimed.

## Acceptance still required before verified listening claims

All automated tests use generated PCM fixtures and owned fake endpoints. They
prove byte/MIME/order preservation, bounded refusal, cleanup, privacy and package
contracts. The implementation tests did not upload live audio; the subsequent
authorized evaluation below records its separate results.

A separately authorized live evaluation must pin helper/gateway versions, file
hashes and actual route/model; compare the same neutral prompt against no-audio
and unrelated-audio controls without filenames/transcripts giving away the answer;
record outcomes, refusals, latency and bounds. If the OAuth backend rejects or
ignores audio, report unsupported/unverified. Do not call this feature complete
as verified listening or infer musical certification from transport success.
Independent listening and qualified source/arrangement review remain necessary.

## Bounded live evaluation — 3 October 2026

A separately authorized batch used audio source
`a6c0910f8ebc589b4afe1ca5b5a4d0cf26f23fb2` and observed actual prepared
`gemini-3.8-flash-tiered` through the existing Antigravity OAuth route.
Five serial audio/control calls returned HTTP 200 with complete output and exact
WAV MIME/byte/SHA-256 preservation. Each used the same neutral prompt without
filenames or transcripts, a 2,048-output-token cap, a 60-second helper deadline,
and no provider retry, fallback or judge.

| Control | Observed result |
| --- | --- |
| No audio | Correctly denied receiving audio |
| First synthesized speech clip | Exact sentence transcription |
| Unrelated synthesized speech clip | Exact different sentence transcription |
| Two descending tones, 880 then 440 Hz | Counted two but incorrectly described ascending pitch |
| Three rising tones, 440 then 880 then 1,320 Hz | Incorrectly described two descending tones |

Independent PCM inspection reconfirmed the event counts and pitch directions.
This proves bounded speech discrimination in these controls; **full listening
acceptance remains unmet**. Do not infer reliable tone counts, pitch order or
musical-quality review from correct speech transcription. [#160](https://github.com/Reedtrullz/codex-antigravity-auth/pull/160)
remains a draft and [#98](https://github.com/Reedtrullz/codex-antigravity-auth/issues/98)
remains open. Catalog and per-run acceptance fields stay conservative; the
helper does not certify listening from a successful response.
