# Codex Antigravity Auth

Create a clean, reliable local gateway server that makes Google Antigravity Claude Opus/Sonnet feel native in OpenAI Codex (CLI or Desktop), with Gemini and BYOK OpenAI-compatible providers still available.

## Features
- **OS-Native Keyring Encryption**: Encrypts Google account tokens and stored BYOK provider config at rest via macOS Keychain, Windows Credential Manager, Linux Secret Service, or a private local fallback key.
- **Transaction-Safe Cooldown Rotations**: Automatically rotates accounts on backend failures (such as `401`, `403`, or `429` rate limiters) with clean exponential backoff.
- **High-Fidelity SSE Translation**: Translates stream candidate envelopes, role alignments, reasoning-text deltas, function-call items, and VALIDATED tool parameter modes into Responses API events.
- **BYOK Provider Routing**: Route model IDs like `deepseek:deepseek-chat`, `xai:grok-code-fast-1`, `kimi:kimi-k2-0711-preview`, `openrouter:deepseek/deepseek-chat`, and custom OpenAI-compatible endpoints through encrypted API-key config.

## Installation

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

The recommended first-run path is the primary setup command. It validates OAuth credentials, runs Google login, writes Codex config only when `--write` is present, can install the optional `$anti` helper skill, starts the gateway in the background, checks `/v1/models`, and ends with Codex readiness diagnostics:

```bash
codex-antigravity setup --write --accounts 1 --model claude-3.5-sonnet --install-skill --start
```

For a read-only preflight that does not mutate OAuth, Codex config, skills, or gateway state:

```bash
codex-antigravity setup --check
codex-antigravity setup --json
```

After setup, inspect native Codex readiness and gateway lifecycle with:

```bash
codex-antigravity status
codex-antigravity doctor --codex-ready
codex-antigravity doctor --codex-ready --json
codex-antigravity stop
```

Install the Codex provider block:

```bash
codex-antigravity configure-codex --write
```

The command validates the Codex model id, provider id, provider name, and gateway base URL before writing. It updates `~/.codex/config.toml` through a private atomic write, follows an existing symlink to update the real config target, and writes a timestamped private backup first when it changes an existing config. To inspect the TOML without writing it:

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

`setup-v2 --write` installs or refreshes the bundled skill, but it does not write `~/.codex/config.toml`; use primary `setup --write`, `setup-google`, or `configure-codex --write` when you explicitly want Codex itself pointed at the gateway.
If an existing `anti` skill differs from the bundled copy, pass `--force` with `install-skill` or `setup-v2 --write` to back it up and replace it before verifying. BYOK provider identity/key checks are skipped by default; add `--check-byok` when you want setup-v2 to inspect provider readiness and confirm configured provider models are advertised by the running gateway.

The skill also ships a helper-level panel mode inspired by MoA/Fusion workflows. Codex remains the acting agent; the helper fans out to gateway-advertised models, asks a judge model to synthesize consensus, contradictions, blind spots, and next actions, then returns advisory output for Codex to verify:

```bash
python3 ~/.codex/skills/anti/scripts/anti.py panel --mode review --scope staged
python3 ~/.codex/skills/anti/scripts/anti.py panel --mode plan --scope working-tree --prompt "Plan this PR"
python3 ~/.codex/skills/anti/scripts/anti.py panel --mode ask --model sonnet --model openrouter:deepseek/deepseek-chat --judge opus --prompt "Compare these approaches"
```

Panel consensus is not proof and should not patch code directly. BYOK panel models such as `openrouter:...` only work when the running gateway advertises them in `/v1/models`, which requires usable provider keys or a key-optional local provider setup.

V2 named workflow presets wrap the same advisory engine for common Codex work:

```bash
python3 ~/.codex/skills/anti/scripts/anti.py workflow review-ready --scope staged
python3 ~/.codex/skills/anti/scripts/anti.py workflow plan-deep --scope working-tree --prompt "Plan this PR" --progress
python3 ~/.codex/skills/anti/scripts/anti.py workflow ship-gate --scope diff --base origin/main --json
python3 ~/.codex/skills/anti/scripts/anti.py workflow provider-compare --model sonnet --model openrouter:deepseek/deepseek-chat --prompt "Compare these approaches"
python3 ~/.codex/skills/anti/scripts/anti.py runs list
```

Workflow runs save sanitized summaries under `~/.codex/anti-runs` by default; primitive `consult`, `plan`, `review`, and `panel` commands default to not writing a ledger unless `--save-output summary` or `--save-output full` is passed. `--fallback-model sonnet --fallback-policy on-retryable` and `--progress` are available for long-running model calls that may otherwise fail silently or hit transient backend rotation errors.

Before running `codex-antigravity login`, create a Google OAuth desktop client. The local callback listener uses:

```text
http://localhost:51121/oauth-callback
```

Then either export the client credentials:

```bash
export ANTIGRAVITY_CLIENT_ID="your-client-id.apps.googleusercontent.com"
export ANTIGRAVITY_CLIENT_SECRET="your-client-secret"
```

Or write them to `~/.codex/antigravity-credentials.json`. This client-credential file is plaintext but permission-repaired to `0600`; account tokens are stored separately in encrypted storage.

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

`setup-google` first verifies that Google OAuth client credentials are configured, then runs the browser OAuth flow before writing Codex config. That keeps Codex config untouched if login cannot start or complete. It forces Google's account chooser for multi-account setup, stores each account in the encrypted rotation pool, clears stale cooldown state for re-authenticated accounts, writes the Codex provider block after successful login unless `--skip-codex-config` is passed, and runs `doctor` against the same config path unless `--skip-doctor` is passed. You can also add more accounts later:

```bash
codex-antigravity login --count 2
codex-antigravity accounts
```

Start the local gateway:

```bash
codex-antigravity start
codex-antigravity start --background
```

### BYOK providers

Built-in presets are available for OpenRouter, DeepSeek, xAI, Kimi/Moonshot, Ollama, OpenCode-compatible local servers, and custom OpenAI-compatible APIs:

```bash
codex-antigravity provider presets
codex-antigravity provider set deepseek --api-key-env DEEPSEEK_API_KEY --model deepseek-chat --model deepseek-reasoner
codex-antigravity provider set openrouter --api-key-env OPENROUTER_API_KEY --model deepseek/deepseek-chat
codex-antigravity provider set xai --api-key-env XAI_API_KEY --model grok-code-fast-1
codex-antigravity provider set kimi --api-key-env KIMI_API_KEY --model kimi-k2-0711-preview
codex-antigravity provider set ollama --base-url http://localhost:11434/v1 --model gpt-oss:20b
codex-antigravity provider list
```

For BYOK-only use, point Codex at a BYOK model when writing the provider block:

```bash
codex-antigravity configure-codex --write --model deepseek:deepseek-chat
codex-antigravity doctor --byok-only
```

`--api-key-env` avoids persisting provider keys and reads them from the gateway process environment. If you intentionally want a provider key stored locally, pass `--api-key`; stored provider keys are encrypted in `~/.codex/antigravity-providers.json`. Built-in provider env vars include `OPENROUTER_API_KEY`, `DEEPSEEK_API_KEY`, `XAI_API_KEY`, `KIMI_API_KEY`, `MOONSHOT_API_KEY`, `OLLAMA_API_KEY`, and `OPENCODE_API_KEY`.
The `/v1/models` catalog only advertises BYOK models when the provider has a usable stored/env key or explicitly supports key-optional loopback/local use. The generic `custom` preset is not auto-enabled; run `codex-antigravity provider set custom --base-url ... --model ...` before routing `custom:model`.
When `provider set` is given `--api-key-env`, models are configured but remain hidden from `/v1/models` until that environment variable is available to the running gateway.
Provider ids reserve model-name separators and may only contain letters, numbers, underscores, and hyphens; model ids themselves may still contain `/` or `:`, but not whitespace or control characters. Unknown `provider:model` prefixes are rejected as BYOK routing errors before any Google account selection.
Custom provider and Codex gateway base URLs must be absolute `http` or `https` URLs without embedded credentials, whitespace/control characters, query strings, fragments, invalid ports, or malformed bracketed hosts. Plain `http` base URLs are accepted only for loopback/local hosts; remote BYOK providers and remote gateway URLs must use `https`. Non-preset custom BYOK providers must provide a base URL before models are exposed. Stored/env BYOK API keys and extra BYOK provider header values must be printable ASCII without control characters; model-picker display names must not contain control characters. Provider API-key env var names must contain only letters, numbers, and underscores and must not start with a number. Extra headers may not override gateway-managed auth, content, host, or transport headers. Invalid BYOK provider URLs, API keys, env vars, model ids, display names, and headers are rejected before config writes; invalid BYOK provider URLs, timeouts, headers, API keys, and missing API keys are also rejected before streaming starts so Codex gets a normal HTTP error instead of a partial SSE response. Key-optional BYOK providers are only treated as keyless on loopback/local base URLs; remote custom or cloud URLs need a usable stored or env API key before they appear in Codex's picker or route requests. Function/tool names and forced `tool_choice` function names must contain only letters, numbers, underscores, and hyphens, and be 1-64 characters; malformed names are rejected or dropped before routing. BYOK streams surface provider error frames as failed Responses API streams instead of successful empty completions, ignore never-named tool-call deltas, and wait for complete valid streamed function names instead of emitting empty, partial, or malformed function names. BYOK structured tool outputs are serialized to JSON text before being sent as Chat Completions tool messages.

The `configure-codex --write` helper writes this equivalent TOML into `~/.codex/config.toml` after validation:

```toml
model = "claude-3.5-sonnet"
model_provider = "antigravity"
wire_api = "responses"

[model_providers.antigravity]
name = "Google Antigravity"
base_url = "http://localhost:51122/v1"
wire_api = "responses"
```

## Verification

To run connection check diagnostics and verify token security:
```bash
codex-antigravity doctor
codex-antigravity doctor --codex-ready
codex-antigravity doctor --byok-only
```

`doctor` parses the active Codex config, verifies `model_provider = "antigravity"` and the matching provider `base_url`, and exits non-zero on hard readiness failures. `doctor --codex-ready` additionally checks that the gateway is reachable, `/v1/models` advertises the selected Codex model, the model routes to Google or BYOK correctly, and the selected Google family has usable rotation state. Use `--config /path/to/config.toml` to verify a non-default Codex config.

The gateway binds to `127.0.0.1` by default. Binding to a non-loopback host requires both `--allow-remote` and an `ANTIGRAVITY_GATEWAY_TOKEN` of at least 32 visible ASCII characters; remote callers must send `Authorization: Bearer <token>`. The built-in server still speaks plain HTTP, so use remote mode only behind a trusted tunnel, local network boundary, or TLS-terminating proxy.

And execute full unit test coverage:
```bash
python3 -m pytest
```
