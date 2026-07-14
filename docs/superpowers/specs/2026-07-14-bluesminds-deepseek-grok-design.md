# BluesMinds, DeepSeek V4, and Explicit Grok Routes Design

## Goal

Add first-class BluesMinds and DeepSeek V4 advisory lanes to the Anti gateway and bundled skill while preserving xAI OAuth Grok behavior, provider identity, and existing secret/privacy boundaries.

## Verified inputs

- Main was clean at `f01174c850ad390ff2aab56c99763bbadd4d9bcc` before the feature worktree was created.
- The existing `deepseek` preset already contains `deepseek-v4-pro` and `deepseek-v4-flash` and uses the official `https://api.deepseek.com` Chat Completions endpoint.
- DeepSeek's official API reference lists exactly `deepseek-v4-pro` and `deepseek-v4-flash`, supports Chat Completions streaming, JSON output, tool calls, usage, and reasoning content, and its authenticated `/models` response advertised both IDs on 2026-07-14.
- The authenticated BluesMinds `/v1/models` response advertised exactly `grok-4.5` and `z-ai/glm-5.2` on 2026-07-14.
- Minimal BluesMinds generation probes reached both `/v1/responses` and `/v1/chat/completions`, but both returned HTTP 402 `billing_error`. This proves authentication and error-shape reachability, not generation fidelity.
- The Clankus 1Password Developer Environment exposes the relevant variables as `api_bluesminds_com` and `codex_deepseek_api_key`; only names were inspected.

## Architecture

### Provider catalog

Add `bluesminds` to `PROVIDER_PRESETS` as an `openai_chat` provider with base URL `https://api.bluesminds.com/v1`, canonical key variable `BLUESMINDS_API_KEY`, and only the two requested models. Normal `provider set bluesminds --api-key-env ...` override behavior remains unchanged, so the existing Clankus variable can be selected without renaming or exposing it.

The Chat adapter is deliberately selected over native Responses. BluesMinds advertises an OpenAI-compatible catalog, but successful streaming, structured output, tool calls, usage, and model-identity behavior could not be proven because generation is credit-blocked. The gateway must not claim native Responses compatibility on this evidence.

DeepSeek stays on its existing official Chat adapter and preset. No model IDs are duplicated and neither DeepSeek alias routes through BluesMinds.

### Anti aliases and collaboration

Add deterministic aliases:

- `grok-oauth` -> `xai-oauth:grok-build-0.1`
- `grok-bluesminds` -> `bluesminds:grok-4.5`
- `grok-4.5` -> `bluesminds:grok-4.5`
- `deepseek-v4-pro` -> `deepseek:deepseek-v4-pro`
- `deepseek-v4-flash` -> `deepseek:deepseek-v4-flash`
- `glm-5.2` and `glm52` -> `bluesminds:z-ai/glm-5.2`

Existing `grok`, `supergrok`, `xai-grok`, `grok-build`, `grok-build-0.1`, `grok-4`, and `grok-4.3` mappings remain unchanged on xAI OAuth. `claude-grok` also remains OAuth-backed by default. Users choose BluesMinds explicitly with `--model grok-bluesminds`; a new `--grok-route` flag is unnecessary because explicit aliases are clearer and already compose with panel and workflow commands.

Opus remains the default panel judge. Native Codex remains the acting agent; all Anti lanes remain advisory.

### Availability, privacy, and records

Every resolved provider-prefixed ID continues through the existing `/v1/models` check before generation. Missing panel models remain explicit failed lanes when `--min-successes` permits partial execution, otherwise the panel fails closed.

Repository/diff/file context sent to BluesMinds or DeepSeek uses the existing BYOK disclosure, which includes the resolved provider-prefixed model IDs. Run records retain those IDs and sanitized provider errors but never API keys or OAuth tokens. Fallback remains opt-in and never changes provider automatically unless the user explicitly names a fallback model and policy.

## Documentation and packaging

Update the bundled `SKILL.md`, README, and USAGE examples for consult, review, planning, panel/MoA/Fusion, provider comparison, fallback, and deliberate Claude/Grok route selection. Document GLM-5.2 as a long-context planning/repository-review lane and the BluesMinds Chat-adapter limitation.

The canonical bundled skill under `codex_antigravity_auth/skills/anti/` remains the only source. Package-data tests and clean-wheel installation prove it ships. The supported `install-skill --force --verify` flow refreshes `~/.codex/skills/anti` only after clean-package proof passes. There is no separate canonical `.codex-plugin` tree in this repository.

## Test and verification strategy

Use red-green TDD for provider preset, alias, workflow, disclosure, ledger, and packaging behavior. Preserve existing xAI OAuth tests and add focused regressions for provider-specific model validation, partial panels, minimum successes, and default judge behavior.

After focused and full tests, run compileall, diff check, wheel/sdist build, Twine checks, a clean Python 3.12 wheel install, temporary-home skill installation verification, installed-skill parity, and Anti smoke. Live provider probes remain bounded and must report HTTP/model evidence separately. HTTP 402 is an explicit blocked generation result, not a successful smoke.

## Non-goals

- Do not add GPT-5.4.
- Do not add automatic cross-provider fallback.
- Do not change `~/.codex/config.toml`.
- Do not create, rotate, revoke, or replace credentials.
- Do not publish or release a package.
- Do not claim BluesMinds native Responses, tool, structured-output, streaming, or usage compatibility without successful live evidence.
