# Changelog

## v2.0.0 — Anti Skill Overhaul (Aug 21, 2026)

### Breaking Changes

**Grok/xAI OAuth and BluesMinds removed as first-class providers.**

The `grok`, `supergrok`, `xai-grok`, `grok-oauth`, `grok-build`, `grok-4.3`,
`grok-bluesminds`, `grok-4.5`, `glm-5.2`, and `glm52` model aliases no longer
exist. The `xai-oauth` provider preset, `bluesminds` provider preset, and
`workflow claude-grok` preset have been removed.

xAI models are still available via the standard BYOK flow:
```bash
codex-antigravity provider set xai --api-key-env XAI_API_KEY --model grok-build-0.1
```

Existing users with xai-oauth configs will see a deprecation warning with
migration instructions instead of a hard crash.

The `--collab` flag has been removed (no active collaboration profiles).

### New Features

**Enriched Findings Schema** — Panel findings now include `confidence`,
`file`, `line`, `evidence`, and `fingerprint` fields. Cross-lane dedup is
automatic: findings with the same fingerprint (same file+line+claim) are
merged, keeping the highest severity and averaging confidence.

**Role-Specialized Panel Prompts** — Each panel lane receives a role-specific
rubric when `--role` is passed. Available roles: correctness, security, tests,
performance, ux, protocol, install-docs, injection, secrets-handling, authz,
dependency-surface, root-cause, regression-risk, discriminating-tests.

**Anonymized Panel Judging** — Lane labels are anonymized and shuffled before
the judge synthesizes to reduce brand bias. Use `--no-anonymize` to disable.

**Smart Auto-Routing** — `--auto-route` picks the cheapest adequate model
based on diff size and file risk. Small diffs → flash-3.6, medium → sonnet,
large or high-risk → opus. Only activates when `--model` is not passed.

**Budget Cap** — `--budget <cost>` fails gracefully when estimated cost
exceeds a cap. Tracks estimated vs actual per lane. Judge retries respect
the budget.

**Evidence-Linked Verification** — `anti_lib/verifier.py` runs syntax checks,
secrets scanning, and eslint on files referenced by findings. Use
`--no-verify` to skip. Path traversal is blocked with workspace containment
checks.

**New Workflow Presets:**
- `workflow quick-check` — Fast pre-commit gate using free models (flash-3.6,
  poolside, nemotron-ultra judge). 60-second budget, 20k prompt cap.
- `workflow consensus` — Disagreement-focused 3-model panel with
  min-successes 2. Default models: sonnet, opus, flash-3.6.

**`openrouter/free` Model Alias** — `free` resolves to `openrouter/free`,
which auto-selects the best available free model on OpenRouter.

**Repo-Level Reflection Memory** — Passively tracks review findings per
repo for pattern analysis. Records findings, models, panel status after
each review. `runs reflections --repo <path>` shows summary with
recurring fingerprints, severity distribution, and most-reviewed files.
Use `--clear` to reset. Does NOT suppress findings — only tracks and
surfaces patterns for periodic review.

### Fixes

- Fixed `normalize_finding_item` nested scope bug (was inside
  `strip_fenced_json_blocks`, caused `NameError` on every panel run)
- Fixed budget double-counting in panel fan-out (subtracted estimate using
  wrong model when fallback occurred)
- Fixed judge retry bypassing budget cap
- Fixed anonymize mapping overwrite on synthesis retry
- Fixed consensus workflow duplicate `--prompt` in expansion
- Fixed verifier path traversal (symlink-safe `Path.relative_to()`)
- Fixed verifier credential leak in secrets scan output
- Fixed verifier `verify_findings` discarding return values

### Tests

- 8 new unit tests for enriched findings, dedup, auto-routing, cost
  estimation, and verifier behavior
- SKILL.md content assertions to prevent documentation drift
- Removed ~800 lines of stale Grok/xAI OAuth/BluesMinds test coverage
- Full suite: 702 passed, 204 subtests

---

## v1.8.1 — Hardening Release (Aug 16, 2026)

Previous release. See git history for details.
