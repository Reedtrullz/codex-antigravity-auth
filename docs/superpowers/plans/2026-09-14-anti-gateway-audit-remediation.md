# Anti and Gateway Audit Remediation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking. The user explicitly selected GPT-5.6-Luna in a separate task; do not ask which execution mode to use.

**Goal:** Resolve A1–A10 and G1–G8 from the 14 September audit, close the two additional persistence risks with evidence, and deliver a locally verified, reviewable patch series without false completeness or release claims.

**Architecture:** Repair existing shared boundaries: captured source and executed coverage, response terminal classification, whole-call accounting, result finalization, account transactions and protocol adapters. Reuse current functions and data structures; do not rewrite the 7,592-line helper, introduce a framework, or duplicate state rules per command.

**Tech Stack:** Python 3.10+, argparse, FastAPI/Starlette, httpx, existing AnyIO runtime, pytest/unittest, encrypted transactional JSON persistence, Git and the existing Anti helper.

**Spec:** /Users/reidar/.codex/anti-audits/2026-09-14-pr26/REVIEW.md. Read it fully, together with /Users/reidar/.codex/attachments/d642aacf-1dc1-48ff-8203-2bb4112d523f/pasted-text.txt. Reproductions are in the audit directory, not the repository.

## Global Constraints

- Audited starting SHA: 117b496db568a7c222dd1698912224c36f8264da, branch codex/anti-scope-integrity, PR #26. Preserve those existing fixes and unrelated user work.
- Use the new Codex-managed worktree; never edit the original checkout used by the running editable sidecar. Ensure the audited SHA is an ancestor before implementation. Use a codex/ branch.
- Start with repo AGENTS.md and applicable skills. Use GPT-5.6-Luna as the implementing task model. No stronger-model substitution; subordinate agents, if genuinely useful, must obey the repo provider/model policy.
- No new dependencies unless a demonstrated gap requires owner approval. AnyIO is already installed; use its existing cancellation primitives.
- Read all callers before modifying a shared function. Keep each task a focused red/green/commit cycle. Do not preserve tests that assert the defect merely to retain the old test count.
- Run df -h /System/Volumes/Data before long test/build loops; stop below 30 GiB. Keep temporary artifacts bounded and clean only owned scratch.
- Never print or copy credentials into artifacts. Synthetic stores and mock transports only for failure/concurrency tests.
- No push, merge, release, primary-checkout update, global skill installation or live service restart in this implementation task. Prepare these as a separately gated handoff. Earlier installation is not permission to point the live service at unfinished work.
- Bounded existing-provider Anti checks are allowed after offline gates. No new provider registration or credentials; no unannounced paid fallback. Existing catalog presence does not prove generation health.
- Empty transport output is not a review. A provider refusal is meaningful protocol output but not a completed code review. Preserve that distinction.
- Record exact test commands, commits, residual risks and verification boundaries. No bug-free/optimal claims.

## Delivery shape and source map

All paths below are repository-relative unless absolute. Prefix with the implementation worktree, not the primary checkout.

| Ownership | Existing production files | Tests / supporting files |
|---|---|---|
| Anti orchestration | codex_antigravity_auth/skills/anti/scripts/anti.py | codex_antigravity_auth/skills/anti/tests/test_anti.py; tests/test_anti_new_features.py |
| Coverage and retention | codex_antigravity_auth/skills/anti/scripts/anti_lib/chunking.py; anti_lib/reflections.py in the same scripts directory | Existing Anti tests; new focused tests in that directory only if current test structure makes this clearer |
| Install identity | codex_antigravity_auth/cli.py | tests/test_cli.py; tests/test_release_workflow.py |
| Account state | codex_antigravity_auth/accounts.py; account_state.py; storage.py; cli_setup.py | tests/test_accounts.py; test_account_state.py; test_storage.py; test_cli.py |
| Streaming and validation | codex_antigravity_auth/server.py; response_protocol.py; schema.py | tests/test_server_streaming.py; test_response_protocol.py; test_schema_sanitization.py |
| Provider translation | codex_antigravity_auth/transform.py; codex_antigravity_auth/openai_transport.py | tests/test_transform.py; test_openai_transport.py; test_fidelity_transforms.py |
| Service observation | codex_antigravity_auth/cli_service.py; service.py | tests/test_service_manager.py; tests/test_cli.py |
| User contracts | README.md; codex_antigravity_auth/skills/anti/SKILL.md | CLI help and existing documentation assertions |

NativeResponsesStreamAdapter is in codex_antigravity_auth/openai_transport.py (line 663 at the audited SHA). Reuse it rather than inventing a parallel adapter. Keep imports compatible with a standalone copied Anti skill.

Deliver one ordered local patch series, with Anti and gateway commits clearly separated. A final report at docs/superpowers/reports/2026-09-14-anti-gateway-remediation.md must map every finding to commit, test and outcome. The coordinator reviews checkpoints after T5, T9, T13 and T15; work may continue through safe independent tasks while feedback arrives. A checkpoint is not an artificial stop.

## Contracts to settle before implementation

Use the following decisions, not new flags or parallel schemas:

1. Keep existing public status names. Exit 0 means the requested operation completed its declared contract. Any partial/failed execution exits nonzero; --allow-partial permits returning partial findings, not calling them complete. Usage/preflight errors retain argparse-compatible behavior.
2. One execution manifest is authoritative. Planned, attempted, successfully completed and omitted/failed work are distinct. BytesSent measures bytes actually submitted, not merely packed. Successful coverage additionally requires a successful review of each required chunk. Duplicate sends/retries must not inflate unique coverage.
3. Diff review means the exact selected diff payload, not full unchanged file contents. Declare sourceKind accordingly, record old/new paths and relevant blob identities, and do not imply whole-file review. Binary/unreadable changes must appear as unsupported/omitted and prevent full content-complete claims.
4. Capture file/diff bytes once for the run. Hash and calculate offsets from that snapshot, including Unicode encoding boundaries. Never substitute a later working-tree reread as review evidence.
5. Upstream completion and normalization loss flow into final status. Never infer completion from a JSON dump or missing token usage.
6. A budget is an admission ceiling based on conservative estimates/reservations, not a promise of exact provider billing. Zero budget means zero calls. Unknown price/usage remains unknown; if a strict monetary ceiling cannot be enforced, refuse before calling and explain.
7. Summary retention keeps a complete sanitized final answer/result and compact call metadata. Full retention additionally keeps sanitized per-call outputs. Neither silently stores raw source/prompts. No-save mode preserves its documented privacy behavior.
8. Findings stay unverified until native verification; model strings cannot populate authoritative commit/chunk/hash fields. Preserve actual identity and same-provider disclosure.

## T0 — Baseline and portable evidence (prerequisite)

Files: this plan, existing audit scripts; create docs/superpowers/reports/2026-09-14-anti-gateway-remediation.md in the worktree.

- [ ] Verify git status, branch, merge-base and Python import path. Capture the initial SHA; do not accidentally import editable code from the primary checkout during worktree tests.
- [ ] Run the full suite and dependency check, saving exact counts. The audit baseline was 764 tests/206 subtests, but remeasure.
- [ ] Read all three audit scripts. They assert or print defective behavior; exit 0 means bugs reproduced, not fixed.
- [ ] Adapt their synthetic cases into normal regression tests as each owning task starts. Remove hard-coded primary-repo imports when moving cases into tests. Do not run the original gateway_probes.py as worktree verification: it explicitly imports the primary checkout.
- [ ] Create the evidence report with rows A1–A10, G1–G8, R1 login concurrency, R2 reflection persistence, D1 docs/duplication. Initial outcome for each is pending, not passing.

Commands:
```sh
git status --short
git merge-base --is-ancestor 117b496db568a7c222dd1698912224c36f8264da HEAD
df -h /System/Volumes/Data
.venv/bin/python -c 'import codex_antigravity_auth; print(codex_antigravity_auth.__file__)'
.venv/bin/python -m pytest -q
uv pip check --python .venv/bin/python
```

Create a worktree-local virtual environment if absent with `uv venv .venv` and `uv pip install --python .venv/bin/python -e '.[dev]'`; do not change the primary environment. Commit only the plan/report initialization, not logs with private data.

## T1 — NUL-safe Git scope and frozen source (A8; foundation for A2/A7)

Files: anti.py changed_paths, collect_review_context and their callers; Anti tests.
Interface: retain changed_paths caller contract where possible; collect_review_context(args) returns dict[str, Any] and must carry exact captured source bytes and path/source-kind metadata rather than forcing later filesystem reads. Extend this existing dictionary rather than inventing a ReviewContext class.

- [ ] Add a temporary Git repository test with modification, deletion, rename, føø.py, space name and newline name. Include staged/unstaged/base-diff modes and literal pathspec escaping.
- [ ] Run the new cases and observe missing deletion/Unicode scope before fixing.
- [ ] Parse NUL-delimited Git records; for renames preserve both path identities. Include deletion changes and obtain their diff/blob from Git. Capture the selected payload once. Do not split quoted names as text lines.
- [ ] Add binary/unreadable-file cases: list the change and explicit limitation; do not claim complete source review.
- [ ] Run Anti scope tests and commit.

Regression assertions, in the existing temporary-Git fixture:
```python
assert {"keep.py", "føø.py", "deleted.py"} <= set(context["paths"])
assert "deleted.py" in context["diff"]
assert "føø.py" in context["diff"]
# Change disk after context capture: the saved snapshot/hash must not change.
```
Test behavior through collect_review_context and actual captured records, not a second test-only collector. Git may quote Unicode in diff headers; assert the decoded file identity in the manifest and use the repository's configured diff rendering in textual header checks.

Gate: every selected Git change is either reviewed or explicitly excluded with a non-complete result.

## T2 — Executed coverage manifest and failure progress (A2)

Files: anti.py chunk review/summary paths; anti_lib/chunking.py; Anti tests.
Consumes: T1 frozen source. Produces: the existing coverage object populated from execution, shared by command finalizers.

- [ ] Port complete_chunk_plan, partial_cap and failed_chunk from reproduce.py into tests. Retain the 14,000-byte/11-chunk fixture and failure on call 2.
- [ ] Assert expected/completed counts, unique byte ranges, actual chunk paths, included/omitted/failed files and top/nested agreement.
- [ ] Create planned records before any call; update attempted/completed state as calls finish. Carry the manifest through errors instead of reconstructing from initial prompt metadata.
- [ ] Merge execution metadata into panel review context; remove stale packing flags only when all affected source was actually processed. Keep source coverage separate from later synthesis/judge success.
- [ ] Test retry does not double-count unique bytes, capped execution does not mark whole content complete, and synthesis failure preserves all completed chunks.
- [ ] Run focused tests and commit.

Expected assertions:
```python
assert result["coverage"]["chunksExpected"] == 11
assert result["coverage"]["chunksCompleted"] == 11
assert result["coverage"]["includedFiles"] == ["fixture.py"]
assert result["coverage"]["omittedFiles"] == []
assert result["coverage"]["files"][0]["bytesSent"] == 14000
```
For second-call failure: expected remains 11, completed is 1, failed is 1, unsent work is explicitly represented. Do not silently redefine chunksExpected as attempted.

## T3 — Upstream terminal and normalized lane integrity (A3/A6)

Files: anti.py post_response, ResponseText metadata, extraction, lane classification, build_panel_synthesis_prompt; Anti tests.
Consumes: upstream response status and parser diagnostics. Produces: explicit terminal/loss facts retained through lane and judge input.

- [ ] Port upstream_incomplete, upstream_completed-empty and repaired_lane probes. Add failed, completed text, tool-only, refusal and missing-usage variants.
- [ ] Stop turning empty response JSON into answer text. Carry incomplete_details and upstream status. Diagnostic serialization must remain separate and sanitized.
- [ ] Preserve parse repair, dropped item counts and truncation reasons through structured normalization. Model-written complete status cannot clear these.
- [ ] Test more than 12 high-severity findings and an oversized judge prompt. Preserve all required high-priority findings within the budget or refuse/mark partial; never slice and call complete.
- [ ] Run tests and commit.

Expected classification:
```python
assert incomplete_lane["status"] != "success"
assert empty_lane["status"] != "success"
assert repaired_judge_metadata["judge_input_status"] != "complete"
```
Use existing status spellings and adapt assertions to their exact metadata locations. Do not invent a second terminal type if ResponseText already carries the fields.

## T4 — One completion decision across commands (A1/A9)

Files: anti.py command_panel, command_review, command_plan, consult path and shared finalization; Anti tests.
Consumes: T2 coverage and T3 terminal/loss facts. Produces: consistent result status plus process exit.

- [ ] Add subprocess tests against a local mock HTTP server, using only synthetic payloads and temporary run directories. Test panel, review and plan as actual CLI invocations.
- [ ] Cover complete, omitted chunks, truncated judge, repaired lane, failed synthesis, upstream incomplete and a nine-chunk plan capped at one.
- [ ] Factor the smallest shared completion decision from existing code. Return nonzero for partial/failed contract even with --allow-partial; retain partial artifacts.
- [ ] Make plan --chunked off exact-or-refuse before provider calls. Apply existing review size checks to its actual full prompt and synthesis budget.
- [ ] Correct the test currently asserting exit zero for a partial panel. Add explicit no-regression for valid distinct-model/provider accounting and required-file refusal.
- [ ] Run subprocess and existing Anti tests; commit.

Core acceptance:
```python
assert partial_process.returncode != 0
assert partial_result["runStatus"] == "partial"
assert complete_process.returncode == 0
assert complete_result["scopeStatus"] == "complete"
```
Preserve existing failed versus partial distinctions; do not make every non-success identical.

## T5 — Trusted provenance and complete durable answers (A5/A7)

Files: anti.py enrich_finding_provenance, write_run_record and chunk ledger retention; redaction helpers only if needed; Anti tests.
Consumes: T1 snapshot, T2 manifest, T4 finalized status.

- [ ] Port forged_provenance and consult_artifact cases. Test consult/review/plan/panel in summary/full/no-save modes and a >1,600-character final answer.
- [ ] Overwrite authoritative provenance from actual snapshot/chunk records. Use null/unresolved for out-of-scope claims, invalid line bounds or missing snapshots; preserve model claims only under clearly nonauthoritative data if necessary.
- [ ] Store the complete sanitized final answer in result.json and keep the index preview short. Preserve successful call metadata on failure; full mode writes actual per-call output files and valid rawLanePaths.
- [ ] Make writes atomic using existing helpers or same-directory temporary replacement. Ensure restrictive permissions and do not follow unsafe result targets.
- [ ] Inject artifact-write failure and assert it cannot produce a success-looking result path. Test redaction in final answer and per-call artifacts.
- [ ] Run tests, commit, and send checkpoint 1 to coordinator with T1–T5 commits and evidence.

Assertions:
```python
assert saved_answer == sanitized_full_answer
assert finding["verificationStatus"] == "unverified"
assert finding["chunkId"] is None  # forged, not in captured scope
assert finding["excerptSha256"] is None
```

## T6 — Whole-call preflight, reservations and usage (A4/ANTI-012)

Files: anti.py dry-run, generation/fallback/retry entry points and aggregation; reuse existing ledger implementation; Anti tests.
Consumes: complete chunk plan from T2. Produces: one record per actual attempt and consistent estimates/totals.

- [ ] Port zero_budget and assert no mocked provider calls, not merely a failed final status.
- [ ] Make dry-run report chunk count, chunk/summary/lane/judge calls, token ceilings, known prices, unknowns and possible bounded retries.
- [ ] Enforce admission before the first chunk and every attempt. Reserve conservative costs under the existing concurrency lock before parallel dispatch; release/settle reservations without negative totals.
- [ ] Include chunk/synthesis calls and each distinct retry/fallback attempt once in usage. Do not recursively sum copied retry history. Missing usage must mark totals partial/unknown.
- [ ] Test budget exhausted between stages, simultaneous lanes racing for the final allowance, unknown-price route and fallback with different pricing.
- [ ] Test local size failure never retries unchanged prompts; provider retry honors Retry-After with the existing bounded policy.
- [ ] Run tests and commit.

Assertions:
```python
assert provider_post.call_count == 0  # budget zero, including chunks
assert len(attempt_records) == provider_post.call_count
assert reported_total_tokens == sum(a["usage"]["total_tokens"] for a in known_usage_attempts)
```
The ledger must distinguish estimated ceiling, observed usage and unknown billable attempts; do not advertise an exact billing guarantee.

## T7 — Runtime parity and copied-skill verification (A10)

Files: cli.py codex_skill_matches_bundled/_skill_manifest_hash; anti.py helper identity/preflight; Anti/CLI tests.
Consumes: existing full-tree manifest algorithm; standalone skill cannot assume package imports succeed.

- [ ] Test matching copied skill, one mutated support module, omitted file and legitimately standalone skill with no discoverable bundle.
- [ ] Reuse the complete-tree algorithm; ignore only intentional generated/cache artifacts consistently. Record executed helper hash and available source/bundle/install identities.
- [ ] A detectable mismatch refuses generation before POST. Undiscoverable bundle reports unverifiable, not equal; explicit integrity-required operation refuses unverifiable provenance using the existing applicable gate or documented contract, not guessed paths.
- [ ] Verify a worktree helper compares with its own bundle, not the unrelated primary installation.
- [ ] Execute tests through a temporary copied installed entrypoint. Do not overwrite ~/.codex/skills/anti.
- [ ] Run Anti/CLI/release workflow tests and commit.

Gate: intentional support-file mutation fails parity before any generation; clean copied skill passes.

## T8 — Cancellation-safe account lease release (G1)

Files: server.py managed_sse_generator and shared acquisition/release paths; tests/test_server_streaming.py.
Consumes: current account lease semantics. Does not change selection policy.

- [ ] Port protocol_probes.py ASGI disconnect case into pytest; change expectation from zero releases to exactly one release per acquired lease.
- [ ] Add upstream exception, cancellation while recording, logging exception and multiple acquired/rotated accounts. Test ASGI cancellation, not just generator.aclose.
- [ ] Shield bounded essential cleanup using existing AnyIO primitives. Release must be in a finally independent of logging/attempt-record failure. Preserve original cancellation rather than masking it with diagnostic failures.
- [ ] Ensure duplicate account appearances do not over-release, and all acquired distinct leases are released according to the actual acquisition semantics.
- [ ] Run streaming/account tests and commit.

Implementation shape, fitted to existing helper ownership:
```python
with anyio.CancelScope(shield=True):
    try:
        await record_attempt()
    finally:
        await release_lease()
```
Bound optional diagnostics separately; never let a logging timeout skip release.

## T9 — Refresh persistence, narrow locking and login merge (G2/G3/R1)

Files: accounts.py, account_state.py, storage.py, cli_setup.py; tests/test_accounts.py, test_storage.py, test_cli.py.
Consumes: existing transactional update_accounts API, not a new storage layer.

- [ ] Port both gateway_probes.py cases into worktree-native tests with synthetic storage. Fail refresh, reload state using a new manager, assert no immediate retry.
- [ ] Mark every cooldown/failure mutation dirty in both selection refresh branches; preserve state on all-account failure.
- [ ] Snapshot refresh candidates under lock, do network refresh/discovery outside manager/store locks, then atomically merge only if the account still exists and its token identity has not changed. Never resurrect removed accounts.
- [ ] Add barrier-controlled tests: fresh account selects before blocked refresh releases; concurrent rotated token is not overwritten; simultaneous refresh of one account is deduplicated or stale response discarded.
- [ ] Reproduce R1: login loads state, gateway changes counters/token or adds another account, login finishes. If stale whole-store save loses these updates, change login to transactional per-account upsert after OAuth completes.
- [ ] Assert unrelated account state, active-family indices and newly added accounts survive. If R1 is already safe, document exact test/source evidence instead of speculative rewriting.
- [ ] Run tests, commit separate persistence/locking changes if independently reviewable, and send checkpoint 2.

Barrier assertion:
```python
assert refresh_started.wait(1)
assert fresh_selection_done.wait(1)  # before releasing blocked network refresh
release_refresh.set()
```
Use deterministic events and bounded joins, not timing-only sleeps.

## T10 — Parallel tool-call replay (G4)

Files: transform.py transform_request_to_chat; tests/test_transform.py and provider route tests.
Consumes: ordered Responses input items; produces valid existing Chat payload.

- [ ] Add two calls followed by two outputs; observe current assistant/assistant/tool/tool sequence.
- [ ] Group adjacent function calls from the same assistant turn into one assistant tool_calls list. Preserve IDs, ordering, arguments and text where supported.
- [ ] Never group across an intervening user/tool turn. Test mixed text, sequential calls, out-of-order paired outputs, duplicate IDs and missing outputs according to existing request-validation semantics.
- [ ] Assert both streaming and non-streaming BYOK routes use the corrected shared transformation.
- [ ] Run transform/fidelity/provider tests and commit.

Expected:
```python
assert [m["role"] for m in messages] == ["assistant", "tool", "tool"]
assert [c["id"] for c in messages[0]["tool_calls"]] == ["call_a", "call_b"]
```

## T11 — Native stream terminal normalization and BYOK telemetry (G5/G7)

Files: server.py; existing NativeResponsesStreamAdapter owner; response_protocol.py only as required; tests/test_openai_transport.py, test_response_protocol.py, test_server_streaming.py.
Consumes: normalized ProviderResult/terminal/usage types already in the project.

- [ ] Port native premature-EOF and BYOK telemetry probes. Assert an explicit failure/incomplete terminal on text-delta EOF and correct nested usage extraction.
- [ ] Wire the existing adapter into native streaming instead of passing raw chunks through. Preserve SSE event IDs/order, display-model mapping, upstream errors and exactly-once resource closure.
- [ ] Test fragmented frames, malformed frame before terminal, duplicate terminal, valid refusal, tool-only completion, empty terminal, DONE-only and cancellation.
- [ ] Do not blindly reject all trailing bytes after a valid terminal: define whether protocol ignores trailing frames or flags malformed input, consistent with existing adapter semantics. Do reject premature completion and empty meaningless output.
- [ ] Read BYOK status/usage from normalized Responses terminal events; record completed status, HTTP status and 2/3/5 usage for the reproduced successful case.
- [ ] Run protocol/stream/transport tests and commit.

Assertions:
```python
assert terminal_count == 1
assert close_count == 1
assert log_record["status"] == "completed"
assert log_record["usage"]["total_tokens"] == 5
```

## T12 — Schema and numeric boundary errors (G6)

Files: schema.py, server.py input validation; tests/test_schema_sanitization.py, test_server_streaming.py.
Consumes: untrusted request JSON. Produces: existing sanitized client-error envelope.

- [ ] Add $ref values [] and 7; properties []; schema root list/null where invalid; temperature 10**400; non-finite floats and booleans for numeric-only fields.
- [ ] Validate schema node types before reference/property traversal. Catch conversion OverflowError/TypeError/ValueError at the numeric input boundary; reject non-finite values.
- [ ] Preserve valid recursive schemas, supported references, enum/nullable constructs and sanitation already covered by tests. Do not implement a new JSON Schema validator.
- [ ] Exercise the ASGI endpoint: response is a documented 4xx, no provider call, no traceback/private input in error output.
- [ ] Run boundary/schema/fidelity tests and commit.

Acceptance:
```python
assert 400 <= response.status_code < 500
assert provider_call_count == 0
assert "Traceback" not in response.text
```

## T13 — Consistent service state and safe reflection persistence (G8/R2)

Files: cli_service.py, service.py; anti_lib/reflections.py; tests/test_service_manager.py, test_cli.py and Anti tests.
These are independent fixes; use separate commits.

- [ ] Mock registered-but-stopped launchd/Windows tasks, running but unhealthy service, responding unmanaged process and fully healthy managed process.
- [ ] Compose top/nested status from one observed reachability/process result. Installed registration alone is not active process evidence. Preserve actual platform parsing behavior.
- [ ] Reproduce R2 with two reflection writers synchronized after read, then failure during write. Check symlink target handling using a harmless temp sentinel file.
- [ ] If confirmed, protect the complete read-modify-write transaction using an existing cross-platform helper, write atomically in the same directory, preserve permissions and reject unsafe symlink targets. Atomic replace alone does not fix lost concurrent updates.
- [ ] Verify failed write preserves previous valid JSON, concurrent records both survive, pruning remains bounded and sentinel is unchanged. If a risk is already mitigated, close with executable evidence.
- [ ] Run both test groups, commit and send checkpoint 3.

Acceptance:
```python
assert status["reachable"] == status["service"]["reachable"]
assert {r["id"] for r in saved_records} >= {"writer-a", "writer-b"}
assert sentinel.read_text() == "unchanged"
```

## T14 — Documentation and measured simplification (D1)

Files: README.md, bundled SKILL.md, CLI help; touched Anti helpers only.
Consumes: proven behavior from T1–T13.

- [ ] Document partial nonzero exit, exact-off planning/review, complete result retrieval, retention/privacy modes, known-vs-unknown estimates, provider diversity and parity states.
- [ ] Audit capability text against the project's actual routing/capability definitions. Remove contradictory blanket multimodal claims and stale runnable-model claims; distinguish optional configured routes from defaults. Live-provider specifications need official verification, not catalog inference.
- [ ] Compute public compatibility fields from a single canonical representation where touched; keep required snake/camel aliases at serialization boundaries instead of maintaining competing state.
- [ ] In chunk_manifest reuse the already-computed per-file matching/sent chunk lists instead of scanning them repeatedly. Preserve existing stronger failed/truncated states.
- [ ] Do not create a large extraction/refactor merely to reduce line count. Add one small deterministic call-count/behavior regression for optimization; no performance claim without measurement.
- [ ] Run CLI/help/Anti tests and commit. Ensure the skill still leaves native agent authority and verification boundaries explicit.

## T15 — End-to-end acceptance and integration handoff

Files: final evidence report; test additions only if a real remaining defect is found.
Consumes: all prior commits. No primary runtime mutation.

- [ ] Run full suite, packaging checks used in repository CI, dependency consistency and git diff --check from the worktree. Record Python version, full SHA and warnings.
- [ ] Run a temporary copied skill against a mock loopback gateway to verify actual subprocess exits, zero-budget zero-POST, multi-chunk coverage, mid-run failure retention and result retrieval. Execute new assertions, not old bug-success probes.
- [ ] Start a bounded isolated gateway from the worktree on a verified unused loopback port with synthetic/mocked upstream where possible. Prove ASGI disconnect cleanup, native EOF, tool replay and telemetry without changing the real service.
- [ ] Run a small live Sonnet+Opus Anti panel using the corrected worktree helper against the existing configured gateway, exact synthetic/public fixture, no fallback, max parallel 2, at most one same-route transport retry, generous but bounded output limits. Then one representative multi-chunk fixture. A successful small run is not proof of full-repo review.
- [ ] Inspect saved results for actual identities, same-provider disclosure, complete coverage, terminal/judge completeness, usage and exits. If a provider times out, preserve failure evidence and mark live acceptance pending; do not keep rerunning or weaken the gate.
- [ ] Coordinator reviews checkpoint 4/final diff. Report all A/G/R/D rows as fixed+tested, disproved+evidence or explicitly blocked. No silently deferred finding.
- [ ] Prepare separate integration instructions: intended commits for PR #26 versus gateway follow-up, installed-tree verification command, current service launch source/rollback target, required restart and live checks. No push/global install/restart until coordinator/user authorizes that integration stage.
- [ ] Update Obsidian with exact evidence and residual boundaries. Return branch, commits, tests, artifacts, live outcomes and integration status.

Final acceptance commands:
```sh
.venv/bin/python -m pytest -q
uv pip check --python .venv/bin/python
git diff --check
git status --short
git log --oneline 117b496db568a7c222dd1698912224c36f8264da..HEAD
```

## Coverage and dependency checklist

| Findings | Tasks | Required evidence |
|---|---|---|
| A1/A9 | T4 | Actual CLI exit and artifact agree; no clipped plan success |
| A2 | T1/T2 | Frozen payload, executed counts/bytes, preserved failure progress |
| A3/A6 | T3/T4 | Terminal and parse-loss facts cannot regain complete status |
| A4 | T6 | Zero POST at budget zero; stage/attempt estimates and accounting |
| A5/A7 | T5 | Full sanitized answer, raw retention contract, trusted snapshot provenance |
| A8 | T1 | Deleted/renamed/Unicode/newline paths |
| A10 | T7 | Copied-skill mutation refuses before generation |
| G1 | T8 | Real ASGI cancellation, exactly-once release |
| G2/G3/R1 | T9 | Persist/reload cooldown, independent selection, conflict-safe login |
| G4 | T10 | Parallel calls grouped, both route modes |
| G5/G7 | T11 | Valid terminal/close/model/telemetry contracts |
| G6 | T12 | Malformed requests yield 4xx/no provider calls |
| G8/R2 | T13 | Consistent state; atomic concurrent reflection persistence |
| D1 | T14 | Correct docs and minimal measured duplication reduction |
| All / original ANTI-001–013 | T15 | Full report, offline end-to-end, bounded live evidence, explicit integration gate |

Order: T0 → T1 → T2 → T3 → T4 → T5 → T6 → T7; then T8 → T9 → T10 → T11 → T12 → T13; finish T14 → T15. Gateway work is technically independent of Anti tasks, but serial execution avoids conflicting shared files and simplifies review. Use parallelism only for a genuinely independent read-only check.

## Steering protocol

At each checkpoint send the coordinator the task numbers, exact commit hashes, red/green commands, unexpected design changes, remaining failures and next task. Do not stop after the first phase or call existing green tests sufficient. Do not send secrets or broad logs. Continue the next safe planned task while awaiting review, incorporating concrete feedback. If blocked by provider health, finish independent offline work and clearly distinguish implementation completion from pending live acceptance. Only material new authority or unresolved conflicting user edits should force an owner decision.
