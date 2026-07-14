# Operationalize DeepSeek V4 and Dual-Route Grok Implementation Plan

> **For Codex:** Execute this plan sequentially with the Superpowers executing-plans workflow when that skill is available. Do not use subagents unless the user explicitly requests delegation. Stop at every authorization gate instead of inferring permission for runtime, credential, service, or paid-provider mutations.

**Goal:** Turn commit `5a28802343f85fc4ae597ea01e15ddddfed75a7a` from a package-correct but split-brained feature into a truthful, usable normal-service deployment for DeepSeek V4 and xAI OAuth Grok, while keeping BluesMinds explicitly degraded until its upstream billing/capacity failures are resolved.

**Architecture:** First fix the misleading Claude/Grok workflow and make gateway runtime identity observable. Then verify an exact wheel and bounded live provider behavior in an isolated process. Only after those gates pass, install that exact artifact into the Python runtime used by launchd and reinstall the service through the existing 1Password wrapper. DeepSeek receives its key through a private dotenv containing an `op://` reference; xAI continues to use its encrypted OAuth token store. BluesMinds remains unconfigured in the durable service until a separate live-compatibility gate passes.

**Tech stack:** Python 3.10+, FastAPI, httpx, pytest/unittest, setuptools/build, Twine, macOS launchd, 1Password CLI `op run --env-file`, curl, jq.

## Verified baseline

- Feature branch: `codex/bluesminds-deepseek-grok`.
- Feature commit: `5a28802343f85fc4ae597ea01e15ddddfed75a7a`.
- Source/package version: `1.7.0`.
- Full branch suite: `591` tests plus `218` subtests on Python 3.14.
- Normal gateway service: launchd label `com.codex-antigravity.gateway.51122`.
- Normal runtime: globally installed `codex-antigravity-auth==1.6.3` under `/Library/Frameworks/Python.framework/Versions/3.10`.
- Normal `/v1/models`: seven Claude/Gemini/Ollama models; all new DeepSeek/xAI/BluesMinds IDs are absent.
- Branch OAuth status: `xai-oauth` ready and refreshable.
- Branch provider list: no configured DeepSeek or BluesMinds provider.
- Live proof: DeepSeek V4 Flash passed JSON, SSE, automatic tool calling, and tool-output continuation; DeepSeek V4 Pro and xAI OAuth generation are not yet proven.
- BluesMinds: Grok 4.5 returned a billing error; GLM-5.2 returned an upstream `429`.
- `/Users/reidar/Projectos/.env` is a mode-600 FIFO, not a regular dotenv file.
- Installed 1Password CLI is `2.34.1`; it supports `op run --env-file` but not `op run --environment`.
- `~/.codex/config.toml` SHA-256 before execution: `57bf0d7e4d8cc08c847bcf097ea6d99cd9abd882298ff62cccef6024f060f560`.

## Completion states

| State | Required evidence |
|---|---|
| Code-ready | Workflow invariant, runtime-version signal, selection guidance, tests, package proof, local commit |
| Provider-ready | Bounded DeepSeek V4 Pro and xAI OAuth live probes pass from the exact branch artifact |
| Service-ready | LaunchAgent runs the exact artifact through 1Password, required IDs appear in the real catalog, and real-service Anti calls pass |
| BluesMinds-ready | Separate future billing/capacity, streaming, structured-output, tool-loop, usage, and identity proof |

The current implementation may be called complete for this follow-up only at **Service-ready with BluesMinds explicitly degraded**. Do not call BluesMinds operational until the fourth state is independently achieved.

## Global constraints

- Never edit `~/.codex/config.toml`; hash it before and after every runtime phase.
- Never print, persist, or place provider values in argv, shell history, logs, prompts, tests, Git, Obsidian, or the LaunchAgent plist.
- The only durable dotenv permitted by this plan contains `op://` references and has mode `0600`.
- Do not pass `/Users/reidar/Projectos/.env` to `--op-env-file`; it is a FIFO and the service validator correctly rejects it.
- Do not configure `BLUESMINDS_API_KEY` in the durable service during this plan.
- Do not add automatic cross-provider fallback.
- Native Codex remains the acting agent; Opus remains the default judge.
- Do not push, open a PR, merge, tag, publish, release, or deploy beyond the explicitly approved local LaunchAgent change.
- Before long test/build loops, run `df -h /System/Volumes/Data` and stop below `30 GiB` free.
- Use one bounded temporary directory with a cleanup trap for builds and live-probe artifacts.

---

### Task 1: Make stale gateway runtime identity visible

**Files:**

- Modify: `codex_antigravity_auth/server.py`
- Modify: `codex_antigravity_auth/skills/anti/scripts/anti.py`
- Modify: `tests/test_regressions.py`
- Modify: `codex_antigravity_auth/skills/anti/tests/test_anti.py`

**Contract:** `/health` exposes a sanitized `package_version`, and Anti smoke prints/returns it. The field is diagnostic only and must not trigger network update checks or write version-cache state.

- [ ] Add a failing server test asserting that `/health` contains the locally installed/source package version and no filesystem path, commit path, or credential data.
- [ ] Add a failing Anti test asserting that `smoke --json` records `gateway_package_version`, and prose smoke prints `Gateway package version: ...`.
- [ ] Implement a small package-version helper using local metadata with a safe `"unknown"` fallback.
- [ ] Fetch `/health` through the same base URL/token boundary used by smoke; a health failure should be an explicit smoke warning, while `/v1/models` remains the readiness authority.
- [ ] Run the focused tests:

  ```bash
  source .venv/bin/activate
  python3 -m pytest -q tests/test_regressions.py codex_antigravity_auth/skills/anti/tests/test_anti.py
  ```

- [ ] Confirm the existing 1.6.3 normal service would now be visibly distinguishable from the 1.7.0 branch without inspecting process internals.

### Task 2: Make `workflow claude-grok` fail closed on fake collaboration

**Files:**

- Modify: `codex_antigravity_auth/skills/anti/scripts/anti.py`
- Modify: `codex_antigravity_auth/skills/anti/tests/test_anti.py`

**Decision:** Custom `--model` values continue to replace the default workflow panel, but `workflow claude-grok` must contain at least one Claude reviewer and one Grok reviewer. A Grok-only panel remains available through the generic `panel` command. This avoids overloading `--model` with special replacement semantics.

- [ ] Add failing table-driven tests for:
  - no explicit models -> Sonnet, Opus, xAI OAuth Grok;
  - Sonnet + Grok -> accepted;
  - Sonnet + Opus + BluesMinds Grok -> accepted;
  - Grok only -> rejected with the correct three-model example;
  - Claude only -> rejected;
  - direct `panel --collab claude-grok --model ...` behavior remains unchanged.
- [ ] Add a helper that classifies resolved reviewer IDs narrowly: Claude IDs start with `claude-`; Grok IDs are the existing `xai-oauth:grok-*` or `bluesminds:grok-*` routes.
- [ ] Validate the reviewer set inside the named workflow before it calls `main(argv)`.
- [ ] Preserve Opus as judge; a judge does not satisfy the Claude-reviewer invariant.
- [ ] Prove the exact regression:

  ```bash
  source .venv/bin/activate
  python3 codex_antigravity_auth/skills/anti/scripts/anti.py \
    workflow claude-grok --model grok-bluesminds \
    --panel-mode ask --prompt x --print-prompt --json
  ```

  Expected: non-zero, sanitized guidance showing the explicit Sonnet + Opus + Grok form.

- [ ] Prove the corrected form expands to the three resolved reviewer IDs without contacting the gateway.

### Task 3: Add truthful model-selection and degradation guidance

**Files:**

- Modify: `codex_antigravity_auth/skills/anti/SKILL.md`
- Modify: `README.md`
- Modify: `USAGE.md`
- Modify: `tests/test_release_workflow.py`

**Guidance:**

- DeepSeek V4 Flash: fast code second opinion, debugging, and explicitly selected retryable fallback.
- DeepSeek V4 Pro: correctness, security, architecture, and deep code review—but describe it as unproven until Task 5 passes.
- xAI OAuth Grok: adversarial assumptions, runtime surprises, and product/UX blind spots.
- BluesMinds Grok/GLM: configured aliases, but unavailable/degraded until the provider live gate passes.
- Repository context goes to these providers only after explicit selection and the existing BYOK disclosure.

- [ ] Add a failing release-contract test requiring the guidance above.
- [ ] Add a failing test rejecting the misleading single-model workflow example from README, USAGE, and bundled SKILL.
- [ ] Replace every `workflow claude-grok --model grok-bluesminds ...` example with explicit Sonnet, Opus, and BluesMinds Grok reviewers.
- [ ] Label BluesMinds examples as conditional on `/v1/models` advertisement and provider health.
- [ ] Preserve the absence of GPT-5.4 and the no-fallback promise.
- [ ] Run:

  ```bash
  source .venv/bin/activate
  python3 -m pytest -q tests/test_release_workflow.py codex_antigravity_auth/skills/anti/tests/test_anti.py
  ```

### Task 4: Re-establish code and package readiness before any live mutation

**Files:** Verify the entire branch; no new implementation unless a gate fails.

- [ ] Check disk space and stop below 30 GiB.
- [ ] Run the full Python 3.14 suite, compileall, and diff hygiene:

  ```bash
  source .venv/bin/activate
  python3 -m pytest -q
  python3 -m compileall -q codex_antigravity_auth tests
  git diff --check
  ```

- [ ] Build in one bounded scratch directory:

  ```bash
  scratch=$(mktemp -d "${TMPDIR:-/tmp}/codex-antigravity-operationalize.XXXXXX")
  trap 'rm -rf "$scratch"' EXIT
  python3 -m build --outdir "$scratch/dist"
  python3 -m twine check "$scratch"/dist/*
  shasum -a 256 "$scratch"/dist/*
  ```

- [ ] Install the wheel into a clean Python 3.12 environment; run `pip check`, CLI help, provider presets, package-content inspection, and temporary-home `install-skill --verify`.
- [ ] Verify the wheel exposes `package_version`, BluesMinds preset data, xAI OAuth commands, and the corrected canonical Anti skill.
- [ ] Commit the code/docs correction locally before provider or service work. Suggested message:

  ```text
  fix: make provider lane readiness truthful
  ```

- [ ] Record the exact commit and wheel SHA-256. All later runtime proof must point to this artifact.

### Task 5: Prove DeepSeek V4 Pro and xAI OAuth in a temporary gateway

**Authorization gate:** This task performs real external model calls. Require explicit user approval immediately before execution. It must not change provider configuration, services, or Codex config. Check xAI token expiry first; if an OAuth refresh is required, stop for separate approval because refresh updates the encrypted token store.

**Files:** No repository changes unless a reproducible transport defect is found.

- [ ] Install the exact Task 4 wheel into a fresh bounded virtual environment, then start that wheel's gateway process on port `51123`; keep raw responses only inside the trapped scratch directory. Keep the real HOME/Codex home so the gateway can read the existing xAI OAuth store; do not start or alter the port-51122 service.
- [ ] Source the existing 1Password Developer Environment once, export only the canonical DeepSeek variable transiently, and unset both the canonical and custom environment names before shell exit. Never export the BluesMinds key under its canonical preset name and never print either value.
- [ ] Confirm the temporary `/v1/models` contains:
  - `deepseek:deepseek-v4-pro`;
  - `deepseek:deepseek-v4-flash`;
  - `xai-oauth:grok-build-0.1`;
  - `xai-oauth:grok-4.3`.
- [ ] Run bounded DeepSeek V4 Pro probes with one attempt and at most 512 output tokens each:
  - non-streaming identity and output text;
  - `json_object` structured output;
  - SSE with `response.completed` and `[DONE]`;
  - automatic tool call with valid JSON arguments;
  - `function_call_output` continuation returning output text.
- [ ] Run bounded xAI OAuth Grok Build probes:
  - OAuth status/refresh only if required;
  - non-streaming native Responses identity/output;
  - SSE terminal event and `[DONE]`;
  - tool call and continuation if the route advertises/supports tools.
- [ ] Classify each capability independently. A catalog success is not generation proof; a generation success is not tool-loop proof.
- [ ] If either route fails, do not deploy that route to the normal service. Capture sanitized HTTP/error classification and stop for diagnosis.
- [ ] Do not retry BluesMinds in this task.

### Task 6: Prepare a secret-safe durable-service credential bridge

**Authorization gate:** Creating a durable reference file changes external state. Require explicit user approval and a user-supplied/copied 1Password secret reference. Never discover it by printing a secret field.

**Files outside the repository:**

- Create only after approval: `~/.codex/antigravity-provider.env`

**Decision:** Use the stable supported `--op-env-file` path. Do not use the existing FIFO and do not upgrade 1Password CLI as part of this work.

- [ ] Require a regular mode-600 dotenv containing exactly two variables, each mapped to an operator-supplied `op://` reference:
  - `DEEPSEEK_API_KEY` supplies the official DeepSeek route.
  - `ANTIGRAVITY_STORAGE_KEY` supplies the gateway's encryption key so this operator's service never needs Apple Keychain.

- [ ] Do not add `BLUESMINDS_API_KEY` while BluesMinds is degraded.
- [ ] Validate names and permissions without resolving or printing the value:

  ```bash
  test -f ~/.codex/antigravity-provider.env
  test "$(stat -f '%Lp' ~/.codex/antigravity-provider.env)" = 600
  test "$(wc -l < ~/.codex/antigravity-provider.env | tr -d ' ')" = 2
  rg -q '^DEEPSEEK_API_KEY=op://[^[:space:]]+$' ~/.codex/antigravity-provider.env
  rg -q '^ANTIGRAVITY_STORAGE_KEY=op://[^[:space:]]+$' ~/.codex/antigravity-provider.env
  ! rg -q '^BLUESMINDS_API_KEY=' ~/.codex/antigravity-provider.env
  ```

- [ ] Run a bounded `op run --env-file ...` check that prints only presence/length classes for `DEEPSEEK_API_KEY` and `ANTIGRAVITY_STORAGE_KEY`, never either value.
- [ ] If a regular `op://` reference file cannot be supplied, stop. Do not fall back to plaintext, the FIFO, argv keys, LaunchAgent environment values, or `provider set --api-key`.

### Task 7: Upgrade the exact launchd runtime and reinstall the service

**Authorization gate:** This task mutates the global Python installation and LaunchAgent. Require explicit user approval immediately before execution. It must not write Codex config.

- [ ] Capture rollback/readback evidence before mutation:
  - global package version and module path;
  - `pip check`;
  - LaunchAgent plist hash and copy;
  - service status and live `/health`/`/v1/models`;
  - xAI OAuth status through the branch CLI;
  - `~/.codex/config.toml` hash;
  - encrypted provider-store hash if present.
- [ ] Download a bounded rollback wheel for the currently installed `1.6.3` package without installing it.
- [ ] Install the exact Task 4 wheel into the interpreter used by launchd. Because the local artifact and public release both use version `1.7.0`, force the exact local file without dependency churn:

  ```bash
  /Library/Frameworks/Python.framework/Versions/3.10/bin/python3.10 \
    -m pip install --force-reinstall --no-deps /absolute/path/to/exact-wheel.whl
  ```

- [ ] Verify from `/tmp` that the global import is `1.7.0`, contains `PROVIDER_PRESETS["bluesminds"]`, exposes `provider status xai-oauth`, and matches the wheel hash/source evidence.
- [ ] Reinstall/restart only the gateway service through the supported wrapper:

  ```bash
  /Library/Frameworks/Python.framework/Versions/3.10/bin/codex-antigravity \
    service install --port 51122 --host 127.0.0.1 \
    --op-env-file ~/.codex/antigravity-provider.env --json
  ```

- [ ] Inspect the plist structurally: it must call the absolute `op` binary with `run --env-file`, contain no secret values or `EnvironmentVariables` block, and keep loopback host/port unchanged.
- [ ] If launchd is inactive/unreachable, provider references cannot resolve, or the wrong module is imported, execute the captured rollback and restore the original service shape. Do not touch Codex config.

### Task 8: Prove the real normal service end to end

**Files:** No code changes unless a reproducible defect is found and returned to Tasks 1-4.

- [ ] Require `service status --json` to report installed, active, and reachable.
- [ ] Require `/health.package_version == "1.7.0"`.
- [ ] Require the real `/v1/models` to contain:
  - `deepseek:deepseek-v4-flash`;
  - `deepseek:deepseek-v4-pro`;
  - `xai-oauth:grok-build-0.1`;
  - `xai-oauth:grok-4.3`.
- [ ] Require the real catalog to omit:
  - `bluesminds:grok-4.5`;
  - `bluesminds:z-ai/glm-5.2`.
- [ ] Run installed Anti smoke against all required IDs:

  ```bash
  python3 ~/.codex/skills/anti/scripts/anti.py smoke \
    --model deepseek-v4-flash \
    --model deepseek-v4-pro \
    --model grok-oauth
  ```

- [ ] Run one bounded real-service DeepSeek V4 Pro structured/tool continuation and one xAI OAuth Grok generation smoke. The temporary proof from Task 5 is not a substitute for this service proof.
- [ ] Run a real `workflow claude-grok --panel-mode ask` using its default xAI route and confirm successful Claude and Grok reviewer lanes plus the Opus judge.
- [ ] Refresh the installed skill through the upgraded global CLI with `install-skill --force --verify`; prove canonical/installed byte parity excluding caches.
- [ ] Recheck the Codex config hash and provider-store hash. The config hash must remain exactly unchanged; the provider store should also remain unchanged in the canonical-env design.

### Task 9: Keep BluesMinds explicitly degraded and define its future enablement gate

**Files:**

- Modify if needed after live results: `README.md`, `USAGE.md`, `codex_antigravity_auth/skills/anti/SKILL.md`
- Append evidence: project verification note or dedicated credential-free evidence document

- [ ] State that BluesMinds aliases are available only when explicitly configured and advertised; the normal service intentionally omits them.
- [ ] Record the last sanitized failures: Grok billing error and GLM upstream 429.
- [ ] Do not add BluesMinds to the durable reference file or provider store.
- [ ] Define a separate future gate requiring, in a temporary process first:
  - catalog identity;
  - non-streaming output and exact model identity;
  - SSE completion and `[DONE]`;
  - structured JSON;
  - tool call and continuation;
  - usage accounting;
  - bounded retries and no billing/capacity error.
- [ ] Only after all checks pass may a later authorized task add `BLUESMINDS_API_KEY=op://...` to the durable reference file and reinstall the service.

### Task 10: Final verification, commit, and durable evidence

- [ ] After any post-live source/doc correction, repeat the entire Task 4 suite and rebuild the exact wheel.
- [ ] Confirm the branch is clean after a local commit and the installed runtime/skill correspond to that final commit/artifact.
- [ ] Run `git diff --check`, credential-pattern checks, and `du -sh /private/tmp/[Vv]ifty* 2>/dev/null || true`.
- [ ] Record separately:
  - exact source commit;
  - focused/full test counts;
  - wheel/sdist hashes and Twine result;
  - temporary-provider proof;
  - normal-service proof;
  - installed-skill parity;
  - config/provider-store hashes;
  - BluesMinds non-claims;
  - rollback status.
- [ ] Append a credential-free summary to today's Obsidian daily note and the Codex Antigravity Auth project note.
- [ ] Stop with a local commit. Do not push, create a PR, merge, tag, publish, release, or edit Codex config unless separately requested.

## Final acceptance criteria

- The named Claude/Grok workflow cannot claim collaboration without both reviewer families.
- The normal gateway identifies its package version and runs the exact verified artifact.
- DeepSeek V4 Flash, DeepSeek V4 Pro, and xAI OAuth Grok are advertised and generation-proven through the real port-51122 service.
- Opus remains the judge and existing OAuth Grok aliases remain unchanged.
- BluesMinds remains absent from the normal catalog and is described as degraded, not usable.
- No secret value is exposed or persisted outside 1Password resolution at process start.
- `~/.codex/config.toml` is byte-for-byte unchanged.
- Full tests, package proof, installed-skill parity, service readback, live provider evidence, rollback evidence, and Obsidian logging are complete.
