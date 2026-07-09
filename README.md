# Codex Antigravity Auth

[![PyPI](https://img.shields.io/pypi/v/codex-antigravity-auth.svg)](https://pypi.org/project/codex-antigravity-auth/)
[![CI](https://github.com/Reedtrullz/codex-antigravity-auth/actions/workflows/ci.yml/badge.svg)](https://github.com/Reedtrullz/codex-antigravity-auth/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/Reedtrullz/codex-antigravity-auth)](https://github.com/Reedtrullz/codex-antigravity-auth/releases)

Released local gateway for using Google Antigravity Claude Opus/Sonnet from OpenAI Codex CLI or Desktop. It exposes an OpenAI Responses-compatible `http://localhost:51122/v1` endpoint, handles Google OAuth/account rotation, and keeps optional Gemini and BYOK OpenAI-compatible providers available.

The default setup is intentionally conservative: it can install the Codex provider block and start the gateway, but it will not replace your active Codex model unless you explicitly pass `--activate`.

## Quick Start

```bash
uv tool install codex-antigravity-auth
codex-antigravity setup --write --accounts 1 --model claude-3.5-sonnet --install-skill --start
codex-antigravity setup --check --model claude-3.5-sonnet
```

To make Antigravity Claude the active Codex default, opt in explicitly:

```bash
codex-antigravity configure-codex --write --activate --model claude-3.5-sonnet
```

Use `$anti` only as an optional sidecar reviewer/planner after the gateway is working; Codex remains the acting agent.

## Features
- **Claude in Codex**: Advertises Antigravity Claude Sonnet/Opus through `/v1/models` so Codex can use the local gateway like a Responses API provider.
- **Safe setup by default**: `setup --write` and `configure-codex --write` install the provider block without changing top-level `model`/`model_provider`; `--activate` is required to switch Codex defaults.
- **Durable local service**: Supports foreground/background gateway runs plus per-user macOS LaunchAgent, Linux systemd user unit, and Windows Scheduled Task service lifecycle.
- **Diagnostics that explain the next step**: `setup --check`, `doctor --codex-ready`, `status`, `/health`, sanitized request logs, and `logs summary` help distinguish config, gateway, service, account, and provider-key issues.
- **OS-Native Keyring Encryption**: Encrypts Google account tokens and stored BYOK provider config at rest via macOS Keychain, Windows Credential Manager, Linux Secret Service, or a private local fallback key.
- **Transaction-Safe Cooldown Rotations**: Automatically rotates accounts on backend failures (such as `401`, `403`, or `429` rate limiters) with clean exponential backoff.
- **High-Fidelity SSE Translation**: Translates stream candidate envelopes, role alignments, reasoning-text deltas, function-call items, and VALIDATED tool parameter modes into Responses API events.
- **BYOK Provider Routing**: Route model IDs like `deepseek:deepseek-chat`, `xai:grok-code-fast-1`, `kimi:kimi-k2-0711-preview`, `openrouter:deepseek/deepseek-chat`, and custom OpenAI-compatible endpoints through encrypted API-key config.

## Installation

From PyPI:

```bash
uv tool install codex-antigravity-auth
```

From GitHub:

```bash
uv tool install "git+https://github.com/Reedtrullz/codex-antigravity-auth.git"
```

From a source checkout:

```bash
uv tool install .
```

For active development, keep it editable:

```bash
uv tool install --editable .
```

If you are already inside a project virtual environment, this also works:

```bash
uv pip install -e .
```

## Configuration

The recommended first-run path is the primary setup command. It validates OAuth credentials, prompts for missing Google OAuth desktop-client credentials on an interactive TTY, runs Google login, writes the Codex provider block only when `--write` is present, can install the optional `$anti` helper skill, starts the gateway in the background, waits for `/v1/models`, and ends with readiness diagnostics. It does not change your active default Codex model unless you add `--activate`:

```bash
codex-antigravity setup --write --accounts 1 --model claude-3.5-sonnet --install-skill --start
```

When `--base-url` is omitted, `setup` derives `http://localhost:<port>/v1` from `--port`; if both `--start --port` and `--base-url` are supplied, their ports must match so Codex is not configured for a different gateway than the one just started.

For a read-only preflight that does not mutate OAuth, Codex config, skills, or gateway state:

```bash
codex-antigravity setup --check
codex-antigravity setup --json
codex-antigravity setup --check --live
```

`setup --check --live` adds an explicit non-streaming `/v1/responses` smoke using the selected Google Antigravity model. Use `--live-model` and `--live-timeout` to override the model or timeout. It is still read-only, but it does spend one real provider request.

After setup, inspect native Codex readiness and gateway lifecycle with:

```bash
codex-antigravity status
codex-antigravity doctor --codex-ready
codex-antigravity doctor --codex-ready --live
codex-antigravity doctor --codex-ready --json
codex-antigravity stop
```

For reboot persistence, install the per-user gateway service after a successful setup:

```bash
codex-antigravity service install --port 51122 --host 127.0.0.1
codex-antigravity service status
codex-antigravity service uninstall
```

On macOS this writes a user LaunchAgent under `~/Library/LaunchAgents`; on Linux it writes a systemd user unit under `~/.config/systemd/user`; on Windows it creates a per-user Scheduled Task. The regular `start --background` command remains the lightweight non-persistent option.

If your Codex provider block drifts, repair only the Codex config without OAuth login, skill install, or gateway mutation:

```bash
codex-antigravity setup --repair --config ~/.codex/config.toml --model claude-3.5-sonnet
```

Install or refresh the Codex provider block without changing your active default model:

```bash
codex-antigravity configure-codex --write
```

The command validates the Codex model id, provider id, provider name, and gateway base URL before writing. It updates `~/.codex/config.toml` through a private atomic write, follows an existing symlink to update the real config target, and writes a timestamped private backup first when it changes an existing config. By default it writes only `[model_providers.antigravity]`; it does not change top-level `model` or `model_provider`. Add `--activate` only when you explicitly want Antigravity to become the active Codex default.

To inspect the TOML without writing it:

```bash
codex-antigravity configure-codex
```

Install the optional Codex `$anti` helper skill that ships with this repo:

```bash
codex-antigravity install-skill
```

This copies the bundled skill into `~/.codex/skills/anti` for optional review/planning workflows after Claude is already available in Codex. It can use Antigravity Opus/Sonnet as a helper reviewer, consult lane, deep work planner, and bounded multi-model panel from chat prompts like `$anti review this diff with opus`. If you already have a local `anti` skill, the command refuses to overwrite it unless you pass `--force`; forced installs back up the existing skill under a sibling `skills-backups` directory so backups are not indexed as live skills.

To verify the V2 helper workflow surface without changing Codex config, run:

```bash
codex-antigravity setup-v2
codex-antigravity install-skill --verify
# If setup-v2 warns that an existing anti skill is stale or locally modified:
codex-antigravity install-skill --force --verify
```

`setup-v2 --write` installs or refreshes the bundled skill, but it does not write `~/.codex/config.toml`; use primary `setup --write`, `setup-google`, or `configure-codex --write` when you explicitly want the gateway provider block installed. Add `--activate` to primary `setup --write` or `configure-codex --write` only when you explicitly want Codex itself pointed at the gateway as the active default.
If an existing `anti` skill differs from the bundled copy, pass `--force` with `install-skill` or `setup-v2 --write` to back it up and replace it before verifying. BYOK provider identity/key checks are skipped by default; add `--check-byok` when you want setup-v2 to inspect provider readiness and confirm configured provider models are advertised by the running gateway.

The skill also ships a helper-level panel mode inspired by MoA/Fusion workflows. Codex remains the acting agent; the helper fans out to gateway-advertised models, asks a judge model to synthesize disagreements, structured findings, blind spots, and next actions, then returns advisory output for Codex to verify:

```bash
python3 ~/.codex/skills/anti/scripts/anti.py panel --mode review --scope staged
python3 ~/.codex/skills/anti/scripts/anti.py panel --mode plan --scope working-tree --prompt "Plan this PR"
python3 ~/.codex/skills/anti/scripts/anti.py panel --mode ask --model sonnet --model openrouter:deepseek/deepseek-chat --judge opus --prompt "Compare these approaches"
python3 ~/.codex/skills/anti/scripts/anti.py panel --mode ask --collab claude-grok --prompt "Compare these approaches"
python3 ~/.codex/skills/anti/scripts/anti.py panel --mode review --scope staged --output findings
```

Panel consensus is not proof and should not patch code directly. Structured findings include `id`, `claim`, `severity`, `lanes`, and `verify`; run the `verify` hint locally before acting. Text and JSON outputs include per-lane/judge usage and latency when the gateway/provider returns it. Broad review panels summarize oversized scopes once before fan-out instead of silently truncating raw context for every lane.

`--collab claude-grok` is an explicit collaboration profile for Claude plus Grok panels. It defaults to Sonnet, Opus, and `xai-oauth:grok-build-0.1`, gives Claude and Grok complementary review instructions, and asks the Opus judge to compare Claude-backed and Grok-backed disagreements before recommending verification steps. It still uses normal gateway-advertised models; if the Grok lane is not visible in `/v1/models`, it is recorded as a failed lane unless you require it with `--min-successes 3`.

BYOK panel models such as `openrouter:...` or `xai-oauth:...` only work when the running gateway advertises them in `/v1/models`, which requires usable provider keys, key-optional local provider setup, or a refreshable OAuth login. When a BYOK lane receives repository, diff, or file context, the helper prints and records a disclosure naming the provider lanes. Virtual picker models such as `panel:*`, `moa:*`, or `fusion:*` are not supported; MoA/Fusion remains a helper workflow, not gateway-side orchestration.

V2 named workflow presets wrap the same advisory engine for common Codex work:

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

Workflow runs save sanitized summaries under `~/.codex/anti-runs` by default; primitive `consult`, `plan`, `review`, and `panel` commands default to not writing a ledger unless `--save-output summary` or `--save-output full` is passed. Saved runs include the Anti run id, which is also sent as `metadata.run_id` to the gateway and appears in sanitized request logs for correlation. Raw prompts are not logged unless you explicitly choose full-output ledgers. `plan` and `review` use a conservative Claude safety budget with `--chunked auto`, so broad Opus/Sonnet work is split into bounded chunk calls before synthesis instead of being sent as one giant request; use `--chunked off` to intentionally bypass that safety budget, including when `--max-prompt-chars 0` is set. `--fallback-model sonnet --fallback-policy on-retryable` and `--progress` are available for long-running model calls that may otherwise fail silently or hit transient backend rotation errors.

Before running `codex-antigravity login`, create a Google OAuth desktop client. The local callback listener uses:

```text
http://localhost:51121/oauth-callback
```

Then either export the client credentials:

```bash
export ANTIGRAVITY_CLIENT_ID="your-client-id.apps.googleusercontent.com"
export ANTIGRAVITY_CLIENT_SECRET="your-client-secret"
```

Or let `codex-antigravity setup --write` prompt for them when it is run from an interactive terminal. Automation can pass `--no-input` to fail fast instead of prompting. The setup prompt writes `~/.codex/antigravity-credentials.json` through a private atomic write and rejects symlinked credential paths.

You can also write the file yourself. This client-credential file is plaintext but permission-repaired to `0600`; account tokens are stored separately in encrypted storage.

```json
{
  "client_id": "your-client-id.apps.googleusercontent.com",
  "client_secret": "your-client-secret"
}
```

Then run the interactive login:
```bash
codex-antigravity login
```

For first-run native Claude setup, prefer `setup --write`. For the older Google-only flow, or when you want several Google accounts in the rotation pool without installing the skill or starting the gateway, use:

```bash
codex-antigravity setup-google --accounts 2
```

`setup-google` first verifies that Google OAuth client credentials are configured, then runs the browser OAuth flow before writing Codex config. That keeps Codex config untouched if login cannot start or complete. It forces Google's account chooser for multi-account setup, stores each account in the encrypted rotation pool, clears stale cooldown state for re-authenticated accounts, writes the Codex provider block after successful login unless `--skip-codex-config` is passed, and runs the active-provider `doctor` only when `--activate` is also passed. You can also add more accounts later:

```bash
codex-antigravity login --count 2
codex-antigravity accounts
codex-antigravity accounts reset you@example.com
codex-antigravity accounts remove old@example.com --yes
```

`accounts reset <email>` clears persisted cooldown/failure state without touching saved tokens or usage counters; use `accounts reset --all --yes` for the whole rotation pool. `accounts remove <email> --yes` removes a revoked or dead account from the encrypted store and repairs active rotation indexes.

Start the local gateway:

```bash
codex-antigravity start
codex-antigravity start --background
```

Background mode writes pid/log files under `~/.codex/`. The log file is append-only and created with private permissions; remove or rotate it manually if it grows too large.

Request diagnostics are written to a sanitized capped JSONL file under `~/.codex/antigravity-requests.jsonl`. The log records request ids, model/route metadata, latency, status, retry/rotation hints, HTTP status, usage totals when available, and redacted error classes/messages. It never stores prompts, request bodies, OAuth material, provider keys, or account emails.

For Google Antigravity routes, concurrent Codex requests are spread across available accounts by process-local in-flight counts while sequential traffic keeps the sticky active account. Cooled-down accounts remain excluded from selection, and in-flight counters are released when non-streaming responses finish or streaming clients disconnect.

```bash
codex-antigravity logs --tail 20
codex-antigravity logs summary --since 24h
codex-antigravity logs --follow
codex-antigravity logs clean
curl http://127.0.0.1:51122/health
```

`logs summary` aggregates the sanitized request log by route/family with request counts, success rate, p50/p95 latency, 429 counts, rotation attempts, and top error classes. It never reads prompts, request bodies, OAuth material, provider keys, account emails, or encrypted stores.

The loopback-only `/health` endpoint reports process health, native model count, BYOK route visibility, anonymous account cooldown summaries, and the request-log path.

### Model catalog overlays

Built-in Claude/Gemini model definitions remain authoritative, but you can add local model-picker entries in `~/.codex/antigravity-models.toml`:

```bash
codex-antigravity models list
codex-antigravity models add claude-experimental \
  --backend-id claude-experimental-backend \
  --display-name "Claude Experimental" \
  --family claude \
  --context-window 200000 \
  --default-reasoning-level high \
  --alias claude-exp
codex-antigravity models doctor
codex-antigravity models remove claude-experimental
```

Overlay ids must be simple printable model ids, cannot shadow built-in ids, backend ids, or aliases unless `--force` is explicit, and only appear in Codex's picker when the gateway advertises them through `/v1/models`. Runtime catalog loads fail soft to built-ins if the local overlay TOML is malformed; `models list` and `models doctor` stay strict so you can find and repair the overlay. Unknown direct Google model ids can still pass through, but picker visibility requires a built-in or overlay definition.

### BYOK providers

Built-in presets are available for OpenRouter, DeepSeek, xAI, Kimi/Moonshot, Ollama, OpenCode-compatible local servers, and custom OpenAI-compatible APIs:

```bash
codex-antigravity provider presets
codex-antigravity provider set deepseek --api-key-env DEEPSEEK_API_KEY --model deepseek-chat --model deepseek-reasoner
codex-antigravity provider set openrouter --api-key-env OPENROUTER_API_KEY --model deepseek/deepseek-chat
codex-antigravity provider set xai --api-key-env XAI_API_KEY --model grok-build-0.1
codex-antigravity provider login xai-oauth
codex-antigravity provider set kimi --api-key-env KIMI_API_KEY --model kimi-k2-0711-preview
codex-antigravity provider set ollama --base-url http://localhost:11434/v1 --model gpt-oss:20b
codex-antigravity provider list
```

Provider presets show their supported auth modes. For xAI/Grok there are two lanes: `xai:*` uses normal xAI console API-key billing through `XAI_API_KEY`, while `xai-oauth:*` uses a SuperGrok/X Premium OAuth login and stores refreshable tokens encrypted in `~/.codex/antigravity-xai-oauth.json`. Use `codex-antigravity provider login xai-oauth` for the browser callback flow, or `codex-antigravity provider login xai-oauth --device` for a headless/device-code flow. `codex-antigravity provider set xai --auth-mode oauth ...` fails with a pointer to `xai-oauth` so the API-key and subscription-backed routes do not get mixed.

For BYOK-only use, point Codex at a BYOK model when writing the provider block:

```bash
codex-antigravity configure-codex --write --model deepseek:deepseek-chat
# Add --activate only if you want DeepSeek to become the active Codex default.
codex-antigravity doctor --byok-only
```

`--api-key-env` avoids persisting provider keys and reads them from the gateway process environment. If you intentionally want a provider key stored locally, pass `--api-key`; stored provider keys are encrypted in `~/.codex/antigravity-providers.json`. Use `--auth-mode api-key` explicitly only when you want an API-key provider config to record that mode. Built-in provider env vars include `OPENROUTER_API_KEY`, `DEEPSEEK_API_KEY`, `XAI_API_KEY`, `KIMI_API_KEY`, `MOONSHOT_API_KEY`, `OLLAMA_API_KEY`, and `OPENCODE_API_KEY`.
The `/v1/models` catalog only advertises BYOK models when the provider has a usable stored/env key, a refreshable OAuth login, or explicitly supports key-optional loopback/local use. The generic `custom` preset is not auto-enabled; run `codex-antigravity provider set custom --base-url ... --model ...` before routing `custom:model`.
When `provider set` is given `--api-key-env`, models are configured but remain hidden from `/v1/models` until that environment variable is available to the running gateway.
Provider ids reserve model-name separators and may only contain letters, numbers, underscores, and hyphens; model ids themselves may still contain `/` or `:`, but not whitespace or control characters. Unknown `provider:model` prefixes are rejected as BYOK routing errors before any Google account selection.
Custom provider and Codex gateway base URLs must be absolute `http` or `https` URLs without embedded credentials, whitespace/control characters, query strings, fragments, invalid ports, or malformed bracketed hosts. Plain `http` base URLs are accepted only for loopback/local hosts; remote BYOK providers and remote gateway URLs must use `https`. Non-preset custom BYOK providers must provide a base URL before models are exposed. Stored/env BYOK API keys and extra BYOK provider header values must be printable ASCII without control characters; model-picker display names must not contain control characters. Provider API-key env var names must contain only letters, numbers, and underscores and must not start with a number. Extra headers may not override gateway-managed auth, content, host, or transport headers. Invalid BYOK provider URLs, API keys, env vars, model ids, display names, and headers are rejected before config writes; invalid BYOK provider URLs, timeouts, headers, API keys, and missing API keys are also rejected before streaming starts so Codex gets a normal HTTP error instead of a partial SSE response. Key-optional BYOK providers are only treated as keyless on loopback/local base URLs; remote custom or cloud URLs need a usable stored or env API key before they appear in Codex's picker or route requests. Function/tool names and forced `tool_choice` function names must contain only letters, numbers, underscores, and hyphens, and be 1-64 characters; malformed names are rejected or dropped before routing. Non-streaming Google failure responses use a structured `detail` object with `message` and sanitized `diagnostics`; clients should handle both this shape and older string details. BYOK streams surface provider error frames as failed Responses API streams instead of successful empty completions, ignore never-named tool-call deltas, and wait for complete valid streamed function names instead of emitting empty, partial, or malformed function names. BYOK structured tool outputs are serialized to JSON text before being sent as Chat Completions tool messages.

For 1Password-backed BYOK keys, keep `provider set --api-key-env` and let 1Password populate that env var for the gateway process. With today's stable 1Password CLI, create a local env file containing secret references such as:

```dotenv
OPENROUTER_API_KEY=op://Private/OpenRouter/sk
DEEPSEEK_API_KEY=op://Private/DeepSeek/api_key
```

Then start the gateway through `op run` without revealing the key:

```bash
chmod 600 ~/.codex/antigravity.env
codex-antigravity start --background --op-env-file ~/.codex/antigravity.env
codex-antigravity service install --port 51122 --host 127.0.0.1 --op-env-file ~/.codex/antigravity.env
```

1Password Environments beta can be used the same way once your local `op` supports `op run --environment`:

```bash
codex-antigravity start --background --op-environment <environment-id>
codex-antigravity service install --port 51122 --host 127.0.0.1 --op-environment <environment-id>
```

The 1Password CLI must be installed on `PATH` when these options are used. `start --background` and `service install` resolve `op` to an absolute path and fail before writing service manifests or starting a gateway if it is missing. Durable services still depend on your local 1Password unlock/session behavior after reboot; verify with `codex-antigravity service status` and `codex-antigravity doctor --codex-ready --live`.

The `configure-codex --write` helper writes this provider block into `~/.codex/config.toml` after validation:

```toml
[model_providers.antigravity]
name = "Google Antigravity"
base_url = "http://localhost:51122/v1"
wire_api = "responses"
```

If you deliberately pass `--activate`, it also writes the active default keys:

```toml
model = "claude-3.5-sonnet"
model_provider = "antigravity"
wire_api = "responses"
```

## Verification

To run connection check diagnostics and verify token security:
```bash
codex-antigravity doctor
codex-antigravity doctor --codex-ready
codex-antigravity doctor --live --live-model claude-3.5-sonnet
codex-antigravity doctor --byok-only
```

`doctor` parses the active Codex config, verifies `model_provider = "antigravity"` and the matching provider `base_url` when Antigravity is active, warns when PyPI has a newer package version, and exits non-zero on hard readiness failures. `doctor --codex-ready` additionally checks that the gateway is reachable, `/v1/models` advertises the selected Codex model, the model routes to Google or BYOK correctly, and the selected Google family has usable rotation state. Add `--live` when you want a real Google Antigravity `/v1/responses` generation smoke; set `CODEX_ANTIGRAVITY_NO_UPDATE_CHECK=1` to skip the once-daily package-version check. Use `--config /path/to/config.toml` to verify a non-default Codex config.

Before tagging a release, run the local verification stack and one credentialed live smoke:

```bash
python3 -m pytest -q
python3 -m compileall -q codex_antigravity_auth tests
git diff --check
codex-antigravity models doctor
codex-antigravity doctor --codex-ready --live --live-model claude-3.5-sonnet
```

The gateway binds to `127.0.0.1` by default. Binding to a non-loopback host requires both `--allow-remote` and an `ANTIGRAVITY_GATEWAY_TOKEN` of at least 32 visible ASCII characters; remote callers must send `Authorization: Bearer <token>`. The built-in server still speaks plain HTTP, so use remote mode only behind a trusted tunnel, local network boundary, or TLS-terminating proxy.

And execute full unit test coverage:
```bash
python3 -m pytest
```

## Release Automation

Tagged releases are prepared for PyPI Trusted Publishing. The `.github/workflows/publish.yml` workflow runs on `v*` tags, builds sdist/wheel artifacts, checks them with Twine, uploads the artifacts between jobs, and publishes with `pypa/gh-action-pypi-publish@release/v1` using OIDC (`id-token: write`) in the `pypi` environment.

Before the first PyPI publish, configure the PyPI project `codex-antigravity-auth` with a trusted publisher for this GitHub repository, workflow file `.github/workflows/publish.yml`, and environment `pypi`. No local PyPI API token is required or expected.
