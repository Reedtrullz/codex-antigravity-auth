# Anti Gemini Capabilities and Keyspilli Music Support — AFK Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task by task. Execute natively; no subagents by default. Steps use checkbox syntax for tracking. The user's AFK instruction takes precedence over routine approval pauses in workflow skills: complete authorized local work and put authorization/listening decisions in the final packet.

**Goal:** Deliver standalone Gemini musical-review capabilities in Anti for users with an existing Google AI account, and implement every other music-processing capability through the Keyspilli plugin and its host tooling.

**Architecture:** Anti owns a Gemini-only review profile over the existing Antigravity OAuth connection, with portable inputs/output and no Keyspilli dependency. Keyspilli owns optional Python analyzers, musical correspondence, other model adapters, reports and repair previews. Keyspilli can export a generic evidence bundle to Anti; both products also operate independently.

**Tech Stack:** Existing npm workspaces, Node 22.22.3, TypeScript/Vitest, Playwright/Chromium, Python 3.11 audio environment, NumPy/SoundFile and optional pinned transcription/model runtimes; existing Anti Python helper and JSON schemas. No new gateway DSP dependency or general agent framework.

**Spec:** [Execution specification](/Users/reidar/.codex/worktrees/stream-integration/codex-antigravity-auth/docs/superpowers/specs/2026-10-05-keyspilli-music-review-afk.md). Read it with [the research report](</Users/reidar/Obsidian/Hermes/Hermes/Personal/Projects/Codex Antigravity Auth/Music review solution and qualification plan - 05-10-2026.md>).

## Global Constraints

All specification boundaries apply, including 30 GiB disk floor, bounded new footprint, one neural worker, frozen old study, zero AFK remote inference/uploads, preserved primary/catalog state and pending human acceptance. This plan does not spend old live-call allocations or adopt old cross-chat ownership instructions.

Implementation source roots inspected for this plan:

- Keyspilli: `/Users/reidar/.codex/worktrees/keyspilli-listening-review/Keyspilli`, HEAD `b75067169a1959c31199f277c4fec3ba4f96f47d`, existing untracked `.superpowers/`.
- Anti: `/Users/reidar/.codex/worktrees/stream-integration/codex-antigravity-auth`, HEAD `56ade46237c6e57efbd7aed37e57e1e3c0b2f6d6`, existing untracked `.superpowers/`.

Task file inventories below are anchored to those full roots; at execution, map their unchanged suffixes into the isolated roots recorded by Task 0. Existing paths are inspected files; new paths are proposed. Do not edit the research/source checkouts during implementation.

## Product ownership and execution order

The user's 5 October correction governs all tasks: new Anti functionality is reusable Gemini capability through Antigravity and an existing Google AI account. All other music support is part of `@Keyspilli`. Existing unrelated Anti routes are outside this change.

| Owner | Required work | Dependencies for ordinary use | Acceptance |
|---|---|---|---|
| Anti | Gemini review profile, bounded WAV transport reuse, optional generic evidence, structured findings, model/route receipts and truthful capability docs. | Existing Anti installation and an eligible connected Google AI account; no Keyspilli, BYOK key, model download or audio ML stack. | Standalone disposable install and mocked OAuth round-trip; fresh live qualification is an end-review item. |
| Keyspilli plugin/host | Transcription, DSP, benchmark/capture tooling, source and arrangement rules, non-Gemini models, previews, listening packs and repair workflow. | Capable Keyspilli checkout; optional tool dependencies only for selected features. | Plugin discovery and host execution, zero-upload baseline, model-specific empirical results, final musical gates. |
| Integration | Keyspilli export to Anti's generic evidence schema; explicit compatible Gemini invocation. | Both installed only when using this optional link. | Contract round-trip without creating a Keyspilli import/dependency in Anti. |

Execution order: Task 0 -> **Task 8A first** (portable Anti feature) -> Tasks 1–7 -> Task 8B -> Task 9 -> Task 9B (plugin delivery) -> Task 10. Each product gets its own commits and review artifacts. The task numbers retain the measurement dependency sequence for readability.

## Review Focus

- Decoder/downmix changes and quiet stereo cancellation: identical derivatives must be reproducible; cancellation is surfaced, not classified as missing authored music. Tasks 1–3.
- Pedal overlap, reattacks and tempo/transport changes: preserve key versus sounding offsets and raw timing errors; alignment must not hide a bad entrance. Tasks 3–5.
- Incorrect supplied score or missing source authority: assistance cannot confirm expectations by construction or invent source approval. Tasks 5–6.
- Crash, timeout, cancellation and partial output: record terminal state, release children, resume only completed matching cases, and make zero hidden requests. Tasks 2, 4, 8.
- Stale repair/report identity and imported captions: preserve catalogue state and bind all claims and previews to fresh audio, checkpoint and event hashes. Tasks 7–10.

## Task 0 — Isolate execution and create the durable work ledger

**Files:** create private ignored `output/music-review/<run-id>/execution.json`, `decisions.json`, `source-pins.json`, and `commands.jsonl` in the execution Keyspilli root. Copy this plan and its spec into the execution Anti root's same documentation suffixes.

**Interfaces:** `execution.json` records repository roots/base HEADs, owned branches/processes/outputs, runtime paths, task states and artifact hashes. `decisions.json` records `id`, `reason`, `preparedArtifact`, `requires: authorization|listening|source-validation|resources`, and `status: pending|resolved`.

- [x] Snapshot status and tracked/untracked file hashes in both source checkouts, primary checkouts and the old outcome; inspect worktree inventory and applicable AGENTS before edits.
- [x] Read using-git-worktrees at execution. Prefer an attached suitable worktree; otherwise create an Anti managed worktree at the pinned base and a separate registered Keyspilli Git worktree at its pinned base. Use `codex/music-review-afk` branches with a unique suffix if occupied. Record exact roots; never reset an existing branch.
- [x] Pin the Node executable explicitly via the task PATH; the current default shell Node is 20.20.2 but `/Users/reidar/.local/bin/node` reports 22.22.3. Create isolated dependency environments, using lockfiles rather than shared dependency mutation. Record disk before/after. Check Python import paths resolve to the worktree.
- [x] Create the ledger, resource/candidate register and final decision queue. An interrupted run must be resumable from identities and terminal receipts without rerunning completed matching cases.
- [x] Establish focused baseline results for the modules being changed; classify unrelated failures with evidence. Commit the plan/spec into the execution branch, leaving private ledgers ignored.

**Gate:** execution roots and owned files are unambiguous; source/WIP/outcome hashes are preserved; runtime and resource budgets verified. Snapshot the canonical Keyspilli plugin tree and installed cache manifests before touching plugin support; installed cache remains unchanged. If the disk floor blocks heavy work, continue documentation/pure-code work and queue heavy checks.

## Task 1 — Raw acoustic receipt and compatibility boundary

**Files:** create `/Users/reidar/.codex/worktrees/keyspilli-listening-review/Keyspilli/packages/catalog/src/acoustic-receipt.ts`, `acoustic-receipt-v1.schema.json`, and `packages/catalog/test/acoustic-receipt.test.ts`. Modify `/Users/reidar/.codex/worktrees/keyspilli-listening-review/Keyspilli/services/transcribe/src/piano-transcriber.ts` and its existing test only to add explicit receipt/provenance compatibility.

**Interfaces:** use the existing shared catalog package for pure receipt types/validation, imported by service and web through `@keyspilli/catalog/src/acoustic-receipt.js` without a service-to-web dependency. `parseAcousticReceipt(value: unknown): AcousticReceipt`; `acousticCacheKey(identity: AnalyzerIdentity): string`. `AcousticReceipt` is `{schemaVersion:1, kind:"keyspilli-acoustic-receipt", status, audio, analyzer, notes, rejectedRows, metadata, resources, limitations}`. `status` is `ok|unavailable|failed`; notes are `{id,midi,onsetSeconds,keyOffsetSeconds:null|number,soundingOffsetSeconds:null|number,confidence:null|number}`. Analyzer identity includes checkpoint, code, frontend and config digests, runtime/device/precision. Define/export these exact types here for later tasks.

- [x] Add failing cases `reject_invalid_events`, `unknown_metadata_stays_unknown`, `checkpoint_changes_cache`, `legacy_normalization_is_not_measurement`: reject noninteger MIDI/negative or nonfinite times, retain unknown tempo/key/meter, different checkpoint digests produce different keys, and legacy defaults remain marked container defaults.
- [x] Run the new test and confirm failure is from missing behavior, then implement strict validation and stable identity hashing. Validate successful receipts' nonempty identities and bounded note inventory; reject incompatible offset semantics rather than inventing offsets.
- [x] Keep the existing beat-based legacy adapter operational. Raw receipt ingestion uses a separate entry point, never silently treats seconds as beats or silently rounds invalid acoustic data.
- [x] Run `npm run test -w @keyspilli/catalog -- test/acoustic-receipt.test.ts` and `npm run test -w @keyspilli/transcribe -- test/piano-transcriber.test.ts` under pinned Node; run affected typechecks. Commit the passing change.

**Gate:** malformed/stale receipts refuse; existing callers preserve behavior; cached neural identity cannot collide after checkpoint/frontend replacement.

## Task 2 — Optional worker and admissible model acquisition

**Files:** create `/Users/reidar/.codex/worktrees/keyspilli-listening-review/Keyspilli/services/transcribe/src/music_analyzer.py`, `services/transcribe/src/acoustic-analyzer.ts`, `services/transcribe/test/acoustic-analyzer.test.ts`, `services/transcribe/test/test_music_analyzer.py`, and `services/transcribe/requirements-music-review.txt`.

**Interfaces:** worker command `music_analyzer.py --request REQUEST.json --output NEW_RECEIPT.json`; request `{audioPin, analyzerIdentity, model: basic-pitch|transkun|hft, timeoutSeconds:120}`. TS `runAcousticAnalyzer(request: AnalyzerRequest, options: AnalyzerOptions): Promise<AcousticReceipt>`; options inject runner and enable flag. `AnalyzerRequest` and `AnalyzerOptions` are exported here. No output overwrite, shell command string, auto-download on inference, or implicit model fallback.

- [x] Add failing subprocess tests for timeout, killed child, malformed output, changed audio hash, missing weights, unknown backend and disabled execution. Assert one invocation, failed/unavailable receipts, no cached failure, no orphan and zero network during inference.
- [x] Implement bounded file decode, derivative hashing and resource receipts; lazy-import optional runtimes. Preserve original channel data and explicit mono derivative. Make decoder precision/channel/resampling settings part of identity.
- [x] Verify exact artifact terms/access and size before acquiring Basic Pitch/Transkun. Install only worktree-local, pinned compatible versions. Transkun source CLI decoding is not assumed safe: decode PCM explicitly and exercise the model API with captured samples. Record checkpoint offset semantics. Use hFT only as an explicitly registered later arm if it changes the decision; conversion requires reference parity.
- [x] If a checkpoint or dependency is gated, unsuitable, unclear, or exceeds the footprint budget, queue it at the end and finish the worker/refusal/fake-runner tests. Do not label a stub result model-tested.
- [x] Run `python -m unittest discover -s services/transcribe/test -p 'test_music_analyzer.py'` with the isolated Python executable, the new TS tests and typecheck. If real assets qualify for download, run a serial tiny smoke inference with receipt hashes and measured resource use. Commit code and sanitized artifact register, never weights.

**Gate:** real model execution and adapter correctness have separate statuses; no implicit uploads/downloads/fallback; dependency failure is localized to that arm.

## Task 3 — Reproducible corpus and actual Player capture

**Files:** create `/Users/reidar/.codex/worktrees/keyspilli-listening-review/Keyspilli/apps/web/scripts/build-music-review-corpus.mts`, `apps/web/src/lib/music-review-corpus.test.ts`, `apps/web/e2e/music-review-capture.spec.ts`, and `apps/web/playwright.music-review.config.ts`. Reuse `/Users/reidar/.codex/worktrees/keyspilli-listening-review/Keyspilli/apps/web/e2e/player-audio-capture.ts`; extend it only if scoped fixtures expose a need. Add the new scripts to `/Users/reidar/.codex/worktrees/keyspilli-listening-review/Keyspilli/apps/web/tsconfig.song-tools.json`.

**Interfaces:** corpus CLI `--output NEW_DIR --seed N`; artifacts include `manifest.json`, private `evaluator-key.json`, `capture-fixtures/bundle.json`, pinned event/tempo/intent files and newly captured WAVs. A case records family/split/content lineage, media/event IDs, supported domain and expected offset/attack definitions; analyzer input excludes truth and answer-bearing filenames.

- [x] Add failing corpus assertions: exactly 24 development and 96 held-out cases, 32/32/32 held-out categories, no split overlap by phrase lineage, reproducible manifest with one seed, distinct neutral analyzer IDs, and fixture/key separation.
- [x] Implement self-authored cases and controlled event mutations across the specified 12 families. A corrupted playback and a faithfully played bad arrangement must be separately labeled. Add second-bank controls when an admissible bank is available; otherwise record that domain as uncovered.
- [x] Configure scratch DB and capture outputs inside the run-owned output directory, one browser worker and a checked free loopback port. Do not use the older corpus's default output directory or unbounded `/private/tmp` creation. Reuse the exact visible Player route and sampled-piano readiness checks; capture the final audio graph.
- [x] Verify Sound settings -> Original/Chord mode -> close tools -> Play -> stop -> return navigation; record sample asset hashes, buses and capture clock. Scheduling observations are transport evidence, not independent acoustic truth. Keep raw captures separate from bounded derivative WAVs.
- [x] Run the corpus test, web script typecheck and `npm exec -w @keyspilli/web -- playwright test --config=playwright.music-review.config.ts`. Inspect captured duration/PCM hashes/silence/clipping and save failure traces. Commit generators/tests/config only.

**Gate:** a repeatable, blindable local corpus exists; actual Player captures are distinguished from synthetic-control renders. Newly discovered faults receive reproducers before scoped fixes; never adjust the answer key to match a fault.

## Task 4 — Benchmark ledger, grading and candidate selection

**Files:** create `/Users/reidar/.codex/worktrees/keyspilli-listening-review/Keyspilli/packages/catalog/src/music-benchmark.ts`, `packages/catalog/test/music-benchmark.test.ts`, and `apps/web/scripts/benchmark-music-review.mts`.

**Interfaces:** `gradeAcousticCase(truth: CaseTruth, receipt: AcousticReceipt, policy: MatchPolicy): CaseGrade`; `summarizeCandidate(grades: readonly CaseGrade[]): CandidateSummary`. Define/export `CaseTruth`, `CaseGrade`, `CandidateSummary`, and `MatchPolicy` here in the shared catalog package. Freeze `attackGroupWindowSeconds:0.02`, grouped against the first onset rather than transitive chaining, and separate 0.05/0.10-second note tolerances. Benchmark CLI `--manifest PATH --candidate ID --split development|heldout --output NEW_DIR`; resume requires a pinned prior ledger and an unchanged experiment fingerprint.

- [x] Write failing tests: one-to-one matching cannot reuse a note; pitches 60 and 72 are octave error, not exact match; three notes in one chord are one attack group; two same-pitch reattacks are two groups; zero denominators are null with counts, not 100%; changed checkpoint/config/split refuses resume.
- [x] Implement deterministic assignment and grades, reporting 50/100 ms onset views separately and offset metrics only for comparable definitions. Preserve raw timing residuals, coverage, failure/unavailable counts and clean false alarms. Store every per-case terminal record atomically.
- [x] Tune only on development data. Freeze thresholds/config in the ledger, then run each admissible candidate once on held-out data, serially. At 120 seconds terminate that inference and record timeout; never substitute a model. Permit one infrastructure repair on development with a new version, then at most one fresh configuration screen; do not tune against the held-out answers. Previously exposed material is no longer a fresh confirmation set.
- [x] Apply provisional 0.95 precision/0.90 recall, critical-control and resource gates with counts and uncertainty. No pass means diagnostic-only; finish the remaining software. Preserve all results and choose a provisionally admissible model by declared task metrics/resource budget, not its published leaderboard number.
- [x] Run benchmark unit tests, execute serial admissible experiments, and commit grading/CLI/sanitized summary. Private audio, evaluator keys and raw model outputs remain ignored.

**Gate:** real candidate results, mock tests and unexecuted arms cannot be confused; failures cannot be hidden by resumption or a aggregate headline.

## Task 5 — Playback clock and renderer-informed fault comparison

**Files:** create `/Users/reidar/.codex/worktrees/keyspilli-listening-review/Keyspilli/apps/web/src/lib/music-event-comparison.ts` and its test; create `/Users/reidar/.codex/worktrees/keyspilli-listening-review/Keyspilli/services/transcribe/src/renderer_verification.py` and `services/transcribe/test/test_renderer_verification.py`. Reuse playback resolution in `/Users/reidar/.codex/worktrees/keyspilli-listening-review/Keyspilli/packages/player-core/src/timeline.ts`.

**Interfaces:** `comparePlaybackEvents(expected: readonly ExpectedPlaybackEvent[], receipt: AcousticReceipt, clock: CaptureClock, policy: MatchPolicy): EventComparison`. Define/export expected event/clock/comparison types here; require occurrence IDs and separate capture offset, tempo-map and speed identity. Renderer worker emits a separate channel receipt with matched activity, residual candidates and template-bank hash.

- [x] Add failing tests for missing/extra/wrong/octave notes, a 200 ms shifted entrance remaining visible, repeated section occurrence identity, speed/transpose mapping, and key release versus pedal tail. Assert malformed/unsupported clocks produce unavailable correspondence, never fabricated alignment.
- [x] Implement constrained matching using actual resolved playback seconds. Report global capture offset separately from per-event error; do not add unconstrained DTW. Represent unresolved source timing explicitly.
- [x] Build bounded templates from isolated supported notes/velocities. Search unexpected pitches and residuals, not just expected score notes. Add adversarial checks: wrong score, missing expected attack, extra pitch, wrong bank, and same-renderer shared fault. Preserve the blind result as a separate channel.
- [x] Run comparator and renderer tests and development ablation; evaluate frozen blind/renderer/combined channels without changing held-out thresholds. Commit modules and sanitized ablation evidence.

**Gate:** playback fault localization survives bad context; renderer agreement cannot certify source fidelity. If renderer-informed verification adds no value, keep it optional and record the negative result.

## Task 6 — Source correspondence, learner intent and uncertainty

**Files:** create `/Users/reidar/.codex/worktrees/keyspilli-listening-review/Keyspilli/apps/web/src/lib/music-correspondence.ts` and its test. Reuse existing symbolic/playability helpers and `/Users/reidar/.codex/worktrees/keyspilli-listening-review/Keyspilli/packages/catalog/src/musical-review.ts` as the human-only attestation boundary.

**Interfaces:** `compareMusicalIntent(source: SourceAnchors, replay: ReplaySnapshot, intent: ArrangementIntent): MusicalComparison`. Define/export source/replay/intent types here; intent includes `mode: original|chords`, difficulty, approved transformations and anchor authority. Output separates preserved, changed-permitted, violated and unknown landmarks.

- [x] Add failing assertions: backing-only melody omission is permitted; missing a required Original melody landmark is a discrepancy; approved octave displacement is permitted; a defining trusted bass/harmonic change lost is localized; unvalidated auto-transcribed anchors produce uncertainty rather than a defect verdict.
- [x] Implement ordered phrase/landmark correspondence, difficulty constraints and explicit approved reduction rules. Reuse trustworthy symbolic voice/span/density checks; do not introduce a new musical scoring engine or require exact source MIDI reproduction.
- [x] Verify corrupted/untrusted source metadata cannot silently become authority. Test repeated section order, pickup, alternate voicing and ambiguous harmony. Unknown sources remain useful to inspect, with musical correspondence incomplete.
- [x] Run focused correspondence and existing musical-review/playability regressions, then typecheck. Commit the passing component.

**Gate:** a good Chords simplification is not penalized for missing melody; human review remains pending even if symbolic checks pass.

## Task 7 — Unified local report and playable review pack

**Files:** create `/Users/reidar/.codex/worktrees/keyspilli-listening-review/Keyspilli/apps/web/src/lib/music-review.ts`, its test, `apps/web/scripts/report-music-review.mts`, `apps/web/src/lib/music-review-cli.test.ts`, and `docs/ops/music-review.md`. Reuse `/Users/reidar/.codex/worktrees/keyspilli-listening-review/Keyspilli/apps/web/src/lib/local-audio-evidence.ts` and existing bounded file/pin validation.

**Interfaces:** `buildMusicReview(input: MusicReviewInput): MusicReviewReport`; `MusicReviewInput` joins pinned legacy manifest, acoustic/renderer receipts, playback comparison and source-intent result. `MusicReviewReport` uses kind `keyspilli-music-review`, schemaVersion 1, per-channel statuses, interval findings, coverage, `providerCalls:0`, `musicalAcceptance:"not-established"`. CLI consumes a manifest and writes a new output directory; it never invokes inference implicitly.

- [x] Add failing tests for stale audio/event/model hashes, contradictory channels preserved as separate findings, no evidence/no approval, path traversal, duplicate IDs, failed/partial receipts and repeated report output refusing overwrite.
- [x] Implement JSON/Markdown plus an escaped static HTML listening pack with local relative media, waveform/piano-roll intervals, evidence origins and coverage. Bind playback buttons to actual saved WAVs. Keep UI labels musical and readable; detailed provenance stays in an expandable evidence area.
- [x] Preserve the legacy no-upload command unchanged. Add explicit new script typecheck inclusion and capability output; unknown/unavailable channels remain visible. Output directory is private and no assets are copied into public web/static paths.
- [x] Add visible browser checks for play/pause, jump to finding, Original/Chords selection, back navigation, and pending status. Use a bounded loopback static server owned by the run; browser tests establish controls/media routing, not musical quality.
- [x] Run web focused tests, typecheck and browser checks; inspect the generated pack. Commit code/docs; keep private media ignored.

**Gate:** the user can inspect and play localized findings without an account change or upload, and every claim has a visible evidence source.

## Task 8A — Standalone Gemini music review in Anti (execute immediately after Task 0)

**Files:** create `/Users/reidar/.codex/worktrees/stream-integration/codex-antigravity-auth/codex_antigravity_auth/skills/anti/scripts/anti_lib/music_evidence.py`, `schemas/music-evidence-v1.json` and `schemas/music-review-v1.json` under the same skill root; create `/Users/reidar/.codex/worktrees/stream-integration/codex-antigravity-auth/tests/test_music_evidence.py` and `tests/test_gemini_music_review.py`. Modify the existing helper `scripts/anti.py`, WAV/skill docs, `/Users/reidar/.codex/worktrees/stream-integration/codex-antigravity-auth/codex_antigravity_auth/skill_assets.json`, and package manifest only for these owned additions.

**Interfaces:** `parse_music_evidence(value: object) -> dict`, `build_music_prompt(evidence: dict | None, objective: str) -> str`, `validate_music_review(value: object, clips: list[dict]) -> dict`. Portable evidence is `{schemaVersion:1,kind:"anti-music-evidence",clips,claims,context,limitations}`: clips have IDs/audio hashes and intervals; claims have IDs, clip interval, origin and uncertainty; context has objective, source authority and allowed differences. No Keyspilli receipt, catalogue, Original/Chords enum or absolute project path is required. Review output has generic clip/time findings classified as model-advisory, uncertainty, comparison status and limitations; source claims are not measurements.

CLI: `anti.py review-music --model GEMINI --audio WAV [--audio WAV] --prompt-file OBJECTIVE [--evidence-json BUNDLE] --probe-unverified-audio --json`, with `--dry-run` for zero-network preparation. Explicit evidence is optional; absent evidence means a blind review. Reuse existing listen/consult transport and bounded policies, not a new OAuth/API-key implementation. Keep old commands backward compatible.

- [x] Write failing standalone tests: one connected Google account fixture with no Keyspilli/BYOK/model assets; eligible Gemini routes only; rejected Claude/text-only/non-Google routes and fallback flags; generic clips unrelated to Keyspilli; optional evidence; missing/corrupt WAV; unknown/stale hashes; malformed JSON/partial response; exactly one mocked provider POST and zero for dry-run/refusal.
- [x] Implement the profile using shared existing admission/transport/run-record owners, strict schema mapping and consumer validation. Use explicit profile classification so the 90s/2,048-token/one-attempt ceilings cannot accidentally inherit general consult retry behavior. Record requested/actual model and backend, audio identity, completion and assistance origin. No automatic account/model experimentation.
- [x] Test adversarial captions/source documents as data, timestamps outside clips, audio-evidence mismatch and observation upgraded to a measurement. No scalar rating/empty findings proves hearing or musical approval. Make capability documentation distinguish transport, observed acceptance, measured perceptual qualification and broader musical suitability.
- [x] Run `python -m pytest tests/test_music_evidence.py tests/test_gemini_music_review.py tests/test_wav_audio.py tests/test_installed_contract.py`, plus relevant helper/refusal regressions. Build/install the bundle into a disposable directory and verify shipped assets/schema parity. Run its CLI with Keyspilli unavailable and only its ordinary dependencies; verify declared Python-floor compatibility through the existing version matrix; no global plugin/config changes.
- [x] Prepare a fresh bounded OAuth Gemini qualification packet: critical missing/mismatched-audio and repeated/held controls first; explicit compatible model, one account fixture/lease identity, prompt/audio/config hashes, maximum eight prospective calls per arm, one attempt per job, no retry/fallback/judge, stop on failed critical control. Leave calls unexecuted until final authorization. Public Gemini API-key comparisons are not an Anti adoption requirement.
- [x] Commit the portable Gemini feature and its tests/docs independently of Keyspilli.

**Gate:** every ordinary Anti user with an eligible Google account/model can invoke the same feature without Reidar's paths, multiple accounts, Keyspilli or extra provider credentials. Mocked install/transport proof does not claim live perceptual qualification. If upstream availability prevents live use, surface its actual limitation rather than promise every account/model works.

## Task 8B — Keyspilli experimental models and optional Anti bridge

**Files:** create `/Users/reidar/.codex/worktrees/keyspilli-listening-review/Keyspilli/services/transcribe/src/music_critic.py`, `services/transcribe/test/test_music_critic.py`, `/Users/reidar/.codex/worktrees/keyspilli-listening-review/Keyspilli/apps/web/src/lib/anti-music-evidence.ts` and its test, `apps/web/scripts/prepare-music-model-experiments.mts`.

**Interfaces:** define/export TypeScript `AntiMusicEvidence` in `anti-music-evidence.ts` to mirror Task 8A's schema and pin the schema version/digest in contract fixtures. `exportAntiMusicEvidence(report: MusicReviewReport): AntiMusicEvidence` converts the host report to Task 8A's generic schema. Local worker request `{backend,audioPins,promptPin,evidencePin|null,modelIdentity,limits}` emits a tagged critic/source result or unavailable receipt. Optional backends: `moss-hf`, `moss-native`, `muscriptor`, `qwen-modelstudio`; network backend defaults preparation-only and is never configured implicitly. All non-Antigravity dependencies and accounts are owned by Keyspilli.

- [x] Add failing bridge tests for pins/units/origins, unsupported context and unvalidated authority, ensuring Keyspilli-specific fields map into optional generic context rather than changing Anti's schema. Round-trip an ordinary non-Keyspilli bundle through the same Anti validator.
- [x] Add critic tests for missing assets, decoder/config mismatch, timeouts, malformed outputs, out-of-clip timestamps, injection in saved captions, unsupported backends and unauthorized network dispatch. Assert Keyspilli's baseline works with Anti/model backends absent.
- [x] Implement lazy local adapters. MOSS records time markers, explicit localized prompts, tower/DeepStack identity and conversion parity requirements. MuScriptor emits unquantized events as estimated source evidence. Qwen support prepares provider-specific requests only; its credentials/billing/license decisions are final-queue items. No such routes or dependencies are added to Anti.
- [x] Attempt real local inference only when public terms and resources permit. Prepare reference/native parity and the four-condition qualitative ablation regardless. A blocked model ends contract-tested/empirically-unexecuted; no text-only substitute counts as audio review. Run mixed-source comparison only with established source anchors.
- [x] Prepare explicit Keyspilli-to-Anti `review-music --dry-run` invocation with pinned helper/bundle versions, clipped media, objective and exported evidence. Validate request shape with zero uploads; finish live Gemini execution as an end-review item.
- [x] Run critic Python tests, bridge tests, host typecheck and generic contract fixtures. Commit only Keyspilli host integration and sanitized experiment definitions.

**Gate:** optional model/Anti features are discoverable, incompatible or unavailable without breaking local measurement. No non-Gemini music functionality leaks into Anti.

## Task 9 — Bounded repair proposals and fresh A/B previews

**Files:** create `/Users/reidar/.codex/worktrees/keyspilli-listening-review/Keyspilli/apps/web/src/lib/music-repair-preview.ts` and its test, `apps/web/scripts/preview-music-repair.mts`; reuse existing `/Users/reidar/.codex/worktrees/keyspilli-listening-review/Keyspilli/apps/web/src/lib/audio-review.ts` repair queue and Task 3 capture.

**Interfaces:** `previewMusicRepair(snapshot: ReplaySnapshot, proposal: RepairProposal): RepairPreview`. Proposal specifies source hash, finding ID, target event IDs, bounded operations, evidence refs and required preserved invariants. Supported preview operations: replace pitch, shift onset, insert/delete event; at most eight event edits in one declared phrase. `RepairPreview` contains changed snapshot, exact diff and required rechecks; it is not a catalogue mutation.

- [x] Add failing assertions for stale hash, unknown/duplicate IDs, unsupported operation, too many edits, altered neighboring phrase and unjustified source authority. Original files/catalog hashes must stay identical after success and failure.
- [x] Implement isolated preview generation with explicit preconditions. Playback realization findings produce software reproducers, not authored-note compensation. Qualitative/harmonic alternatives with inadequate authority remain text proposals. Self-authored seeded ground truth can support deterministic preview tests.
- [x] Freshly capture original and candidate in scratch, recompute target/neighbor/ending checks and task identities. Verify the target seeded fault changes and preserved invariants hold. Use a new recheck identity; prior model observations cannot approve changed audio.
- [x] Run focused repair/CLI tests, capture an admissible synthetic A/B demonstration and extend the pack. Commit code/docs. Mark musical preference pending even after a technically corrected seed.

**Gate:** reversible, reproducible repair previews work; actual-song musical edits await the final source/listening decision rather than quietly overwriting variants.

## Task 9B — Deliver host support through the Keyspilli plugin

**Files:** canonical source `/Users/reidar/plugins/keyspilli/skills/keyspilli-song/SKILL.md`, `scripts/check_checkout.py`, `references/audio-listening-review.md`, `references/musical-quality-gate.md`, `references/evaluation-and-repair.md`; create `references/music-review-workflow.md`. Inspect `/Users/reidar/plugins/keyspilli/.codex-plugin/plugin.json` for its release/version workflow. This directory is not a Git repository; stage an exact owned-file patch with before/after manifests rather than invent a Git commit there.

**Interfaces:** extend checkout preflight with an optional `music_review` capability inventory for the new report/benchmark/preview scripts and schemas; each backend reports `compatible|absent|incompatible|unchecked`. Capability probing is read-only and never invokes a model, download or gateway. The skill routes music support through those host commands, with optional Gemini bridge and separate local/non-Google models.

- [x] Write failing preflight self-tests for capable/absent/incompatible hosts, Node ABI/runtime identity, no analyzer/Anti installed, optional backend refusal, and zero network/calls during discovery. Existing capable-checkout selection must remain conservative.
- [x] Implement capability probing and maintained commands/docs. Preserve no-upload default, authored/measured/advisory separation, frozen-screen boundaries and human-only gates. Explicitly show the minimal local path and optional models, rather than require every user to install all backends.
- [x] Update canonical skill support through an owned patch while preserving unrelated source changes. Record hashes, prepare version metadata and a reproducible plugin package; test a disposable install, all documented file links and command dispatch into the execution host. Never edit installed cache files.
- [x] Keep the installed-user plugin update/release action in the final queue, with concrete package/hash/rollback. The test package and host launch commands already work before that approval.

**Gate:** `@Keyspilli` owns and exposes the music workflow; Anti remains usable without this plugin. Canonical support, disposable-package proof and installed adoption are separately recorded.

## Task 10 — Full regression, review and AFK delivery

**Files:** update both execution repositories' relevant ops docs, canonical plugin documentation and plan checkboxes; create private `final/status.json`, `final/authorization.md`, `final/listening/index.html`, `final/listening/worksheet.json`, and `final/evidence-index.json` under the owned run output. No new attestation type is invented.

- [x] Run relevant complete workspace suites/typechecks once after focused gates pass: `npm test`, `npm run typecheck`, `npm run build` in Keyspilli; `python -m pytest` including the bundled Anti test file explicitly if default discovery omits it, plus existing lint/package/installed-contract checks. Run exact visible capture/report/preview browser scenarios. Retest only new changes or unresolved failures; preserve unrelated baseline failures distinctly.
- [x] Review diffs against source/WIP snapshot and the spec. Audit evidence origins, seconds/beats, cache/checkpoint identity, zero-network paths, failed-state handling, resource cleanup and human-only attestation. Fix actionable findings and rerun affected checks. Native self-review is the default; do not claim independent code review if none occurred.
- [x] Make cohesive local commits per task; prepare per-repository draft PRs if existing GitHub authentication/network allows, containing only code, synthetic fixtures and sanitized summaries. Publishing a draft is reversible review preparation; merge/deploy remains in the end queue. Attach created PRs to this chat and verify exact-head CI where available. If PR publication is unavailable, leave clean commits plus review commands and queue it without stopping delivery.
- [x] Assemble 36 phrase items as specified. If validated real sources are unavailable, finish the pack with clearly tagged self-authored cases and pending real-source slots; do not fabricate real-song annotations. Provide A/B playback, neutral order, localized questions, separate source/listening/keyboard fields and a private answer key. Never prefill a human pass.
- [x] Assemble the final decision queue once: exact license/access/resource choices, any proposed external request arm with data destinations/maximum calls, install/merge/deploy choices, and unresolved musical/source judgments. Include a ready command/artifact and the reason for each request. Routine implementation choices must already be resolved.
- [x] Verify original/source/WIP/frozen outcome preservation, artifact hashes, disk and owned processes. Stop only owned servers/workers; preserve a one-command launcher and evidence. Remove only confirmed owned disposable scratch/caches; never archive worktrees containing deliverables.
- [x] Update Obsidian daily Log and project note with commits, tests, real model results, pending items and review-pack path. Deliver one completion summary with links, launch command, measured results and a single consolidated end-review checklist.

**Gate:** software implementation, empirical model status, and human musical acceptance are independently stated. Every task has a terminal status and every deferred item has a concrete end-review artifact.

## AFK continuation and failure rules

- Continue independent tasks after any model/access/license failure. Finish the diagnostic workflow and tests with unavailable states, then queue that arm. A failed held-out candidate does not justify an automatic production fallback.
- After an implementation/test error, diagnose and repair the owned component; do not mask a schema, prompt, permission or code error by changing models. Do not spend loops trying to make an opaque listener pass.
- If interrupted, verify identities and resume the first incomplete task. Record partial work before large operations. Atomic receipts and exclusive outputs prevent overwriting prior evidence.
- Do not ask the AFK user mid-run for preference, ears, credentials or a larger budget. If a truly necessary authorization arises, record it and proceed with all unblocked work. If all remaining work requires the user, deliver the final packet with exact unfinished scope; do not claim those gates passed.
- No unsolicited recurring automation is created. This plan defines an active implementation run and its resumable ledger; execution is not a promise of unattended work after the process ends.

## Final owner review, after autonomous work

1. Resolve actually blocked model terms/resources and optional fresh external experiment arms from their concrete pins, data destination, call ceiling and cost. After any granted authorization, run only that frozen arm, evaluate it automatically and update the pack before the listening session. This is the end phase, not a mid-implementation interruption.
2. Open the completed pack and listen to short clean/fault/simplification groups, Original and Chords separately. Confirm or reject localized diagnoses and A/B repair usefulness; preserve uncertainty.
3. Validate critical real-song anchors or request a qualified pianist/teacher for source/keyboard judgments. Software metrics cannot supply those attestations.
4. Review final diffs/CI and choose installed adoption, merge or deployment. The diagnostic-only default stays until acceptance is explicitly established.

## Plan self-review

The plan covers the research's measurement, alignment, source intent, criticism, resource/license, repair and acceptance concerns, and the user's explicit Gemini-only Anti / all-other-music Keyspilli ownership correction. Candidate integrations and human gates have explicit unavailable/deferred outcomes. Interfaces are owned by Tasks 1, 2, 4–8B; later tasks consume those exact types. Review Focus failure cases map to named tests. Existing no-upload commands, frozen studies, saved source evidence and human-only receipts are preserved. No runtime benchmark, model qualification or musical acceptance is claimed by this planning document.

## Execution record — 2026-10-05

Execution Anti root: `/Users/reidar/Projectos/.worktrees/anti-music-review-afk`; Keyspilli root: `/Users/reidar/.codex/worktrees/keyspilli-music-review-afk/Keyspilli`. Original checkouts and frozen failed outcome were hash-verified unchanged. The earlier Keyspilli research checkout was removed externally; twelve research pins match the adopted base.

Local regression: Anti 3,667 passed, 1 skipped and 245 subtests; installed ordinary-dependency contract 2 passed, installed music profile 20 passed, actual bridge dry run 1 passed with zero HTTP. Keyspilli 2,601 passed, full typecheck/build passed, optional worker/critic Python 14 tests and renderer 5 tests passed. Recorder, Player capture and review-pack controls passed browser proof; automated playback was muted.

Real local screen: 24 development and 96 held-out cases per candidate. Basic Pitch held-out precision/recall at 50 ms: 0.7992495/0.9342105; Transkun: 0.9921260/0.5526316. Both diagnostic-only. Short-phrase latency is measured; a 30-second p95 profile is unqualified. Exact note counts are unchanged after audited zero-inference rescore.

Execution rulings: the first recorder discarded its buffer origin; its grades are invalidated. Fresh sample-frame capture and an independent impulse control were used. No thresholds were tuned against held-out data. Source replay and resolved playback event hashes are separate. Interrupted terminal receipts are recovered without inference; changed media/config/grader fingerprints refuse resume. Plugin self-tests were executed with the implementation rather than claimed as a historical red-first cycle. Current Git signing needs locked 1Password presence, so remaining review commits use a per-command unsigned override; no signing configuration or installed cache is changed.

Optional hFT/reference conversion, MOSS resources/native parity, gated MuScriptor weights and external Qwen are prepared and empirically unexecuted. Actual-bank renderer template admission, second-bank/real source coverage and human listening/source/keyboard judgments remain pending. All AFK provider calls are zero. Canonical plugin support and disposable package are prepared; installed adoption, signing, merge/deploy and fresh Gemini upload arm wait in the final queue.

Private complete packet: Keyspilli execution root `output/music-review/20261005-afk/final/`. It contains 36 neutral A/B items with separate blank human fields and fresh repair audio. Evaluator answers remain outside the served review directory. Published/attached drafts: Anti #169 against main, Keyspilli #218 stacked on #216. Keyspilli CI excludes stacked bases; local regression is complete. Anti exact-head CI is recorded separately in the private final packet. Obsidian daily Log and project implementation note updated. No live/provider or human gate has been passed.
