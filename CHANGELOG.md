# Changelog

## v2.4.1 - Anti Free-Lane Preset (Sep 19, 2026)

### Added

- Anti helper `--model-free` flag for `panel` and `workflow` commands: expands
  deterministically into the explicit free-lane preset (`nemotron-ultra`,
  `poolside`, `gemma-4`, `nemotron-super`). Pure shell convenience; the requested
  identities remain visible in logs and run manifests. No automatic routing.
- `--model-free` fails closed when combined with `--model` instead of silently merging.

## v2.4.0 - Terminal Ban Detection (Sep 19, 2026)

### Added

- Accounts are disabled after repeated terminal account-scoped auth failures
  (limit 3, `BAN_STRIKE_LIMIT`); any success resets strikes. Curable blocks
  (`validation_required`, `age_rejection`) ride the normal cooldown path and never
  accrue strikes; `codex-antigravity login` clears ban state and strikes on re-auth.
- Rotation lines surface `DISABLED (banned)` and `auth-strikes=N/3`;
  `account_health_summary` exposes `disabled_accounts` and excludes banned accounts
  from per-family availability counts.

## v2.3.0 - Unified Routing & Review Integrity (Sep 16, 2026)

### Added

- Opt-in unified Codex model picker and routing for OpenAI, Antigravity, and
  configured BYOK models, with explicit authentication and provider errors.
- Gemini 3.8 Flash catalog support.
- Durable, sanitized Anti result artifacts with explicit completion, scope,
  coverage, provider identity, budget, and installed-skill parity information.

### Hardened

- Anti reviews fail closed on incomplete scope, synthesis overflow, and lost
  lane input; preserve complete structured findings and chunk provenance.
- Panel results distinguish model diversity from independent providers and
  retain partial/failure diagnostics rather than claiming complete coverage.
- Account leases, concurrent token refresh, request replay, and schema
  validation handle failure and contention more consistently.
- Native non-stream requests have bounded deadlines and deterministic
  cancellation/cleanup; OpenAI streaming HTTP failures remain visible.
- Packaged Anti fixtures and regressions cover installed helper behavior.

### Upgrade Notes

- Unified routing remains opt-in; classic Antigravity configuration is retained.
- Incomplete Anti runs may now explicitly fail or report partial results where
  previous versions could imply success. Consumers should inspect status and
  coverage fields, not infer completion from generated prose.
- After upgrading, run `codex-antigravity install-skill --force --verify` to
  back up, refresh, and verify the installed Anti helper.

## v2.2.0 - Security Hardening & Release Readiness (Aug 21, 2026)

### Security Fixes

- **Reflection files now written at 0600, directory at 0700.** Legacy
  world-readable reflection JSON files are migrated to owner-only on the next
  write. These files contain review findings from private repos and should
  never have been group/world-readable.
- **Test isolation.** Reflection tests no longer write to real user data;
  REFLECTIONS_DIR is monkeypatched to a tempdir for all test runs.

### Bug Fixes (External Agent Report)

- **Plan scope grounding**: plan prompts now include a language/framework
  profile (pyproject.toml, package.json, etc.) plus top-level directories,
  so models stop guessing TypeScript on Python repos when the tree is clean.
- **Chunk-cap fail-fast ordering**: panel review pre-checks chunk overflow
  before spending any model call; pre-computed chunks pass through to
  run_chunked_review instead of being recomputed.
- **VALIDATION_REQUIRED 403 recovery**: Google re-auth URLs are extracted
  and surfaced with [ACTION REQUIRED] guidance; query-param tokens are
  redacted from both the displayed URL and the original error body.
- **Error-path run records**: failed runs populate models, prompt_chars,
  output_chars, and failed_lanes from generation metadata instead of writing
  empty fields.

### New Features & Improvements

- **runs clean --older-than N prunes stale reflections** alongside run
  records; empty per-repo files are deleted entirely.
- **Smoke version-drift check** compares installed gateway version against
  repo pyproject.toml; works on Python 3.10 via tomli/regex fallback chain.
- **_vkey hardened** against unparseable version strings.
- **Polyglot repos** report all matching language manifests, not just the first.
- **Error diagnostics helper** extracted to module-level
  _extract_error_diagnostics() for testability.

### Documentation

- SKILL.md documents runs reflections, passive tracking semantics, and
  the track-don't-suppress contract.

### Tests

- Full suite: 719 tests + 204 subtests (up from 702), covering reflection
  permissions, TTL pruning, bounding, migration, chunk-cap fail-fast, URL
  redaction, malformed-metadata coercion, polyglot profiles, and version-key
  parsing. POSIX permission assertions are skipped on Windows.

---

## v2.0.0 - Anti Skill Overhaul (Aug 21, 2026)

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
