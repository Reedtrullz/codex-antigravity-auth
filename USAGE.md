# Google Antigravity Auth for OpenAI Codex Usage Guide

This guide describes real-world examples, advanced configurations, and diagnostics routines to run Google Antigravity models inside OpenAI Codex efficiently.

## 0. Quick Codex Setup

Install the command from a checkout, write the Codex provider block, authenticate or configure a BYOK provider, then start the gateway:

```bash
uv tool install .
codex-antigravity configure-codex --write
codex-antigravity install-skill
codex-antigravity login
codex-antigravity start
codex-antigravity doctor
```

`configure-codex` validates the Codex model id, provider id, provider name, and gateway base URL before writing. `--write` uses private atomic writes, preserves a symlinked Codex config path by updating its real target, and creates a private timestamped backup before changing an existing Codex config.

`install-skill` installs the bundled Codex `$anti` sidecar skill into `~/.codex/skills/anti`. Use it when you want chat prompts such as `$anti review this diff with opus`, `$anti plan --scope staged`, or `$anti smoke` to route through the repo-shipped helper. Existing local `anti` skills are left untouched unless `--force` is passed, and forced installs create a timestamped backup.

For the easiest Google Antigravity OAuth setup, use the guided command:

```bash
codex-antigravity setup-google --accounts 2
codex-antigravity start
```

This first verifies that Google OAuth client credentials are configured, then runs the browser OAuth login before writing Codex config so a login startup failure does not leave Codex pointed at an unusable gateway setup. It forces Google's account chooser when adding multiple accounts, stores every successful login in the encrypted rotation pool, clears stale cooldown state on re-authentication, prints the active Gemini/Claude rotation status, writes the Codex provider block, and runs `doctor` against the same config path. To add more accounts later, run `codex-antigravity login --count 2`; to inspect rotation state, run `codex-antigravity accounts`.

For BYOK-only use, replace `codex-antigravity login` with a provider setup command such as:

```bash
codex-antigravity provider set deepseek --api-key-env DEEPSEEK_API_KEY --model deepseek-chat
codex-antigravity configure-codex --write --model deepseek:deepseek-chat
codex-antigravity doctor --byok-only
```

BYOK provider ids may contain only letters, numbers, underscores, and hyphens. Provider model ids may contain `/` or `:`, but not whitespace or control characters. Unknown `provider:model` prefixes are rejected as BYOK routing errors before any Google account selection. Non-preset custom BYOK providers must provide a base URL, and the generic `custom` preset is not auto-enabled until `provider set custom ...` is run. `--api-key-env` is preferred because it avoids persisting provider keys; `--api-key` stores a key in encrypted provider config. Stored/env BYOK API keys and extra provider header values must be printable ASCII without control characters; model-picker display names must not contain control characters. Provider API-key env var names must contain only letters, numbers, and underscores and must not start with a number. Custom provider and Codex gateway base URLs must be absolute `http` or `https` URLs without embedded credentials, whitespace/control characters, query strings, fragments, invalid ports, or malformed bracketed hosts. Plain `http` base URLs are accepted only for loopback/local hosts; remote providers and remote gateway URLs must use `https`. Extra BYOK provider headers may not override gateway-managed auth, content, host, or transport headers; malformed provider config is rejected before it is written and before streaming begins. Key-optional BYOK providers are only keyless on loopback/local base URLs; remote custom or cloud endpoints need a stored or env API key. BYOK streams surface provider error frames as failed Responses API streams, ignore never-named tool-call deltas, and wait for complete streamed function names before emitting function-call items.
Models configured with `--api-key-env` remain hidden from `/v1/models` until the env var exists in the gateway process environment. `doctor --byok-only` fails when configured BYOK providers have missing or malformed keys, and `doctor --config /path/to/config.toml` can verify non-default Codex config files.
The gateway binds to loopback by default. Non-loopback binds require `--allow-remote` plus `ANTIGRAVITY_GATEWAY_TOKEN` set to at least 32 visible ASCII characters; remote clients must send it as a bearer token. The built-in server is still plain HTTP, so remote use should go through a trusted tunnel, local network boundary, or TLS-terminating proxy.

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
- **Rate-Limiting Cooldowns**: If a request returns `429 RESOURCE_EXHAUSTED` (such as Anthropic/Claude limiters), the account is marked on an account-level cooldown backoff strategy with exponential delay. Cooldowns persist across restarts so the gateway does not immediately retry a recently limited account.
- **Sticky Active Selection**: The `AccountManager` keeps independent active-account slots for Gemini and Claude families to preserve conversational continuity before rotating on connection timeouts/failures.

---

## 3. High-Fidelity Streaming & Reasoning
The local server natively isolates explicit thinking blocks and stream envelopes, ensuring standard formatting:
- **Thinking/Reasoning block**: Emits `response.reasoning_text.delta` for explicit backend thinking parts while preserving regular `thoughtSignature` text as visible output.
- **SSE Stream**: Formats candidates, function calls, usage metadata, and completion events into Responses API SSE chunks parsed correctly by both Codex CLI and Codex Desktop.
