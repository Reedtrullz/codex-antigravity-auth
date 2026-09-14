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

Offline closure is verified, but live generation is not healthy in the currently
reachable gateway. A bounded live panel attempt on `README.md` used Sonnet and
Opus lanes with `--fallback-policy never`, `--max-parallel 2`, `--retry 1`, and
`--timeout 45`. The gateway catalog probe returned HTTP 200 and advertised both
requested Claude models; Sonnet generation timed out on both allowed attempts,
the helper reported that `/v1/models` remained responsive while the generation
path was unhealthy, and no JSON result artifact was produced. No further live
retry, restart, credential/provider mutation, push, merge, or release was done.

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
health or production readiness. A representative multi-chunk live run was not
attempted after that terminal failure. Therefore this report does not claim
live provider health, full-repository review coverage, or release acceptance.
