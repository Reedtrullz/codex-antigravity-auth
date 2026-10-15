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

Before upload, `review-music` refuses any attachment whose decoded PCM samples
are all exactly zero, identifying its attachment letter. This is a deterministic
digital-silence check, not model hearing, a loudness threshold or a music detector.
Quiet nonzero recordings and recordings with internal rests remain eligible.
Use `listen` for an explicitly authorized blind silence probe; it retains silent
audio unchanged. Every WAV is preceded by its attachment letter and ordinal in
the upstream message so comparison IDs have explicit boundaries. Those labels
contain no filename, source claim or expected answer.

An explicitly advertised alias with a distinct configured Google backend keeps
that backend selection through gateway normalization. In particular,
`gemini-3.1-pro-high` selects `gemini-pro-agent`; selecting the base
`gemini-3.1-pro` uses its separately configured backend. Availability and measured
listening results for either route do not transfer to the other.

A fixed advertised model ID may contain an effort word without accepting a
separate reasoning parameter. Anti does not synthesize that parameter when the
exact catalog entry declares reasoning unsupported; the selected model ID is
unchanged. Explicit effort aliases on supported routes still carry their level.

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

## Extended practical review

For an explicitly authorized evidence-assisted comparison, `--extended-review`
permits up to65,536 output tokens and600 seconds while retaining exactly one
generation attempt. It is mutually exclusive with `--compact-review`. Select
the allowance, deadline and Gemini reasoning alias explicitly; the flag alone
does not raise the standard defaults. `listen`, standard and compact review
retain their existing caps and defaults, including for frozen qualifications.

```sh
python scripts/anti.py review-music --extended-review \
  --model gemini-3.8-flash-medium \
  --audio original.wav --audio revised.wav --evidence-json evidence.json \
  --prompt-file objective.txt --account-binding-json binding.json \
  --max-output-tokens 65536 --max-total-output-tokens 65536 \
  --timeout 600 --run-timeout 600 --save-output full --json --dry-run
```

Replace `--dry-run` with `--probe-unverified-audio` only for the authorized
upload. The private binding must identify a currently eligible idle account;
neither preparation nor a saved binding proves current eligibility. A bound
failure never refreshes, rotates, retries or substitutes an account. The fixed
review schema, exact ordered WAV/evidence hashes and response validation still
apply. Extended review supplies an assessment, not a music-specific edit plan
or renderer; those remain the consuming application's responsibility.

Results record `music_review_profile: extended-v1` and the selected allowance,
deadlines and requested `reasoningEffort` in `music_request_configuration`.
`music_token_usage` reports observed input, output including reasoning, reasoning,
answer and total tokens. Unknown counts stay null, including answer tokens when
reasoning usage is unavailable. These are provider-reported counts, not native
audio-token classification or a billing guarantee. `metadata.elapsed_ms` records
generation latency. The same accounting is available for standard/compact music
reviews. Full retention keeps the returned text and immutable revision, including
partial or invalid output; truncated JSON never becomes a completed review.

The October11 Blackbird experiment supports medium reasoning with a liberal
explicit allowance as a practical starting point for this workflow. Medium
produced complete dynamics proposals and comparisons; high exhausted both32,768
and65,536 allowances, while low was faster but proposed a smaller local change.
The owner preferred the final cumulative revision against the untouched Original.
That is acceptance of one saved29-second preview, not proof that medium is
universally best, a new qualification, or permission to publish arrangements.
The large allowance is a ceiling: actual use should be measured per task.

For comparisons, identify the untouched baseline and cumulative candidate
clearly, omit earlier verdicts and favorable proposal rationales, and evaluate
the whole arc and local tradeoffs. Permit improved, unchanged, worse and uncertain
verdicts. Keep measured levels, authored events and subjective observations
separate; quieter audio alone is not better music, and an excerpt cut is not a
verified song ending. Targeted repairs must preserve the accepted neighboring
event values and the original; validate and render them locally before review.

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

## Private Bound Listen

`listen` and `review-music` both accept `--account-binding-json /absolute/file`.
The existing four-field private binding is verified against the selected model's
current gateway inventory before generation; the gateway then acquires that exact
lease. Stale, busy or ineligible bindings fail closed without refresh, rotation,
retry or account substitution. Only the private request header carries the binding.
Receipts expose its canonical digest, gateway instance and
`account_binding_verified_before_attempt`, never the account reference or inventory.
Dry-run does not contact the gateway and cannot establish account eligibility.

Pairwise consumers can pass a JSON object as `--response-schema` (not a filename).
The helper appends its canonical sorted JSON to the exact prompt identity; the
consumer remains responsible for strict result and evidence validation. This is
a transport/format contract, not acoustic qualification or musical approval.

Character limits are enforced by the local result validator. The provider
response schema uses only the gateway's supported native constraints; it does
not promise provider-side string-length enforcement.
