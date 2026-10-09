# Fixture Manifest Reconciliation — Bounded Gemini Listening Qualification

**Status:** Review-only prep. Nothing is armed, no run ID is minted, and no live provider call has been made under this protocol.

**Date:** 2026-10-09
**Protocol:** `docs/superpowers/specs/2026-10-09-bounded-gemini-listening-qualification-protocol.md` (merged via PR #171)
**Artifacts:**
- Manifest: `docs/qualification/fixture-manifest.json` — SHA-256 `7e0d0822e622407de43fb016dd84c927b063a9bd6c1e2e2b6933a74fc1ba05c9`
- Generator: `scripts/qualification/build_qualification_manifest.py`
- Scorer: `scripts/qualification/score_qualification_run.py` (self-test 10/10 pass)

## 1. What the evidence tree actually contains

Ground truth comes from the Keyspilli capture worktree
(`20261005-afk/player-search-solution/20261006-014631/fresh`): 28 rows in
`evaluation/rows.json`, each with fitted events (MIDI, start seconds,
amplitude), plus per-clip capture receipts and clip WAVs under
`capture-retry/`. Only the `capture-retry` tree has the accepted attempt's
receipts for all captured clips; the `corpus` tree stops at attempt 1
(fresh-00..06).

### Provenance finding (hash discrepancy resolved)

`rows.audioSha256` is the SHA-256 of the **pre-compressor** audio the fit was
computed against, not the post-compressor clip. The capture receipt records the
final clip hash. Verified: for every included clip, the receipt hash matches
the clip bytes exactly (e.g. fresh-10: receipt `7ab82596...` == media file;
rows basis `699fe7e7...` == the `*-before-compressor.wav` variant). The
manifest records both: `clipSha256` (receipt-verified) and
`fitBasisSha256` (fit basis).

## 2. Spec targets vs available fixtures

| Class | Spec target | Available | Excluded and why |
|---|---|---|---|
| Core count | 32 | **19** | fresh-00..06: only attempt-1 (failed) capture exists, no accepted receipt/clip (7). foreign-solo-2/3: no fitted events, no ground truth (2). |
| Quiet | 16 | **19** (same clips) | — |
| Repeat pitch | 12 | **5** | Structural: only 5 clips contain a repeated pitch. |
| Refusal controls | 12 | 12 defined in manifest | Executed at preflight, not manifest build. |

Total live generation calls under the reconciled set: 19 + 19 + 5 = **43**
(spec assumed 60).

## 3. Quiet threshold calibration (owner decision required)

The spec set the quiet-note ratio at <= 0.15. No clip in the tree meets that:
minimum available ratio is 0.180, so **zero** fixtures would have a "yes" ground
truth and the quiet gate would pass trivially by answering "no" every time.

The rebuilt manifest uses **0.30**, which yields 4 "yes" / 15 "no" — a gate
that actually tests perception. This is a deliberate, documented deviation from
the spec, flagged for owner approval before arming.

## 4. Gate scaling for the smaller set (owner decision required)

The spec's absolute thresholds assume its larger fixture set. Proportional
proposals preserving roughly the spec's error budgets:

| Gate | Spec (at spec's set size) | Proposed (reconciled set) |
|---|---|---|
| Core count errors | <= 2 of 32 | <= 2 of 19 (unchanged) |
| Quiet correct | >= 14 of 16 | >= 17 of 19 (<= 2 errors) |
| Repeat correct | >= 10 of 12 | >= 4 of 5 (<= 1 error) |
| Refusal controls | 12/12 | 12/12 (unchanged) |
| Accepted-wrong | 0 | 0 (unchanged) |
| Latency p95 | <= 20 s | <= 20 s (unchanged) |
| Peak RSS | <= 2 GiB | <= 2 GiB (unchanged) |

The shipped scorer's built-in gate constants still match the **spec** values;
the owner-approved reconciliation values must be confirmed before any run so
the scorer can be finalized against them (scorer is frozen after that).

## 5. Scorer

Deterministic Python, standard library only, not an LLM, no network. Scores
forced-integer answers for core/repeat, boolean for quiet, marks high-confidence
(>= 0.7) wrong answers as accepted-wrong, computes latency p95 and RSS gate,
writes the verdict report exactly once. Self-test: 10/10 checks pass
(passing answer scored correct; confident wrong repeat answer triggers both the
wrong count and the accepted-wrong gate; 25 s latency fails the p95 gate).

## 6. Success-criteria status (protocol section 12)

| Criterion | Status |
|---|---|
| Owner read/approved spec | Pending (this doc is part of that review) |
| Fixture manifest written, SHA-256 recorded | **Done** (rebuilt set; owner confirms) |
| Independent scorer exists, tested, SHA-256 recorded | **Done** (self-test 10/10) |
| No live generation under this protocol's run ID | **Holds** (no run ID minted) |

## 7. Explicitly not done

- No run ID minted. No dispatch. No upload. No generation. No token refresh.
- Refusal controls not executed (they run at preflight, under the run ID).
- Scorer gate constants not yet updated to the owner-approved reconciliation
  values (deliberately left at spec values until the owner confirms).
