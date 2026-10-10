# Anti and Keyspilli Music Listening Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans for the Anti work; the separate Keyspilli project chat owns its integration and skill. Steps use checkbox syntax for tracking.

**Goal:** Provide a working local flow in which the Keyspilli skill sends actual sampled-piano excerpts through Anti, receives bounded advisory musical findings, combines them with note evidence, and exposes playable previews and localized review results for the user.

**Architecture:** Reuse #160's PCM WAV transport and consult result envelope. Add a small `listen` profile that permits one bounded attempt and avoids source pre-reading, automatic retry and fallback. Keyspilli owns phrase preparation, domain finding validation, timing maps, budget/checkpoint ledger, symbolic interpretation and repair/recheck.

**Tech Stack:** Python 3.10+, existing FastAPI/httpx gateway and Anti helper; Keyspilli TypeScript/Node 22, FFmpeg and existing sampled-piano renderer. No new provider/account or large model installation.

**Spec:** [Music listening research and design](/Users/reidar/.codex/worktrees/pr-integration/codex-antigravity-auth/.superpowers/sdd/2026-10-02-pr-integration-and-issue-closure/music-listening-research-20261003.md).

## Global constraints

- Preserve primary WIP and other tasks. Anti work uses `/Users/reidar/.codex/worktrees/stream-integration/codex-antigravity-auth`, starting at `3dce6f4b0a540223d1d868cf0198c75b82714000`.
- Audio: one/two explicitly selected PCM16 WAVs, ≤2MiB each, ≤4MiB total, ≤30s each. Use 20–22s sampled piano clips when suitable.
- Explicit model and experimental audio opt-in remain required. No Claude/text-only audio fallback. Catalog claims remain conservative.
- Every `listen` invocation allows at most one provider POST, ≤2,048 output tokens and ≤90s total. No automatic retry, judge, fallback or source pre-read. Smaller limits are allowed; attempts to loosen these limits fail before network.
- Distinguish captured/submitted media, provider audio consumption, listening calibration and musical approval. Do not fabricate modality token usage or declare empty findings approval.
- Before long tests/renders, disk ≥30GiB. Reuse existing compatible runtimes and dependencies.
- The user approved the research direction and requested execution through a working, user-testable system. The earlier eight-call batch remains exhausted. Prepare and run the new bounded piano study, up to 48 serial attempts across two explicitly selected OAuth routes, as necessary verification of this authorized system. No paid fallback, private recordings or additional generation outside that ledger.

## Review focus

- A failed/incomplete output must remain incomplete with no hidden second POST.
- Changing audio, prompt, model, rendering or timing invalidates matching review evidence.
- Successful audio capture and summed token counts must not become a false AUDIO consumption attestation.
- Source and output clocks may differ; precise repairs require validated correspondence.
- A user must be able to run a no-upload preflight and play/view actual outputs without editing global Codex settings.

## Shared interface and ownership

Keyspilli chat: `01a101bf-7d8c-74c2-b0f8-553c2e712c33` (Keyspilli music listening integration). Parent is explicitly authorized to inspect and steer this chat. Child owns its repository and canonical `/Users/reidar/plugins/keyspilli` skill changes; parent owns Anti code and the shared live-attempt ledger. Child performs no live inference until allocated by the parent.

Command: explicit Python and pinned `anti.py`, `listen --base-url URL --model MODEL --audio REF.wav --audio CANDIDATE.wav --probe-unverified-audio --prompt-file PROMPT --json`. Optional limits may tighten defaults. `--dry-run` performs no HTTP. Result keeps the established `schemaVersion`, `runStatus`, `mode`, `model`, `metadata`, `output_text` envelope; `mode` identifies listening. `metadata.media_coverage` pins captured audio identities and records attempts while preserving unverified/not-run listening claims. Keyspilli parses/validates musical JSON in `output_text` and checks result completeness separately.

## Task 1: Bounded Anti listening entry point

Files: `codex_antigravity_auth/skills/anti/scripts/anti.py`, `tests/test_wav_audio.py`, `codex_antigravity_auth/skills/anti/WAV_AUDIO.md`, `codex_antigravity_auth/skills/anti/SKILL.md`.

- [x] Write failing tests for parser/profile, missing model/audio, rejected loose limits/fallback, no pre-read, no-upload dry run, complete output and incomplete output with exactly one observed POST.
- [x] Run guarded focused tests and preserve RED evidence.
- [x] Add shared consult argument registration and `listen` profile using existing transport/policy/admission owners. Keep consult/ask behavior unchanged.
- [x] Run focused GREEN tests, existing WAV/privacy/budget/incomplete-output tests and artifact/parity checks. Document exact command, result evidence and known musical limitations.
- [x] Commit reviewable Anti changes and communicate the implemented contract to Keyspilli.

## Task 2: Keyspilli adapter and skill, separate chat

- [ ] Implement explicit Anti provider selection, pairwise actual-playback excerpts, PCM rendering and versioned timing/hash pins.
- [ ] Implement no-upload preflight, one aggregate request cap, matching-only resume and incomplete/malformed/stale-result refusal.
- [ ] Update canonical skill/check_checkout requirements and expose exact compatible commands. Preserve unrelated skill WIP and installed cache immutability.
- [ ] Verify symbolic exact-note/playability checks separately from audible claims; include per-mode/difficulty/phrase coverage and repair/recheck.
- [ ] Produce a playable local package and readable review report, focused tests and draft PR; parent reviews and sends needed corrections.

## Task 3: Integrated local demonstration and route comparison

- [ ] Build a run-local validation mirror, owned temporary gateway and safe observer using existing OAuth. Do not interrupt the user's installed gateway.
- [ ] Freeze 24 tasks per selected route: eight clean comparisons, eight checked mutations, eight piano/negative/order controls. Pin sources, sampled bank, PCM bytes, neutral prompts, answer keys, expected faults and immutable attempt ledger.
- [ ] Run at most 48 serial attempts with zero retries/fallback/judge; stop a route after a required negative gate fails. Ensure preparation errors make zero POSTs.
- [ ] Grade audio-only responses before symbolic context, including stream, category and localized evidence. Keep pilot thresholds and confidence limits from the research design; do not certify broader musical acceptance.
- [ ] Verify Keyspilli-to-Anti-to-report behavior with real outputs. Diagnose and repair integration failures, then rerun only affected offline checks or remaining allocated live tasks.

## Task 4: Review, delivery and user test

- [ ] Independent final code review and required focused/full/installed-artifact checks; fix actionable findings.
- [ ] Publish/attach reviewable draft PRs as appropriate and verify exact-head CI. Do not merge/deploy merely to claim completion.
- [ ] Surface a single local user-test command, pinned helper/gateway paths, no-upload option, playable Original/Chords previews, readable findings and explicit current route calibration.
- [ ] Refresh the canonical skill through the normal supported plugin path if required for the user to invoke it, preserving unrelated local changes.
- [ ] Log exact evidence in Obsidian, preserve receipts and clean owned scratch. Keep the user-test runtime accessible through an explicit launch command; no global model/config changes.

## Completion evidence

A Keyspilli skill invocation can prepare actual audio, successfully submit an explicitly chosen pair to Anti within one attempt, validate a complete returned musical review, combine findings with the note evidence, and expose a playable/report package. Missing audio, unsupported routes, incomplete output, stale inputs and ambiguous resumes refuse safely. Live calibration results are reported honestly; a weak listener remains unqualified rather than being presented as musically accepted.
