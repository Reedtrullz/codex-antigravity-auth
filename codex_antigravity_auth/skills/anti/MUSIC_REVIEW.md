# Gemini music review

`review-music` is a standalone advisory profile using the ordinary Google
Antigravity OAuth account and an eligible advertised Gemini audio route. It
requires neither Keyspilli nor an API key, transcription model or second account.
Current audio transport is experimental; availability and listening quality are
unverified for an arbitrary connected account.

```sh
python scripts/anti.py review-music --model gemini-3.8-flash --audio phrase.wav --prompt-file objective.txt --dry-run --json
```

Add `--probe-unverified-audio` only when explicitly authorizing the upload.
Optional `--evidence-json bundle.json` uses `schemas/music-evidence-v1.json`.
Clip order must match attachments; SHA-256 and duration must match captured WAV
bytes. Captions, source claims and allowed differences remain untrusted data.
The schema uses seconds, generic source authority and evidence origins; it
requires no product-specific music mode or paths. Duplicate JSON keys, stale
hashes and invalid intervals refuse before a gateway request.

The profile shares listen's PCM16 limits (two files, 2 MiB each, 30 seconds each),
90 second deadline and one backend attempt. The default output allowance is
2,048 tokens, including reasoning; explicitly setting `--max-output-tokens 4096`
permits a larger bounded response. Retry,
fallback, automatic routing and source pre-reading are disabled. The response
schema is fixed. A complete response may be bare JSON or one whole `json`/plain
Markdown fence. The fence is removed only for validation, with its encoding
recorded in `music_response_encoding`; `output_text` retains the original bytes.
Surrounding prose, multiple documents, invalid fields and truncated output
remain partial with no
automatic retry. All findings are `model-advisory`. Empty findings, ratings,
completion and model confidence cannot establish provider listening or musical
acceptance. `musicalAcceptance` always remains `not-established`.

Transport support, observed backend acceptance, measured perceptual qualification
and suitability for a musical task are separate claims. This feature implements
transport and validation; qualification requires fresh controlled listening
experiments and human musical review. Non-Google models, DSP and repair workflows
belong in the consuming application, including the Keyspilli plugin.

Increasing the allowance can prevent reasoning from crowding out the answer;
it does not verify the answer's acoustic claims. Count and timing controls must
still pass independently. A bounded diagnostic run on 5 October found that Pro
completed a repeated-note review at 4,096 tokens but reported five attacks on a
four-attack capture. Neither completion nor format normalization qualifies a
model for musical approval. Use narrower objectives and retain uncertainty.

## Compact one-claim profile

Use `review-music --compact-review --audio CLIP.wav --evidence-json ONE_CLAIM.json`
with an explicitly selected eligible Antigravity Gemini model. Compact mode
requires exactly one attached clip and one evidence claim. It returns at most
one advisory finding citing that claim, descriptions/uncertainties of at most
500 characters and at most four limitations of 240 characters each. Existing
one-attempt, timeout, token, upload and route controls still apply. Defaults
remain the standard profile; compact mode adds no dependency on Keyspilli.

A supplied measured claim is evidence, not independent Gemini hearing. Unknown
source authority and uncertainty remain explicit. Empty findings never approve
music. Invalid or truncated output is retained as partial with no retry; the
result records `music_review_profile` and its response-schema/evidence digests.
Dry-run prepares the prompt/schema without contacting a provider.

Character limits are enforced by the local result validator. The provider
response schema uses only the gateway's supported native constraints; it does
not promise provider-side string-length enforcement.
