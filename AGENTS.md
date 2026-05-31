# AGENTS.md — Codex Antigravity Auth

Guidance for AI coding agents (Codex, Claude Code, OpenCode, etc.) working on this project.

## Overview

Local gateway server that allows OpenAI Codex (CLI and Desktop) to use Google Antigravity models (Gemini 3.x, Claude Opus/Sonnet 4.6) via Google OAuth PKCE and multi-account rotation, plus BYOK OpenAI-compatible providers such as OpenRouter, DeepSeek, xAI, Kimi/Moonshot, Ollama, and OpenCode-compatible endpoints.

## Architecture

```
codex_antigravity_auth/
├── server.py        # FastAPI gateway: POST /v1/responses, GET /v1/models
├── transform.py     # Codex Responses API ↔ Google Gemini/Claude translation
├── accounts.py      # AccountManager: selection, rotation, cooldowns, refresh
├── oauth.py         # PKCE OAuth flow, token exchange, refresh
├── storage.py       # Encrypted JSON persistence (Fernet + OS keyring)
├── cli.py           # CLI: login, doctor, accounts, start
├── constants.py     # Endpoints, credential resolution, platform detection
├── byok.py          # Encrypted BYOK provider config, presets, model routing
├── models.py        # User-facing model name → backend ID mapping
├── schema.py        # JSON Schema sanitization for Antigravity compatibility
├── fingerprint.py   # Device fingerprint generation (Electron UA, IDE metadata)
tests/
├── test_transform.py
├── test_server_streaming.py
├── test_accounts.py
├── test_storage.py
├── test_cli.py
├── test_schema_sanitization.py
├── test_fidelity_transforms.py
├── test_fidelity_edge_cases.py
```

## How Requests Flow

```
Codex Desktop/CLI
    │  POST /v1/responses  (Responses API format)
    ▼
server.py: create_response()
    │  1. select_active_account(model)  → accounts.py
    │  2. transform_request()           → transform.py
    │  3. POST to cloudcode-pa.googleapis.com/v1internal:generateContent
    │  4. transform_response()          → transform.py
    ▼
Codex Desktop/CLI  ←  Responses API formatted response
```

## Key Conventions

- **Python 3.10+** — use `python3` or activate venv
- **Virtual env**: `source .venv/bin/activate`
- **Install**: `uv pip install -e .`
- **Test**: `python3 -m pytest` (34 tests, all must pass)
- **Run server**: `codex-antigravity start --port 51122`
- **Credentials**: `~/.codex/antigravity-credentials.json` or env vars
- **Accounts**: `~/.codex/antigravity-accounts.json` (Fernet-encrypted)
- **BYOK providers**: `~/.codex/antigravity-providers.json` (Fernet-encrypted) or provider API key env vars

## Model Name Mapping

User-facing aliases → Google backend models (`models.py`):
- `gemini-3.5-flash-high` → `gemini-3-flash-agent`
- `gemini-3.5-flash-medium` → `gemini-3.5-flash-low`
- `gemini-3.1-pro-high` → `gemini-3.1-pro-high`
- `claude-3.5-sonnet` → `claude-sonnet-4-6`
- `claude-opus-4-6` → `claude-opus-4-6-thinking`

BYOK provider models use a provider prefix:
- `deepseek:deepseek-chat`
- `openrouter:deepseek/deepseek-chat`
- `xai:grok-code-fast-1`
- `kimi:kimi-k2-0711-preview`
- `ollama:gpt-oss:20b`

## Critical Pitfalls

1. **`thoughtSignature` is NOT reasoning** — The Google backend emits `thoughtSignature` on regular text parts. Do NOT treat them as thinking blocks; they're normal output text. The transform layer must let `text` through to `output.content[]` regardless of `thoughtSignature` presence.

2. **Import `time` in `transform.py`** — `created_at` uses `int(time.time())`. If `time` isn't imported, the fallback produces bogus UUID timestamps.

3. **Non-streaming response wrapping** — Backend responses come wrapped in `{"response": {"candidates": [...]}}`. Always unwrap via `gemini_resp.get("response", gemini_resp)` before parsing.

4. **Rate-limit cooldowns are persisted** — `AccountManager._cooldowns` and `._failures` are mirrored into the encrypted accounts JSON under `accountState` so restarts do not immediately retry cooled-down accounts.

5. **`/v1/models` endpoint is REQUIRED** for Codex Desktop's model picker to show custom models. Without it, the picker only shows OpenAI's native models.

6. **Streaming function call output_index** — function calls must keep unique, incrementing output indices and stable item IDs between `added` and `done` events.

## Configuring Codex Desktop

`~/.codex/config.toml`:
```toml
model = "gemini-3.5-flash-high"
model_provider = "antigravity"
wire_api = "responses"

[model_providers.antigravity]
name = "Google Antigravity"
base_url = "http://localhost:51122/v1"
wire_api = "responses"
```

Remove `model_provider` to revert to standard OpenAI/ChatGPT.

## OAuth Credential Setup

1. Create Google OAuth desktop client with redirect `http://localhost:51121/oauth-callback`
2. Write `~/.codex/antigravity-credentials.json`:
   ```json
   {"client_id": "...", "client_secret": "..."}
   ```
3. Run `codex-antigravity login`
