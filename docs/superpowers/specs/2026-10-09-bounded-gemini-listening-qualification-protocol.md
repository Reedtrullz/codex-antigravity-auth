# Bounded Gemini Listening Qualification Protocol

**Status:** Review-only draft. No run is armed, no generation is scheduled, and no upload is authorized by this document. Arming requires explicit owner approval recorded against the immutable run ID defined below.

**Date:** 2026-10-09
**Branch:** `codex/music-evidence-account-binding` at `25a47ceedbf5a07a1f7b4ac884e53abf591060b1` (PR #171)
**Companion worktrees:**
- Anti: `/Users/reidar/Projectos/.worktrees/anti-music-review-afk`
- Keyspilli: `/Users/reidar/.codex/worktrees/keyspilli-music-review-afk/Keyspilli` (code candidate `ebb77a52174583e1fe6a947b56b3df5818c68192`)

---

## 1. Purpose and Scope

This protocol defines the admission criteria, evidence taxonomy, confabulation controls, and fail-closed transport profile for a single bounded qualification of Gemini's acoustic listening through the Anti sidecar, using ground-truth piano fixtures produced by Keyspilli's own capture pipeline.

It exists because the 2026-10-09 test verification proved the transport and binding route end to end while simultaneously producing a concrete acoustic mismatch: the model reported three distinct piano attacks where fitted ground truth records four note events at two temporal attack points. This is exactly the model-advisory-not-qualified outcome the architecture predicts, and it motivates every count-accuracy control below.

**In scope:** one immutable qualification run on the branch gateway; fresh bound audio calls against known fixtures; independent ground-truth scoring; admission decision.

**Out of scope:** production admission; merge of PR #171; musical acceptance for any real catalog piece; playability, range, fingering, or difficulty review; owner listening assignments; any modification to the frozen `queen-original-replay` study.

## 2. Evidence Claim Taxonomy

Every artifact produced under this protocol must classify each finding into exactly one tier. Collapsing tiers is a protocol violation.

| Tier | Claim | Established by |
|------|-------|----------------|
| T1 | Transport: the request reached the provider and a response returned | HTTP status, latency, run record |
| T2 | Binding: the correct account was used without refresh/rotation/substitution | Binding verification receipt, inventory hash match |
| T3 | Provider audio acceptance: the backend accepted the audio payload | Provider status field, usage counters |
| T4 | Acoustic perception: the model's audio-derived claims match ground truth | Independent scorer comparing model output to fitted events |
| T5 | Musical acceptance: a real catalog piece sounds correct to a human | Owner or delegated human judgment; never automated |

Only T1-T4 are automatable. T5 remains permanently outside this protocol's scope.

## 3. Ground Truth Authority

Ground truth comes exclusively from Keyspilli's capture/evaluation pipeline in the Keyspilli worktree. The canonical fixture source is:

    /Users/reidar/.codex/worktrees/keyspilli-music-review-afk/Keyspilli/output/music-review/20261005-afk/player-search-solution/20261006-014631/fresh/

within which `evaluation/rows.json` and `capture-retry/media/*.wav` define paired fixtures. Each fixture has:

- A unique clip ID (e.g. `fresh-10`)
- A fitted event list with MIDI pitch, start time (seconds), and amplitude
- A WAV file with a recorded SHA-256 hash
- A temporal attack count (distinct onset timestamps, not distinct MIDI notes)

Ground truth is immutable once the run ID is minted. Any post-hoc correction to fixture data invalidates the run and requires a new immutable run ID.

### Known Fixture Evidence

From the 2026-10-09 verification receipt, fixture `fresh-10`:

| Event ID | MIDI | Start (s) | Amplitude |
|----------|------|-----------|-----------|
| ref-60-v92 | 60 | 0.291 | 1.183 |
| ref-67-v56 | 67 | 0.291 | 1.307 |
| ref-55-v92 | 55 | 0.725 | 0.856 |
| ref-64-v56 | 64 | 0.725 | 0.536 |

Temporal attack count: 2. Note event count: 4. The 2026-10-09 model advisory claimed 3 attacks, which is wrong on both counts. This fixture is retained in the qualification set as a known confabulation probe.

## 4. Immutable Run Identity

The run is identified by a single monotonically assigned ID:

    gemini-listening-qualification-<YYYYMMDDTHHMMSSZ>

Once minted, this ID is written to every artifact, log, and receipt. The run cannot be resumed, retried, extended, or re-scored under the same ID. A protocol violation, infrastructure failure, or threshold change requires minting a new ID and restarting from zero completed attempts. No state carries over.

The prior failed `queen-original-replay` study and the 2026-10-09 test verification are neither resumed nor retuned. They remain as historical evidence.

## 5. Qualification Fixture Set

The set is drawn from the Keyspilli worktree's existing captured fixtures without generating new captures. It consists of four probe classes:

### 5.1 Core Count Probes (32 fixtures)

Each fixture has a known temporal attack count (1-8). The model is asked: "How many distinct piano note attacks do you hear in this clip?" The independent scorer compares the model's integer answer to ground truth. Pass requires exact match.

### 5.2 Quiet Note Probes (16 fixtures)

Each fixture contains at least one note with amplitude below a defined threshold relative to the loudest note in the same clip (target: ratio <= 0.15, calibrated per fixture from ground truth amplitude data). The model is asked whether any very quiet note is present. Pass requires correct yes/no.

### 5.3 Repeat Pitch Probes (12 fixtures)

Each fixture contains the same MIDI pitch played two or more times at distinct temporal attacks. The model is asked how many times that pitch is struck. Pass requires exact match to the ground truth repeat count.

### 5.4 Refusal Controls (12 fixtures)

These are protocol-level controls that do not require audio: submitting malformed payloads, stale bindings, expired tokens, oversized files, and unsupported routes. All 12 must be refused closed before any provider generation occurs. This validates the fail-closed perimeter under the qualification run ID.

## 6. Admission Thresholds

The run is admitted (meaning: "Gemini demonstrates sufficient acoustic perception for advisory-grade score review") only if all of the following hold simultaneously:

| Gate | Threshold | Rationale |
|------|-----------|-----------|
| Core count accuracy | >= 30/32 correct | Allows at most 2 confabulation errors across the full range |
| Quiet note accuracy | >= 14/16 correct | Permits 2 misses on the hardest perceptual class |
| Repeat pitch accuracy | >= 10/12 correct | Tests temporal streaming, not just spectral onset |
| Refusal controls | 12/12 refused | Zero tolerance for boundary violations |
| Accepted-wrong | 0 | No response may assert a claim the scorer marks wrong while self-reporting high confidence; any such case is an immediate fail regardless of aggregate score |
| Latency p95 | <= 20 seconds | Keeps bounded review usable in interactive workflows |
| Peak RSS | <= 2 GiB | Bounds resource consumption for the gateway process |

Failure of any single gate means the run is not admitted. The result is recorded as final under the immutable run ID. No retuning, re-prompting, or threshold adjustment is permitted after the run begins.

## 7. Confabulation Controls

The 2026-10-09 probe demonstrated that the model can produce a confident, structured advisory that is factually wrong on a simple count. These controls mitigate that:

1. **Forced integer answers:** count questions require the model to emit a JSON field `attackCount` as an integer, not prose. Free-text hedging ("approximately three", "I hear several") is scored as incorrect.
2. **Self-reported uncertainty is recorded but does not affect scoring.** A high-confidence wrong answer is strictly worse than a low-confidence wrong answer (see Accepted-wrong gate above).
3. **No prompt leakage:** prompts must not contain the ground-truth answer, the fixture ID, the event count, or any metadata beyond the audio payload itself. The scorer validates prompt text post-hoc.
4. **Independent scorer:** scoring is performed by a deterministic script reading ground truth from `rows.json` and the model output from the run record. The scorer is written and reviewed before the first dispatch. It is not an LLM.
5. **Blank/wrong format handling:** a response that does not parse to the required schema scores as incorrect for that fixture. No re-dispatch.

## 8. Bounded Transport Profile

All live calls use the branch gateway on an isolated port with an isolated state home, as proven in the 2026-10-09 test verification:

- **Gateway:** branch worktree venv, `--port <isolated>`, `--state-home <isolated>`
- **Binding:** four-field opaque JSON (`schemaVersion`, `gatewayInstance`, `accountRef`, `inventorySha256`), minted from live inventory immediately before dispatch, chmod 600, absolute non-symlink path
- **Helper:** branch venv bundled `anti.py` (parity-matched by construction)
- **Audio format:** PCM16 mono RIFF/WAVE, 44100 Hz
- **File limits:** at most 2 files per call, 2 MiB each, 4 MiB combined, 30 seconds each
- **Output tokens:** 4096 maximum
- **Retry/fallback/rotation/refresh:** disabled; single attempt per fixture
- **Attempts:** exactly one backend generation call per fixture; total live generation calls = 32 + 16 + 12 = 60 (refusal controls make zero provider calls)
- **Run timeout:** 90 seconds per fixture; a timeout is scored as incorrect and consumes the attempt

## 9. Execution Sequence

This sequence is recorded here for review. None of it is armed by this document.

1. **Preflight:** verify branch head, clean worktree, isolated state home, no stale accounts, fresh binding minted from live inventory. All refusal controls pass. Record preflight receipt.
2. **Scorer readiness:** the independent scorer script is reviewed, tested against known-input/known-output pairs, and its SHA-256 is recorded. It must not be modified after step 1.
3. **Fixture preparation:** all 60 fixture metadata entries (clip ID, WAV path, SHA-256, expected answers) are written to a single manifest file. The manifest SHA-256 is recorded. The manifest is immutable once the run ID is minted.
4. **Owner authorization:** the owner reviews this spec and the manifest and explicitly approves arming. Without this, execution stops here.
5. **Dispatch:** the 60 core/quiet/repeat fixtures are called sequentially (no parallelism) through the bound helper. Each response and its scorer verdict are appended to the run record immediately.
6. **Scoring:** after all attempts complete, the scorer produces a verdict report. The report is written once. It cannot be re-run.
7. **Decision:** the verdict report is compared against the Section 6 thresholds. The admission decision is recorded as admitted or not-admitted. No partial admission exists.
8. **Receipt:** a final receipt (JSON) captures the run ID, all per-fixture verdicts, aggregate counts, latency percentiles, RSS peak, threshold comparison, and the admission decision. The receipt is stored alongside the run record.

## 10. Frozen Study Preservation

The following are immutable and must not be modified, resumed, retried, or reinterpreted as successes:

- `queen-original-replay` study (failed on m04, permanently stopped)
- The 2026-10-09 test verification run `gemini-test-verification-bound-20261009144311` (transport verified, acoustic mismatch recorded)
- The 2026-10-07 bounded Gemini study (stopped on max_output_tokens truncation)
- The 2026-10-08 player-qualification study (completed with productionAdmission=false)

This protocol does not supersede, replace, or re-score any of them. It is a new, independent qualification with a new immutable run ID.

## 11. Open Questions (Owner-Gated)

1. **One-time session refresh:** the prior session requested a one-time OAuth token refresh before dispatch. This remains owner-gated and is not self-authorized by this spec. If the owner approves arming, the refresh is a single bounded `accounts reset --all --yes` in the isolated state home, followed by one text-mode transport probe (16-token cap) to confirm eligibility.
2. **Fixture selection confirmation:** the 60 fixtures proposed above are drawn from the existing capture tree. The owner may wish to review the specific fixture manifest before arming.
3. **Threshold calibration:** the Section 6 thresholds are informed by the 2026-10-09 mismatch and the frozen m04 failure. The owner may wish to tighten or loosen them before arming.

## 12. Success Criteria for This Document

This spec is complete when:

- The owner has read it and either approved arming or requested changes.
- The fixture manifest is written and its SHA-256 recorded.
- The independent scorer exists, is tested, and its SHA-256 is recorded.
- No live generation has occurred under this protocol's run ID.

Until all four conditions hold, the protocol remains review-only.
