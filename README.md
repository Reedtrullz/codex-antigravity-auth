# Codex Antigravity Auth

Create a clean, reliable local gateway server that allows you to use Google Antigravity models and BYOK OpenAI-compatible providers directly in OpenAI Codex (CLI or Desktop).

## Features
- **OS-Native Keyring Encryption**: Automatically encrypts OAuth credentials and tokens at rest securely inside macOS Keychain, Windows Credential Manager, or Linux Secret Service via the `keyring` package.
- **Transaction-Safe Cooldown Rotations**: Automatically rotates accounts on backend failures (such as `401`, `403`, or `429` rate limiters) with clean exponential backoff.
- **High-Fidelity SSE Translation**: Fully translates complex stream candidate envelopes, role alignments, thoughts token tracking, and VALIDATED tool parameter modes.
- **BYOK Provider Routing**: Route model IDs like `deepseek:deepseek-chat`, `xai:grok-code-fast-1`, `kimi:kimi-k2-0711-preview`, `openrouter:deepseek/deepseek-chat`, and custom OpenAI-compatible endpoints through encrypted API-key config.

## Installation

Ensure you are in the correct Python virtual environment (e.g. one configured under your current shell) and run:

```bash
uv pip install -e .
```

## Configuration

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

Configure your `~/.codex/config.toml` to route Codex models through this server:

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
