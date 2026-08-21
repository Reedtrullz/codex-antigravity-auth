---
name: anti
description: Use the optional Anti helper after Antigravity Claude Opus/Sonnet is available in Codex: sidecar review, consult lane, deep autonomous work plan, official DeepSeek V4, $anti workflow preset, or $anti panel MoA/Fusion workflow. Trigger when the user writes $anti, @anti, $anti workflow, $anti workflow review-ready, $anti workflow plan-deep, $anti panel, $anti moa, $anti fusion, asks for Antigravity, Opus, Sonnet, DeepSeek V4, a sidecar review, second-opinion model review, multi-model panel, MoA, Fusion, deep work plan, long autonomous session plan, implementation plan, gateway smoke checks, Google Antigravity setup, Codex Antigravity configuration, or codex-antigravity doctor/start workflows.
---

# Anti

Use this skill to ask the local `codex-antigravity-auth` gateway for an external Antigravity review, consult, deep work plan, named workflow preset, or bounded multi-model panel while native Codex remains the primary agent.

V3's primary product is native Claude in Codex through `codex-antigravity setup`; `$anti` is an optional helper for review and planning after the gateway and Codex model picker are already working.

## Core Rule

Treat Antigravity output as a second opinion. Run the helper, read the result, then synthesize it with your own analysis before answering the user. Do not blindly forward the Antigravity result as final truth.

Literal `@anti` is a text convention in v1, not a guaranteed app-level mention chip. `$anti` is the reliable explicit skill invocation.

Panel, MoA, and Fusion workflows are advisory only. The helper can fan out to multiple gateway-advertised models and ask a judge model to synthesize their views, but Codex remains the acting agent and must verify findings before editing. Structured panel findings include a `verify` hint; run or inspect that local check before acting on the claim.

## Models

- Use `opus` for deep review. It maps to `claude-opus-4-6-thinking` (the `claude-opus-4-6` name remains a compatibility alias).
- Use `sonnet` for faster focused consults. It maps to `claude-sonnet-4-6` (the `claude-3.5-sonnet` name remains a compatibility alias).
- Use `deepseek-v4-pro` for `deepseek:deepseek-v4-pro` and `deepseek-v4-flash` for `deepseek:deepseek-v4-flash` through the official DeepSeek API key.
- Use `flash-3.7` or `flash-high` for `gemini-3.7-flash` (current Gemini Flash generation). Fast agent-tuned reasoning, 1M context.
- Use `flash` or `flash-medium` for `gemini-3.5-flash-medium` (Gemini Flash General). Balanced speed and quality, 1M context.
- Use `flash-3.6` for `gemini-3.6-flash-high` and `flash-3.6-medium` for `gemini-3.6-flash-medium` (newer Flash line; more token-efficient than 3.5).
- Use `gemini-pro` for `gemini-3.1-pro` (Gemini Pro). Deep reasoning and analysis, 1M context.
- Use `gpt-oss-120b` for `gpt-oss-120b-medium` (text-only, 131K context).
- Use `gemini-3.1-flash-image` for image generation; it is image-only and does not support tools.
- Use `nemotron-super` for `openrouter:nvidia/nemotron-3-super-120b-a12b:free` (120B MoE, 262K ctx). Fast, good for second opinions.
- Use `nemotron-ultra` for `openrouter:nvidia/nemotron-3-ultra-550b-a55b:free` (550B, 1M ctx, vendor-reported). Large-context analysis and planning.
- Use `nemotron-omni` for `openrouter:nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free` (30B MoE, 256K ctx, reasoning-capable, vision). Good for image tasks and reasoning.
- Use `nemotron-vl` for `openrouter:nvidia/nemotron-nano-12b-v2-vl:free` (12B, 128K ctx, vision). Fast and reliable for image understanding — the default vision sidecar model.
- Use `free` for `openrouter/free` (auto-selects the best available free model on OpenRouter). Good for quick checks when you want zero-cost and don't care which model answers.
- Use `poolside` for `openrouter:poolside/laguna-s-2.1:free`. Coding-focused model for code generation and refactoring.
- Use `gemma-4` for `openrouter:google/gemma-4-31b-it:free` (30.7B dense, 262K ctx, vision). Lightweight, fast for simple consults and image tasks.
- Use `gpt-oss` for `ollama:gpt-oss:20b` (local). Private, offline inference.
- Use `qwen3` for `ollama:qwen3:8b` (local). Private, offline inference.
- Default review model: `opus`.
- Default plan model: `opus`.
- Default consult/ask model: `sonnet`, unless the user asks for deep review.
- Default panel models: `sonnet` and `opus`.
- Default panel judge: `opus`.
- **Gateway advertisement is required.** Every model below (including DeepSeek,
Nemotron, Poolside, and Gemma routes) must be advertised by the
gateway's `/v1/models` before a call can succeed. `smoke --check-documented`
diffs this documented table against the live catalog and reports drift
(documented-but-unadvertised ids, plus double-prefixed `openrouter:openrouter/…`
catalog ids that upstream rejects). The helper also fuzzy-matches aliases
against the catalog (e.g. `openrouter:x` ≡ `openrouter:openrouter/x`) and
suggests the closest advertised id when a requested model is missing, so
documented-but-drifted models fail with an actionable message instead of a
confusing two-layer error.

BYOK providers use the gateway's OpenAI Chat Completions adapter. Native Responses streaming, structured output, tool-call, usage, and model-identity fidelity are not claimed until successful live probes prove them. Opus remains the default judge for DeepSeek advisory lanes.

### Model capabilities and cost tiers

The helper tracks per-model capabilities and cost tiers to make cost-aware decisions. When Opus/Sonnet quota is limited, prefer free models for simple tasks.

| Model | Alias | Cost | Context | Images | Video | Audio | Tools | Quality |
|---|---|---|---|---|---|---|---|---|
| `claude-opus-4-6-thinking` | `opus` | quota | 250K | yes | no | no | yes | 100 |
| `gemini-3.1-pro` | `gemini-pro` | quota | 1M | yes | yes | yes | yes | 90 |
| `claude-sonnet-4-6` | `sonnet` | quota | 250K | yes | no | no | yes | 85 |
| `gemini-3.7-flash` | `flash-3.7`, `flash-high` | quota | 1M | yes | yes | yes | yes | 85 |
| `gemini-3.1-flash-image` | — | quota | 1M | yes (generation) | no | no | no | 50 |
| `gpt-oss-120b-medium` | `gpt-oss-120b` | quota | 131K | no | no | no | no | 65 |
| `gemini-3.6-flash-high` | `flash-3.6` | quota | 1M | yes | yes | yes | yes | 82 |
| `gemini-3.5-flash-high` | retired alias | quota | 1M | yes | yes | yes | yes | 80 |
| `gemini-3.6-flash-medium` | `flash-3.6-medium` | quota | 1M | yes | yes | yes | yes | 70 |
| `openrouter:nvidia/nemotron-3-ultra-550b-a55b:free` | `nemotron-ultra` | free | 1M | no | no | no | yes | 70 |
| `gemini-3.5-flash-medium` | `flash` | quota | 1M | yes | yes | yes | yes | 68 |
| `openrouter:nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free` | `nemotron-omni` | free | 256K | yes | no | no | yes | 65 |
| `openrouter:nvidia/nemotron-3-super-120b-a12b:free` | `nemotron-super` | free | 262K | no | no | no | yes | 65 |
| `openrouter:nvidia/nemotron-nano-12b-v2-vl:free` | `nemotron-vl` | free | 128K | yes | no | no | yes | 60 |
| `openrouter:poolside/laguna-s-2.1:free` | `poolside` | free | 128K | no | no | no | yes | 60 |
| `openrouter:google/gemma-4-31b-it:free` | `gemma-4` | free | 262K | yes | no | no | yes | 55 |
| `deepseek:deepseek-v4-pro` | `deepseek-v4-pro` | paid | — | no | no | no | yes | 88 |
| `deepseek:deepseek-v4-flash` | `deepseek-v4-flash` | paid | — | no | no | no | yes | 74 |
| `ollama:gpt-oss:20b` | `gpt-oss` | free | 128K | no | no | no | yes | 50 |
| `ollama:qwen3:8b` | `qwen3` | free | 128K | no | no | no | yes | 40 |

Table rows are *documented routes*, not guarantees: every row (except Ollama
local) requires the gateway to advertise its id in `/v1/models`. Quality ranks
are planning heuristics; observed output-cap failures or unavailable routes
can be checked live with `smoke --check-documented` and do not downgrade the
table itself. Context figures are vendor-reported and were not live-verified
against OpenRouter (these routes are not currently runnable through this
gateway); re-verify the OpenRouter spec when a route is enabled.

**Cost tiers:**
- `free` — No metering. OpenRouter free tier and Ollama local.
- `quota` — Google Antigravity quota, shared across 4 accounts. Opus and Sonnet share this pool.
- `paid` — Metered billing (not currently in rotation).

**Cost-aware selection strategy:**
- When Opus quota is low, use `nemotron-ultra` (70 quality, free, 1M) for broad scans and planning.
- For quick consults, prefer `flash-3.6` (82 quality, quota, 1M, more efficient than 3.5).
- For code review, prefer `poolside` (60 quality, free, coding-focused) first, then fall back to quota models.
- For image/video/audio tasks, Gemini and Claude families support full multimodal. Free OpenRouter vision models (nemotron-vl, nemotron-omni, gemma-4) also support images, making them cost-effective for image tasks when Gemini/Claude quota is low. For video/audio, only Gemini models support those modalities.
- Gemini 3.6 Flash is more token-efficient than 3.5 Flash (17% fewer output tokens) at a lower cost. Prefer it over 3.5 Flash for new workflows.
- The helper's `cheapest_models_for_task()` function automates this: it filters by capability requirements, then sorts free models first, then by quality.

### Choosing complementary reviewer lanes

- DeepSeek V4 Flash is for a fast code second opinion, debugging, and an explicitly selected retryable fallback. It is never an automatic cross-provider fallback.
- DeepSeek V4 Pro is for correctness, security, architecture, and deep code review. Treat it as unproven until the live V4 Pro generation, structured-output, and tool-loop gate passes.
- Gemini Flash models are free, fast, and good for quick sanity checks and lightweight consults. They use the same Google Antigravity backend as Claude.
- Gemini Pro is for deep reasoning when you want a Gemini perspective on architecture or complex logic.
- Nemotron Ultra (550B, 1M context) is the largest free model available; use it for broad codebase scans and long-document analysis.
- Nemotron Super (120B MoE) is a fast second opinion when you want a non-Claude perspective.
- Poolside is coding-focused; use it for code generation, refactoring suggestions, and implementation alternatives.
- Gemma 4 is lightweight and fast; use it for simple consults where latency matters more than depth.
- Ollama models run locally; use them when you need privacy or offline inference, but expect lower quality than cloud models.
Repository context leaves the Google Antigravity lane only after explicit selection and the existing BYOK disclosure. Opus remains the default judge; native Codex remains the acting agent and must verify advisory output locally.

## Helper

Use `scripts/anti.py` from this skill:

```bash
python3 ~/.codex/skills/anti/scripts/anti.py --help
```

Common commands:

```bash
python3 ~/.codex/skills/anti/scripts/anti.py smoke
python3 ~/.codex/skills/anti/scripts/anti.py smoke --check-documented
python3 ~/.codex/skills/anti/scripts/anti.py consult --model sonnet --prompt "Review this idea" --dry-run
python3 ~/.codex/skills/anti/scripts/anti.py consult --model deepseek-v4-flash --prompt "Give a fast second opinion"
python3 ~/.codex/skills/anti/scripts/anti.py consult --model flash-high --prompt "Quick sanity check on this approach"
python3 ~/.codex/skills/anti/scripts/anti.py consult --model gemini-pro --prompt "Deep analysis of this architecture"
python3 ~/.codex/skills/anti/scripts/anti.py consult --model nemotron-ultra --prompt "Review this large codebase change"
python3 ~/.codex/skills/anti/scripts/anti.py consult --model poolside --prompt "Suggest refactoring for this function"
python3 ~/.codex/skills/anti/scripts/anti.py panel --mode review --scope staged --model sonnet --model opus --model flash-high --judge opus
python3 ~/.codex/skills/anti/scripts/anti.py plan --prompt "Plan a long autonomous hardening pass"
python3 ~/.codex/skills/anti/scripts/anti.py plan --scope working-tree --prompt "Plan the next PR"
python3 ~/.codex/skills/anti/scripts/anti.py panel --mode review --scope staged
python3 ~/.codex/skills/anti/scripts/anti.py panel --mode review --scope diff --base origin/main --model sonnet --model opus --judge opus
python3 ~/.codex/skills/anti/scripts/anti.py panel --mode plan --scope working-tree --prompt "Plan this PR"
python3 ~/.codex/skills/anti/scripts/anti.py panel --mode ask --model sonnet --model openrouter:deepseek/deepseek-chat --judge opus --prompt "Compare these approaches"
python3 ~/.codex/skills/anti/scripts/anti.py moa --mode review --role correctness --role security --role tests --output findings
python3 ~/.codex/skills/anti/scripts/anti.py fusion --mode plan --model opus --model sonnet --judge opus --scope working-tree --prompt "Plan this change"
python3 ~/.codex/skills/anti/scripts/anti.py workflow review-ready --scope staged
python3 ~/.codex/skills/anti/scripts/anti.py workflow plan-deep --scope working-tree --prompt "Plan V2" --progress
python3 ~/.codex/skills/anti/scripts/anti.py workflow ship-gate --scope diff --base origin/main --json
python3 ~/.codex/skills/anti/scripts/anti.py workflow provider-compare --model sonnet --model openrouter:deepseek/deepseek-chat --prompt "Compare these approaches"
python3 ~/.codex/skills/anti/scripts/anti.py workflow provider-compare --model deepseek-v4-pro --model sonnet --prompt "Compare repository planning approaches"
python3 ~/.codex/skills/anti/scripts/anti.py workflow security-review --scope staged --output findings
python3 ~/.codex/skills/anti/scripts/anti.py workflow quick-check --scope staged
python3 ~/.codex/skills/anti/scripts/anti.py workflow consensus --scope staged --prompt "Review for bugs"
python3 ~/.codex/skills/anti/scripts/anti.py workflow debug-consensus --prompt "Intermittent 502s after rotation"
python3 ~/.codex/skills/anti/scripts/anti.py runs list
python3 ~/.codex/skills/anti/scripts/anti.py review --model opus --scope working-tree
python3 ~/.codex/skills/anti/scripts/anti.py review --model sonnet --scope staged --file path/to/file.py
python3 ~/.codex/skills/anti/scripts/anti.py review --model deepseek-v4-pro --scope staged
python3 ~/.codex/skills/anti/scripts/anti.py review --model opus --scope files --timeout 240 --max-prompt-chars 120000 --file src/main.ts --file src/config.ts
python3 ~/.codex/skills/anti/scripts/anti.py review --model opus --scope diff --base origin/main
python3 ~/.codex/skills/anti/scripts/anti.py review --model opus --scope files --files-from /tmp/anti-files.zlist --max-review-chunks 0 --priority-file src/prices.ts --priority-file src/scanner.ts
python3 ~/.codex/skills/anti/scripts/anti.py review --model opus --scope files --files-from /tmp/anti-files.zlist --max-review-chunks 10 --allow-partial --json
python3 ~/.codex/skills/anti/scripts/anti.py consult --model gemini-3.6-flash-high --prompt "Explain this" --max-output-tokens 8192 --save-output summary
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
4. For multi-model review or planning, use `panel --mode review` or `panel --mode plan`. Use `--role` for lenses such as correctness, security, tests, protocol, or UX. Use `--output findings` when you want machine-readable `id`, `claim`, `severity`, `lanes`, `verify`, `confidence`, `file`, `line`, `evidence`, and `fingerprint` fields. Cross-lane dedup is automatic by fingerprint. Use BYOK `provider:model` ids only when `/v1/models` advertises them.
5. For common helper flows, prefer named workflow presets: `workflow review-ready` before commit/PR review, `workflow plan-deep` for long autonomous planning, `workflow ship-gate` for merge readiness, `workflow security-review` for injection/secrets/authz/dependency lenses, `workflow debug-consensus` for ranked hypotheses plus discriminating tests, and `workflow provider-compare` for BYOK/provider lane comparisons,
`workflow quick-check` for fast free-model pre-commit gates (60s budget), and
`workflow consensus` for disagreement-focused 3-model panels with min-successes 2.
6. For code review, prefer `review --scope staged`, `workflow review-ready --scope staged`, or `panel --mode review --scope staged` when the user asks about commit readiness; use `review --scope working-tree` for current local changes and `review --scope diff --base origin/main` for a clean merge-candidate branch.
7. For focused questions, use `consult --prompt` for one model or `panel --mode ask --prompt` for a bounded multi-model comparison. Write temporary prompt files outside the repo and pass `--prompt-file` when useful.
8. Read the helper output and synthesize it with native Codex analysis. Call out disagreements, caveats, and what was or was not live-verified.

## Operational Fallbacks

- If `smoke` fails because `Gateway /v1/models` is unreachable but accounts/models otherwise look configured, run `start --port 51122`, rerun `smoke`, then proceed when the gateway is reachable and the requested model is listed.
- A Codex `config.toml` provider failure in `smoke` is only a blocker when the user asked to make Antigravity the active Codex backend. It is not a blocker for sidecar `consult`, `plan`, or `review` calls through this helper.
- `review --scope working-tree` and `review --scope staged` require a git repository. If the workspace is not a git repo, switch to `review --scope files` and pass a curated list of high-risk files.
- For large Opus/Sonnet reviews and plans, prefer focused batches. With `--chunked auto`, Claude-family calls use a conservative safety budget of about 30k prompt chars by default, splitting broad work into bounded chunk calls plus synthesis so one huge request is less likely to time out or lose auth rotation progress. `--max-prompt-chars 0` does not bypass that Claude safety budget; use `--chunked off` only when you intentionally want one large call.
- For large Opus reviews, add `--timeout 240`, `--retry 2`, and realistic `--max-prompt-chars` / `--max-synthesis-chars` budgets when needed. `review` defaults to `--chunked auto`, so incomplete broad prompts are split into bounded chunk calls and a bounded synthesis call. The helper emits a review manifest with included, omitted, excluded, and warning fields; treat `status: incomplete` as a scope limitation and rerun a narrower batch when missing files matter.
- Scope honesty is enforced, not just reported: if a review scope needs more chunks than `--max-review-chunks` (default 8), the helper prints the chunk plan and fails before any model call unless `--allow-partial` is passed. Use `--max-review-chunks 0` to review everything in as many chunks as needed. Partial runs prefix the synthesis with `⚠ INCOMPLETE — N item(s) NOT reviewed`, and the full diff is always split across chunks instead of silently truncated before chunking.
- Use `--priority-file <path>` (repeatable) to force important files into the first chunks of a broad `--files-from` review; `--dry-run` prints the full chunk plan so you can check coverage before spending quota.
- Consult answers are checked against the output-token cap: a truncated answer is retried once at double the cap and recorded as `truncated` with the full output saved in the run record. Raise `--max-output-tokens` (default 4096) for long answers.
- Run records now split lifecycle from coverage: `runStatus` (success/failed/interrupted) and `scopeStatus` (complete/partial) are separate top-level fields, with `omittedFileCount`/`omittedChunkCount` always present. A `running` placeholder record is written before the first model call, so killed backgrounded runs leave an identifiable record instead of a 0-byte file; `runs list` flags 0-byte/corrupt records, and `runs clean` also prunes stale `.tmp` files. Backgrounding is still unsupported — use a foreground run or the workflow presets.
- If a broad review times out, do not keep retrying the same prompt. Narrow to the files most likely to contain the bug, or split by concern such as config, scanner, verifier, report, and tests.
- Use `--files-from` with newline- or NUL-delimited file lists for large PRs. Prefer NUL-delimited lists from `git diff -z --name-only` when paths may contain spaces.
- Path lists must be valid UTF-8. Generate them from git or another trusted local command rather than hand-editing binary path lists.
- Use `--json` when a release workflow needs to separate helper caveats, chunk metadata, and model output.
- Use `panel --json` when you need model-by-model success/error metadata, usage/latency, panel caveats, omitted files, structured findings, and judge synthesis in separate fields.
- Panel lane outcomes are reported honestly: `success`, `truncated` (hit the output-token cap), `non_answer`, or `empty`, with one bounded retry before a lane is recorded in `failed_models`/`truncated_models`. Truncated lanes still count as usable, but `--min-successes` is satisfied by distinct actual provider/model identities, not repeated logical lanes that collapsed onto one fallback. Every lane keeps requested and actual identity separately (`requestedModel`, `actualModel`, `provider`, `fallbackChain`, `primaryError`, and `fallbackReason`).
- Panel integrity is explicit: `complete_multi_model`, `partial_multi_model`, `degraded_single_model`, or `failed`. A collapsed panel is never presented as independent consensus; the judge receives the status, distinct actual identities, per-lane independence flags, and original primary/fallback errors.
- If distinct actual identities fall below `--min-successes`, the panel fails closed before judging; `--json` and `--output findings` still emit the lane/fallback evidence and panel status with a non-zero exit code.
- `panel --output findings` and `panel --json` keep a stable schema: the findings contract always includes `summary`, `disagreements`, `findings`, `unverifiable`, `recommended_next_actions`, `caveats`, `parse_warning`, `findings_total`, and `findings_dropped`. When judge JSON is malformed or truncated, the helper repairs it, retries once with a stricter JSON-only instruction, and only then falls back to prose with a `parse_warning`; it never embeds a broken JSON blob inside `summary`.
- Broad `panel --mode review` runs summarize oversized review scopes before fan-out instead of silently truncating raw context for every lane. Treat the summary caveat as a scope limitation.
- `review` with an empty scope (for example `--scope staged` with nothing staged, or a clean working tree) fails with an actionable error before any model call instead of asking the model to explain "no content".
- Use `--fallback-model sonnet --fallback-policy on-retryable` for long Opus planning/review calls when backend `502`/timeout drift would otherwise block the workflow.
- A provider fallback is always explicit. For example, `--fallback-model deepseek-v4-flash --fallback-policy on-retryable` may send the same prompt/context to DeepSeek; use it only when that disclosure and trust boundary are acceptable.
- `/v1/models` is a catalog/readiness hint, not proof that a model can generate. Generation failures remain attached to the requested lane; use the bounded panel generation call (or an explicit smoke/probe workflow when available) to establish live readiness. After retryable generation failures, the helper probes `/v1/models`; if that probe also times out, treat the gateway as wedged and restart it before retrying the same Opus job.
- `--progress` is enabled by default for all `workflow`, `plan`, `review`, `consult`, `panel`, `moa`, and `fusion` runs, streaming real-time `[anti]` step milestones (model call starts, completed prompt/output char counts, elapsed time, chunk progress, and judge synthesis) directly to stderr for live visibility in Codex. Use `--no-progress` to suppress stderr progress logging if quiet output is explicitly required.
- V2 workflow presets default to sanitized run summaries under `~/.codex/anti-runs`; use `runs list`, `runs show <id>`, and `runs clean --older-than N` (add `--dry-run` to preview deletions) to inspect or prune them. Primitive commands default to `--save-output never`; pass `--save-output summary` or `--save-output full` only when useful.
- The helper emits a cost-awareness hint to stderr when a quota/paid-tier model is selected and free alternatives of similar quality are available. Use `--model <free-alias>` to switch.
- `--dry-run` prints token estimates and cost tiers without contacting the gateway. Available on `consult`, `review`, `plan`, `panel`, and `workflow` commands.
- Treat sidecar and panel findings as leads. Consensus is not proof. Before editing, verify actionable claims with local source inspection, official docs when relevant, typecheck/tests, or a small reproducer; record dubious or unverified claims as caveats instead of patching them blindly.

## New Flags

- `--auto-route` — Automatically pick the cheapest adequate model based on diff size and file risk. Small diffs use flash-3.6, medium use sonnet, large or high-risk files use opus. Only activates when `--model` is not explicitly passed.
- `--budget <cost>` — Maximum estimated cost for a run. Skips remaining panel lanes when the cap is exceeded. Cost is in arbitrary units (not real USD), tracked per lane with estimated vs actual.
- `--no-verify` — Skip evidence-linked verification of findings (syntax, secrets, eslint checks on referenced files).
- `--no-anonymize` — Preserve original model names and lane order in judge synthesis (default: anonymize and shuffle).

## Agent Execution Pattern

**anti.py runs synchronously.** Every command (`consult`, `review`, `plan`, `panel`, `workflow`) blocks until the API response arrives and prints the result directly to stdout. There is no background mode and no separate output file to poll.

### Correct pattern for Codex agents

```bash
# Run anti.py directly — it blocks until complete
exec_command(
  cmd='python3 ~/.codex/skills/anti/scripts/anti.py consult --model sonnet --prompt "Review this"',
  yield_time_ms=120000  # 2 minutes for consults; 300s for panels/reviews
)
# Read the result from stdout — no file polling needed
```

### What NOT to do

1. **Do NOT background the process** and poll for output files in `~/.codex/anti-runs/`. The `--save-output` flag writes a run record for auditing, not for primary result retrieval.
2. **Do NOT use `sleep N && cat ...` polling loops.** If `exec_command` times out, the process is still running — use `write_stdin` with the session_id or check `ps aux | grep anti.py` to verify, then decide whether to wait longer or abort.
3. **Do NOT escalate sleep durations** (60s → 90s → 120s → ...) as a recovery strategy. After 2-3 failed waits, report the situation to the user.
4. **Do NOT assume output lands in a specific file path.** The run record path includes a timestamp that may not match a naive glob. stdout is the primary output channel.

### Timeout recovery

If `exec_command` times out: check whether the process is still running (`ps aux | grep anti.py`). If it is, wait once more with a longer `yield_time_ms`. If it has finished, the output was already returned. If it is stuck or the gateway is unreachable, abort and report to the user. Never loop more than 3 wait cycles on the same command.


## Findings Schema

Panel findings use an enriched schema with provenance and dedup:

- `id` — stable short identifier (e.g., F001)
- `claim` — specific claim about the code
- `severity` — critical, high, medium, low, or info
- `confidence` — float 0.0-1.0 indicating model certainty
- `file` / `line` — file path and line number when applicable
- `evidence` — concrete evidence (test output, type error) or "unverified"
- `verify` — a concrete local check Codex should run before acting
- `lanes` — array of model identities that support this finding
- `fingerprint` — sha256 hash for cross-lane dedup (same file+line+claim)

Cross-lane dedup is automatic: when multiple lanes produce findings with the same fingerprint, they are merged (lanes combined, highest severity kept, confidence averaged).

## Anonymized Panel Judging

By default, panel lane labels are anonymized and shuffled before the judge synthesizes. This reduces brand bias ("Opus said X" receiving unwarranted weight). The mapping is recorded in metadata for post-hoc audit.

- Use `--no-anonymize` to preserve original model names and lane order
- Anonymization only applies when there are 2+ lanes
- Single-lane or `--mode ask` panels are not anonymized

## Role-Specialized Prompts

When `--role` is passed, each panel lane receives a role-specific rubric appended to the prompt. Available roles:

| Role | Focus |
|------|-------|
| correctness | logic errors, edge cases, type mismatches |
| security | injection, secrets, authz, trust boundaries |
| tests | missing coverage, testable contracts, regression risk |
| performance | complexity, allocations, N+1, blocking I/O |
| ux | usability, accessibility, error messages |
| protocol | API contracts, schema validation, wire format |
| install-docs | install regressions, documentation accuracy |
| injection | prompt/SQL/command/template injection |
| secrets-handling | hardcoded secrets, credential leakage |
| authz | authorization bypass, privilege escalation |
| dependency-surface | vulnerable deps, version pinning |
| root-cause | identifying the most likely root cause |
| regression-risk | what existing functionality could break |
| discriminating-tests | cheapest tests to confirm/rule out hypotheses |

Use `--role` multiple times for different lenses: `--role security --role correctness --role tests`.

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
- Panel JSON keeps `panel_models` as requested lanes and records actual execution identity in each `panel_results` entry and in `metadata` (`panel_status`/`status`, `distinct_actual_models`, `successful_actual_models`, `judge_requested_model`, `judge_actual_model`, and fallback metadata). A judge fallback is disclosed separately from the requested judge.
- Separate local proof, live gateway proof, CI proof, and non-claims.
- For plans, convert the Antigravity plan into a concise execution-ready plan, preserving useful phase/checkpoint structure while removing unsupported claims.
