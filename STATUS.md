# Current Integration Status — 3 July 2026

## Build & Test Health
- **local pytest**: 168/168 passing, plus 120 subtests, with `python3 -m pytest -q` ✅
- **compile check**: `python3 -m compileall -q codex_antigravity_auth tests` ✅
- **diff hygiene**: `git diff --check` ✅
- **wheel install smoke**: built wheel, installed into a clean venv, ran `pip check`, verified console script importability, and verified installed malformed Google project override/account fingerprint, non-ASCII BYOK API-key/header handling, and unknown BYOK provider-prefix rejection. Earlier install smoke also verified temp Codex config mode `600`, malformed-request rejection, function-name filtering, keyless-loopback BYOK gating, Google generation controls, and fragmented BYOK tool-name streaming ✅
- **live backend smoke**: live Google OAuth/runtime smoke passed for `claude-3.5-sonnet`; live BYOK smokes passed through transient env vars only for `deepseek:deepseek-v4-flash` and OpenRouter. OpenRouter evidence covered `/v1/models` exposure for `openrouter:openrouter/auto`, exact non-streaming and streaming sentinels for `openrouter:openrouter/auto`, and exact non-streaming sentinel routing for `openrouter:deepseek/deepseek-chat` ✅
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
| Encrypted API-key provider config | ✅ |
| BYOK model exposure requires valid keys or loopback key-optional paths | ✅ |
| BYOK/Codex URL validation before config writes | ✅ |
| BYOK managed-header guardrails | ✅ |
| BYOK API-key/header value sanitization before config writes | ✅ |
| BYOK API-key/header values limited to HTTP header-safe ASCII | ✅ |
| BYOK model-picker field sanitization before config writes | ✅ |
| BYOK config preflight before streaming | ✅ |
| BYOK streaming tool-call output waits for complete valid function names | ✅ |
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
| CLI setup/provider write failures reported without traceback | ✅ |
| `/v1/responses` request-shape validation | ✅ |
| Central redaction for auth/provider errors | ✅ |
| Finite Retry-After / Google RetryInfo cooldown hints | ✅ |
| Fail-closed malformed-store write/update guardrails | ✅ |
| Malformed OAuth `expires_in` fallback handling | ✅ |

## Known Limitations
- Live Google Antigravity, DeepSeek V4 Flash BYOK, and OpenRouter BYOK smokes have passed with configured credentials/API keys; xAI, Kimi/Moonshot, Ollama cloud, and arbitrary custom BYOK providers still need their own live-key smoke.
- `previous_response_id` is rejected by design in this stateless gateway; replay the full conversation, including tool calls and outputs, in `input`.
- `/v1/responses/compact` is not implemented.
- CI proves local unit/compile health only; live backend availability is covered only by the credentialed smoke runs noted above.

## Last Documented Release
[v0.1.0-alpha](https://github.com/Reedtrullz/codex-antigravity-auth/releases/tag/v0.1.0-alpha)

## Next Priorities
1. Add `/v1/responses/compact` support
2. Expand live backend smoke coverage beyond DeepSeek/OpenRouter to additional BYOK providers
3. Add a credentialed smoke-test profile for Google and configured BYOK providers
