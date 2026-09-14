# Anti and gateway audit remediation

## T0 baseline

- Worktree: `/Users/reidar/.codex/worktrees/dfb9/codex-antigravity-auth`
- Branch: `codex/anti-gateway-remediation`
- Starting SHA: `117b496db568a7c222dd1698912224c36f8264da`
- Primary checkout was not edited.
- Initial finding outcomes are pending until regression evidence is collected.

Offline remediation evidence through T15:

- T1/T2: staged/working-tree scope is NUL-safe, preserves deletions/renames/Unicode, captures one source snapshot, and records planned/attempted/completed/failed/not-sent chunk buckets.
- T3/T4: upstream incomplete/empty output is not a completed answer; partial panel/review/plan results retain artifacts and exit nonzero, while successful retries remain zero; capped plans refuse exact execution unless `--allow-partial` is explicit.
- T5: consult summary artifacts retain the complete answer; finding provenance clears model-forged chunk/excerpt fields and derives excerpt hashes only from the captured snapshot.
- Focused Anti/gateway regression suite after the reopened checkpoint: `374 passed, 80 subtests, 2 warnings`.
- Full suite with the repository's 1Password signing configuration isolated for temporary Git fixtures: `804 passed, 220 subtests, 2 warnings`.
- `uv pip check --python .venv/bin/python`: 31 packages compatible.
- `git diff --check`: pass.

T5 follow-up reopened the checkpoint after review found gaps in the first
checkpoint: `plan --chunked off` now refuses before generation; unresolved
provenance clears both line and excerpt hash; chunk records carry structured
line ranges and distinguish bytes submitted from bytes successfully reviewed;
plan chunks and synthesis classify terminal output; full-mode result artifacts
write atomic per-call lane files while summary indexes retain only previews and
the complete answer remains in `result.json`.

Baseline evidence (2026-09-14):

- `.venv/bin/python` is `/Users/reidar/.codex/worktrees/dfb9/codex-antigravity-auth/.venv/bin/python`.
- `codex_antigravity_auth.__file__` resolves inside this worktree, not the primary checkout.
- `git merge-base --is-ancestor 117b496db568a7c222dd1698912224c36f8264da HEAD`: pass.
- `uv pip check --python .venv/bin/python`: 31 packages compatible.
- `.venv/bin/python -m pytest -q`: 764 passed, 206 subtests passed, 2 warnings, 12.71s.
- The required audit scripts were read; their successful exit means defect reproduction, not acceptance.

## Finding matrix

| Finding | Outcome | Commit | Evidence |
|---|---|---|---|
| A1 | fixed offline | `90c719f` plus Anti commits | Exact-off planning refuses before generation; capped execution remains partial/nonzero. |
| A2 | fixed offline | `90c719f` | Chunk ledger preserves completed/failed/not-sent buckets and submitted vs reviewed bytes. |
| A3 | fixed offline | `90c719f` | Upstream incomplete and completed-empty responses retain distinct non-success classifications. |
| A4 | fixed offline | current remediation patch | Shared locked admission covers every provider attempt; panel dry-run and execution share chunk/synthesis/lane/judge stage planning with per-lane models and bounded fallback topology; budget exhaustion after summary blocks panel/judge calls; first-state initialization is barrier-tested; Retry-After over-cap defers rather than retries early; unknown-price refusal is explicit. |
| A5 | fixed offline | `90c719f` | Complete consult answer remains retrievable in the result artifact; prompt retention uses hashes/counts and sanitized outputs. |
| A6 | fixed offline | `90c719f` | Parse/truncation/loss facts force partial status and nonzero exit. |
| A7 | fixed offline | `90c719f` | Provenance is derived from captured scope/chunk ranges; forged unresolved fields clear to null. |
| A8 | fixed offline | `90c719f` | NUL-safe staged scope preserves deletions/renames and the exact scope snapshot; partial coverage remains explicit in artifacts. |
| A9 | fixed offline | `90c719f` | Plan caps cannot silently become complete. |
| A10 | fixed offline | `a3b97ff`, `90c719f` | Helper/bundle parity is checked before generation; clean local and bundled hashes matched in the worktree. |
| G1 | fixed offline | `90c719f` | Real ASGI disconnect regression covers bounded diagnostics and exactly-once lease release; cleanup is shielded and release is independent. |
| G2 | fixed offline | current remediation patch | Hard-expiry failures mark the mutation dirty and persist cooldown state; background-refresh contention is a transient per-request exclusion with no auth cooldown, and selection cannot use an expired token. |
| G3 | fixed offline | `90c719f` | Refresh merge checks refresh token, access token and expiry snapshot; stale same-token results are discarded. |
| G4 | fixed offline | `340ec96` | Adjacent Responses function calls group into one ordered assistant tool-call turn without crossing intervening messages. |
| G5 | fixed offline | `90c719f` | Native Responses SSE adapter is exercised for valid terminal, premature EOF, duplicate terminal, model rewrite and closure. |
| G6 | fixed offline | current remediation patch | Malformed `$ref`/`properties` schemas return 400 before account/provider selection; both flat and nested valid tool shapes pass schema validation; numeric boundaries remain fail-closed. |
| G7 | fixed offline | `90c719f` | BYOK telemetry reads normalized Responses terminal status and nested usage, with compatibility coverage for the transport seam. |
| G8 | fixed offline | current remediation patch | Platform probes distinguish registered/loaded from running, then compose installed/active/reachable; registered-but-unreachable is `active_unreachable`, while a reachable managed service is `ready`. |
| R1 | fixed offline | `340ec96` | OAuth login uses transactional per-account merge and preserves concurrent account state. |
| R2 | fixed offline | `90c719f` | Reflection read-modify-write is cross-process locked and atomic; concurrent writers, pruning and symlink sentinels are tested. |
| D1 | fixed offline | `13e8df9`, `a3b97ff`, `90c719f`, current remediation patch | README/SKILL describe partial exits, retention, estimates, parity and non-claims; dry-run labels heuristic tiers versus unknown provider prices; no new broad abstraction was introduced. |

## Verification boundary

Offline closure is verified, but the bounded live command did not reach panel
lane execution. The exact command was:

```text
.venv/bin/python codex_antigravity_auth/skills/anti/scripts/anti.py panel --mode review --scope files --file README.md --model sonnet --model opus --judge opus --fallback-policy never --max-parallel 2 --retry 1 --max-output-tokens 128 --judge-output-tokens 128 --chunked auto --max-review-chunks 4 --timeout 45 --save-output never --json --no-progress
```

It used the default `http://127.0.0.1:51122/v1`, exited `1`, had no run ID,
wrote zero bytes to `/tmp/anti-live-sonnet-opus.json`, and
redirected stdout JSON file, and emitted the timeout diagnostic on stderr. The
gateway catalog probe returned HTTP 200 and advertised both requested Claude
models. The dry-run execution plan was two Sonnet `review_chunk` calls, one
Sonnet `review_synthesis`, Sonnet and Opus panel lanes, and an Opus judge; the
actual run timed out on the first Sonnet review chunk (`prompt_chars=29152`) on
both allowed attempts, before the second chunk, synthesis, either panel lane,
or judge ran. No further live retry, restart, credential/provider mutation,
push, merge, or release was done.

The missing live `result.json` was intentional because `--save-output never`
causes `ensure_run_id()` and `write_run_record()` to skip persistence. A copied
CLI mock-HTTP timeout reproduction with `--save-output summary --run-id
timeout-repro` exited `1` and persisted both the run record and sanitized
`result.json`, including `resultPath`, the failed Sonnet lane, timeout evidence,
and the execution plan. This confirms no persistence defect was found.

## Exact offline gate evidence

- A1/A9/T4: `test_review_partial_scope_fails_preflight_without_allow_partial`, `test_chunked_off_refuses_incomplete_content_before_model_call`, and `test_capped_plan_is_partial_and_nonzero`.
- A2/T1-T2: `test_staged_git_scope_is_nul_safe_and_includes_deletions_renames`, `test_partial_chunk_manifest_reports_bytes_and_boundaries`, and `test_chunk_failure_separates_failed_from_never_sent_chunks`.
- A3/A6/T3-T4: `test_upstream_incomplete_and_empty_completed_are_not_success`, `test_diff_review_marks_incomplete_when_cap_cuts_diff_parts`, and `test_partial_panel_cannot_report_complete_panel_status`.
- A4/T6: `test_budget_admission_allows_only_one_concurrent_last_allowance`, `test_panel_dry_run_plan_matches_mock_provider_stage_calls`, `test_panel_budget_exhaustion_after_summary_blocks_panel_and_judge`, `test_panel_dry_run_includes_bounded_fallback_topology`, `test_retry_after_hint_controls_bounded_provider_retry`, `test_retry_after_over_cap_is_deferred_and_http_date_is_supported`, `test_budget_refuses_unknown_model_price_before_state_or_provider`, `test_chunked_plan_budget_refusal_keeps_completed_progress_and_makes_no_extra_call`, and `test_dry_run_reports_stages_prices_retries_and_unknowns`.
- A5/A7/T5: `test_consult_truncated_output_retries_and_saves_full_output`, `test_run_record_has_stable_result_artifact`, `test_enrich_finding_provenance_rejects_forged_chunk_and_uses_snapshot`, and `test_enrich_finding_provenance_clears_unresolved_hashes_and_ranges`.
- A8/T1: `ScopeIntegrityContractTests.test_staged_git_scope_is_nul_safe_and_includes_deletions_renames`.
- A10/T7: `test_install_codex_skill_copies_bundled_anti_skill`, `test_install_codex_skill_does_not_follow_symlinked_existing_files`, and the parity checks in the Anti/CLI suite.
- G1/T8: `test_google_stream_disconnect_records_cancellation_and_releases_lease` plus the native ASGI disconnect coverage in the streaming suite.
- G2/G3/R1/T9: `test_hard_refresh_failure_persists_for_select_and_acquire`, `test_selection_does_not_wait_on_background_same_account_refresh`, `test_busy_sole_account_is_transient_and_selectable_after_refresh`, `test_refresh_does_not_overwrite_rotated_token`, and `test_upsert_google_account_adds_to_rotation_and_clears_stale_state`.
- G4/T10: `test_adjacent_function_calls_share_one_assistant_tool_call_turn` and `test_function_calls_do_not_group_across_user_turn`.
- G5/G7/T11: `test_native_openai_route_normalizes_terminal_and_closes_upstream`, `test_native_openai_route_premature_eof_emits_failed_terminal`, and `test_byok_chat_stream_logs_finish_reason_success_and_error_events`.
- G6/T12: `test_responses_endpoint_rejects_malformed_tool_schema_before_provider` and `test_responses_endpoint_accepts_flat_and_nested_tool_schema_shapes`.
- G8/T13: `TestServiceResult.test_macos_loaded_but_stopped_is_not_active`, `TestServiceResult.test_macos_running_and_windows_registered_states_are_distinct`, `TestV3NativeSetup.test_run_gateway_status_uses_waiting_reachability_probe`, `TestV3NativeSetup.test_run_gateway_status_marks_registered_but_unreachable_service_degraded`, and `test_service_status_prints_reachable_gateway_when_service_has_no_pid_file`.
- R2/T13: `ReflectionTests.test_concurrent_writers_keep_all_records` and `ReflectionTests.test_clear_and_prune_do_not_follow_symlinks`.
- D1/T14: documentation/source assertions and `test_dry_run_reports_stages_prices_retries_and_unknowns`; no new broad abstraction was introduced.

## Acceptance boundary

The reopened offline blocker suite is green in the current worktree: `804
passed, 220 subtests, 2 warnings`. Exact closure evidence includes
`test_hard_refresh_failure_persists_for_select_and_acquire`,
`test_selection_does_not_wait_on_background_same_account_refresh`,
`test_responses_endpoint_rejects_malformed_tool_schema_before_provider`,
`test_responses_endpoint_accepts_flat_and_nested_tool_schema_shapes`,
`test_busy_sole_account_is_transient_and_selectable_after_refresh`,
`test_macos_loaded_but_stopped_is_not_active`,
`test_macos_running_and_windows_registered_states_are_distinct`,
`test_run_gateway_status_uses_waiting_reachability_probe`, and
`test_run_gateway_status_marks_registered_but_unreachable_service_degraded`,
`test_panel_dry_run_plan_matches_mock_provider_stage_calls`,
`test_panel_budget_exhaustion_after_summary_blocks_panel_and_judge`,
`test_panel_dry_run_includes_bounded_fallback_topology`,
`test_retry_after_hint_controls_bounded_provider_retry`, and
`test_retry_after_over_cap_is_deferred_and_http_date_is_supported`,
`test_budget_refuses_unknown_model_price_before_state_or_provider`.
An actual worktree CLI dry-run for a file review produced stages
`review_chunk=2`, `review_synthesis=1`, `panel_lane=1`, `judge=1` and reported
`pricing_basis=heuristic_tier; provider prices are not known at dry-run time`.
Coordinator acceptance remains pending review of this closure. The temporary Git-fixture failures under the normal environment were
caused by the user's global 1Password SSH signing hook; the authoritative full
suite run used `GIT_CONFIG_GLOBAL=/dev/null` and did not change repository or
global configuration.

The bounded live attempt is evidence of a generation-path timeout, not provider
health or production readiness; catalog responsiveness and generation latency
are separate observations. A representative multi-chunk live run was not
attempted after that terminal failure. Therefore this report does not claim
live provider health, full-repository review coverage, or release acceptance.

## Final T15 small-fixture gate

One final authorized live check used the synthetic, non-sensitive
`scratch/t15-small-fixture.md` fixture (24 lines) and the corrected worktree
helper at
`/Users/reidar/.codex/worktrees/dfb9/codex-antigravity-auth/codex_antigravity_auth/skills/anti/scripts/anti.py`.
The dry-run passed exact-source fit with `--chunked off`: no review-chunk or
synthesis stages were planned; Sonnet and Opus lanes were each 2,013 prompt
characters, followed by the Opus judge. Fallback was disabled, parallelism was
2, retry was `0`, lane output was `2048`, and judge output was `4096` tokens.

The single live command used `--save-output summary --run-id
t15-small-fixture-20260914 --json`, exited `1`, and stopped after both requested
lanes timed out once at 90 seconds. The persisted record reports
`runStatus=failed`, `scopeStatus=complete`, `panelStatus=failed`, no actual
models/providers, and no judge execution. Sanitized evidence is retained at
[`result.json`](/Users/reidar/.codex/anti-runs/t15-small-fixture-20260914/result.json);
the run record's `resultPath` points to that file. Its helper identity is the
worktree path above with matching bundled/worktree tree hashes; the gateway was
the already-running localhost service, and no installed skill, provider,
credential, runtime, or configuration state was changed.

Gate status: offline remediation remains green (`804 passed`, 220 subtests);
exact-fit dry-run is green; the final small live fixture is a recorded failed
gate due to generation timeouts; larger multi-chunk live acceptance remains
pending. Integration requires no code change from this check: retain the
failure artifact for diagnosis, and only rerun after an independently
authorized gateway/provider remediation.

## Non-stream liveness remediation

The bounded reproductions separated two issues without assigning the
35-minute live latency to a specific provider cause:

- `GoogleTransport(timeout=0.1)` completed a local response after `1.721s`
  while the server trickled one byte every `50ms`; HTTPX's scalar timeout is a
  phase/inactivity limit, not a total request deadline.
- A non-stream `create_response` run with a backend sleeping `0.5s` and a
  request whose `is_disconnected()` returned true completed after `0.515s` and
  called `is_disconnected()` zero times.
- The correlated T15 gateway records completed with HTTP 200 after
  `2,119,998ms` (Sonnet) and `2,132,306ms` (Opus), while the helper had already
  stopped waiting at 90 seconds. This proves the client/gateway lifetime
  boundary was broken, not that the upstream provider was globally unhealthy.

The remediation keeps HTTPX inactivity timeouts separate and adds a clamped
monotonic total deadline for native non-stream operations. That single budget
covers initial account acquisition, backend attempts, rotation acquisition,
and the next backend attempt; it is never reset for rotation. The helper now
sends `antigravity_request_timeout_seconds` at client timeout minus a 10-second
cleanup margin, so a 90-second helper call gives the gateway an 80-second
server budget. Disconnect cancellation, deadline expiry, and provider failure
remain separate outcomes. Late threadpool account acquisition releases its
lease, and cleanup/diagnostic paths are bounded and shielded.

Fresh focused evidence:

- ASGI non-stream disconnect cancels the delayed backend and releases exactly
  once; no rotation occurs.
- A total deadline cancels a trickling backend; rotation uses remaining time
  and does not issue a second POST after expiry.
- A blocked account acquisition that returns after disconnect releases the late
  account and never issues a POST.
- Diagnostic failure does not prevent lease release; normal success and the
  existing provider-failure/rotation tests remain green.
- Full authoritative suite: `812 passed, 220 subtests, 2 warnings`.

No live install, restart, canonical account-store write, push, PR, or merge has
been performed for this remediation yet.

Post-review correction commit coverage adds pre-expiry zero-POST behavior,
rotated-lease registration before record awaits, bounded cancellation drain,
remaining-budget diagnostics, a blocked synchronous log-worker test, a
cancellation-resistant backend drain test, and a terminal timeout record
assertion carrying run ID and attempt counts. Windows fixture portability is
preserved with byte-exact source fixtures, platform-legal newline-name
coverage, and plan-derived unsent counts. The authoritative suite after these
corrections is `816 passed, 220 subtests, 2 warnings`.

Retained build artifacts for the candidate are in
`/Users/reidar/.codex/antigravity-builds/anti-gateway-remediation-bea624e/`:

- wheel SHA256 `c7199ab919437c6b049b1b925d1601014fdc7d87e13cd6f1aff92d8a0bdb5b60`;
- source distribution SHA256 `551c50fb1aebf47819cc9c7ffc7d5ea21ea6709854141056eaf4ca3bedcd9ddf`.

## Final offline gate and retained candidate

The final source candidate is `e01e118102f32a529e3e898bd3bce896b46441a9`.
Both fresh CI workflows passed all 12 jobs: PR run `34874242908` and push run
`34874237839` (Ubuntu Python 3.10, 3.11, 3.12, 3.14; Windows Python 3.12;
package). The coordinator account-module rerun was `42 passed, 4 subtests` in
1.49 seconds. The authoritative local suite remains `816 passed, 220
subtests, 2 warnings`.

The retained candidate was rebuilt from the e01e118 source into
`/Users/reidar/.codex/antigravity-builds/anti-gateway-remediation-e01e118/`:

- wheel SHA256 `3f8a70e790763a2e1a05c83af8db6601486e956dfcff92663a2679930c2e3889`;
- source distribution SHA256 `2829610aecf2a10005c2483bc7a84a44d5ce4034293f39d04e1dfd5de9e06160`;
- artifact import identity: Python `/Users/reidar/.codex/worktrees/dfb9/codex-antigravity-auth/.venv/bin/python`, package version `2.2.0`, imported package/server/Anti helper from the extracted retained wheel;
- extracted `server.py` SHA256 `c3567ba9806ebece827274c71f74006cb9ae06b946af40abd6d81592447c4fcc`;
- extracted Anti helper SHA256 `c7e0eb32d8e81c86726d9253e3c941e55cb341762522fe9ca139b77374f3af6c`.

Offline gate status: clear. The authorized next gate is a reversible single-writer
live candidate trial with preserved canonical refreshed state, Sonnet + Opus +
judge, and representative multi-chunk acceptance. Merge remains blocked until
that live gate passes.

## 2026-09-14 — Multi-chunk provenance hold and offline closure

The authorized small live panel on the e01 candidate succeeded, but the
representative multi-chunk live gate did not pass and was stopped. The
cap-256 run failed closed with all five chunk generations incomplete. The
cap-512 run generated all five chunks, then produced a partial result whose
machine manifest was inconsistent with the executed scope: the top-level file
record declared `bytesDeclared=5186`, `bytesSent=5186`, and
`bytesReviewed=5186`, but reported `chunksExpected=0`, `chunksSent=0`, null
chunk IDs, and `includedFiles=[]`. The five actual prompts were only 4,785
characters in total against 5,138 Unicode source characters before wrapper
overhead. The cap-3000 run failed closed on chunks 2/5, 4/5, and 5/5; its
saved failure artifact had `chunksExpected=4`, `completed2`, `failed2`, and an
empty saved chunk list. These are preserved artifacts, not acceptance evidence:

- `/Users/reidar/.codex/anti-runs/t15-remediation-e01e118-multichunk-512/result.json`
- `/Users/reidar/.codex/anti-runs/t15-remediation-e01e118-multichunk-3000/result.json`

The source-level root cause was then fixed in `9b44c73` and strengthened in
`08d5735`/`7b24b27`: chunk budgeting now probes the complete prompt scaffolding,
future source parts are not mislabeled as omitted during fit checks, empty
chunks cannot enter the execution plan, per-file source ranges are merged, and
failure artifacts persist every planned chunk as `success`, `failed`, or
`not_sent`. The attempted sent-ID boundary is updated at runtime. Regression
coverage extracts the actual fenced payload from every generated prompt and
asserts exact ordered equality with the source at 1,000- and 3,000-character
caps; a mocked CLI run also compares dry-run planned calls with execution and
checks artifact bytes, counters, IDs, included files, failed labels, and
never-sent chunk statuses.

Offline evidence for `7b24b27`: full local suite `819 passed, 220 subtests, 2
warnings`; exact Python 3.10 deadline/rotation/cancellation gate `4 passed, 32
deselected`; Anti plus new-feature suite `237 passed, 9 subtests`; `git
diff --check` passed. Fresh PR CI `34877596123` and push CI `34877607713` both
passed all 12 jobs (Ubuntu Python 3.10/3.11/3.12/3.14, Windows Python 3.12,
package). The one Windows failure on the prior commit was test-fixture newline
translation and was corrected with byte-exact fixture writes; the final CI is
green.

Runtime posture after rollback: candidate PID 12227 is stopped; launchd
`com.codex-antigravity.gateway.51122` is the sole listener on
`127.0.0.1:51122`, PID 35501, state `running`, using
`/Library/Frameworks/Python.framework/Versions/3.10/bin/python3.10`, with
process cwd `/`. The launchd editable finder maps imports to the primary
checkout `/Users/reidar/Projectos/codex-antigravity-auth` at
`117b496db568a7c222dd1698912224c36f8264da`; the worktree helper was not the
live process. `/v1/models` returned HTTP 200. Current canonical encrypted-state
hashes are accounts
`6d0b77ca989dae8a44cbf22b6ba9bde649b9c3b2be007b9616fdc9e3cdad2bb3` and
providers
`1959f85e8a68235ab04bb885ccb904f1eaa41c116cf81d2b193214873e416b94`.

Decision: hold further live multi-chunk retries, candidate install/restart, and
merge. The e01 retained wheel remains historical live-trial evidence only; no
new wheel was installed or claimed equivalent to `7b24b27`.

## 2026-09-14 — Corrected-source live trial

Parent offline closure independently confirmed the original panel path at caps
1000 and 3000, including actual mocked calls, failed-chunk artifacts, byte
counters, and IDs. A rebuilt current candidate was then created from the
`7b24b27` source:

- wheel SHA256 `9ac2116f5a8339c524d4fa3f9a6282a0a83b73d1f2398c5c6127c6027eab7db9`;
- sdist SHA256 `a6288caecc6f73d3059581cf6df3c6c3c3b8c626e5720c3bd26e2b030764c2d9`;
- extracted server SHA256 `c3567ba9806ebece827274c71f74006cb9ae06b946af40abd6d81592447c4fcc`;
- extracted Anti helper SHA256 `ecea7dc9a2d428eff358ed47c85618bf9fc74134f600b2c5c07a18a3e7aa7ba4`.

The candidate-only dry-run selected three real-content chunks with prompt
sizes 2,948, 2,939, and 1,927 characters, then one synthesis, two panel lanes,
and one judge. Fallback was `never`, retry `0`, chunk output `2048`, synthesis
and lane output `4096`, judge output `4096`, synthesis cap `16000`, and no
`--allow-partial`.

The single-writer live trial used the extracted wheel and run ID
`t15-remediation-7b24b27-multichunk`. All three chunks completed with HTTP 200.
The saved artifact reports `bytesDeclared=bytesSent=bytesReviewed=5186`,
`chunksExpected=chunksCompleted=3`, zero failed/not-sent chunks, the expected
source SHA256 `480423e2c050ecab0a14f5935439c29774ad951eaf4841e20a1eac47d916fc06`,
and matching first/last/sent chunk IDs. Synthesis then returned HTTP 504 after
the gateway's 90-second total deadline (`prompt_chars=12099`), before panel
lanes or judge execution. The result is therefore `runStatus=failed`,
`scopeStatus=partial`, with no summary non-loss acceptance:

- `/Users/reidar/.codex/anti-runs/t15-remediation-7b24b27-multichunk/result.json`

The candidate process PID 26237 was stopped. The original launchd plist was
bootstrapped again; final rollback posture is launchd PID 53238, Python 3.10,
sole listener on `127.0.0.1:51122`, `/v1/models` HTTP 200. The launchd editable
mapping remains the primary checkout at
`/Users/reidar/Projectos/codex-antigravity-auth` (primary SHA
`117b496db568a7c222dd1698912224c36f8264da`), not the candidate worktree.
Providers remained unchanged at SHA256
`1959f85e8a68235ab04bb885ccb904f1eaa41c116cf81d2b193214873e416b94`.
The canonical accounts file was preserved through the trial and final launchd
restart; its final SHA256 is
`6c6e2129bf060d7fcbf26000964e0fba9e5f1c909ce4caaeaabe768cf20231ce`.

Decision: the corrected-source live gate is failed on synthesis liveness, not
source coverage or chunk-manifest integrity. Do not merge or install the
candidate. No additional live retry was started.

## 2026-09-14 — Synthesis failure provenance fix and timeout correlation

The failed synthesis artifact exposed one remaining reporting defect: the same
file appeared in both `coverage.includedFiles` and `coverage.omittedFiles`, even
though its content status was complete and all three chunks were reviewed.
Commit `6e9c63e` fixes the shared synthesis-failure metadata merge so the
chunked coverage manifest replaces stale single-prompt omission fields. The
new panel CLI regression asserts `includedFiles=[fixture.py]`,
`omittedFiles=[]`, complete file status, full bytes reviewed, and 3/3 completed
chunks when synthesis fails. Evidence: Anti/new-feature suite `238 passed, 9
subtests`; full local suite `820 passed, 220 subtests, 2 warnings`; exact
Python 3.10 focused gate `4 passed, 32 deselected`; fresh PR CI
`34880141830` and push CI `34880138134` passed all 12 jobs.

Read-only request-log correlation for run
`t15-remediation-7b24b27-multichunk`:

- chunk 1: `latency_ms=22328`, HTTP 200, success;
- chunk 2: `latency_ms=23778`, HTTP 200, success;
- chunk 3: `latency_ms=18110`, HTTP 200, success;
- synthesis: `latency_ms=80008`, `attempt_count=3`, `rotation_count=2`,
  `rotation_attempted=true`, `cancelled=false`, `http_status=504`,
  `error_class=request_deadline_exceeded`, error `Native non-stream request
  deadline expired before completion`.

The helper command used `--timeout 90` and `--retry 0`. Its request-timeout
hint subtracts the 10-second cleanup margin, so the gateway received an
80-second total request budget; the recorded 80,008 ms server latency matches
that total deadline. The helper only sends a backend-timeout hint above 120
seconds, so this run used the gateway's default 60-second backend inactivity/
phase timeout. The evidence therefore identifies bounded gateway total-deadline
expiry during account rotation, not an upstream HTTP response or a client
inactivity timeout. Candidate PID 26237 is gone; launchd PID 53238 is the only
listener on 51122 and `/v1/models` remains HTTP 200.

Owner options: keep the fail-closed gate and do not merge/install; or separately
authorize a new bounded experiment with a smaller synthesis payload/latency
surface or an explicitly reviewed timeout/rotation policy change. No further
live attempt was made in this turn.

## 2026-09-14 — Current-head longer live trial

The one separately authorized longer trial used the rebuilt current-head
candidate at `af47e6c73d7000dd473730dda015865b8d2f8d85`, extracted from wheel
SHA256
`334d92c743fdbc90b5c134155feaf6795baf77f6ac858eda926a8b21574d8343`.
Candidate-only import parity matched (`bundleTreeHash=treeHash=
276d36da1b8feb4cce0d9689d925089d27858ff7319025f0f88b73a6c38b9487`). The
helper mapping was verified as `--timeout 180 -> request/backend hint 170`.

Run ID `t15-remediation-af47e6c-multichunk180` used fallback `never`, retry `0`,
no `--allow-partial`, three real-content chunks, two panel lanes, and an Opus
judge. All three chunks completed with HTTP 200 and exact complete coverage:
`bytesDeclared=bytesSent=bytesReviewed=5186`, `chunksExpected=chunksCompleted=3`,
zero failed/not-sent/omitted chunks, and source SHA256
`480423e2c050ecab0a14f5935439c29774ad951eaf4841e20a1eac47d916fc06`.
The intermediate review synthesis and both panel lanes also returned HTTP 200;
request-log latencies were 19.071s, 21.155s, 15.517s, 38.633s, 26.544s, and
30.450s respectively. No retry or fallback occurred.

The final panel-synthesis stage then failed closed before sending another
request: its exact input was 46,741 characters against the configured
`--max-synthesis-chars 16000`. The Opus judge therefore did not run. The
artifact is `runStatus=failed`, `scopeStatus=complete`,
`panelStatus=partial_multi_model`, with truthful complete chunk coverage:

- `/Users/reidar/.codex/anti-runs/t15-remediation-af47e6c-multichunk180/result.json`
- `/Users/reidar/.codex/anti-runs/t15-remediation-af47e6c-multichunk180.json`

This live gate is failed on final panel-synthesis budget fit, not source
coverage, chunk identity, provider HTTP failure, or the 180-second timeout
mapping. Per authorization, no repeat was attempted.

The candidate was stopped and the old launchd service restored. Final rollback
state is PID 16780, Python 3.10, sole listener on `127.0.0.1:51122`, and
`/v1/models` HTTP 200. The editable launchd mapping remains the primary
checkout `/Users/reidar/Projectos/codex-antigravity-auth` at
`117b496db568a7c222dd1698912224c36f8264da`, not this worktree. Providers remain
SHA256 `1959f85e8a68235ab04bb885ccb904f1eaa41c116cf81d2b193214873e416b94`.
The post-trial canonical accounts SHA256 is
`1250e212a8ef749a6d9619fbd375a524950f80a1b21135694e4ad8ded39ce648`; this
current refreshed state was preserved rather than restoring the pre-trial hash.

PR #27 remains draft/open. The candidate code commit `af47e6c` has green PR
and push CI runs `34880417584` and `34880421722` across all 12 jobs; the later
documentation-only branch head has fresh CI still in progress at capture time.
PR #26 remains open and unchanged.
No candidate installation, skill update, merge, close, or production-readiness
claim was made.

## 2026-09-14 — Current-head 64k live trial

The next and final authorized live attempt used verified PR #27 head
`ea23c7cd0b966a29adb791dd08ea24f8b3593c52`. The code path was unchanged from
`af47e6c`; only forward documentation commits followed it. Offline inspection
confirmed the retained prior final judge payload was 46,741 characters, so the
requested `--max-synthesis-chars 64000` left 17,259 characters of headroom.
The dry-run plan selected 3 chunks at 2,948/2,939/1,927 characters, review
synthesis at 64,000, two 3,000-character panel lanes, and the Opus judge at
64,000; output caps were 2,048/4,096/4,096, retry was 0, fallback was never,
and timeout mapping was 180 -> 170 for both request and backend hints.

The exact-head wheel was rebuilt without installation:

- wheel SHA256 `784f8e9c05a6871f2f6c35f58c06a8cc0f875eae7964d6903782f3c438279d8b`;
- sdist SHA256 `1052fb4be0a2eb3123f2c6b187ffd7c4156f7ac72b05311fc03717bd2e2453a6`;
- extracted server SHA256 `c3567ba9806ebece827274c71f74006cb9ae06b946af40abd6d81592447c4fcc`;
- extracted Anti helper SHA256 `68bdaccfb1dc1be782b51abe6ea256e0cb21e8df1e28207f8838a90553bbd53d`;
- helper/bundle tree hashes matched `276d36da1b8feb4cce0d9689d925089d27858ff7319025f0f88b73a6c38b9487`.

Run ID `t15-remediation-ea23c7c-multichunk64000` reached every planned stage.
Coverage was complete: 3/3 chunks, 5,186/5,186 bytes, matching source SHA256
`480423e2c050ecab0a14f5935439c29774ad951eaf4841e20a1eac47d916fc06`, and no
omitted or failed chunks. Review synthesis succeeded with
`synthesis_prompt_chars=10637` and HTTP 200 in 48.413s. Sonnet succeeded in
31.890s. Opus returned a non-answer first, then succeeded on its built-in
second logical attempt; the retained artifact records its final lane as
`status=success`, `output_chars=10602`, with `truncated_models=[]`. The judge
itself then succeeded with HTTP 200 in 54.244s, `findings_status=parsed`,
`judge_truncated=false`, and no judge retry. Its input status was nevertheless
`partial` because Anti compacted the successful Opus lane material locally.

The artifact is therefore not a full gate: `runStatus=partial`,
`scopeStatus=complete`, `panelStatus=partial_multi_model`,
`judge_input_status=partial`, and `judge_input_lossy_lanes` contains the Opus
lane. The retained request log records eight successful HTTP 200 calls: three
chunks, review synthesis, the initial Opus non-answer, Sonnet lane, Opus lane
retry, and judge. No fallback was used. Per authorization, no further live
attempt will be made.

Evidence:

- `/Users/reidar/.codex/anti-runs/t15-remediation-ea23c7c-multichunk64000/result.json`
- `/Users/reidar/.codex/anti-runs/t15-remediation-ea23c7c-multichunk64000.json`

The candidate was stopped and old launchd restored. Final state is PID 25153,
Python 3.10, sole listener on `127.0.0.1:51122`, and `/v1/models` HTTP 200.
Providers remain SHA256
`1959f85e8a68235ab04bb885ccb904f1eaa41c116cf81d2b193214873e416b94`. The
post-trial canonical accounts SHA256 is
`682e44464728514821253bceda38e3b207307f206152a98c6aeeadc3c2ca54e7`; this
current refreshed state was preserved rather than rolled back.

The tested PR #27 source head was `ea23c7c`; the report is carried by later
forward-only documentation commits, and PR #27 remains draft/open. PR #26
remains open and unchanged. No candidate installation, local skill update,
merge, close, credential/provider change, or production-readiness claim was
made.

No-force-push record: the previously disclosed historical rewrite was
`164988f29bac2a917bd2ba9f3de3891eeadd4f11 ->
c484a7f05d6cf5ce46efb280474405e15cbcf358`, and read-only diff inspection shows
only this report changed. Subsequent `9050ec3` and `ea23c7c` commits were normal
forward commits. This trial made no amend or force push.

## 2026-09-14 — Offline diagnosis correction for 64k trial

The prior section's phrase “truncated lane” was too strong and is corrected
here. The authoritative retained artifact says both result lanes were
`status=success`, `truncated_models=[]`, and `judge_truncated=false`; the Opus
provider response was not token-truncated upstream, and the judge response was
parsed successfully.

The lossy judge-input flag came from the helper's local
`build_panel_synthesis_prompt.lane_material` branch: any serialized lane
material with `encoded_len > 8000` is changed to `materialStatus=compacted`, a
2,400-character line-boundary summary, at most 12 findings, and an explicit
partial caveat. The retained Opus response was 10,602 characters, so this
fixed threshold necessarily applied. The exact compacted final judge prompt is
retained as `metadata.synthesis_prompt_chars=47125`. The raw lane body is not
retained, so its byte-exact uncompact prompt cannot be reconstructed; a
length-preserving offline reconstruction from the retained 1,600-character
preview and 10,602-character total yields approximately 55,208 characters for
ordinary prose and a conservative quote/backslash-escaping upper bound of
63,410 characters. Both fit below 64,000. This establishes that the hardcoded
8,000-character lane compaction, not upstream token truncation, caused
`judge_input_lossy_lanes` for Opus.

There was a second independent loss boundary: the raw chunk review summary was
9,178 characters and was compacted to 2,808 before fan-out because the lane
source budget was 3,000. That set `summary_input_lossy=true`, which the helper
promotes to `panel_status=partial_multi_model` even when both final lane
statuses are success. Removing only lane-material compaction would therefore
not make this exact run a full acceptance; the fan-out summary boundary also
remains a declared non-loss gate.

`--retry 0` controls provider retry count in `generate_with_fallback`; it does
not disable the panel's fixed two-logical-attempt loop. `run_panel_call` always
tries attempts 1 and 2, adding a retry instruction after a non-answer, so the
Opus non-answer caused the second logical attempt despite `retry_count=0`.
The judge had no second attempt because its first output parsed successfully;
`judge_retried=false` is authoritative.

Smallest proposed root fix, not implemented: remove the fixed `encoded_len >
8000` lane compaction and let the already-existing final assembled-prompt
`max_chars` guard fail closed when the complete judge prompt exceeds the
configured budget. If budget-aware compaction is later retained, it must use
remaining whole-prompt budget and mark the result partial explicitly.

Deterministic regression proposal: build two successful mock lanes with one
10,602-character prose response, call
`build_panel_synthesis_prompt(..., max_chars=64000)`, assert the full tail is
present, `judge_input_status=complete`, no lossy lanes, and prompt length below
64,000; then repeat with a cap below the complete prompt and assert a fail-closed
`AntiError` rather than silent lane compaction. A separate small test should
document that `retry=0` still permits the existing non-answer logical second
attempt, while a judge parse-success path has exactly one judge call.

## 2026-09-14 — Offline shared-boundary remediation checkpoint

Commit `dee9b8a` fixes both confirmed local content-loss boundaries in the Anti
panel path. `build_panel_synthesis_prompt` no longer applies the fixed 8,000
character lane-material compaction. Complete successful or upstream-truncated
lane material is retained until the existing assembled judge-prompt
`max_chars` guard; an over-budget complete prompt fails closed. The chunked
review fan-out boundary likewise no longer silently compacts a complete review
summary: when the exact configured fan-out budget is too small, it raises before
any panel lane call with `summary_input_status=not_sent`,
`summary_input_lossy=false`, and truthful scope/status metadata.

Deterministic regressions cover: a >8,000-character lane tail preserved when a
64,000-character synthesis budget fits; a >9,000-character summary preserved
through the actual panel path at a 12,000-character fan-out budget; rejection of
the same summary at a 3,000-character budget with zero provider calls; and the
existing incomplete/non-answer/truncated metadata contracts. The retry
regression explicitly records that `--retry 0` means zero provider retries but
does not disable Anti's existing second logical attempt after a non-answer.

Evidence from this checkpoint:

- Anti helper suite: `205 passed, 9 subtests`.
- Authoritative repository suite: `823 passed, 220 subtests, 2 warnings`.
- Python 3.10 focused regression: `4 passed, 201 deselected`.
- Wheel parity: extracted packaged `anti.py` SHA256 equals source
  `beeeeff9417336a256b1bb9802153b334efdf398d6b6f514d7eeddb33673764b`; the
  packaged request/backend timeout helpers both return `170` for `180`.
- `git diff --check` passed and disk free space was 58 GiB at validation.

This is an offline parent-review checkpoint only. No live gateway switch,
credential/provider mutation, install, restart, merge, or release claim was
made in this turn. The authorized live validation described above remains
pending parent review of `dee9b8a` and current-head CI.

## 2026-09-14 — Structured lane normalization correction

Parent offline review of `dee9b8a` found a sibling loss boundary: valid
structured lane JSON was normalized before judge assembly, including a 1,600
character summary cap and bounded finding/list fields, while the lane metadata
still reported `judge_input_status=complete`. Commit `b746b9b` corrects this
without weakening display or provenance normalization. `parse_panel_findings`
now retains a recursively redacted, uncapped structured copy for judge input;
the existing normalized contract remains the artifact/display representation.
The existing assembled-prompt budget guard still rejects the complete material
before provider generation when it cannot fit.

The new regression uses valid structured JSON with independent long summary,
finding-claim, and list tails. It asserts all tails reach the judge prompt,
mutated input metadata remains `judge_input_status=complete` with an empty
`judge_input_lossy_lanes`, and a cap one character below the complete prompt
fails closed. Offline evidence after this correction: Anti suite `206 passed,
9 subtests`; authoritative repository suite `824 passed, 220 subtests, 2
warnings`; Python 3.10 focused structured/content-loss set `5 passed, 201
deselected`; packaged helper SHA parity
`ba75a8a347c2f853338811f57ebd332ae06a44c726ff7152ee35d832dd2eb44a` and
timeout helpers `180 -> 170`.

The previous `ef0a2b8` CI is stale for this correction. The branch must be
rebuilt and current-head CI must pass for `b746b9b` (plus its forward report
commit) before any separately authorized live validation. Live remains held.

## 2026-09-14 — Offline three-chunk live preflight

The reproducible representative fixture is committed at source head `f2079d2`
under `scratch/t15-representative-3chunk/`; the older
`scratch/t15-multichunk-fixture.md` and all prior artifacts remain untouched.
The three non-overlapping source files are complete and AST-parseable:

- `parser_contract.py`: 5,710 bytes,
  `62ede283c7dd7b8ba8f7222a2ea5422a3759e88c8f4da084e3512fd3963ee227`.
- `storage_contract.py`: 6,707 bytes,
  `a6cf1a2c14cc34f15547bf8387e5d611d7518475a31cdb1e78e5e15040706bc4`.
- `transport_contract.py`: 4,941 bytes,
  `eb10d9971b1d0366176ac7f213c8abed2780a35974722b9eb2e38978dcbd4123`.

Total source delivery is 17,358 bytes. The exact CLI dry-run uses all three
paths as required files, `--max-prompt-chars 12000`, `--max-synthesis-chars
64000`, `--chunked always`, and `--max-review-chunks 3`. It plans exactly three
complete chunks with no omissions: parser 6,753 prompt chars / 5,710 source
bytes; storage 7,752 / 6,707; transport 5,990 / 4,941. The chunk IDs are
`files-088de10e6a1793f5`, `files-8accf10819a92682`, and
`files-3a91c46210c0b66c`.

The planned stages are three Sonnet review chunks, one Sonnet review synthesis,
one Sonnet lane, one Opus lane, and one Opus judge. The release-bound command
uses helper timeout `180` (gateway mapping `170`), `--retry 0`,
`--fallback-policy never`, `--max-parallel 2`, lane/chunk output ceilings of
2,048 tokens, judge output ceiling 4,096, and a finite 64,000-character judge
budget. Primary calls are 7; Anti's fixed second logical attempt for each lane
and judge raises the hard provider-call ceiling to 10 even with retry0; fallback
adds zero calls. An external 20-minute watchdog remains required because the
CLI's 180-second timeout is per provider call, not a whole-run deadline.

The 12,000-character fan-out budget leaves 2,557 characters over a 9,193-
character bounded-summary prompt. An offline structured-payload headroom probe
with two ~8.2k-character lane outputs produced a 41,675-character judge prompt,
leaving 22,325 characters under the 64,000 cap, with
`judge_input_status=complete` and no lossy lanes.

Candidate parity was rebuilt from `f2079d2` without installation: wheel SHA256
`aefb736273a9872178de8c772bd5ee3f5650303068465bd65bae28d75358fa6b`, sdist
SHA256 `d70bd93bbaf57783911a4281aba99fad527a7279e29e5d7938fa30f8886ff99a`,
and packaged Anti/server hashes match the worktree (`ba75a8a3...2eb44a` and
`c3567ba9...c4fcc`). This is plan-ready offline evidence only; no live switch
or provider request has been made.
