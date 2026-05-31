# Verification Guide

## Quick Start (5 minutes)

### 1. Install
```bash
git clone https://github.com/Reedtrullz/codex-antigravity-auth
cd codex-antigravity-auth
uv pip install -e .
```

### 2. Configure Credentials
Write `~/.codex/antigravity-credentials.json`:
```json
{
  "client_id": "YOUR_CLIENT_ID.apps.googleusercontent.com",
  "client_secret": "YOUR_CLIENT_SECRET"
}
```
Or export: `ANTIGRAVITY_CLIENT_ID` + `ANTIGRAVITY_CLIENT_SECRET`.

### 3. Login
```bash
codex-antigravity login
```
Opens browser → pick Google account → tokens stored encrypted.

### 4. Configure Codex
Add to `~/.codex/config.toml`:
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
pytest                           # 18 tests, all must pass
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
- `gemini-3.1-pro-high` → Gemini 3.1 Pro (Reasoning)
- `claude-3.5-sonnet` → Claude Sonnet 4.6 (Google)
- `claude-opus-4-6` → Claude Opus 4.6 (Google)
