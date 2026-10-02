# Anti Review Fix Plan

Based on the Sonnet review of the Anti skill improvement work (Aug 21, 2026).
False positives and confirmed-non-issues are omitted.

---

## Critical (fixed)

- **C-1: `normalize_finding_item` nested** — ✅ Fixed by un-nesting to module scope.

## High — Behavioral Fixes

### H-1: Budget double-counting in panel fan-out ✅ FIXED
**File:** `anti.py` — `command_panel` `as_completed` loop
**Issue:** `running_cost -= estimate(...)` then `running_cost += actual(...)` uses
the same `panel_results[index]` dict that was just overwritten, so the model
string may not match the original reservation.
**Fix:** Capture `reserved_model` before the lane submits; subtract estimate
using the reserved model, add actual using the completed model.

### H-2: Judge retry bypasses budget cap ✅ FIXED
**File:** `anti.py` — judge retry block in `command_panel`
**Issue:** The retry call adds `retry_estimate` to `running_cost` but performs
no cap check before issuing the retry.
**Fix:** Check `running_cost + retry_estimate > budget` before the retry call;
skip retry and record `budget_exceeded` if over cap.

### H-3: anonymize_mapping overwritten on render retry ✅ FIXED
**File:** `anti.py` — `build_panel_synthesis_prompt` `render` closure
**Issue:** If `render` is called more than once (retry synthesis), the mapping
is overwritten with a new shuffle order.
**Fix:** Only set `metadata["anonymize_mapping"]` on first call; cache the
shuffled order in a local variable that persists across retries.

### H-4: xai-oauth users get hard failure with no migration ✅ FIXED
**File:** `cli_setup.py`
**Issue:** Users with `provider_prefix == "xai-oauth"` in existing config hit
`SystemExit` with "BYOK provider 'xai-oauth' is not configured." No deprecation
warning or migration guidance.
**Fix:** Catch the xai-oauth case and emit a deprecation warning directing
users to `codex-antigravity provider set xai --api-key-env XAI_API_KEY`. Don't
crash — log the warning and continue with available providers.

### H-5: consensus workflow has no reviewer-count validator ✅ FIXED
**File:** `anti.py` — `workflow_expansion` consensus branch
**Issue:** User can run `workflow consensus` with a single model, get a prompt
asking for "disagreements between reviewers," and get no warning.
**Fix:** In the consensus branch, set `--min-successes 2` explicitly and
validate that at least 2 models are available; raise AntiError if fewer.

### H-6: OAuth-mode provider silently returns False — ✅ Confirmed non-issue (grep shows no remaining OAuth presets)
**File:** `server.py` — `provider_has_usable_key`
**Issue:** `PROVIDER_AUTH_MODE_OAUTH` removed from imports but `provider_auth_mode`
is still called. If any surviving provider config returns `auth_mode == "oauth"`,
it silently returns False.
**Fix:** Confirm via grep that no remaining provider preset has
`authMode: "oauth"`. If none do, this is a non-issue. If any do, raise
or route correctly.

## Medium — Test Coverage

### M-4: No tests for new functions ✅ FIXED (8 tests passing)
**Files:** new test file or extend existing tests
**Issue:** `estimate_call_cost`, `actual_call_cost`, `resolve_auto_model`,
`normalize_finding_item` (with new fields), and the dedup logic have no tests.
**Fix:** Add a focused test file covering:
1. `normalize_finding_item` with all new fields (confidence, file, line,
   evidence, fingerprint), defaults, backward compat (missing fields)
2. `normalize_finding_item` dedup: same fingerprint merges lanes, keeps
   highest severity, averages confidence
3. `resolve_auto_model`: small diff → flash-3.6, large diff → opus,
   high-risk path → opus, no context → default
4. `estimate_call_cost`: free tier returns 0, quota tier returns > 0
5. `actual_call_cost`: falls back to estimate when usage is None
6. Verifier: `_check_python_syntax` catches SyntaxError, `_check_secrets`
   finds patterns, skips non-applicable file types

### M-2: Positive OAuth-acceptance test removed — ✅ Confirmed acceptable (OAuth no longer supported)
**File:** `tests/test_byok_providers.py`
**Issue:** The only test of the positive OAuth case was removed with xai-oauth.
**Fix:** Since OAuth is no longer a supported auth mode in the gateway,
this is acceptable. No replacement needed. The `validate_supported_provider_auth_mode`
function should reject OAuth cleanly now.

### M-3: SKILL.md content assertions removed ✅ FIXED
**File:** `tests/test_release_workflow.py`
**Issue:** Assertions verifying SKILL.md contained specific text were deleted.
**Fix:** Add back lightweight SKILL.md content assertions for the new
sections (Findings Schema, Anonymized Panel Judging, Role-Specialized Prompts)
to prevent regression.

### M-5: auto_route_decision/reason written twice ✅ FIXED
**File:** `anti.py` — `command_review`
**Issue:** Redundant double-write of metadata fields.
**Fix:** Remove the duplicate write. Keep only one assignment.

## Cleanup (low priority, no behavioral impact)

### Dead collab functions ✅ FIXED (removed)
- `panel_collaboration_instruction` — returns empty string, called but no-op
- `default_panel_models_for_collab` — returns DEFAULT_PANEL_MODELS, thin wrapper
**Fix:** Remove both functions; inline the calls where needed or simplify.

### SKILL.md description still mentions removed features
- Description references `workflow claude-grok` in trigger list
**Fix:** Already cleaned by Phase 9. Verify no stale references remain.

---

## Implementation Order

1. **H-1 + H-2** (budget bugs) — 30 min, same code region
2. **H-3** (anonymize retry) — 15 min, isolated
3. **H-5** (consensus validator) — 10 min, isolated
4. **H-4** (xai-oauth migration) — 20 min, cli_setup.py
5. **H-6** (OAuth mode check) — 5 min, grep verification
6. **M-5** (duplicate metadata write) — 5 min, trivial
7. **M-4** (new tests) — 1-2 hours, most effort
8. **M-3** (SKILL.md assertions) — 20 min
9. **Cleanup** (dead collab functions) — 15 min

Total estimated: ~3 hours
