# Codex Antigravity Auth

Create a clean, reliable local gateway server that allows you to use Google Antigravity models and BYOK OpenAI-compatible providers directly in OpenAI Codex (CLI or Desktop).

## Features
- **OS-Native Keyring Encryption**: Automatically encrypts OAuth credentials and tokens at rest securely inside macOS Keychain, Windows Credential Manager, or Linux Secret Service via the `keyring` package.
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

Install the Codex provider block:

```bash
codex-antigravity configure-codex --write
```

The command validates the Codex model id, provider id, provider name, and gateway base URL before writing. It updates `~/.codex/config.toml` through a private atomic write, follows an existing symlink to update the real config target, and writes a timestamped private backup first when it changes an existing config. To inspect the TOML without writing it:

```bash
codex-antigravity configure-codex
```

Before running `codex-antigravity login`, set up your desktop Google OAuth credentials. Either export them:

```bash
export ANTIGRAVITY_CLIENT_ID="your-client-id.apps.googleusercontent.com"
export ANTIGRAVITY_CLIENT_SECRET="your-client-secret"
```

Or write them to `~/.codex/antigravity-credentials.json`:

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

Start the local gateway:

```bash
codex-antigravity start
```

### BYOK providers

Built-in presets are available for OpenRouter, DeepSeek, xAI, Kimi/Moonshot, Ollama, OpenCode-compatible local servers, and custom OpenAI-compatible APIs:

```bash
codex-antigravity provider presets
codex-antigravity provider set deepseek --api-key "$DEEPSEEK_API_KEY" --model deepseek-chat --model deepseek-reasoner
codex-antigravity provider set openrouter --api-key "$OPENROUTER_API_KEY" --model deepseek/deepseek-chat
codex-antigravity provider set xai --api-key "$XAI_API_KEY" --model grok-code-fast-1
codex-antigravity provider set kimi --api-key "$KIMI_API_KEY" --model kimi-k2-0711-preview
codex-antigravity provider set ollama --base-url http://localhost:11434/v1 --model gpt-oss:20b
codex-antigravity provider list
```

Stored provider keys are encrypted in `~/.codex/antigravity-providers.json`. You can also use provider env vars such as `OPENROUTER_API_KEY`, `DEEPSEEK_API_KEY`, `XAI_API_KEY`, `KIMI_API_KEY`, `MOONSHOT_API_KEY`, `OLLAMA_API_KEY`, and `OPENCODE_API_KEY`.
The `/v1/models` catalog only advertises BYOK models when the provider has a usable stored/env key or explicitly supports key-optional local use.
Provider ids reserve model-name separators and may only contain letters, numbers, underscores, and hyphens; model ids themselves may still contain `/` or `:`, but not whitespace or control characters.
Custom provider and Codex gateway base URLs must be absolute `http` or `https` URLs without embedded credentials, whitespace/control characters, query strings, fragments, invalid ports, or malformed bracketed hosts. Non-preset custom BYOK providers must provide a base URL before models are exposed. Stored BYOK API keys, model-picker display names, and extra BYOK provider header values must not contain control characters; provider API-key env var names must contain only letters, numbers, and underscores and must not start with a number. Extra headers may not override gateway-managed auth, content, host, or transport headers. Invalid BYOK provider URLs, API keys, env vars, model ids, display names, and headers are rejected before config writes; invalid BYOK provider URLs, timeouts, headers, API keys, and missing API keys are also rejected before streaming starts so Codex gets a normal HTTP error instead of a partial SSE response. BYOK structured tool outputs are serialized to JSON text before being sent as Chat Completions tool messages.

The `configure-codex --write` helper writes this equivalent TOML into `~/.codex/config.toml` after validation:

```toml
model = "gemini-3.5-flash-high"
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
```

The gateway binds to `127.0.0.1` by default. Binding to a non-loopback host requires both `--allow-remote` and `ANTIGRAVITY_GATEWAY_TOKEN`; remote callers must send `Authorization: Bearer <token>`.

And execute full unit test coverage:
```bash
python3 -m pytest
```
