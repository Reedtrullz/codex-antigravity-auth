# BluesMinds, DeepSeek V4, and Explicit Grok Routes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add first-class BluesMinds, official DeepSeek V4, and explicit dual-route Grok support to the gateway and bundled/installable Anti skill without changing existing OAuth defaults.

**Architecture:** Extend the existing generic BYOK preset and Anti alias surfaces. Route BluesMinds through the mature OpenAI Chat adapter because native Responses fidelity is not proven; keep DeepSeek on its existing official Chat preset and xAI OAuth Grok unchanged.

**Tech Stack:** Python 3.10+, FastAPI, httpx, unittest/pytest, setuptools package data, Twine.

## Global Constraints

- Never expose or persist API keys or OAuth tokens; use environment-variable references and synthetic test secrets.
- Never edit `~/.codex/config.toml` or introduce automatic cross-provider fallback.
- Canonical skill source is `codex_antigravity_auth/skills/anti/`; refresh the installed copy only through `install-skill` after package proof.
- BluesMinds initially advertises only `grok-4.5` and `z-ai/glm-5.2`; do not add GPT-5.4.
- Preserve existing Grok aliases and `claude-grok` OAuth behavior; Opus remains the default judge.

---

### Task 1: BluesMinds provider preset

**Files:**
- Modify: `tests/test_byok_providers.py`
- Modify: `codex_antigravity_auth/byok.py`

**Interfaces:**
- Produces: `PROVIDER_PRESETS["bluesminds"]` with Chat transport, canonical key env, and exact model list.

- [x] Add a focused test asserting provider identity, base URL, `BLUESMINDS_API_KEY`, API-key auth, Chat kind, and exact models.
- [x] Run the focused test and confirm it fails because the preset is absent.
- [x] Add the minimal preset without new transport code.
- [x] Run provider, transport, streaming, storage, and CLI tests.

### Task 2: Anti aliases and Grok compatibility

**Files:**
- Modify: `codex_antigravity_auth/skills/anti/tests/test_anti.py`
- Modify: `codex_antigravity_auth/skills/anti/scripts/anti.py`

**Interfaces:**
- Produces: deterministic aliases for BluesMinds Grok/GLM, DeepSeek V4, and explicit OAuth Grok.
- Preserves: `CLAUDE_GROK_PANEL_MODELS = ["sonnet", "opus", "grok"]` and Opus judge default.

- [x] Add table-driven alias tests, existing-alias compatibility assertions, default-judge assertions, and `claude-grok` expansion assertions.
- [x] Run each focused test and confirm the new aliases fail before implementation.
- [x] Add only the requested alias entries.
- [x] Run the focused Anti suite and preserve all existing tests.

### Task 3: Panel identity, disclosure, and ledger regressions

**Files:**
- Modify: `codex_antigravity_auth/skills/anti/tests/test_anti.py`
- Modify only if a failing test requires it: `codex_antigravity_auth/skills/anti/scripts/anti.py`

**Interfaces:**
- Consumes: resolved provider-prefixed IDs.
- Preserves: `/v1/models` preflight, explicit missing lanes, `--min-successes`, BYOK disclosures, and sanitized run records.

- [x] Add tests showing BluesMinds and DeepSeek IDs are checked against `/v1/models` and unavailable models become explicit failed lanes.
- [x] Add tests showing repository context disclosures name both providers and saved records preserve provider IDs but redact synthetic credentials.
- [x] Run the new tests red; make only minimal implementation changes if current generic behavior does not already satisfy them.
- [x] Run all panel/workflow/ledger tests green.

### Task 4: Documentation and packaged skill guidance

**Files:**
- Modify: `codex_antigravity_auth/skills/anti/SKILL.md`
- Modify: `README.md`
- Modify: `USAGE.md`
- Modify: `tests/test_release_workflow.py`

**Interfaces:**
- Documents: provider setup with canonical and Clankus override env names, explicit Grok routes, DeepSeek V4 and GLM aliases, Chat-adapter limitation, and advisory-only ownership.

- [x] Add documentation contract tests for required aliases/routes and the absence of GPT-5.4.
- [x] Run the focused release tests and confirm they fail on missing documentation.
- [x] Update canonical skill, README, and USAGE examples for consult/review/plan/panel/MoA/Fusion/provider-compare/fallback.
- [x] Run release and Anti tests green.

### Task 5: Full verification and installation proof

**Files:**
- Verify only: package and installed-skill artifacts.

**Interfaces:**
- Produces: fresh local evidence without publishing.

- [x] Run focused Anti, BYOK, xAI OAuth, CLI, streaming, transport, storage, redaction, and release tests.
- [x] Run `python3 -m pytest -q`, compileall, and `git diff --check`.
- [x] Build fresh wheel and sdist in bounded workspace directories and run Twine checks.
- [x] Install the wheel into a clean temporary Python 3.12 environment; verify CLI help, provider presets, and temporary-home `install-skill --verify`.
- [x] Refresh the real installed skill with `install-skill --force --verify`, run Anti smoke, and prove byte parity with the packaged canonical skill.
- [x] Run bounded live `/models`, non-streaming, streaming, structured-output, and tool probes where credentials/provider support allow; report all blocks exactly.
- [x] Recheck `~/.codex/config.toml` hash, secret redaction, diff scope, and free space.
- [x] Commit the complete feature and evidence docs locally; do not push or publish.
- [x] Append an evidence-backed, credential-free summary to today's Obsidian daily/project note.
