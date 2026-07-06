# Current Integration Status — 6 July 2026

## Build & Test Health
- **local pytest**: full local suite passing with `python3 -m pytest -q` (`370` tests plus `134` subtests) ✅
- **compile check**: `python3 -m compileall -q codex_antigravity_auth tests` ✅
- **diff hygiene**: `git diff --check` ✅
- **wheel install smoke**: ran `python3 -m build`, `python3 -m twine check dist/*`, installed the wheel into a clean venv, ran `pip check`, verified console script help plus `service status --json`, `models list --json`, `logs --tail 1 --json`, scratch `setup --check`, and packaged `install-skill --verify` ✅
- **PR #8 CI**: head `3265240` passed duplicate GitHub CI runs `28768862063` and `28768878878` across `package`, Python `3.10`, `3.11`, and `3.12` ✅
- **live backend smoke**: credentialed live Google OAuth/runtime smoke passed on 2026-07-03 for `claude-3.5-sonnet`; live BYOK smokes passed through transient env vars for `deepseek:deepseek-v4-flash` and OpenRouter. Latest OpenRouter evidence covered direct `/api/v1/auth/key` success, `/v1/models` exposure for `openrouter:openrouter/free`, and exact non-streaming sentinel `anti-openrouter-byok-ok` through `/v1/responses` with the gateway stopped afterward ✅
- **install command**: `uv tool install .` for normal use, `uv tool install --editable .` for development
- **doctor/connectivity**: redacted `codex-antigravity doctor` passed after live Google OAuth smoke

## Core Features
| Feature | Status |
|---|---|
| OAuth PKCE login | ✅ |
| Guided multi-account OAuth setup | ✅ |
| OS keyring token encryption | ✅ |
| Multi-account rotation | ✅ |
| Exponential cooldown backoff | ✅ |
| Auto token refresh | ✅ |
| Responses API translation | ✅ |
| SSE streaming | ✅ |
| Tool/function calling | ✅ |
| Reasoning/thinking isolation | ✅ |
| `/v1/models` endpoint | ✅ |
| Codex CLI model refresh-compatible `/v1/models` catalog metadata | ✅ |
| Codex Desktop model picker | ✅ |
| Schema sanitization | ✅ |
| Device fingerprinting | ✅ |
| BYOK provider presets | ✅ |
| OpenAI-compatible provider routing | ✅ |
| Unknown colon-prefixed BYOK providers rejected before Google routing | ✅ |
| Reserved slash-style Codex/OpenAI model prefixes protected from BYOK shadowing | ✅ |
| Encrypted API-key provider config | ✅ |
| BYOK model exposure requires valid keys or loopback key-optional paths | ✅ |
| Generic `custom` BYOK preset requires explicit user configuration before routing | ✅ |
| BYOK/Codex URL validation before config writes | ✅ |
| Plain HTTP BYOK/gateway URLs limited to loopback/local hosts | ✅ |
| BYOK managed-header guardrails | ✅ |
| BYOK transport/hop-by-hop header guardrails | ✅ |
| BYOK API-key/header value sanitization before config writes | ✅ |
| BYOK API-key/header values limited to HTTP header-safe ASCII | ✅ |
| BYOK model-picker field sanitization before config writes | ✅ |
| BYOK config preflight before streaming | ✅ |
| BYOK streaming tool-call output waits for complete valid function names | ✅ |
| BYOK streaming final output order matches emitted output indices | ✅ |
| BYOK nested and orphan valid tool outputs preserved as Chat Completions tool messages | ✅ |
| BYOK key-env models stay hidden until the env key exists | ✅ |
| Google streaming account-scoped error frames rotate before failing when no output was emitted | ✅ |
| Streaming provider error frames surface as `response.failed` | ✅ |
| Non-streaming backend error payloads surface as errors instead of empty completions | ✅ |
| Malformed SSE JSON chunks fail streams instead of completing | ✅ |
| Streaming completion snapshots include reconstructed output/model metadata | ✅ |
| Tool-only streaming responses do not synthesize empty assistant messages | ✅ |
| Responses streaming emits sequence numbers, item IDs, and `response.output_text.done` lifecycle events | ✅ |
| Internal schema placeholder arguments are stripped from returned function calls | ✅ |
| JSON Schema `required` values normalized before backend routing | ✅ |
| Malformed function-tool metadata normalization before routing | ✅ |
| Malformed function-call names normalized or dropped before routing | ✅ |
| Malformed generation option and `tool_choice` rejection before routing | ✅ |
| Google/BYOK `top_p` and stop-sequence generation option forwarding | ✅ |
| BYOK structured-output `response_format` normalization before routing | ✅ |
| Malformed provider/backend usage-counter normalization | ✅ |
| Google `developer` messages preserved as system instructions | ✅ |
| Malformed Google account fingerprint data ignored before routing | ✅ |
| Malformed Google project overrides ignored before routing | ✅ |
| Codex config helper with private atomic symlink-safe backups | ✅ |
| `setup-google` preflights OAuth credentials before Codex config writes | ✅ |
| Custom `setup-google --port` derives matching Codex base URL | ✅ |
| BYOK-only doctor mode | ✅ |
| Doctor parses active Codex config provider/base URL and supports `--config` | ✅ |
| Doctor validates the selected BYOK provider/model instead of unrelated local presets | ✅ |
| Doctor supports custom Codex provider ids via setup and `--provider` | ✅ |
| Doctor exits non-zero on hard failures | ✅ |
| OAuth callback port conflicts report actionable CLI errors | ✅ |
| CLI setup/provider write failures reported without traceback | ✅ |
| `/v1/responses` request-shape validation | ✅ |
| Browser-style cross-site/plaintext loopback POST guard for `/v1/responses` | ✅ |
| Loopback Host validation for browser-style `/v1/responses` requests | ✅ |
| Remote gateway token strength floor | ✅ |
| Central redaction for auth/provider errors | ✅ |
| Finite Retry-After / Google RetryInfo cooldown hints | ✅ |
| Fail-closed malformed-store write/update guardrails | ✅ |
| Malformed OAuth `expires_in` fallback handling | ✅ |
| Bundled `$anti` skill installer with external backup directory and verification mode | ✅ |
| `setup-v2` readiness checks for sidecar, installed skill, gateway model visibility, and optional BYOK readiness | ✅ |
| `$anti panel` / MoA / Fusion advisory multi-model review and planning helper | ✅ |
| `$anti workflow` presets for review readiness, deep planning, ship gates, and provider comparison | ✅ |
| Sanitized `$anti` run ledger with list/show/clean and dry-run pruning | ✅ |
| `$anti` fallback/progress controls for long model calls and retryable backend drift | ✅ |
| PyPI Trusted Publishing workflow for `v*` tags | ✅ |
| Cross-platform per-user gateway service install/status/uninstall | ✅ |
| 1Password `op run` runtime injection for BYOK gateway env keys | ✅ |
| Nonblocking Google account selection through Starlette threadpool | ✅ |
| Refresh-ahead helper for accounts expiring within 5 minutes | ✅ |
| Sanitized capped request JSONL log with CLI tail/follow/clean | ✅ |
| Loopback-only `/health` endpoint with anonymous cooldown/request diagnostics | ✅ |
| Local model catalog overlays via `~/.codex/antigravity-models.toml` | ✅ |
| Runtime fail-soft fallback for malformed local model overlays | ✅ |
| Strict model-overlay identifier shadowing checks in `models add` / `models doctor` | ✅ |
| `setup --repair` Codex config reconciliation without OAuth/skill/gateway mutation | ✅ |
| Persisted per-account usage/failure/429 counters by model family | ✅ |
| Claude reasoning-effort audit in `models doctor` | ✅ |
| `doctor --live` and `setup --check --live` real Google `/v1/responses` smoke probes | ✅ |
| Interactive OAuth client credential onboarding in primary `setup --write` | ✅ |
| 1Password CLI presence enforced before gateway/service wrapping | ✅ |
| Process-local in-flight Google account spreading for concurrent Codex requests | ✅ |
| Cached PyPI package-version drift warning in doctor/readiness diagnostics | ✅ |

## Known Limitations
- Live Google Antigravity, DeepSeek V4 Flash BYOK, and OpenRouter BYOK smokes have passed with configured credentials/API keys; xAI, Kimi/Moonshot, Ollama cloud, and arbitrary custom BYOK providers still need their own live-key smoke.
- `doctor --live` and `setup --check --live` are explicit opt-in checks because they spend a real Google provider request.
- `previous_response_id` is rejected by design in this stateless gateway; replay the full conversation, including tool calls and outputs, in `input`.
- `/v1/responses/compact` is not implemented.
- CI includes unit/compile checks and a release-artifact smoke job. PR #8 head `3265240` passed pull-request CI before merge.
- Live backend availability is covered only by the credentialed smoke runs noted above.

## Release State
- Current package metadata: `1.4.0`
- Previous tagged GitHub release: [v1.4.0](https://github.com/Reedtrullz/codex-antigravity-auth/releases/tag/v1.4.0)
- The current dirty worktree contains the planned v1.5.0 implementation for live install smoke, OAuth credential onboarding, 1Password hardening, concurrent account spreading, and package-version drift diagnostics. Release metadata/tagging are intentionally left for maintainer release prep.

## Next Priorities
1. Add `/v1/responses/compact` support
2. Expand live backend smoke coverage beyond DeepSeek/OpenRouter to additional BYOK providers
3. Run a final credentialed `doctor --codex-ready --live` smoke before the v1.5.0 release
4. Add a documented credentialed smoke-test profile for 1Password-backed BYOK providers without persisting raw API keys
