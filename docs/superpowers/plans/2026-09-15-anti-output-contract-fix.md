# Anti Output Contract Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop valid, fully preserved panel-lane JSON from being marked lossy solely because its finding vocabulary differs from the final judge schema.

**Architecture:** Keep one strict final findings normalizer for judge output. Add a separate lane-material normalization boundary that preserves the complete redacted structured payload for synthesis, records any schema adaptation explicitly, and only marks judge input partial when content was actually omitted or repaired. Add offline fixtures for the v5 failure and v6 prose path, then verify with the full suite and one bounded candidate live run.

**Tech Stack:** Python 3.10+, `unittest`, Anti panel helper, saved redacted JSON run artifacts.

**Spec:** `docs/superpowers/reports/2026-09-14-anti-gateway-remediation.md` plus the follow-up acceptance request.

## Global Constraints

- Preserve fail-closed scope, model/provider provenance, and redacted complete lane content.
- Do not relax provider-diversity or scope gates and do not claim provider-independent consensus.
- Do not mutate the installed runtime, Codex default provider/model, accounts, providers, or credentials.
- Use sanitized offline real-output fixtures before production code changes.

---

### Task 1: Add failing offline regressions

**Files:**
- Create: `codex_antigravity_auth/skills/anti/tests/fixtures/v5-lane-sonnet.json`
- Create: `codex_antigravity_auth/skills/anti/tests/fixtures/v5-lane-opus.json`
- Modify: `codex_antigravity_auth/skills/anti/tests/test_anti.py`

- [x] **Step 1: Add sanitized v5 lane fixtures**

Copy only the observed JSON shape from the saved v5 lanes, with synthetic source text and no account/provider secrets.

- [x] **Step 2: Add a failing synthesis regression**

Assert that both fixtures retain their structured payload in the synthesis prompt, `judge_input_status` is complete when no content is omitted, and any schema-adaptation warning is explicit rather than silently discarded.

- [x] **Step 3: Add a passing v6 prose preservation regression**

Assert that prose lane output remains preserved and complete, matching the existing v6 behavior.

- [x] **Step 4: Run only the new tests**

Run: `python3 -m pytest codex_antigravity_auth/skills/anti/tests/test_anti.py -k 'v5 or v6 or structured_lane' -q`

Expected: the v5 regression fails on current `judge_input_status`/lossy metadata.

### Task 2: Implement the shared lane normalization fix

**Files:**
- Modify: `codex_antigravity_auth/skills/anti/scripts/anti.py:5088-5147`
- Modify: `codex_antigravity_auth/skills/anti/tests/test_anti.py`

- [x] **Step 1: Preserve the complete sanitized structured payload as the authoritative lane material**

Keep the existing final judge normalizer strict. For lane synthesis, retain `structuredOutput` and record `structured_normalization_warnings` when individual items do not satisfy the final findings schema.

- [x] **Step 2: Mark loss only for actual content loss**

Use parse repair, truncation, or failure to preserve a safe structured payload as lossy. Do not mark a lane lossy merely because final-schema normalization dropped an item that remains in `structuredOutput`.

- [x] **Step 3: Add explicit metadata/caveat for adapted lane contracts**

Expose the affected lane IDs and warning without upgrading them to verified findings or changing source scope/provenance.

- [x] **Step 4: Run the focused regression and Anti test module**

Run: `python3 -m pytest codex_antigravity_auth/skills/anti/tests/test_anti.py -q`

Expected: all tests pass and the v5 fixture no longer produces false partial status.

### Task 3: Verify candidate and live behavior

**Files:**
- Modify: `docs/superpowers/reports/2026-09-14-anti-gateway-remediation.md`

- [x] **Step 1: Run the repository suite and diff checks**

Run: `python3 -m pytest -q` and `git diff --check`.

- [x] **Step 2: Run one bounded candidate panel using the accepted installed scope**

Use the two small installed skill files, Sonnet and Opus lanes, Opus judge, no retries/fallbacks, complete save artifacts, and record actual model/provider identities.

- [x] **Step 3: Confirm the result is honest**

Require complete scope/coverage, no omitted content, explicit any normalization warnings, and `same_provider_multi_model` rather than provider-independent consensus.

- [ ] **Step 4: Update the report and commit the follow-up**

Record the root cause, regression evidence, candidate live artifact, and remaining non-claims. Commit the minimal source/tests/report changes and open the follow-up PR from `codex/anti-output-contract-fix`.
