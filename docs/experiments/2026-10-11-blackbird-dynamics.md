# Blackbird dynamics experiment closeout - 11 October 2026

The owner listened to the untouched Original and the final cumulative dynamics
revision of the first 29 seconds of Keyspilli's Blackbird Advanced arrangement and
stated that the revised version sounds higher quality. Record this as one
owner-approved preview. The earlier failed listening qualifications remain
unchanged; no new qualification, publication or general hearing admission follows.

## What was accepted

- Untouched Original SHA256: `478fd79e14c97e2b194fc71c0232691235d179d61a2bdb04e9711adcbe630e8a`.
- Final revised SHA256: `d9d9ea8837669154f60a565c5ae6e2a32c5ce394ae62812f7e56a2355126ad11`.
- Intermediate full-dynamics SHA256: `a6487a519446c757afd9ca4490b1afd68dc7b831fd2e751c2cfd43a8fe09e90c`.

The 190 notes retain pitches, onsets, durations, hands, initial two-second rest
and all non-event render settings. The final version has 163 velocity reductions
relative to the untouched Original, 27 unchanged velocities, 66 changed left-hand
and 97 changed right-hand events. Its taper revision raised 29 late velocities by
1-7 units relative to the intermediate version, confined to 24.5-28.875s. Earlier
accepted authored dynamics remained exact. The 29s endpoint is an excerpt cut.
Original is an application mode, not an authenticated artist score.

Local previews used the same 226 hash-pinned SplendidGrandPiano assets and shared
Player settings, without loudness normalization. Both final comparison files
are PCM16 mono 32kHz, exactly 29 seconds, non-silent and free of clipped rail samples.
Keyspilli owns this source/arrangement/render workflow; Anti owns only the
connected-Google Gemini advisory transport and portable evidence validation.

## Observed medium runs

All four runs used explicit medium thinking, 65,536 allowed output tokens, one
backend attempt, no rotation/retry/fallback and retained complete output.

| Task | Input | Output including reasoning | Reasoning | Answer | Total | Seconds |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Full dynamics proposal | 7,988 | 17,206 | 11,346 | 5,860 | 25,194 | 104.180 |
| Targeted taper repair | 9,191 | 16,890 | 15,155 | 1,735 | 26,081 | 134.295 |
| Intermediate versus revised taper | 4,557 | 4,399 | 3,368 | 1,031 | 8,956 | 41.708 |
| Untouched Original versus final revision | 8,330 | 3,723 | 2,426 | 1,297 | 12,053 | 31.741 |

Local evidence directories are retained under Keyspilli's
`output/song-review/blackbird-original29-medium-{full-dynamics,taper-repair,taper-ab-review,original-revised-ab-review}-20261011`.
The final gateway request was `req_8acc79e6c2b2c6bb`,
`2026-10-11T16:15:37Z`, completed/stop. Its raw text matched the helper's final
record and checksum-verified immutable result revision. Its advisory preference
was B: clearer opening balance, middle phrasing and apex-to-taper continuity,
with softer percussive bite as the tradeoff. The owner independently agreed with
the resulting audio preference; processor behavior and voice-role explanations
are still not independently established.

## Code changes justified by the experiments

1. Preserve truncated consult output before a permitted text-only retry can
   fail; disable automatic token-cap retries for audio consult/ask.
2. Add an explicit extended music-review option for 65,536 tokens/600 seconds while
   preserving standard/compact/listen limits, one-attempt behavior, route checks,
   upload intent, account binding, result validation and advisory status.
3. Record actual reasoning/answer/total token use separately from the allowance
   and preserve unknown values. No source pre-reading or implicit fallback.
4. Document medium with liberal explicit allowances as a practical starting
   point, not a universal model-quality ranking. High failed to complete at both
   tested caps; larger allowances alone did not guarantee a usable answer.

No new qualification study or provider call belongs to this closeout. Future
musical-capability development should build on portable evidence and preserve
this accepted preview and the failed qualification records. Do not encode this
song's velocity numbers as a general musical rule or alter catalog arrangements
automatically. The user has not authorized publication of this preview.

## Initial local implementation verification

The changes are local on `codex/blackbird-music-review-wrapup`, based on
`c4413668d8d0e417803f0dff68383345ae4f74f3`. Validation ran on an identical snapshot
outside the protected real `.codex` tree, retaining the test startup guard.

- Full protected suite: 4,044 passed, 245 subtests passed, 3 skipped, 4 warnings.
- Final focused music, audio, run-control and spend-control checks: 205 passed.
- Ruff and whitespace checks passed.
- Basedpyright reports 179 existing helper errors in both baseline and changed
  source, with no errors on changed lines. Its existing warning backlog remains.

No provider request, new qualification, installation, commit, merge or deployment
was performed during the initial local closeout. The subsequent user instruction
explicitly authorizes committing, merging and updating the installed Anti skill.
