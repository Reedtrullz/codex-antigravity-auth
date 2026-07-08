# Current Integration Status — 7 July 2026

## Build & Test Health
- **local pytest**: full local suite passing with `python3 -m pytest -q` (`407` tests plus `138` subtests); focused CLI suite also passed with `159` tests plus `20` subtests ✅
- **compile check**: `python3 -m compileall -q codex_antigravity_auth tests` ✅
- **diff hygiene**: `git diff --check` ✅
- **wheel install smoke**: v1.6.4 branch ran `python3 -m build`, `python3 -m twine check dist/*`, installed the wheel into a clean venv, ran `pip check`, verified package version `1.6.4`, proved provider-only config guidance preserves scratch `gpt-5.5`/`xhigh`, proved `--activate` switches the scratch model/provider, verified packaged `status --json` reports 7 reachable models, and verified packaged `install-skill --verify` ✅
- **CI matrix**: PR CI includes Ubuntu Python 3.10/3.11/3.12 plus a Windows Python 3.12 test leg ✅
- **v1.6.3 CI/release**: PR #13 passed duplicate CI runs `28835880811` and `28835888260`; merge commit `55e0e79` passed main CI run `28835931271`; tag `v1.6.3` passed tag CI run `28835943159`; Publish workflow `28835943138` uploaded wheel and sdist to PyPI ✅
- **live backend smoke**: credentialed live Google OAuth/runtime smoke passed on 2026-07-03 for `claude-3.5-sonnet`; live BYOK smokes passed through transient env vars for `deepseek:deepseek-v4-flash` and OpenRouter. Latest release-prep Google smoke on 2026-07-06 used a scratch Codex config, the live gateway on `127.0.0.1:51122`, and `doctor --codex-ready --live --live-model claude-3.5-sonnet`; it passed model catalog, routing, Claude account availability, and real non-streaming generation with preview `ready`. Latest OpenRouter evidence covered direct `/api/v1/auth/key` success, `/v1/models` exposure for `openrouter:openrouter/free`, and exact non-streaming sentinel `anti-openrouter-byok-ok` through `/v1/responses` with the gateway stopped afterward ✅
- **install command**: `uv tool install codex-antigravity-auth` for normal use, `uv tool install --editable .` for development from a checkout
- **doctor/connectivity**: redacted scratch-config `codex-antigravity doctor --codex-ready --live` passed after release-prep live Google OAuth smoke
- **current branch diagnostics fix**: branch-local `python3 -m codex_antigravity_auth.cli status --json` reports the reachable live gateway with 7 models after extending the status reachability probe; installed PyPI `1.6.3` still has the older shorter probe until this branch is released ✅

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
| BYOK auth-mode guardrails (`xai` API-key route kept separate from `xai-oauth` SuperGrok OAuth) | ✅ |
| xAI SuperGrok OAuth browser/device login, encrypted token refresh, and `xai-oauth:*` routing | ✅ |
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
| `$anti panel` verifiable findings contract with usage/latency reporting | ✅ |
| `$anti` BYOK repo-context disclosure and large-review panel summarization | ✅ |
| `$anti workflow` presets for review readiness, deep planning, ship gates, and provider comparison | ✅ |
| `$anti workflow security-review` and `debug-consensus` presets | ✅ |
| `$anti` Claude/Grok collaboration profile and workflow preset | ✅ |
| Sanitized `$anti` run ledger with list/show/clean and dry-run pruning | ✅ |
| Anti run-id correlation into sanitized gateway request logs | ✅ |
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
| Account CLI remove/reset for encrypted Google rotation store | ✅ |
| `logs summary` request-log aggregation by route/family | ✅ |
| `doctor --codex-ready` suggests `setup --repair` for existing config drift | ✅ |
| Safe Codex config activation requiring explicit `--activate` | ✅ |
| Clear provider-block-only `configure-codex --write` success messaging | ✅ |
| Less brittle gateway status reachability probe for cold `/v1/models` responses | ✅ |

## Known Limitations
- Live Google Antigravity, DeepSeek V4 Flash BYOK, and OpenRouter BYOK smokes have passed previously with configured credentials/API keys; current BYOK live proof still depends on fresh provider keys. The 1Password OpenRouter/DeepSeek login items inspected during the MoA/Fusion PR did not contain provider API-key-shaped values, so fresh BYOK credential material is still needed for another live provider smoke.
- `doctor --live` and `setup --check --live` are explicit opt-in checks because they spend a real Google provider request.
- `previous_response_id` is rejected by design in this stateless gateway; replay the full conversation, including tool calls and outputs, in `input`.
- `/v1/responses/compact` is not implemented.
- CI includes unit/compile checks, a release-artifact smoke job, and Windows Python 3.12 coverage. Latest released v1.6.3 proof is listed above; this v1.6.4 polish branch still needs PR CI before merge.
- Live backend availability is covered only by the credentialed smoke runs noted above.
- Helper-level MoA/Fusion remains advisory; virtual picker models such as `panel:*`, `moa:*`, or `fusion:*` are not implemented.

## Release State
- Current package metadata: `1.6.4` on the release polish/status repair branch.
- Latest tagged GitHub release: [v1.6.3](https://github.com/Reedtrullz/codex-antigravity-auth/releases/tag/v1.6.3)
- PyPI Trusted Publishing run `28835943138` published `codex-antigravity-auth==1.6.3`; strict clean `pip install codex-antigravity-auth==1.6.3` plus scratch safe-config activation smoke passed.

## Next Priorities
1. Merge and optionally tag `v1.6.4` after PR CI proves the status/messaging/docs polish branch.
2. Add `/v1/responses/compact` support.
3. Expand live backend smoke coverage beyond DeepSeek/OpenRouter to additional BYOK providers.
4. Add a documented credentialed smoke-test profile for 1Password-backed BYOK providers without persisting raw API keys.
