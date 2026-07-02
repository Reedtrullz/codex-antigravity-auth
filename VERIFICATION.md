# Verification Guide

## Quick Start (5 minutes)

### 1. Install
```bash
uv tool install "git+https://github.com/Reedtrullz/codex-antigravity-auth.git"
```

From a checkout, use `uv tool install .` instead.

### 2. Configure Credentials
Write `~/.codex/antigravity-credentials.json`:
```json
{
  "client_id": "YOUR_CLIENT_ID.apps.googleusercontent.com",
  "client_secret": "YOUR_CLIENT_SECRET"
}
```
Or export: `ANTIGRAVITY_CLIENT_ID` + `ANTIGRAVITY_CLIENT_SECRET`.

### 3. Login Or Configure BYOK
```bash
codex-antigravity login
```
Opens browser → pick Google account → tokens stored encrypted.

For BYOK providers:
```bash
codex-antigravity provider set deepseek --api-key "$DEEPSEEK_API_KEY" --model deepseek-chat
codex-antigravity provider set openrouter --api-key "$OPENROUTER_API_KEY" --model deepseek/deepseek-chat
codex-antigravity provider set xai --api-key "$XAI_API_KEY" --model grok-code-fast-1
codex-antigravity provider set kimi --api-key "$KIMI_API_KEY" --model kimi-k2-0711-preview
codex-antigravity provider set ollama --base-url http://localhost:11434/v1 --model gpt-oss:20b
```
Provider ids may only contain letters, numbers, underscores, and hyphens; provider model ids may still contain `/` or `:`, but not whitespace or control characters.

### 4. Configure Codex
Add to `~/.codex/config.toml`:

```bash
codex-antigravity configure-codex --write
```

Equivalent manual TOML:

```toml
model = "gemini-3.5-flash-high"
model_provider = "antigravity"
wire_api = "responses"

[model_providers.antigravity]
name = "Google Antigravity"
base_url = "http://localhost:51122/v1"
wire_api = "responses"
```

### 5. Start Gateway
```bash
codex-antigravity start
```

### 6. Verify
```bash
codex-antigravity doctor        # diagnostics
python3 -m pytest -q             # current local suite, 147 tests plus 99 subtests
curl http://localhost:51122/v1/models | python3 -m json.tool  # model catalog
```

## Manual Smoke Test
```bash
curl -s -X POST http://localhost:51122/v1/responses \
  -H "Content-Type: application/json" \
  -d '{"model":"gemini-3.5-flash-high","input":"Say hello!"}'
# Expect: 200 with output containing text
```

## Switching Between ChatGPT and Antigravity
- **Use ChatGPT**: Remove `model_provider` line from config.toml
- **Use Antigravity**: Add `model_provider = "antigravity"`, ensure gateway is running

## Available Models
- `gemini-3.5-flash-high` → Gemini 3.5 Flash (Agent High)
- `gemini-3.5-flash-medium` → Gemini 3.5 Flash (General)
- `gemini-3.1-pro-high` → Gemini 3.1 Pro (Reasoning)
- `claude-3.5-sonnet` → Claude Sonnet 4.6 (Google)
- `claude-opus-4-6` → Claude Opus 4.6 (Google)
- `deepseek:deepseek-chat` → DeepSeek BYOK
- `openrouter:deepseek/deepseek-chat` → OpenRouter BYOK
- `xai:grok-code-fast-1` → xAI BYOK
- `kimi:kimi-k2-0711-preview` → Kimi/Moonshot BYOK
- `ollama:gpt-oss:20b` → Ollama BYOK/local
