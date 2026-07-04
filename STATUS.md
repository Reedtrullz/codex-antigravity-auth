# Current Integration Status — 3 July 2026

## Build & Test Health
- **local pytest**: 215/215 passing, plus 128 subtests, with `python3 -m pytest -q` ✅
- **compile check**: `python3 -m compileall -q codex_antigravity_auth tests` ✅
- **diff hygiene**: `git diff --check` ✅
- **wheel install smoke**: built wheel/sdist from a scratch copy, ran `twine check`, confirmed project URLs/metadata and MIT license inclusion in built artifacts, installed the wheel into a clean venv, ran `pip check`, verified console script help, `doctor --help`, and provider preset listing ✅
- **live backend smoke**: credentialed live Google OAuth/runtime smoke passed on 2026-07-03 for `claude-3.5-sonnet`; live BYOK smokes passed through transient env vars only for `deepseek:deepseek-v4-flash` and OpenRouter on PR #1 head `e6a81ac` before squash merge `191daa4`. OpenRouter evidence covered `/v1/models` exposure for `openrouter:openrouter/auto`, exact non-streaming and streaming sentinels for `openrouter:openrouter/auto`, and exact non-streaming sentinel routing for `openrouter:deepseek/deepseek-chat` ✅
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

## Known Limitations
- Live Google Antigravity, DeepSeek V4 Flash BYOK, and OpenRouter BYOK smokes have passed with configured credentials/API keys; xAI, Kimi/Moonshot, Ollama cloud, and arbitrary custom BYOK providers still need their own live-key smoke.
- `previous_response_id` is rejected by design in this stateless gateway; replay the full conversation, including tool calls and outputs, in `input`.
- `/v1/responses/compact` is not implemented.
- CI includes unit/compile checks and a release-artifact smoke job, but this local review did not trigger a fresh remote CI run for the uncommitted patch.
- Live backend availability is covered only by the credentialed smoke runs noted above.

## Release State
- Current package metadata on `main`: `1.0.0`
- Last tagged GitHub release: [v0.1.0-alpha](https://github.com/Reedtrullz/codex-antigravity-auth/releases/tag/v0.1.0-alpha)
- A `v1.0.0` tag/release has not been cut in this local review.

## Next Priorities
1. Add `/v1/responses/compact` support
2. Expand live backend smoke coverage beyond DeepSeek/OpenRouter to additional BYOK providers
3. Add a credentialed smoke-test profile for Google and configured BYOK providers
