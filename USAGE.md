# Google Antigravity Auth for OpenAI Codex Usage Guide

This guide describes real-world examples, advanced configurations, and diagnostics routines to run Google Antigravity models inside OpenAI Codex efficiently.

## 0. Quick Codex Setup

Install the command from a checkout, write the Codex provider block, authenticate or configure a BYOK provider, then start the gateway:

```bash
uv tool install .
codex-antigravity configure-codex --write
codex-antigravity login
codex-antigravity start
codex-antigravity doctor
```

`configure-codex` validates the Codex model id, provider id, provider name, and gateway base URL before writing. `--write` uses private atomic writes, preserves a symlinked Codex config path by updating its real target, and creates a private timestamped backup before changing an existing Codex config.

For BYOK-only use, replace `codex-antigravity login` with a provider setup command such as:

```bash
codex-antigravity provider set deepseek --api-key "$DEEPSEEK_API_KEY" --model deepseek-chat
```

BYOK provider ids may contain only letters, numbers, underscores, and hyphens. Provider model ids may contain `/` or `:`, but not whitespace or control characters. Non-preset custom BYOK providers must provide a base URL. Stored/env BYOK API keys and extra provider header values must be printable ASCII without control characters; model-picker display names must not contain control characters. Provider API-key env var names must contain only letters, numbers, and underscores and must not start with a number. Custom provider and Codex gateway base URLs must be absolute `http` or `https` URLs without embedded credentials, whitespace/control characters, query strings, fragments, invalid ports, or malformed bracketed hosts. Extra BYOK provider headers may not override gateway-managed auth, content, host, or transport headers; malformed provider config is rejected before it is written and before streaming begins. Key-optional BYOK providers are only keyless on loopback/local base URLs; remote custom or cloud endpoints need a stored or env API key. BYOK streams ignore never-named tool-call deltas and wait for complete streamed function names before emitting function-call items.

## 1. Supported Models & Aliases
You can use standard, developer-friendly names in your `~/.codex/config.toml` that the gateway automatically translates to the official Google Antigravity backend model definitions:

| OpenAI Codex Model ID | Antigravity Backend Model |
| --- | --- |
| `gemini-3.5-flash-high` | `gemini-3-flash-agent` (DeepMind Agentic Flash) |
| `gemini-3.5-flash-medium` | `gemini-3.5-flash-low` (General Purpose Flash) |
| `gemini-3.1-pro-high` | `gemini-3.1-pro-high` (Advanced Reasoning Pro) |
| `claude-3.5-sonnet` | `claude-sonnet-4-6` (High-Fidelity Anthropic Sonnet) |
| `claude-opus-4-6` | `claude-opus-4-6-thinking` (Deep Anthropic Opus Reasoning) |

---

## 2. Advanced Multi-Account Rotation & Rate-Limiting
When multiple Google accounts are registered, the gateway automatically rotates through them:
- **Rate-Limiting Cooldowns**: If a request returns `429 RESOURCE_EXHAUSTED` (such as Anthropic/Claude limiters), the account is marked on a cooldown backoff strategy (exponential delay) for the specific model family.
- **Sticky Active Selection**: The `AccountManager` maintains selection stickiness based on family defaults to ensure conversational continuity before rotating on connection timeouts/failures.

---

## 3. High-Fidelity Streaming & Reasoning
The local server natively isolates explicit thinking blocks and stream envelopes, ensuring standard formatting:
- **Thinking/Reasoning block**: Emits `response.reasoning_text.delta` for explicit backend thinking parts while preserving regular `thoughtSignature` text as visible output.
- **SSE Stream**: Formats candidates, function calls, usage metadata, and completion events into Responses API SSE chunks parsed correctly by both Codex CLI and Codex Desktop.
