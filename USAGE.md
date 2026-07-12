# Google Antigravity Auth for OpenAI Codex Usage Guide

This guide describes real-world examples, advanced configurations, and diagnostics routines to run Google Antigravity models inside OpenAI Codex efficiently.

## 0. Quick Codex Setup

Install the command from PyPI, then run the primary Claude-in-Codex setup:

```bash
uv tool install codex-antigravity-auth
codex-antigravity setup --write --accounts 1 --model claude-3.5-sonnet --install-skill --start
codex-antigravity setup --check --model claude-3.5-sonnet
```

For a read-only check that does not mutate OAuth state, Codex config, installed skills, or gateway processes:

```bash
codex-antigravity setup --check
codex-antigravity setup --check --live
codex-antigravity setup --json
codex-antigravity status --json
```

The setup command validates the selected model/provider/base URL, preflights Google OAuth or BYOK provider readiness before any config write, prompts for missing Google OAuth desktop-client credentials on an interactive TTY, runs login when needed, writes the Codex provider block, optionally installs the `$anti` helper, optionally starts the gateway in the background, waits for `/v1/models`, and ends with readiness diagnostics. By default, `setup --write` does **not** change top-level `model` or `model_provider`; add `--activate` only when you explicitly want the gateway model to become the active Codex default. When `--base-url` is omitted, setup derives `http://localhost:<port>/v1` from `--port`; if both are supplied with `--start`, their ports must match. Add `--no-input` for automation that should fail instead of prompting, and add `--live` when a read-only setup check should spend one real Google Antigravity `/v1/responses` provider request.

`configure-codex` validates the Codex model id, provider id, provider name, and gateway base URL before writing. `--write` uses private atomic writes, preserves a symlinked Codex config path by updating its real target, and creates a private timestamped backup before changing an existing Codex config. By default it writes only `[model_providers.<provider>]`; add `--activate` to also write top-level `model` and `model_provider`.

Use `setup --repair` when Codex config has drifted and you only want to reconcile the provider block and selected model. It does not run OAuth, install the `$anti` skill, or start/stop the gateway:

```bash
codex-antigravity setup --repair --config ~/.codex/config.toml --model claude-3.5-sonnet
```

For durable startup after reboot, install the gateway as a per-user service:

```bash
codex-antigravity service install --port 51122 --host 127.0.0.1
codex-antigravity service status --json
codex-antigravity service uninstall --port 51122
```

The service command writes a macOS LaunchAgent, Linux systemd user unit, or Windows Scheduled Task depending on the platform. `doctor --codex-ready` and `status --json` report both the lightweight pid-file process state and the durable service state.

Gateway request diagnostics are local and sanitized:

```bash
codex-antigravity logs --tail 50
codex-antigravity logs summary --since 24h
codex-antigravity logs --follow
codex-antigravity logs clean
curl http://127.0.0.1:51122/health
```

The request JSONL log is capped and rotated at `10 MiB`. It records request ids, Anti run correlation, model route/provider/family, stream mode, terminal reason, attempt/rotation counts, cooldown scope/category, cancellation, latency, HTTP status, usage totals, and redacted errors. It does not store raw prompts, request bodies, provider keys, OAuth tokens, account emails, or encrypted stores. `logs summary` aggregates those sanitized records by route/family with terminal, attempt, rotation, cancellation, usage, success-rate, latency, 429, and error-class metrics.

`codex-antigravity doctor --codex-ready --json` includes read-only account/provider store format and migration status, account-state schema version, observed service state, and provider capability mismatches under `diagnostics`. These checks do not migrate stores or rewrite config. See `docs/refactor-migration.md` before upgrading or rolling back a store used by an older package.

Google account selection is sticky for sequential requests but load-aware for concurrent ones: request handlers acquire an account, prefer the lowest process-local in-flight count among non-cooling accounts, and release it when non-streaming responses finish or streaming responses end/disconnect.

To expose a local model definition in Codex's model picker, add an overlay entry:

```bash
codex-antigravity models list
codex-antigravity models add claude-experimental \
  --backend-id claude-experimental-backend \
  --display-name "Claude Experimental" \
  --family claude \
  --context-window 200000 \
  --alias claude-exp
codex-antigravity models doctor
```

Overlays are stored in `~/.codex/antigravity-models.toml`. Built-ins are still the source of truth; overlay ids, backend ids, and aliases cannot collide with built-ins unless `--force` is passed. Runtime requests and `/v1/models` fall back to built-ins if the overlay file is malformed; use strict `models list` or `models doctor` to repair it.

`install-skill` installs the bundled Codex `$anti` helper skill into `~/.codex/skills/anti`. Use it after native Claude is working in Codex when you want chat prompts such as `$anti review this diff with opus`, `$anti plan --scope staged`, `$anti panel --mode review --scope staged`, or `$anti smoke` to route through the repo-shipped helper. Existing local `anti` skills are left untouched unless `--force` is passed, and forced installs create a timestamped backup under a sibling `skills-backups` directory so backups do not show up as extra personal skills.

For a no-config-mutation V2 readiness check, run:

```bash
codex-antigravity setup-v2
codex-antigravity install-skill --verify
# If setup-v2 warns that the installed anti skill differs from this package:
codex-antigravity install-skill --force --verify
```

Use `setup-v2 --write` only when you want it to install or refresh the bundled skill. It does not write `~/.codex/config.toml`; use primary `setup --write`, `setup-google`, or `configure-codex --write` to install the provider block. Add `--activate` to those config-writing commands only when you explicitly want to switch the active Codex default. When checking a remote gateway, export the bearer token and pass `--gateway-token-env` (defaults to `ANTIGRAVITY_GATEWAY_TOKEN`) so the `/v1/models` probe can authenticate.
If the existing `anti` skill is locally modified or stale, add `--force` before verification to back it up under `skills-backups` and replace it. BYOK provider checks are opt-in; add `--check-byok` to inspect provider readiness and compare configured provider models with the gateway's `/v1/models` catalog.

For bounded multi-model advice, use the helper-level panel mode. It does not replace Codex's native acting model loop; it returns advisory synthesis and verifiable findings for Codex to check locally:

```bash
python3 ~/.codex/skills/anti/scripts/anti.py panel --mode review --scope staged
python3 ~/.codex/skills/anti/scripts/anti.py panel --mode review --scope diff --base origin/main --role correctness --role security --role tests
python3 ~/.codex/skills/anti/scripts/anti.py panel --mode plan --scope working-tree --prompt "Plan this PR"
python3 ~/.codex/skills/anti/scripts/anti.py panel --mode ask --model sonnet --model openrouter:deepseek/deepseek-chat --judge opus --prompt "Compare these approaches"
python3 ~/.codex/skills/anti/scripts/anti.py panel --mode ask --collab claude-grok --prompt "Compare these approaches"
python3 ~/.codex/skills/anti/scripts/anti.py panel --mode review --scope staged --output findings
```

Panel mode validates requested judge/fallback models against `/v1/models` before generation and records missing panel lanes as failed metadata when `--min-successes` can still be met. BYOK models only appear there when the gateway process has usable provider credentials or a key-optional local provider setup. Treat panel consensus as a prioritization hint, not proof; verify actionable findings locally before editing.

Use `--collab claude-grok` when you explicitly want a Claude/Grok cross-check. It defaults to Sonnet, Opus, and `xai-oauth:grok-build-0.1`, asks the lanes to lean into complementary strengths, and asks the judge to compare Claude-backed and Grok-backed disagreements. This is not automatic model-loop blending; it is a bounded advisory panel. If Grok is not visible in `/v1/models`, the lane is recorded as failed unless you set `--min-successes 3` to require it.

The panel judge returns a structured findings contract with `id`, `claim`, `severity`, `lanes`, and `verify`. Default prose output renders disagreements first, then findings, unverifiable observations, and caveats. `--output findings` emits just the sanitized findings JSON, while `--json` includes panel results, usage/latency metadata, caveats, findings, and the rendered output. Broad `panel --mode review` scopes reuse the review chunking path to create one bounded summary before fan-out rather than silently truncating full context for every lane.

If a BYOK `provider:model` lane, including `xai-oauth:...`, receives repository, diff, or file context, the helper prints and records a BYOK disclosure naming the provider lanes. Virtual picker models such as `panel:*`, `moa:*`, or `fusion:*` are not supported; MoA/Fusion is a helper workflow, not gateway-side fan-out or server-side judging.

V2 workflow presets provide safer defaults for recurring work:

```bash
python3 ~/.codex/skills/anti/scripts/anti.py workflow review-ready --scope staged
python3 ~/.codex/skills/anti/scripts/anti.py workflow plan-deep --scope working-tree --prompt "Plan this PR" --progress
python3 ~/.codex/skills/anti/scripts/anti.py workflow ship-gate --scope diff --base origin/main --json
python3 ~/.codex/skills/anti/scripts/anti.py workflow provider-compare --model sonnet --model openrouter:deepseek/deepseek-chat --prompt "Compare these approaches"
python3 ~/.codex/skills/anti/scripts/anti.py workflow security-review --scope staged --output findings
python3 ~/.codex/skills/anti/scripts/anti.py workflow debug-consensus --prompt "Intermittent 502s after rotation"
python3 ~/.codex/skills/anti/scripts/anti.py workflow claude-grok --panel-mode review --scope staged --output findings
python3 ~/.codex/skills/anti/scripts/anti.py workflow claude-grok --panel-mode ask --prompt "Should this UX use route A or B?"
python3 ~/.codex/skills/anti/scripts/anti.py runs list
```

Workflow presets save sanitized summaries under `~/.codex/anti-runs` by default. Primitive commands default to `--save-output never`; opt into `summary` or redacted `full` records when useful. Saved runs include a run id; Anti sends it to the gateway as `metadata.run_id`, and the sanitized request JSONL log records it for correlation without forwarding it to Google or BYOK providers. With `--chunked auto`, Opus/Sonnet plan and review calls use a conservative Claude safety budget and split broad context into bounded chunk calls before synthesis; use `--chunked off` only when you intentionally want one large request, including when `--max-prompt-chars 0` would otherwise mean unlimited. Use `--fallback-model sonnet --fallback-policy on-retryable` for long Opus calls that should degrade after retryable backend failures, and `--progress` to print model/chunk progress to stderr.

For the older Google-only OAuth setup, use:

```bash
codex-antigravity setup-google --accounts 2
codex-antigravity start
```

This first verifies that Google OAuth client credentials are configured, then runs the browser OAuth login before writing Codex config so a login startup failure does not leave Codex pointed at an unusable gateway setup. It forces Google's account chooser when adding multiple accounts, stores every successful login in the encrypted rotation pool, clears stale cooldown state on re-authentication, prints the active Gemini/Claude rotation status, writes the Codex provider block, and runs the active-provider doctor only when `--activate` is also passed. To add more accounts later, run `codex-antigravity login --count 2`; to inspect rotation state, run `codex-antigravity accounts`. Use `codex-antigravity accounts reset <email>` to clear persisted cooldown/failure state, `accounts reset --all --yes` for the whole pool, and `accounts remove <email> --yes` to remove a revoked account without hand-editing encrypted storage.

For BYOK-only use, replace `codex-antigravity login` with a provider setup command such as:

```bash
codex-antigravity provider set deepseek --api-key-env DEEPSEEK_API_KEY --model deepseek-chat
codex-antigravity configure-codex --write --model deepseek:deepseek-chat
# Add --activate only if you want DeepSeek to become the active Codex default.
codex-antigravity doctor --byok-only
```

For SuperGrok/X Premium xAI access without an API key, use the dedicated OAuth lane:

```bash
codex-antigravity provider login xai-oauth
# or, for headless/remote use:
codex-antigravity provider login xai-oauth --device
codex-antigravity setup --write --model xai-oauth:grok-build-0.1 --start
```

BYOK provider ids may contain only letters, numbers, underscores, and hyphens. Provider model ids may contain `/` or `:`, but not whitespace or control characters. Unknown `provider:model` prefixes are rejected as BYOK routing errors before any Google account selection. Non-preset custom BYOK providers must provide a base URL, and the generic `custom` preset is not auto-enabled until `provider set custom ...` is run. `--api-key-env` is preferred because it avoids persisting provider keys; `--api-key` stores a key in encrypted provider config. `xai:*` uses the normal xAI API-key route with `XAI_API_KEY`; `xai-oauth:*` uses encrypted SuperGrok OAuth tokens in `~/.codex/antigravity-xai-oauth.json`. `provider set xai --auth-mode oauth` fails with a pointer to `xai-oauth` so the two routes stay distinct. Stored/env BYOK API keys and extra provider header values must be printable ASCII without control characters; OAuth tokens are never written to request logs. Model-picker display names must not contain control characters. Provider API-key env var names must contain only letters, numbers, and underscores and must not start with a number. Custom provider and Codex gateway base URLs must be absolute `http` or `https` URLs without embedded credentials, whitespace/control characters, query strings, fragments, invalid ports, or malformed bracketed hosts. Plain `http` base URLs are accepted only for loopback/local hosts; remote providers and remote gateway URLs must use `https`. Extra BYOK provider headers may not override gateway-managed auth, content, host, or transport headers; malformed provider config is rejected before it is written and before streaming begins. Key-optional BYOK providers are only keyless on loopback/local base URLs; remote custom or cloud endpoints need a stored/env API key or a refreshable OAuth login. BYOK streams surface provider error frames as failed Responses API streams, ignore never-named tool-call deltas, and wait for complete streamed function names before emitting function-call items.
Models configured with `--api-key-env` remain hidden from `/v1/models` until the env var exists in the gateway process environment. `doctor --byok-only` fails when configured BYOK providers have missing or malformed keys, and `doctor --config /path/to/config.toml` can verify non-default Codex config files.

For 1Password-backed BYOK keys, store secret references in a local env file and let the gateway process run under `op run`:

```dotenv
OPENROUTER_API_KEY=op://Private/OpenRouter/sk
```

```bash
codex-antigravity provider set openrouter --api-key-env OPENROUTER_API_KEY --model openrouter/free
codex-antigravity start --background --op-env-file ~/.codex/antigravity.env
codex-antigravity service install --port 51122 --host 127.0.0.1 --op-env-file ~/.codex/antigravity.env
```

Set the env file to private permissions (`chmod 600 ~/.codex/antigravity.env`). The 1Password CLI must be installed on `PATH`; gateway start and service install resolve it to an absolute path and fail before starting the process or writing a manifest if `op` is missing. Durable services still depend on your local 1Password unlock/session behavior after reboot.

If your 1Password CLI includes the Environments beta commands, use `--op-environment <environment-id>` instead of `--op-env-file`.
The gateway binds to loopback by default. Non-loopback binds require `--allow-remote` plus `ANTIGRAVITY_GATEWAY_TOKEN` set to at least 32 visible ASCII characters; remote clients must send it as a bearer token. The built-in server is still plain HTTP, so remote use should go through a trusted tunnel, local network boundary, or TLS-terminating proxy.

Use live diagnostics sparingly when proving a final install:

```bash
codex-antigravity doctor --codex-ready --live --live-model claude-3.5-sonnet
```

`doctor --live` currently supports Google Antigravity models only. It also performs a once-daily cached package-version check against PyPI and warns when an upgrade is available. Set `CODEX_ANTIGRAVITY_NO_UPDATE_CHECK=1` to disable that external metadata lookup.

## 1. Supported Models & Aliases
You can use standard, developer-friendly names in your `~/.codex/config.toml` that the gateway automatically translates to the official Google Antigravity backend model definitions:

| OpenAI Codex Model ID | Antigravity Backend Model |
| --- | --- |
| `gemini-3.5-flash-high` | `gemini-3-flash-agent` (DeepMind Agentic Flash) |
| `gemini-3.5-flash-medium` | `gemini-3.5-flash-low` (General Purpose Flash) |
| `gemini-3.1-pro-high` | `gemini-3.1-pro-high` (Advanced Reasoning Pro) |
| `claude-3.5-sonnet` | `claude-sonnet-4-6` (High-Fidelity Anthropic Sonnet) |
| `claude-opus-4-6` | `claude-opus-4-6-thinking` (Deep Anthropic Opus Reasoning) |

Claude-first setup aliases are accepted anywhere the CLI accepts a Codex model id:

| Alias | Canonical Codex Model ID |
| --- | --- |
| `sonnet` | `claude-3.5-sonnet` |
| `claude-sonnet` | `claude-3.5-sonnet` |
| `opus` | `claude-opus-4-6` |
| `claude-opus` | `claude-opus-4-6` |

`codex-antigravity models doctor` also prints the Claude thinking-budget mapping for `low`, `medium`, `high`, and `xhigh` so advertised reasoning metadata can be compared with runtime request transforms.

---

## 2. Advanced Multi-Account Rotation & Rate-Limiting
When multiple Google accounts are registered, the gateway automatically rotates through them:
- **Rate-Limiting Cooldowns**: If a request returns `429 RESOURCE_EXHAUSTED` (such as Anthropic/Claude limiters), the account is marked on an account-level cooldown backoff strategy with exponential delay. Cooldowns persist across restarts so the gateway does not immediately retry a recently limited account.
- **Sticky Active Selection**: The `AccountManager` keeps independent active-account slots for Gemini and Claude families to preserve conversational continuity before rotating on connection timeouts/failures.
- **Claude Diagnostics**: Google request failures include sanitized family-level diagnostics such as selected family, cooldown count, retry-after source, rotation attempt status, and whether all Claude accounts are cooling down. Non-streaming Google failure responses use a structured `detail` object with `message` and `diagnostics`; clients should handle both this shape and older string details. Account identifiers are reserved for authenticated account-list commands.

---

## 3. High-Fidelity Streaming & Reasoning
The local server natively isolates explicit thinking blocks and stream envelopes, ensuring standard formatting:
- **Thinking/Reasoning block**: Emits `response.reasoning_text.delta` for explicit backend thinking parts while preserving regular `thoughtSignature` text as visible output.
- **SSE Stream**: Formats candidates, function calls, usage metadata, and completion events into Responses API SSE chunks parsed correctly by both Codex CLI and Codex Desktop.
