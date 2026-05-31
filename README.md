# Google Antigravity Auth for OpenAI Codex

Create a clean, reliable local gateway server that allows you to use Google Antigravity models (Gemini 3.x, Claude Opus/Sonnet 4.6, etc.) directly in OpenAI Codex (CLI or Desktop), utilizing proper Google OAuth (PKCE) authentication and automatic multi-account switching / rotation.

## Features
- **OS-Native Keyring Encryption**: Automatically encrypts OAuth credentials and tokens at rest securely inside macOS Keychain, Windows Credential Manager, or Linux Secret Service via the `keyring` package.
- **Transaction-Safe Cooldown Rotations**: Automatically rotates accounts on backend failures (such as `401`, `403`, or `429` rate limiters) with clean exponential backoff.
- **High-Fidelity SSE Translation**: Fully translates complex stream candidate envelopes, role alignments, thoughts token tracking, and VALIDATED tool parameter modes.

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

And execute full unit test coverage:
```bash
pytest
```
