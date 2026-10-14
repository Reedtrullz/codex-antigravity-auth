# Anti Gemini capabilities and Keyspilli music support — execution specification

Date: 5 October 2026. Owner: current Codex chat. Execution method: native, without subagents by default.

## Objective and authority

Implement reusable Gemini musical-review capabilities in Anti using its existing Antigravity Google-account connection. Implement all other music support through the Keyspilli plugin and its host workflow. It must distinguish actual playback realization, source correspondence, and learner suitability. The user will be AFK during implementation and requests that authorization decisions and listening checks wait until the end. Complete all independent software work, local verification, admissible offline experiments, documentation, and delivery preparation without routine check-ins. Keep a final decision queue for actions that require the user.

This specification accompanies the implementation plan. Its research basis is [the deep research report](</Users/reidar/Obsidian/Hermes/Hermes/Personal/Projects/Codex Antigravity Auth/Music review solution and qualification plan - 05-10-2026.md>). This turn prepares the plan; execution begins when the user directs implementation. During execution the existing AFK authorization governs routine implementation decisions.

## Product scope — explicit user correction, 5 October 2026

Anti owns only new Gemini capabilities available through the existing Antigravity Google AI account connection. Its audio/evidence review must work without Keyspilli, private paths, a paid provider, another API key, local model weights or an audio-ML installation. Existing unrelated Anti routes remain unchanged. Capability/eligibility limitations are surfaced honestly; portable software does not prove upstream access or accurate hearing for every account/model.

The Keyspilli plugin owns transcription/DSP, arrangement/source reasoning, other music models, benchmark tooling, repair previews and listening packs. Keyspilli may optionally export portable evidence to Anti; neither product imports the other's implementation. Public Gemini API-key comparisons are optional diagnostics outside Anti's required Google-account workflow.

## Required outputs

1. Standalone Gemini `review-music` profile with optional portable evidence, strict bounded transport, structured advisory findings, dry-run and generic install/compatibility tests.
2. Keyspilli-owned seconds-based, validated acoustic receipt and optional bounded worker: Basic Pitch baseline, Transkun first challenger, hFT if the comparison can change the choice.
3. A reproducible offline corpus and result ledger, with immutable configuration, developer/held-out separation, resource measurements and explicit unavailable/failed states.
4. Playback event comparison and renderer-informed verification, with independent transcription retained and clock/offset/pedal meanings explicit.
5. Mode-aware comparison against trusted source anchors, preserving legitimate simplification and uncertainty.
6. A local report and playable review pack with audio intervals, event links, evidence origins, limitations and pending musical acceptance.
7. Portable, Gemini-specific Anti evidence consumption with zero-network preparation; experimental music-model integration boundaries that can be tested without provider calls.
8. Bounded repair proposals and isolated previews, fresh capture/recheck, and protected source/catalog state.
9. Reproducible tests, locally validated build/artifacts, reviewable commits/draft PRs where possible, and one final authorization/listening packet.

## Fixed boundaries

- Preserve dirty primary checkouts, all prior evidence, installed plugins and running services. Use isolated checkouts based on verified Anti `56ade46237c6e57efbd7aed37e57e1e3c0b2f6d6` and Keyspilli `b75067169a1959c31199f277c4fec3ba4f96f47d`, or explicitly record a reviewed newer equivalent.
- Keep the old failed piano screen frozen; its outcome SHA-256 is `5fe2638eac6c5f09f36411683c0b2e93907683aa389cf502a2dc6c5f0898612e`. Its historical call ledger grants no new calls.
- Existing remote WAV ceiling: classic PCM16, at most two files, 2 MiB per file, 4 MiB combined, 30 seconds per file. Existing listen ceiling: one attempt, 2,048 output tokens, 90 seconds; no automatic retry/fallback/judge. Do not loosen these boundaries.
- No remote audio upload, paid inference, credential/account modification, gated-license acceptance, production merge/deployment, installed gateway/plugin replacement, or messages to people during the AFK phase. Prepare these actions for end review when needed.
- Normal worktree-local dependencies and ungated public model artifacts with established suitable terms are allowed within the resource budget. Record exact license provenance and hashes; do not infer weight terms from a repository's code license. If terms/access are unclear, finish adapter tests and put that model in the final queue.
- Stop heavy work below 30 GiB Data-volume free space. Maximum new run footprint is max(0, the smaller of 8 GiB and current free space minus 32 GiB). Download/build staging counts too. One neural worker at a time; target peak worker footprint below 8 GiB. Never reclaim another project's files to make a model fit.
- Pin Node 22.22.3 from `/Users/reidar/.local/bin/node`; isolated audio Python 3.11.15 is available at `/Users/reidar/.local/bin/python3.11`. Anti remains compatible with its declared Python >=3.10. Recheck runtimes at execution.
- Measured events stay in seconds. Container defaults for tempo/key/meter are marked defaults; unknown values remain unknown. Retain invalid row counts and offset definitions. Raw confidence is uncalibrated until validated.
- No majority vote resolves contradictory channels. No automated output creates a human musical-review receipt or changes `musicalAcceptance: "not-established"`.
- Repairs are previews in new private output directories. Playback realization faults should produce software reproducers rather than compensating edits to authored music. Existing source, catalogue and variants remain preserved until final decisions.

## Evaluation contract

First corpus: 24 development cases and 96 held-out cases from 12 families. Balance the 96 as 32 clean, 32 seeded faults and 32 difficult valid realizations; stratify families and split by musical material, bank and seed. Fixtures/evaluator keys are inaccessible to the analyzer. Include silence/missing input, pitch direction/register, repeated/held attacks, pedal, chords/octaves, quiet overlap, wrong/missing/extra notes, shifted entrance, repeated sections and legitimate reduction. Generated ground truth does not substitute for independent real-recording annotations.

Freeze matching tolerances and configuration before held-out evaluation. Report onset thresholds at 50 ms and 100 ms separately, with raw timing residuals, key/acoustic offset semantics, exact pitch, octave errors, attack groups, fault precision/recall, clean false alarms, coverage and independent-unit uncertainty. Initial candidate-screen targets are fault precision >=0.95, recall >=0.90, no critical-control failure, and 30-second clip p95 runtime <=60 seconds with the stated memory target. These are candidate gates, not production reliability claims. Zero/undefined denominators must remain explicit.

Compare blind, renderer-informed and combined measurement channels. Qualitative ablation has four conditions: deterministic report, blind audio review, evidence-only text interpretation, and audio plus evidence. A model with no qualified auditory capability can still explain measurements, labeled accordingly. Optional models that cannot run locally get offline adapter tests and an unexecuted, bounded end-review experiment.

Final human packet: 36 short phrase items, 12 clean, 12 meaningful faults, 12 legitimate simplifications, with Original/Chords and difficulty coverage. Distinguish self-authored fixtures from real-song items awaiting source validation. Provide audio A/B, neutral labels, locations and a hidden evaluator key. Human source/listening/keyboard judgments stay pending.

## Completion criteria

Prove Anti's normal feature in a standalone disposable install with a single Google-account fixture and Keyspilli absent. Prove Keyspilli's local music path with Anti absent and each optional backend unavailable. Test their bridge only through the generic schema. Update and package the canonical Keyspilli skill support, preserving installed caches.

Finish the software and reviewable artifacts even if a particular model is unavailable or fails qualification. Every scope item must end as implemented and software-tested, empirically passed, empirically failed, or explicitly deferred to the final user queue. Contract tests are never counted as actual model inference. Do not claim musical acceptance or silently select the next model after a held-out failure. A failed candidate leaves the system in diagnostic-only mode while other work continues.
