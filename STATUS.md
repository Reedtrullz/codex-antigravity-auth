# Current Integration Status — 2 July 2026

## Build & Test Health
- **local pytest**: 141/141 passing, plus 99 subtests, with `python3 -m pytest -q` ✅
- **compile check**: `python3 -m compileall -q codex_antigravity_auth tests` ✅
- **diff hygiene**: `git diff --check` ✅
- **wheel install smoke**: built wheel, installed into a clean venv, ran `pip check`, wrote temp Codex config mode `600`, and verified installed `/v1/models` env-key behavior ✅
- **install command**: `uv tool install .` for normal use, `uv tool install --editable .` for development
- **doctor/connectivity**: available through `codex-antigravity doctor`; not live-verified in this local hardening pass

## Core Features
| Feature | Status |
|---|---|
| OAuth PKCE login | ✅ |
| OS keyring token encryption | ✅ |
| Multi-account rotation | ✅ |
| Exponential cooldown backoff | ✅ |
| Auto token refresh | ✅ |
| Responses API translation | ✅ |
| SSE streaming | ✅ |
| Tool/function calling | ✅ |
| Reasoning/thinking isolation | ✅ |
| `/v1/models` endpoint | ✅ |
| Codex Desktop model picker | ✅ |
| Schema sanitization | ✅ |
| Device fingerprinting | ✅ |
| BYOK provider presets | ✅ |
| OpenAI-compatible provider routing | ✅ |
| Encrypted API-key provider config | ✅ |
| Env-enabled BYOK model exposure requires valid API-key values | ✅ |
| BYOK/Codex URL validation before config writes | ✅ |
| BYOK managed-header guardrails | ✅ |
| BYOK API-key/header value sanitization before config writes | ✅ |
| BYOK model-picker field sanitization before config writes | ✅ |
| BYOK config preflight before streaming | ✅ |
| Malformed function-tool metadata normalization before routing | ✅ |
| Malformed generation option and `tool_choice` rejection before routing | ✅ |
| BYOK structured-output `response_format` normalization before routing | ✅ |
| Malformed provider/backend usage-counter normalization | ✅ |
| Google `developer` messages preserved as system instructions | ✅ |
| Codex config helper with private atomic symlink-safe backups | ✅ |
| `/v1/responses` request-shape validation | ✅ |
| Central redaction for auth/provider errors | ✅ |
| Finite Retry-After / Google RetryInfo cooldown hints | ✅ |
| Fail-closed malformed-store write/update guardrails | ✅ |
| Malformed OAuth `expires_in` fallback handling | ✅ |

## Known Limitations
- Live Google Antigravity and BYOK provider smoke tests require configured credentials/API keys.
- `previous_response_id` is rejected by design in this stateless gateway; replay the full conversation, including tool calls and outputs, in `input`.
- `/v1/responses/compact` is not implemented.
- CI proves local unit/compile health only; it does not prove live backend availability.

## Last Documented Release
[v0.1.0-alpha](https://github.com/Reedtrullz/codex-antigravity-auth/releases/tag/v0.1.0-alpha)

## Next Priorities
1. Add `/v1/responses/compact` support
2. Expand live backend smoke coverage for BYOK providers
3. Add a credentialed smoke-test profile for Google and configured BYOK providers
