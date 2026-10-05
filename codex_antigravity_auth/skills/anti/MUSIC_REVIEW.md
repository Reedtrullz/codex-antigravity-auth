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
90 second deadline, 2,048 output token ceiling and one backend attempt. Retry,
fallback, automatic routing and source pre-reading are disabled. The response
schema is fixed; invalid or truncated output is retained as partial with no
automatic retry. All findings are `model-advisory`. Empty findings, ratings,
completion and model confidence cannot establish provider listening or musical
acceptance. `musicalAcceptance` always remains `not-established`.

Transport support, observed backend acceptance, measured perceptual qualification
and suitability for a musical task are separate claims. This feature implements
transport and validation; qualification requires fresh controlled listening
experiments and human musical review. Non-Google models, DSP and repair workflows
belong in the consuming application, including the Keyspilli plugin.
