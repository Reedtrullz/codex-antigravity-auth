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
- Full suite with the repository's 1Password signing configuration isolated for temporary Git fixtures: `795 passed, 220 subtests, 2 warnings`.
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
| A4 | fixed offline | `90c719f` | Shared locked admission covers every provider attempt; zero-budget and concurrent last-allowance tests make no provider call when refused. |
| A5 | fixed offline | `90c719f` | Complete consult answer remains retrievable in the result artifact; prompt retention uses hashes/counts and sanitized outputs. |
| A6 | fixed offline | `90c719f` | Parse/truncation/loss facts force partial status and nonzero exit. |
| A7 | fixed offline | `90c719f` | Provenance is derived from captured scope/chunk ranges; forged unresolved fields clear to null. |
| A8 | fixed offline | `90c719f` | NUL-safe staged scope preserves deletions/renames and the exact scope snapshot; partial coverage remains explicit in artifacts. |
| A9 | fixed offline | `90c719f` | Plan caps cannot silently become complete. |
| A10 | fixed offline | `a3b97ff`, `90c719f` | Helper/bundle parity is checked before generation; clean local and bundled hashes matched in the worktree. |
| G1 | fixed offline | `90c719f` | Real ASGI disconnect regression covers bounded diagnostics and exactly-once lease release; cleanup is shielded and release is independent. |
| G2 | fixed offline | current remediation patch | Hard-expiry failures mark the mutation dirty and persist cooldown state; a same-account background refresh cannot make selection wait and then use an expired token. |
| G3 | fixed offline | `90c719f` | Refresh merge checks refresh token, access token and expiry snapshot; stale same-token results are discarded. |
| G4 | fixed offline | `340ec96` | Adjacent Responses function calls group into one ordered assistant tool-call turn without crossing intervening messages. |
| G5 | fixed offline | `90c719f` | Native Responses SSE adapter is exercised for valid terminal, premature EOF, duplicate terminal, model rewrite and closure. |
| G6 | fixed offline | current remediation patch | Malformed `$ref`/`properties` schemas return 400 before account/provider selection; both flat and nested valid tool shapes pass schema validation; numeric boundaries remain fail-closed. |
| G7 | fixed offline | `90c719f` | BYOK telemetry reads normalized Responses terminal status and nested usage, with compatibility coverage for the transport seam. |
| G8 | fixed offline | current remediation patch | Service state composes installed/active/reachable observations; registered-but-unreachable is `active_unreachable`, while a reachable managed service is `ready`. |
| R1 | fixed offline | `340ec96` | OAuth login uses transactional per-account merge and preserves concurrent account state. |
| R2 | fixed offline | `90c719f` | Reflection read-modify-write is cross-process locked and atomic; concurrent writers, pruning and symlink sentinels are tested. |
| D1 | fixed offline | `13e8df9`, `a3b97ff`, `90c719f` | README/SKILL describe partial exits, retention, estimates, parity and non-claims; no new broad abstraction was introduced. |

## Verification boundary

## Acceptance boundary

The reopened offline blocker suite is green in the current worktree: `795
passed, 220 subtests, 2 warnings`. Exact closure evidence includes
`test_hard_refresh_failure_persists_for_select_and_acquire`,
`test_selection_does_not_wait_on_background_same_account_refresh`,
`test_responses_endpoint_rejects_malformed_tool_schema_before_provider`,
`test_responses_endpoint_accepts_flat_and_nested_tool_schema_shapes`,
`test_run_gateway_status_uses_waiting_reachability_probe`, and
`test_run_gateway_status_marks_registered_but_unreachable_service_degraded`.
Coordinator acceptance remains pending review of this closure. The temporary Git-fixture failures under the normal environment were
caused by the user's global 1Password SSH signing hook; the authoritative full
suite run used `GIT_CONFIG_GLOBAL=/dev/null` and did not change repository or
global configuration.

No live Sonnet/Opus panel, representative multi-chunk live run, gateway restart,
credential mutation, provider mutation, push, merge, or release was performed.
The coordinator explicitly paused further live calls while reviewing the
offline blockers. Therefore this report does not claim live provider health,
production readiness, full-repository review coverage, or release acceptance.
