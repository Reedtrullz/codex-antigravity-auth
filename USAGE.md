# Google Antigravity Auth for OpenAI Codex Usage Guide

This guide describes real-world examples, advanced configurations, and diagnostics routines to run Google Antigravity models inside OpenAI Codex efficiently.

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
The local server natively isolates thinking blocks and stream envelopes, ensuring standard formatting:
- **Thinking/Reasoning block**: Emits standard `response.reasoning.delta` containing self-reflection tokens cleanly, avoiding text stream pollution.
- **SSE Stream**: Formats candidates, annotations, and metadata into exact Response API SSE stream chunks parsed correctly by both Codex CLI and Codex Desktop.
