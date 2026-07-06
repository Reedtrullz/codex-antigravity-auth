---
name: anti
description: Use the optional Anti helper after Antigravity Claude Opus/Sonnet is available in Codex: sidecar review, consult lane, deep autonomous work plan, $anti workflow preset, or $anti panel MoA/Fusion workflow. Trigger when the user writes $anti, @anti, $anti workflow, $anti workflow review-ready, $anti workflow plan-deep, $anti panel, $anti moa, $anti fusion, asks for Antigravity, Opus, Sonnet, a sidecar review, second-opinion model review, multi-model panel, MoA, Fusion, deep work plan, long autonomous session plan, implementation plan, gateway smoke checks, Google Antigravity setup, Codex Antigravity configuration, or codex-antigravity doctor/start workflows.
---

# Anti

Use this skill to ask the local `codex-antigravity-auth` gateway for an external Antigravity review, consult, deep work plan, named workflow preset, or bounded multi-model panel while native Codex remains the primary agent.

V3's primary product is native Claude in Codex through `codex-antigravity setup`; `$anti` is an optional helper for review and planning after the gateway and Codex model picker are already working.

## Core Rule

Treat Antigravity output as a second opinion. Run the helper, read the result, then synthesize it with your own analysis before answering the user. Do not blindly forward the Antigravity result as final truth.

Literal `@anti` is a text convention in v1, not a guaranteed app-level mention chip. `$anti` is the reliable explicit skill invocation.

Panel, MoA, and Fusion workflows are advisory only. The helper can fan out to multiple gateway-advertised models and ask a judge model to synthesize their views, but Codex remains the acting agent and must verify findings before editing.

## Models

- Use `opus` for deep review. It maps to `claude-opus-4-6`.
- Use `sonnet` for faster focused consults. It maps to `claude-3.5-sonnet`.
- Default review model: `opus`.
- Default plan model: `opus`.
- Default consult/ask model: `sonnet`, unless the user asks for deep review.
- Default panel models: `sonnet` and `opus`.
- Default panel judge: `opus`.

## Helper

Use `scripts/anti.py` from this skill:

```bash
python3 ~/.codex/skills/anti/scripts/anti.py --help
```

Common commands:

```bash
python3 ~/.codex/skills/anti/scripts/anti.py smoke
python3 ~/.codex/skills/anti/scripts/anti.py consult --model sonnet --prompt "Review this idea"
python3 ~/.codex/skills/anti/scripts/anti.py plan --prompt "Plan a long autonomous hardening pass"
python3 ~/.codex/skills/anti/scripts/anti.py plan --scope working-tree --prompt "Plan the next PR"
python3 ~/.codex/skills/anti/scripts/anti.py panel --mode review --scope staged
python3 ~/.codex/skills/anti/scripts/anti.py panel --mode review --scope diff --base origin/main --model sonnet --model opus --judge opus
python3 ~/.codex/skills/anti/scripts/anti.py panel --mode plan --scope working-tree --prompt "Plan this PR"
python3 ~/.codex/skills/anti/scripts/anti.py panel --mode ask --model sonnet --model openrouter:deepseek/deepseek-chat --judge opus --prompt "Compare these approaches"
python3 ~/.codex/skills/anti/scripts/anti.py moa --mode review --role correctness --role security --role tests --json
python3 ~/.codex/skills/anti/scripts/anti.py workflow review-ready --scope staged
python3 ~/.codex/skills/anti/scripts/anti.py workflow plan-deep --scope working-tree --prompt "Plan V2" --progress
python3 ~/.codex/skills/anti/scripts/anti.py workflow ship-gate --scope diff --base origin/main --json
python3 ~/.codex/skills/anti/scripts/anti.py workflow provider-compare --model sonnet --model openrouter:deepseek/deepseek-chat --prompt "Compare these approaches"
python3 ~/.codex/skills/anti/scripts/anti.py runs list
python3 ~/.codex/skills/anti/scripts/anti.py review --model opus --scope working-tree
python3 ~/.codex/skills/anti/scripts/anti.py review --model sonnet --scope staged --file path/to/file.py
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
4. For multi-model review or planning, use `panel --mode review` or `panel --mode plan`. Use `--role` for lenses such as correctness, security, tests, protocol, or UX. Use BYOK `provider:model` ids only when `/v1/models` advertises them.
5. For common V2 flows, prefer named workflow presets: `workflow review-ready` before commit/PR review, `workflow plan-deep` for long autonomous planning, `workflow ship-gate` for merge readiness, and `workflow provider-compare` for BYOK/provider lane comparisons.
6. For code review, prefer `review --scope staged`, `workflow review-ready --scope staged`, or `panel --mode review --scope staged` when the user asks about commit readiness; use `review --scope working-tree` for current local changes and `review --scope diff --base origin/main` for a clean merge-candidate branch.
7. For focused questions, use `consult --prompt` for one model or `panel --mode ask --prompt` for a bounded multi-model comparison. Write temporary prompt files outside the repo and pass `--prompt-file` when useful.
8. Read the helper output and synthesize it with native Codex analysis. Call out disagreements, caveats, and what was or was not live-verified.

## Operational Fallbacks

- If `smoke` fails because `Gateway /v1/models` is unreachable but accounts/models otherwise look configured, run `start --port 51122`, rerun `smoke`, then proceed when the gateway is reachable and the requested model is listed.
- A Codex `config.toml` provider failure in `smoke` is only a blocker when the user asked to make Antigravity the active Codex backend. It is not a blocker for sidecar `consult`, `plan`, or `review` calls through this helper.
- `review --scope working-tree` and `review --scope staged` require a git repository. If the workspace is not a git repo, switch to `review --scope files` and pass a curated list of high-risk files.
- For large Opus reviews, start with focused batches or add `--timeout 240`, `--retry 2`, and realistic `--max-prompt-chars` / `--max-synthesis-chars` budgets. `review` defaults to `--chunked auto`, so incomplete broad prompts are split into bounded chunk calls and a bounded synthesis call. The helper emits a review manifest with included, omitted, excluded, and warning fields; treat `status: incomplete` as a scope limitation and rerun a narrower batch when missing files matter.
- If a broad review times out, do not keep retrying the same prompt. Narrow to the files most likely to contain the bug, or split by concern such as config, scanner, verifier, report, and tests.
- Use `--files-from` with newline- or NUL-delimited file lists for large PRs. Prefer NUL-delimited lists from `git diff -z --name-only` when paths may contain spaces.
- Path lists must be valid UTF-8. Generate them from git or another trusted local command rather than hand-editing binary path lists.
- Use `--json` when a release workflow needs to separate helper caveats, chunk metadata, and model output.
- Use `panel --json` when you need model-by-model success/error metadata, panel caveats, omitted files, and judge synthesis in separate fields.
- Use `--fallback-model sonnet --fallback-policy on-retryable` for long Opus planning/review calls when backend `502`/timeout drift would otherwise block the workflow.
- Use `--progress` for long `workflow`, `plan`, `review`, or `panel` runs so stderr shows which model/chunk is active.
- V2 workflow presets default to sanitized run summaries under `~/.codex/anti-runs`; use `runs list`, `runs show <id>`, and `runs clean --older-than N` (add `--dry-run` to preview deletions) to inspect or prune them. Primitive commands default to `--save-output never`; pass `--save-output summary` or `--save-output full` only when useful.
- Treat sidecar and panel findings as leads. Consensus is not proof. Before editing, verify actionable claims with local source inspection, official docs when relevant, typecheck/tests, or a small reproducer; record dubious or unverified claims as caveats instead of patching them blindly.

## Safety

- Do not include secrets, OAuth material, provider keys, key files, `.env` files, encrypted account/provider stores, or credential JSON in review prompts.
- The helper excludes common secret/cached/binary paths by default. If it reports exclusions, mention that scope caveat.
- Do not run `setup`, `setup-google`, or `configure-codex` unless the user explicitly asks for setup/configuration.
- Prefer `--api-key-env` workflows in the underlying `codex-antigravity` CLI; do not put provider keys into chat, shell history, notes, or prompt files.
- If the gateway is remote, use `--gateway-token-env` rather than passing bearer tokens in argv.
- Do not use panel mode as an always-on background swarm. Keep model counts, roles, tokens, retries, and scope bounded.
- Run ledgers are sanitized, but avoid `--save-output full` for prompts that may contain credentials, OAuth material, `.env` content, or private account/provider stores.
- V2/V3 helper workflows remain advisory. They do not create true Codex subagents, gateway virtual `panel:*` models, automatic code edits, recursive swarms, or background always-on model calls.

## Output Shape

When answering the user after an Antigravity run:

- Start with the native Codex conclusion.
- Include an `Antigravity` paragraph with the model, scope, and useful findings or planning choices.
- For panel runs, include the panel models, judge model, scope, disagreements, failed models, and caveats. Make clear that native Codex/local verification still owns the final decision.
- Separate local proof, live gateway proof, CI proof, and non-claims.
- For plans, convert the Antigravity plan into a concise execution-ready plan, preserving useful phase/checkpoint structure while removing unsupported claims.
