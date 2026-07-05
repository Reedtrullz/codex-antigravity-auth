---
name: anti
description: Use Antigravity Claude Opus/Sonnet as a sidecar reviewer, consult lane, or deep autonomous work planner from Codex. Trigger when the user writes $anti, @anti, asks for Antigravity, Opus, Sonnet, a sidecar review, second-opinion model review, deep work plan, long autonomous session plan, implementation plan, gateway smoke checks, Google Antigravity setup, Codex Antigravity configuration, or codex-antigravity doctor/start workflows.
---

# Anti

Use this skill to ask the local `codex-antigravity-auth` gateway for an external Antigravity review, consult, or deep work plan while native Codex remains the primary agent.

## Core Rule

Treat Antigravity output as a second opinion. Run the helper, read the result, then synthesize it with your own analysis before answering the user. Do not blindly forward the Antigravity result as final truth.

Literal `@anti` is a text convention in v1, not a guaranteed app-level mention chip. `$anti` is the reliable explicit skill invocation.

## Models

- Use `opus` for deep review. It maps to `claude-opus-4-6`.
- Use `sonnet` for faster focused consults. It maps to `claude-3.5-sonnet`.
- Default review model: `opus`.
- Default plan model: `opus`.
- Default consult/ask model: `sonnet`, unless the user asks for deep review.

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
python3 -m unittest discover -s ~/.codex/skills/anti/tests
```

## Workflow

1. Infer whether the user wants `consult`, `plan`, `review`, `smoke`, `start`, `setup-google`, `configure-codex`, or `doctor`.
2. Run `smoke` first when gateway/account readiness is uncertain. Default `smoke` is sidecar readiness; use `smoke --mode full` only when the user asked to make Antigravity the active Codex backend.
3. For deep autonomous work planning, use `plan --model opus`. Add `--scope working-tree`, `--scope staged`, or `--file` when the plan should account for current repo state.
4. For code review, prefer `review --scope staged` when the user asks about commit readiness, `review --scope working-tree` for current local changes, and `review --scope diff --base origin/main` for a clean merge-candidate branch.
5. For focused questions, use `consult --prompt` or write a temporary prompt file outside the repo and pass `--prompt-file`.
6. Read the helper output and synthesize it with native Codex analysis. Call out disagreements, caveats, and what was or was not live-verified.

## Operational Fallbacks

- If `smoke` fails because `Gateway /v1/models` is unreachable but accounts/models otherwise look configured, run `start --port 51122`, rerun `smoke`, then proceed when the gateway is reachable and the requested model is listed.
- A Codex `config.toml` provider failure in `smoke` is only a blocker when the user asked to make Antigravity the active Codex backend. It is not a blocker for sidecar `consult`, `plan`, or `review` calls through this helper.
- `review --scope working-tree` and `review --scope staged` require a git repository. If the workspace is not a git repo, switch to `review --scope files` and pass a curated list of high-risk files.
- For large Opus reviews, start with focused batches or add `--timeout 240`, `--retry 2`, and realistic `--max-prompt-chars` / `--max-synthesis-chars` budgets. `review` defaults to `--chunked auto`, so incomplete broad prompts are split into bounded chunk calls and a bounded synthesis call. The helper emits a review manifest with included, omitted, excluded, and warning fields; treat `status: incomplete` as a scope limitation and rerun a narrower batch when missing files matter.
- If a broad review times out, do not keep retrying the same prompt. Narrow to the files most likely to contain the bug, or split by concern such as config, scanner, verifier, report, and tests.
- Use `--files-from` with newline- or NUL-delimited file lists for large PRs. Prefer NUL-delimited lists from `git diff -z --name-only` when paths may contain spaces.
- Path lists must be valid UTF-8. Generate them from git or another trusted local command rather than hand-editing binary path lists.
- Use `--json` when a release workflow needs to separate helper caveats, chunk metadata, and model output.
- Treat sidecar findings as leads. Before editing, verify actionable claims with local source inspection, official docs when relevant, typecheck/tests, or a small reproducer; record dubious or unverified claims as caveats instead of patching them blindly.

## Safety

- Do not include secrets, OAuth material, provider keys, key files, `.env` files, encrypted account/provider stores, or credential JSON in review prompts.
- The helper excludes common secret/cached/binary paths by default. If it reports exclusions, mention that scope caveat.
- Do not run `setup-google` or `configure-codex` unless the user explicitly asks for setup/configuration.
- Prefer `--api-key-env` workflows in the underlying `codex-antigravity` CLI; do not put provider keys into chat, shell history, notes, or prompt files.
- If the gateway is remote, use `--gateway-token-env` rather than passing bearer tokens in argv.

## Output Shape

When answering the user after an Antigravity run:

- Start with the native Codex conclusion.
- Include an `Antigravity` paragraph with the model, scope, and useful findings or planning choices.
- Separate local proof, live gateway proof, CI proof, and non-claims.
- For plans, convert the Antigravity plan into a concise execution-ready plan, preserving useful phase/checkpoint structure while removing unsupported claims.
