---
name: anti
description: Use the optional Anti helper after Antigravity Claude Opus/Sonnet is available in Codex: sidecar review, consult lane, deep autonomous work plan, Claude/Grok collaboration, BluesMinds Grok/GLM, official DeepSeek V4, $anti workflow preset, or $anti panel MoA/Fusion workflow. Trigger when the user writes $anti, @anti, $anti workflow, $anti workflow review-ready, $anti workflow plan-deep, $anti workflow claude-grok, $anti panel, $anti moa, $anti fusion, asks for Antigravity, Opus, Sonnet, Grok, SuperGrok, xAI, BluesMinds, GLM-5.2, DeepSeek V4, Claude/Grok collaboration, a sidecar review, second-opinion model review, multi-model panel, MoA, Fusion, deep work plan, long autonomous session plan, implementation plan, gateway smoke checks, Google Antigravity setup, Codex Antigravity configuration, or codex-antigravity doctor/start workflows.
---

# Anti

Use this skill to ask the local `codex-antigravity-auth` gateway for an external Antigravity review, consult, deep work plan, named workflow preset, or bounded multi-model panel while native Codex remains the primary agent.

V3's primary product is native Claude in Codex through `codex-antigravity setup`; `$anti` is an optional helper for review and planning after the gateway and Codex model picker are already working.

## Core Rule

Treat Antigravity output as a second opinion. Run the helper, read the result, then synthesize it with your own analysis before answering the user. Do not blindly forward the Antigravity result as final truth.

Literal `@anti` is a text convention in v1, not a guaranteed app-level mention chip. `$anti` is the reliable explicit skill invocation.

Panel, MoA, and Fusion workflows are advisory only. The helper can fan out to multiple gateway-advertised models and ask a judge model to synthesize their views, but Codex remains the acting agent and must verify findings before editing. Structured panel findings include a `verify` hint; run or inspect that local check before acting on the claim.

## Models

- Use `opus` for deep review. It maps to `claude-opus-4-6`.
- Use `sonnet` for faster focused consults. It maps to `claude-3.5-sonnet`.
- Use `grok`, `supergrok`, `grok-build`, or `grok-oauth` for the xAI OAuth lane. They map to `xai-oauth:grok-build-0.1`.
- Use `grok-4.3` for `xai-oauth:grok-4.3`.
- Use `grok-bluesminds` or `grok-4.5` for `bluesminds:grok-4.5`. This is a separate API-key route; never silently fail over to or from xAI OAuth.
- Use `deepseek-v4-pro` for `deepseek:deepseek-v4-pro` and `deepseek-v4-flash` for `deepseek:deepseek-v4-flash` through the official DeepSeek API key.
- Use `glm-5.2` or `glm52` for `bluesminds:z-ai/glm-5.2`, especially as a long-context planning or repository-review lane.
- Default review model: `opus`.
- Default plan model: `opus`.
- Default consult/ask model: `sonnet`, unless the user asks for deep review.
- Default panel models: `sonnet` and `opus`.
- Default panel judge: `opus`.
- `panel --collab claude-grok` defaults to `sonnet`, `opus`, and `grok`, with Opus judging. If Grok is not advertised by `/v1/models`, it is recorded as a failed lane unless `--min-successes` requires it.

BluesMinds uses the gateway's OpenAI Chat Completions adapter. Native Responses streaming, structured output, tool-call, usage, and model-identity fidelity are not claimed until successful live probes prove them. Opus remains the default judge for BluesMinds and DeepSeek advisory lanes.

### Choosing complementary reviewer lanes

- DeepSeek V4 Flash is for a fast code second opinion, debugging, and an explicitly selected retryable fallback. It is never an automatic cross-provider fallback.
- DeepSeek V4 Pro is for correctness, security, architecture, and deep code review. Treat it as unproven until the live V4 Pro generation, structured-output, and tool-loop gate passes.
- xAI OAuth Grok is for adversarial assumptions, runtime surprises, and product/UX blind spots.
- BluesMinds Grok/GLM aliases exist but remain unavailable/degraded until the requested route is advertised by `/v1/models` and the provider live-health gate passes. Every BluesMinds example is conditional on both checks.

The normal service intentionally omits BluesMinds. The last bounded live checks returned a billing error for Grok 4.5 and an upstream 429 for GLM-5.2, so neither route is operationally enabled.

A future enablement gate must pass first in a temporary process: catalog identity; non-streaming output and exact model identity; SSE completion and `[DONE]`; structured JSON; tool call and continuation; usage accounting; and bounded retries and no billing/capacity error. Only then may a later authorized task add `BLUESMINDS_API_KEY=op://...` to the durable service reference file and reinstall the service.

Repository context leaves the Google Antigravity lane only after explicit selection and the existing BYOK disclosure. Opus remains the default judge; native Codex remains the acting agent and must verify advisory output locally.

## Helper

Use `scripts/anti.py` from this skill:

```bash
python3 ~/.codex/skills/anti/scripts/anti.py --help
```

Common commands:

```bash
python3 ~/.codex/skills/anti/scripts/anti.py smoke
python3 ~/.codex/skills/anti/scripts/anti.py consult --model sonnet --prompt "Review this idea"
python3 ~/.codex/skills/anti/scripts/anti.py consult --model deepseek-v4-flash --prompt "Give a fast second opinion"
python3 ~/.codex/skills/anti/scripts/anti.py plan --prompt "Plan a long autonomous hardening pass"
python3 ~/.codex/skills/anti/scripts/anti.py plan --scope working-tree --prompt "Plan the next PR"
python3 ~/.codex/skills/anti/scripts/anti.py plan --model glm-5.2 --scope working-tree --prompt "Plan this long-context repository change"
python3 ~/.codex/skills/anti/scripts/anti.py panel --mode review --scope staged
python3 ~/.codex/skills/anti/scripts/anti.py panel --mode review --scope diff --base origin/main --model sonnet --model opus --judge opus
python3 ~/.codex/skills/anti/scripts/anti.py panel --mode plan --scope working-tree --prompt "Plan this PR"
python3 ~/.codex/skills/anti/scripts/anti.py panel --mode ask --model sonnet --model openrouter:deepseek/deepseek-chat --judge opus --prompt "Compare these approaches"
python3 ~/.codex/skills/anti/scripts/anti.py panel --mode ask --collab claude-grok --prompt "Compare these approaches"
python3 ~/.codex/skills/anti/scripts/anti.py panel --mode ask --collab claude-grok --model sonnet --model opus --model grok-bluesminds --prompt "Compare these approaches"
python3 ~/.codex/skills/anti/scripts/anti.py moa --mode review --role correctness --role security --role tests --output findings
python3 ~/.codex/skills/anti/scripts/anti.py fusion --mode plan --model opus --model glm-5.2 --judge opus --scope working-tree --prompt "Plan this change"
python3 ~/.codex/skills/anti/scripts/anti.py workflow review-ready --scope staged
python3 ~/.codex/skills/anti/scripts/anti.py workflow plan-deep --scope working-tree --prompt "Plan V2" --progress
python3 ~/.codex/skills/anti/scripts/anti.py workflow ship-gate --scope diff --base origin/main --json
python3 ~/.codex/skills/anti/scripts/anti.py workflow provider-compare --model sonnet --model openrouter:deepseek/deepseek-chat --prompt "Compare these approaches"
python3 ~/.codex/skills/anti/scripts/anti.py workflow provider-compare --model deepseek-v4-pro --model glm-5.2 --prompt "Compare repository planning approaches"
python3 ~/.codex/skills/anti/scripts/anti.py workflow security-review --scope staged --output findings
python3 ~/.codex/skills/anti/scripts/anti.py workflow debug-consensus --prompt "Intermittent 502s after rotation"
python3 ~/.codex/skills/anti/scripts/anti.py workflow claude-grok --panel-mode review --scope staged --output findings
python3 ~/.codex/skills/anti/scripts/anti.py workflow claude-grok --panel-mode ask --prompt "Should this UX use route A or B?"
python3 ~/.codex/skills/anti/scripts/anti.py workflow claude-grok --model sonnet --model opus --model grok-bluesminds --panel-mode ask --prompt "Stress-test this design"
python3 ~/.codex/skills/anti/scripts/anti.py runs list
python3 ~/.codex/skills/anti/scripts/anti.py review --model opus --scope working-tree
python3 ~/.codex/skills/anti/scripts/anti.py review --model sonnet --scope staged --file path/to/file.py
python3 ~/.codex/skills/anti/scripts/anti.py review --model deepseek-v4-pro --scope staged
python3 ~/.codex/skills/anti/scripts/anti.py review --model opus --scope files --timeout 240 --max-prompt-chars 120000 --file src/main.ts --file src/config.ts
python3 ~/.codex/skills/anti/scripts/anti.py review --model opus --scope diff --base origin/main
git diff -z --name-only origin/main...HEAD > /tmp/anti-files.zlist
python3 ~/.codex/skills/anti/scripts/anti.py review --model opus --scope files --files-from /tmp/anti-files.zlist --json
python3 ~/.codex/skills/anti/scripts/anti.py review --model opus --scope diff --base origin/main --chunked auto --max-review-chunks 8 --max-synthesis-chars 120000 --json
python3 ~/.codex/skills/anti/scripts/anti.py start --port 51122
python3 ~/.codex/skills/anti/scripts/anti.py setup-google --accounts 2
python3 ~/.codex/skills/anti/scripts/anti.py configure-codex --model opus
python3 ~/.codex/skills/anti/scripts/anti.py doctor
codex-antigravity setup --check
codex-antigravity setup --write --accounts 1 --model sonnet --install-skill --start
codex-antigravity doctor --codex-ready
python3 -m unittest discover -s ~/.codex/skills/anti/tests
```

## Workflow

1. Infer whether the user wants `consult`, `plan`, `review`, `workflow`, `runs`, `panel`/`moa`/`fusion`, `smoke`, `start`, `setup-google`, `configure-codex`, or `doctor`.
2. Run `smoke` first when helper readiness is uncertain. Use `codex-antigravity setup --check` or `codex-antigravity doctor --codex-ready` when the user asks whether Claude is native-ready in Codex. Default `smoke` is sidecar readiness; use `smoke --mode full` only when the user asked to make Antigravity the active Codex backend.
3. For deep autonomous work planning, use `plan --model opus`. Add `--scope working-tree`, `--scope staged`, or `--file` when the plan should account for current repo state.
4. For multi-model review or planning, use `panel --mode review` or `panel --mode plan`. Use `--role` for lenses such as correctness, security, tests, protocol, or UX. Use `--output findings` when you want machine-readable `id`, `claim`, `severity`, `lanes`, and `verify` fields. Use BYOK `provider:model` ids only when `/v1/models` advertises them. Use `--collab claude-grok` when the user explicitly wants Claude and Grok to cross-check each other.
5. For common helper flows, prefer named workflow presets: `workflow review-ready` before commit/PR review, `workflow plan-deep` for long autonomous planning, `workflow ship-gate` for merge readiness, `workflow security-review` for injection/secrets/authz/dependency lenses, `workflow debug-consensus` for ranked hypotheses plus discriminating tests, `workflow provider-compare` for BYOK/provider lane comparisons, and `workflow claude-grok --panel-mode review|plan|ask` for explicit Claude/Grok collaboration.
6. For code review, prefer `review --scope staged`, `workflow review-ready --scope staged`, or `panel --mode review --scope staged` when the user asks about commit readiness; use `review --scope working-tree` for current local changes and `review --scope diff --base origin/main` for a clean merge-candidate branch.
7. For focused questions, use `consult --prompt` for one model or `panel --mode ask --prompt` for a bounded multi-model comparison. Write temporary prompt files outside the repo and pass `--prompt-file` when useful.
8. Read the helper output and synthesize it with native Codex analysis. Call out disagreements, caveats, and what was or was not live-verified.

## Operational Fallbacks

- If `smoke` fails because `Gateway /v1/models` is unreachable but accounts/models otherwise look configured, run `start --port 51122`, rerun `smoke`, then proceed when the gateway is reachable and the requested model is listed.
- A Codex `config.toml` provider failure in `smoke` is only a blocker when the user asked to make Antigravity the active Codex backend. It is not a blocker for sidecar `consult`, `plan`, or `review` calls through this helper.
- `review --scope working-tree` and `review --scope staged` require a git repository. If the workspace is not a git repo, switch to `review --scope files` and pass a curated list of high-risk files.
- For large Opus/Sonnet reviews and plans, prefer focused batches. With `--chunked auto`, Claude-family calls use a conservative safety budget of about 30k prompt chars by default, splitting broad work into bounded chunk calls plus synthesis so one huge request is less likely to time out or lose auth rotation progress. `--max-prompt-chars 0` does not bypass that Claude safety budget; use `--chunked off` only when you intentionally want one large call.
- For large Opus reviews, add `--timeout 240`, `--retry 2`, and realistic `--max-prompt-chars` / `--max-synthesis-chars` budgets when needed. `review` defaults to `--chunked auto`, so incomplete broad prompts are split into bounded chunk calls and a bounded synthesis call. The helper emits a review manifest with included, omitted, excluded, and warning fields; treat `status: incomplete` as a scope limitation and rerun a narrower batch when missing files matter.
- If a broad review times out, do not keep retrying the same prompt. Narrow to the files most likely to contain the bug, or split by concern such as config, scanner, verifier, report, and tests.
- Use `--files-from` with newline- or NUL-delimited file lists for large PRs. Prefer NUL-delimited lists from `git diff -z --name-only` when paths may contain spaces.
- Path lists must be valid UTF-8. Generate them from git or another trusted local command rather than hand-editing binary path lists.
- Use `--json` when a release workflow needs to separate helper caveats, chunk metadata, and model output.
- Use `panel --json` when you need model-by-model success/error metadata, usage/latency, panel caveats, omitted files, structured findings, and judge synthesis in separate fields.
- `panel --collab claude-grok` sends the same bounded context to Claude and Grok lanes, asks them to lean into complementary strengths, and asks the judge to compare Claude-backed and Grok-backed disagreements. It is still advisory, not automatic collaboration in Codex's native model loop.
- Broad `panel --mode review` runs summarize oversized review scopes before fan-out instead of silently truncating raw context for every lane. Treat the summary caveat as a scope limitation.
- Use `--fallback-model sonnet --fallback-policy on-retryable` for long Opus planning/review calls when backend `502`/timeout drift would otherwise block the workflow.
- A provider fallback is always explicit. For example, `--fallback-model deepseek-v4-flash --fallback-policy on-retryable` may send the same prompt/context to DeepSeek; use it only when that disclosure and trust boundary are acceptable.
- After retryable generation failures, the helper probes `/v1/models`; if that probe also times out, treat the gateway as wedged and restart it before retrying the same Opus job.
- Use `--progress` for long `workflow`, `plan`, `review`, or `panel` runs so stderr shows which model/chunk is active.
- V2 workflow presets default to sanitized run summaries under `~/.codex/anti-runs`; use `runs list`, `runs show <id>`, and `runs clean --older-than N` (add `--dry-run` to preview deletions) to inspect or prune them. Primitive commands default to `--save-output never`; pass `--save-output summary` or `--save-output full` only when useful.
- Treat sidecar and panel findings as leads. Consensus is not proof. Before editing, verify actionable claims with local source inspection, official docs when relevant, typecheck/tests, or a small reproducer; record dubious or unverified claims as caveats instead of patching them blindly.

## Safety

- Do not include secrets, OAuth material, provider keys, key files, `.env` files, encrypted account/provider stores, or credential JSON in review prompts.
- The helper excludes common secret/cached/binary paths by default. If it reports exclusions, mention that scope caveat.
- Do not run `setup`, `setup-google`, or `configure-codex` unless the user explicitly asks for setup/configuration.
- Prefer `--api-key-env` workflows in the underlying `codex-antigravity` CLI; do not put provider keys into chat, shell history, notes, or prompt files.
- If a panel includes BYOK `provider:model` lanes and repo/diff/file context, the helper prints a BYOK disclosure and records it in the run caveats. Treat that as an explicit reminder that code context is leaving the Google Antigravity lane for the named provider.
- If the gateway is remote, use `--gateway-token-env` rather than passing bearer tokens in argv.
- Do not use panel mode as an always-on background swarm. Keep model counts, roles, tokens, retries, and scope bounded.
- Run ledgers are sanitized, but avoid `--save-output full` for prompts that may contain credentials, OAuth material, `.env` content, or private account/provider stores.
- Helper workflows remain advisory. They do not create true Codex subagents, gateway virtual `panel:*`, `moa:*`, or `fusion:*` picker models, automatic code edits, recursive swarms, or background always-on model calls.

## Output Shape

When answering the user after an Antigravity run:

- Start with the native Codex conclusion.
- Include an `Antigravity` paragraph with the model, scope, and useful findings or planning choices.
- For panel runs, include the panel models, judge model, collaboration profile when present, scope, disagreements, failed models, and caveats. Make clear that native Codex/local verification still owns the final decision.
- Separate local proof, live gateway proof, CI proof, and non-claims.
- For plans, convert the Antigravity plan into a concise execution-ready plan, preserving useful phase/checkpoint structure while removing unsupported claims.
