# AGENTS.md — Codex Antigravity Auth

> Start at [STATUS.md](STATUS.md) for current source contracts and evidence boundaries. Historical release claims are preserved in [the dated archive](docs/history/2026-10-01/AGENTS.md); they are not proof for this checkout.

> **Current state snapshot:** See [CURRENT_STATE.md](CURRENT_STATE.md) for the 2026-10-04 refresh and retained September source analysis. See [VALIDATION_COPIES.md](VALIDATION_COPIES.md) for newer hidden source copies; equal package versions do not establish equal source.

Guidance for AI coding agents (Codex, Claude Code, OpenCode, etc.) working on this project.

## Overview

Local gateway server that allows OpenAI Codex (CLI and Desktop) to use Google Antigravity models (Gemini 3.8/3.7/3.1, Claude Sonnet/Opus 4.6) via Google OAuth PKCE and multi-account rotation, plus BYOK OpenAI-compatible providers such as OpenRouter, DeepSeek, xAI, Kimi/Moonshot, Ollama, and OpenCode-compatible endpoints.

Newer models are registered but plan-gated: Gemini 4 Argon (Ultra/Enterprise; the docs list it as not available in Antigravity IDE, which is the client identity this gateway presents) and Claude Sonnet/Opus 5.5 (paid, non-trial Pro or Ultra plans). Their backend wire ids are unverified and they were not offered to the Jio-provided Google AI Pro accounts checked on 2026-10-15. Per Google's in-API notice, third-party model access (Claude 4.6, GPT-OSS) ends on non-paid plans on 2026-11-02, so the anti skill defaults to Gemini 3.8 Flash.

## Architecture

[STATUS.md](STATUS.md) maps Google, native OpenAI and BYOK routes to their owning
modules. Use the central capability catalog for model aliases/families and adapter
support; do not infer support from names or duplicate a model registry in docs.

## Key Conventions

- **Python 3.10+** — use `python3` or activate venv
- **Virtual env**: `source .venv/bin/activate`
- **Install**: `uv pip install -e .`
- **Test**: `python3 scripts/run_tests.py -q` (isolates credentials/state and denies non-fixture sockets before pytest startup; all tests must pass)
- **Run server**: `codex-antigravity start --port 51122`
- **Credentials**: `~/.codex/antigravity-credentials.json` or env vars
- **Accounts**: `~/.codex/antigravity-accounts.json` (Fernet-encrypted)
- **BYOK providers**: `~/.codex/antigravity-providers.json` (Fernet-encrypted) or provider API key env vars

## Model and capability ownership

`models.py` owns Google model aliases; `capability_catalog.py` projects route
contracts and the generated standalone Anti snapshot. See
[capability semantics](codex_antigravity_auth/design/capabilities.md) and
[discovery evidence](codex_antigravity_auth/design/model-discovery.md). Catalog
advertisement, transport support and live generation evidence are distinct.
Known image-generation backend IDs are recognized, but generation stays disabled
until the Google adapter has the required output bridge.

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
model = "gemini-3.8-flash"
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
