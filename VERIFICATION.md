# Verification Guide

> **Current release snapshot — 2026-08-24:** Canonical `main` is v2.2.0 at `499ff436c311638ebe67cea6ad2de354a309e6ec`. CI run `32522720655` and Publish run `32522720637` passed for that exact SHA, the public GitHub release is [v2.2.0](https://github.com/Reedtrullz/codex-antigravity-auth/releases/tag/v2.2.0), and the PyPI index reports `codex-antigravity-auth==2.2.0`. The release-candidate paragraphs below refer to July 1.7.0 evidence and remain historical; no fresh credentialed live generation was run in this audit.

## Quick Start (5 minutes)

### 1. Install
```bash
uv tool install "git+https://github.com/Reedtrullz/codex-antigravity-auth.git"
```

From a checkout, use `uv tool install .` instead.

### 2. Configure Google Credentials
Create a Google OAuth desktop client with this local callback:

```text
http://localhost:51121/oauth-callback
```

Write `~/.codex/antigravity-credentials.json`:
```json
{
  "client_id": "YOUR_CLIENT_ID.apps.googleusercontent.com",
  "client_secret": "YOUR_CLIENT_SECRET"
}
```
Or export: `ANTIGRAVITY_CLIENT_ID` + `ANTIGRAVITY_CLIENT_SECRET`. The credential JSON is plaintext but permission-repaired to `0600`; login tokens are stored separately in encrypted storage.

### 3. Login Or Configure BYOK
```bash
codex-antigravity login
```
Opens browser → pick Google account → tokens stored encrypted.

For BYOK providers:
```bash
codex-antigravity provider set deepseek --api-key-env DEEPSEEK_API_KEY --model deepseek-chat
codex-antigravity provider set openrouter --api-key-env OPENROUTER_API_KEY --model deepseek/deepseek-chat
codex-antigravity provider set xai --api-key-env XAI_API_KEY --model grok-code-fast-1
codex-antigravity provider set kimi --api-key-env KIMI_API_KEY --model kimi-k2-0711-preview
codex-antigravity provider set ollama --base-url http://localhost:11434/v1 --model gpt-oss:20b
```
Provider ids may only contain letters, numbers, underscores, and hyphens; provider model ids may still contain `/` or `:`, but not whitespace or control characters.

### 4. Configure Codex
Add to `~/.codex/config.toml`:

```bash
codex-antigravity configure-codex --write
# BYOK-only example:
codex-antigravity configure-codex --write --model deepseek:deepseek-chat
```

Equivalent manual TOML:

```toml
model = "gemini-3.8-flash"
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
codex-antigravity doctor --byok-only
codex-antigravity doctor --codex-ready --json  # read-only store/schema/service/capability report
python3 -m pytest -q             # current full local suite
curl http://localhost:51122/v1/models | python3 -m json.tool  # model catalog
```

Treat verification evidence in layers: unit/mocked route tests prove local contracts; wheel and installed-skill checks prove packaging; `/health`, service status, and model-catalog readbacks prove the running local gateway; only an explicit `doctor --codex-ready --live` or manual `/v1/responses` call proves a credentialed provider path. Do not present local or mocked evidence as a live-provider claim.

The `1.7.0` release-candidate paragraph below is historical and must not be used as current release state. For the current v2.2.0 release, use the exact-SHA CI/Publish and PyPI evidence in the snapshot above. No fresh credentialed live-provider result is claimed.

## Manual Smoke Test
```bash
curl -s -X POST http://localhost:51122/v1/responses \
  -H "Content-Type: application/json" \
  -d '{"model":"gemini-3.8-flash","input":"Say hello!"}'
# Expect: 200 with output containing text
```

### Optional BYOK Smoke
With provider API keys exported only in the shell environment:

```bash
export DEEPSEEK_API_KEY="..."
curl -s http://localhost:51122/v1/models | python3 -m json.tool
curl -s -X POST http://localhost:51122/v1/responses \
  -H "Content-Type: application/json" \
  -d '{"model":"deepseek:deepseek-chat","input":"Return exactly: byok-smoke-ok","max_output_tokens":256}'

export OPENROUTER_API_KEY="..."
curl -s http://localhost:51122/v1/models | python3 -m json.tool
curl -s -X POST http://localhost:51122/v1/responses \
  -H "Content-Type: application/json" \
  -d '{"model":"openrouter:openrouter/auto","input":"Return exactly: openrouter-smoke-ok","max_output_tokens":256}'
```

If you want to smoke `deepseek:deepseek-v4-flash` instead, include `--model deepseek-v4-flash` in the DeepSeek `provider set` command so it appears in `/v1/models`.

Historical credentialed smokes were run on 2026-07-03 against PR #1 head `e6a81ac` before squash merge `191daa4`; they are not evidence for `1.7.0`. DeepSeek used a transient `DEEPSEEK_API_KEY` environment variable only, did not persist the key, exposed `deepseek:deepseek-v4-flash` in `/v1/models`, and returned exact sentinels for both non-streaming and streaming `/v1/responses`.
OpenRouter was also smoke-tested with a transient `OPENROUTER_API_KEY` environment variable only: `/v1/models` exposed `openrouter:openrouter/auto`, non-streaming and streaming `/v1/responses` returned exact sentinels for `openrouter:openrouter/auto`, and manual explicit routing returned an exact non-streaming sentinel for `openrouter:deepseek/deepseek-chat`. The Anti V2 workflow release has local package/unit proof and CI proof recorded in `STATUS.md`, but has not rerun credentialed live Google or BYOK generation smokes.

## Switching Between ChatGPT and Antigravity
- **Use ChatGPT**: Remove `model_provider` line from config.toml
- **Use Antigravity**: Add `model_provider = "antigravity"`, ensure gateway is running

## Available Models
- `gemini-3.8-flash` → Gemini 3.8 Flash (Low/Medium/High)
- `gemini-3.7-flash` → Gemini 3.7 Flash (Low/Medium/High)
- `gemini-3.6-flash-high` → Gemini 3.6 Flash compatibility ID
- `gemini-3.1-pro` → Gemini 3.1 Pro (Reasoning)
- `gemini-3.1-flash-image` → Gemini 3.1 Flash Image generation
- `claude-sonnet-4-6` → Claude Sonnet 4.6 (Google)
- `claude-opus-4-6-thinking` → Claude Opus 4.6 (Google)
- `gpt-oss-120b-medium` → GPT-OSS 120B (text-only)
- `deepseek:deepseek-v4-flash` → DeepSeek V4 Flash BYOK
- `deepseek:deepseek-chat` → DeepSeek BYOK
- `openrouter:openrouter/auto` → OpenRouter BYOK auto-router
- `openrouter:deepseek/deepseek-chat` → OpenRouter BYOK
- `xai:grok-code-fast-1` → xAI BYOK
- `kimi:kimi-k2-0711-preview` → Kimi/Moonshot BYOK
- `ollama:gpt-oss:20b` → Ollama BYOK/local
