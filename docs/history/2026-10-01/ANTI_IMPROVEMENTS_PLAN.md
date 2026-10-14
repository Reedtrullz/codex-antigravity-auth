# Anti Skill Improvement Plan

Based on multi-model code review product research (Aug 2026). Each phase is
independently shippable and testable. Estimated effort is rough — assumes
familiarity with the codebase.

---

## Phase 1: Enriched Findings Schema (low risk, high signal)

**Goal:** Make findings machine-actionable with provenance, evidence, and dedup.

**Current schema** (`normalize_finding_item` at anti.py:3008):
```json
{"id": "F001", "claim": "...", "severity": "medium", "lanes": [...], "verify": "..."}
```

**Proposed schema:**
```json
{
  "id": "F001",
  "claim": "...",
  "severity": "medium",
  "confidence": 0.8,
  "lanes": ["sonnet", "opus"],
  "verify": "...",
  "evidence": "test output or type error or 'unverified'",
  "file": "src/foo.py",
  "line": 42,
  "fingerprint": "sha256:src/foo.py:42:claim-hash"
}
```

**Changes:**
- Extend `normalize_finding_item` to accept and normalize `confidence` (float 0-1, default 0.5), `file`/`line` (extracted from claim or verify text via regex), `evidence` (string, default "unverified").
- Add `fingerprint` = `sha256(file:line:normalized_claim)` for cross-lane dedup.
- Add `normalize_panel_findings` dedup step: group by fingerprint, merge lanes, keep highest severity, average confidence.
- Update judge prompt template (panel `--output findings`) to instruct the judge to emit `file`, `line`, `confidence`, and `evidence` per finding.
- Update `fallback_findings_contract` and the findings JSON schema validation.
- Backward-compatible: old prompts that don't emit new fields get defaults.

**Files:** `anti.py` (normalize_finding_item, parse_panel_findings, fallback_findings_contract, judge prompt templates in _panel_argv region)

**Tests:** Extend `test_fidelity_transforms.py` or add `test_findings_schema.py` for dedup, defaults, backward compat.

---

## Phase 2: Role-Specialized Panel Prompts (medium risk, high value)

**Goal:** Each panel lane gets a role-specific rubric instead of the same generic prompt.

**What exists:** `--role` flag is passed to the judge prompt but lanes all receive the same `prompt`. The `review-ready` workflow already specifies roles `["correctness", "security", "tests", "install-docs"]`.

**Changes:**
- In `run_panel_call` (called by the thread pool in `command_panel`), if `args.role` is set, append a role-specific rubric block to each lane's prompt:
  ```
  ## Your Review Role: {role}
  Focus your analysis exclusively on {role} concerns.
  - For "security": injection, secrets, authz, trust boundaries, dependency exposure.
  - For "correctness": logic errors, edge cases, type mismatches, off-by-one.
  - For "tests": missing coverage, testable contracts, regression risk.
  - For "performance": algorithmic complexity, allocations, N+1 queries.
  - For "UX": usability, accessibility, error messages, edge-case UX.
  ```
- Define a `ROLE_RUBRICS` dict in `anti.py` with ~8 standard roles.
- If multiple lanes share the same role (e.g., two `sonnet` lanes), they still get the same rubric — diversity comes from model diversity, not role duplication.
- The judge prompt already receives `panel_results` metadata with per-lane roles; ensure it weights role-matching findings higher.

**Files:** `anti.py` (new ROLE_RUBRICS dict, run_panel_call modification, judge prompt in command_panel region ~line 3400)

**Tests:** Unit test that role rubric is injected into lane prompts; test that judge prompt includes role assignments.

---

## Phase 3: Anonymized Panel Before Judging (low risk, behavioral)

**Goal:** Remove model brand bias from judge synthesis.

**What exists:** Judge receives raw `panel_results` with model names visible.

**Changes:**
- Before calling the judge, anonymize lane labels: replace model names with generic labels ("Lane A", "Lane B", etc.) in the prompt text sent to the judge.
- Record the mapping (Lane A → sonnet, Lane B → opus) in metadata for post-hoc audit.
- Keep the non-anonymized panel_results in the run record; only anonymize at judge-call time.
- Add `--no-anonymize` flag to disable this (for debugging or when user explicitly wants brand-weighted synthesis).
- Randomize lane order before sending to judge (shuffle panel_results).

**Files:** `anti.py` (command_panel, around the judge call ~line 3600-3800)

**Tests:** Test that judge prompt contains "Lane A/B" not model names; test that run record preserves original identities; test `--no-anonymize` bypass.

---

## Phase 4: Smart Auto-Routing by Task Complexity (medium risk, cost-saving)

**Goal:** Automatically pick the cheapest adequate model based on diff size and risk.

**What exists:** User manually picks `--model`. Cost-awareness hint prints a suggestion.

**Changes:**
- Add `--auto-route` flag to `consult`, `review`, and `panel`.
- Classification logic:
  - Small diff (<200 lines), low-risk files (config, docs, tests) → flash-3.6 or poolside
  - Medium diff (200-1000 lines), mixed risk → sonnet or nemotron-ultra
  - Large diff (>1000 lines), security-sensitive, core logic → opus
  - Prompt-only (no scope) → sonnet (current default)
- Risk signals: file paths containing `auth`, `crypto`, `security`, `migration`, `schema`; presence of `+`/`-` in security-sensitive patterns.
- Store the routing decision in metadata: `auto_route_decision`, `auto_route_reason`.
- Don't override if user explicitly passes `--model`.

**Files:** `anti.py` (new `resolve_auto_model` function, integration in command_consult/command_review/command_panel entry points)

**Tests:** Unit tests for routing heuristics; test that explicit `--model` bypasses auto-route; test metadata includes routing decision.

---

## Phase 5: New Workflow Presets (low risk, user-facing)

**Goal:** Fill gaps identified by research.

### 5a. `workflow quick-check`
Fast pre-commit gate using only free/cheap models. 60s budget. No Opus.

```
anti.py workflow quick-check --scope staged
```

Expands to: `panel --mode review --scope staged --model flash-3.6 --model poolside --judge nemotron-ultra --timeout 60 --max-prompt-chars 20000 --output findings`

### 5b. `workflow consensus`
Focused on disagreement detection. Run 3+ models, find where they disagree, escalate only disagreements to judge.

```
anti.py workflow consensus --scope staged --prompt "Review for bugs"
```

Expands to: `panel --mode review --scope staged --model sonnet --model flash-3.6 --model poolside --judge opus --output findings` with a custom judge prompt that emphasizes: "Report only disagreements. For consensus items, state briefly and move on."

### 5c. Enhance `workflow security-review`
Already exists but could be strengthened:
- Add `--model deepseek-v4-pro` as default for security lane (code-specialized, paid).
- Add explicit "secrets audit" role to the rubric.
- Add `--verify` flag to run `grep -r` for common secret patterns as a tool-backed check.

**Files:** `anti.py` (workflow_expansion function, add new elif branches), `SKILL.md` (document new presets)

**Tests:** Test that new presets expand to correct argv; test unknown workflow still errors.

---

## Phase 6: Evidence-Linked Verification Stage (medium risk, high trust)

**Goal:** Turn "model thinks this is a bug" into "tests/lint confirm this is a bug."

**Changes:**
- Add a new `verify` stage that runs after panel synthesis.
- When findings have `file` and `line`, run targeted checks:
  - `python3 -m py_compile {file}` for Python syntax
  - `mypy {file}` or `pyright {file}` for type errors (if configured)
  - `pytest {file} -x --tb=short` for test failures (if file has tests)
  - `grep -n "pattern" {file}` for secrets patterns
  - `eslint {file}` for JS/TS
- Attach tool output as `evidence` on each finding.
- Add `--no-verify` to skip this stage.
- This is optional and best-effort: if tools aren't installed, skip silently.

**Files:** New `anti_lib/verifier.py` with `verify_finding(finding, workspace_root) -> finding`; integration in `command_panel` after judge synthesis.

**Tests:** Test verifier with a known-broken Python file; test graceful skip when tools missing.

---

## Phase 7: Per-Run Budget Cap (low risk, cost control)

**Goal:** Fail gracefully when estimated cost exceeds a cap.

**Changes:**
- Add `--budget` flag (max tokens or max USD estimate).
- Before each model call, estimate cost from prompt chars and max_output_tokens using model cost tier.
- If estimated total exceeds budget, skip remaining models and mark panel as `degraded_single_model` with budget-exceeded caveat.
- Surface cumulative cost in `runs show` output.
- Track actual usage per call (already in `generation_metadata`); compare estimated vs actual.

**Files:** `anti.py` (new `estimate_cost` function, budget check in generate_with_fallback and panel fan-out)

**Tests:** Test budget cap stops panel early; test single consult respects budget; test cost tracking in run record.

---

## Phase 8: Repo-Level Reflection Memory (higher risk, long-term)

**Goal:** Learn from past reviews to reduce false positives and improve routing.

**Changes:**
- Store per-repo review outcomes in `~/.codex/anti-runs/reflections/{repo_hash}.json`.
- Track: recurring false-positive patterns (finding claim + file + "dismissed"), accepted findings, model accuracy by file domain.
- On subsequent reviews, suppress findings matching known false-positive patterns (with `--no-reflections` to bypass).
- Use reflection data to calibrate auto-routing (Phase 4): if a model has high false-positive rate on a repo's files, deprioritize it.
- Bound with TTL (90 days) and max entries (500).

**Files:** New `anti_lib/reflections.py`; integration in `command_panel` and `command_review` for read/write; `resolve_auto_model` in Phase 4 for routing calibration.

**Tests:** Test reflection write/read/cleanup; test suppression of known false positives; test TTL expiry.

---

## Implementation Order

| Phase | Risk | Value | Effort | Depends On |
|-------|------|-------|--------|------------|
| 1. Findings schema | Low | High | 1-2 days | — | ✅ DONE |
| 2. Role prompts | Medium | High | 1-2 days | — | ✅ DONE |
| 3. Anonymize judge | Low | Medium | 0.5 day | — | ✅ DONE |
| 5. New presets | Low | Medium | 0.5 day | 1, 2 | ✅ DONE |
| 4. Auto-routing | Medium | High | 2-3 days | — |
| 7. Budget cap | Low | Medium | 1 day | — | ✅ DONE |
| 6. Verification | Medium | High | 3-4 days | 1 | ✅ DONE |
| 8. Reflections | Higher | Medium | 3-4 days | 1, 4 |

**Recommended start:** Phases 1 → 2 → 3 → 5 (fast wins, no breaking changes, immediately testable).

---

## What NOT to Change

- The synchronous execution model is a feature, not a bug. Backgrounding would add complexity without clear value for the sidecar use case.
- Keep `--save-output` as an auditing mechanism, not the primary output channel. stdout remains the contract.
- Don't add a GUI, daemon, or always-on mode. Anti stays a CLI tool invoked per-task.
- Don't add recursive agent spawning or autonomous multi-hour sessions. Bounded runs with clear exit conditions.

---

## Phase 9: Remove Specialized Grok/xAI OAuth and BluesMinds Support (breaking, strategic)

**Goal:** Double down on Antigravity as the primary product. Keep BYOK for convenience but remove dedicated Grok/xAI OAuth lanes and BluesMinds as first-class citizens.

**Rationale:** The Grok/xAI OAuth integration and BluesMinds routes add significant code complexity (dedicated OAuth module, server handlers, BYOK presets, collab profiles, workflow presets) for routes that are operationally degraded (BluesMinds billing errors, xAI OAuth entitlement fragility). Antigravity (Google Gemini + Claude) is the core value prop. BYOK support stays — users can still add `xai` with `XAI_API_KEY` via the standard BYOK flow — but no special-cased OAuth, collab profiles, or workflow presets.

### What gets removed

**Anti skill (`anti.py`):**
- MODEL_ALIASES: Remove `grok`, `supergrok`, `xai-grok`, `grok-oauth`, `grok-build`, `grok-build-0.1`, `grok-4.3`, `grok-4`, `grok-bluesminds`, `grok-4.5`, `glm-5.2`, `glm52`
- MODEL_CAPABILITIES: Remove `xai-oauth:grok-build-0.1`, `xai-oauth:grok-4.3`, `bluesminds:grok-4.5`, `bluesminds:z-ai/glm-5.2`
- MODEL_COST_TIERS: Remove those 4 entries
- MODEL_QUALITY_SCORES: Remove those 4 entries
- COLLAB_PROFILES: Remove `"claude-grok"` — set to `{"none"}` only
- CLAUDE_GROK_PANEL_MODELS: Delete constant
- `claude_grok_reviewer_family()`: Delete function
- `validate_claude_grok_workflow_reviewers()`: Delete function
- `normalize_collab_profile()`: Simplify to always return `"none"` (or remove the collab concept entirely if no other profiles exist)
- Panel judge prompt: Remove the `collaboration_profile == "claude-grok"` special-case branch
- `command_panel` assembly: Remove collab-profile-specific model selection and prompt adjustments
- `workflow_expansion()`: Remove the `elif args.name == "claude-grok"` branch (~30 lines)
- Workflow preset list: Remove `claude-grok` from recognized workflows

**SKILL.md:**
- Remove model entries for grok/supergrok/grok-build/grok-oauth/grok-4.3/grok-bluesminds/grok-4.5/glm-5.2/glm52 from the model table
- Remove `--collab claude-grok` from all examples
- Remove `workflow claude-grok` from examples and workflow docs
- Remove BluesMinds caveats section (lines 127-130)
- Remove "Grok/xAI" from complementary reviewer lanes section
- Remove "BluesMinds Grok/GLM" from description trigger list
- Simplify collab profile references throughout

**Gateway (`codex-antigravity-auth`):**
- Keep `xai` as a standard BYOK provider in `byok.py` (API-key auth, no OAuth)
- Remove `xai-oauth` provider preset from `byok.py`
- Remove `bluesminds` provider preset from `byok.py`
- Remove `xai_oauth.py` module entirely (or gate behind `--experimental`)
- Remove xai-oauth handler functions from `server.py` (~100 lines: `xai_oauth_headers`, `prepare_xai_oauth_responses_request`, `create_xai_oauth_response`, `xai_oauth_responses_sse_generator`, `xai_oauth_entitlement_detail`)
- Remove xai-oauth setup flow from `cli_setup.py`
- Remove xai-oauth login command from `cli.py` (if present)

### What stays

- `xai` BYOK provider with `XAI_API_KEY` env var — users can still add it via `codex-antigravity provider set xai --api-key-env XAI_API_KEY --model grok-build-0.1`
- OpenRouter BYOK routes (these are provider-agnostic, not Grok-specific)
- DeepSeek BYOK routes
- Ollama local routes
- All Antigravity routes (Gemini, Claude)
- The `--collab` flag infrastructure (just remove the `claude-grok` profile; the mechanism stays for future profiles)

### Migration

- Users with active xai-oauth login: document that they need to switch to `xai` BYOK with `XAI_API_KEY`
- Users with `grok`/`glm-5.2` in their model config: those aliases will error with "unknown model" — they need to use the full BYOK `xai:grok-build-0.1` form
- No data loss: encrypted account/provider stores keep working; just the OAuth token becomes unused

### Risk

- Breaking change for Grok/BluesMinds users (likely very few given operational status)
- Simplifies codebase significantly (~200 lines removed across skill + gateway)
- Reduces test surface (OAuth flow tests, BluesMinds health-check tests)

### Files

- `anti.py` (model dicts, collab logic, workflow expansion)
- `SKILL.md` (model table, examples, workflow docs)
- `byok.py` (remove bluesminds/xai-oauth presets)
- `server.py` (remove xai-oauth handlers)
- `xai_oauth.py` (delete or gate)
- `cli_setup.py` (remove xai-oauth setup)
- Tests: remove/update xai-oauth and bluesminds test cases

---

## Revised Implementation Order

| Phase | Risk | Value | Effort | Depends On |
|-------|------|-------|--------|------------|
| 1. Findings schema | Low | High | 1-2 days | — | ✅ DONE |
| 2. Role prompts | Medium | High | 1-2 days | — | ✅ DONE |
| 3. Anonymize judge | Low | Medium | 0.5 day | — | ✅ DONE |
| 5. New presets | Low | Medium | 0.5 day | 1, 2 | ✅ DONE |
| 9. Remove Grok/BluesMinds | Breaking | High | 2-3 days | — | ✅ DONE |
| 4. Auto-routing | Medium | High | 2-3 days | 9 | ✅ DONE |
| 7. Budget cap | Low | Medium | 1 day | — | ✅ DONE |
| 6. Verification | Medium | High | 3-4 days | 1 | ✅ DONE |
| 8. Reflections | Higher | Medium | 3-4 days | 1, 4 |

**Recommended start:** Phases 1 → 2 → 3 → 5 → 9 → 4

Phase 9 should come before Phase 4 (auto-routing) so the routing logic doesn't need to account for Grok/BluesMinds models that are being removed.
