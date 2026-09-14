# Anti and gateway audit remediation

## T0 baseline

- Worktree: `/Users/reidar/.codex/worktrees/dfb9/codex-antigravity-auth`
- Branch: `codex/anti-gateway-remediation`
- Starting SHA: `117b496db568a7c222dd1698912224c36f8264da`
- Primary checkout was not edited.
- Initial finding outcomes are pending until regression evidence is collected.

Offline remediation evidence through T5:

- T1/T2: staged/working-tree scope is NUL-safe, preserves deletions/renames/Unicode, captures one source snapshot, and records planned/attempted/completed/failed/not-sent chunk buckets.
- T3/T4: upstream incomplete/empty output is not a completed answer; partial panel/review/plan results retain artifacts and exit nonzero, while successful retries remain zero; capped plans refuse exact execution unless `--allow-partial` is explicit.
- T5: consult summary artifacts retain the complete answer; finding provenance clears model-forged chunk/excerpt fields and derives excerpt hashes only from the captured snapshot.
- Focused Anti suite: `189 passed, 9 subtests passed`.

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
| A1 | fixed offline | pending commit | Capped plan/review exits nonzero; exact-off refusal test; `189 passed, 9 subtests passed` |
| A2 | fixed offline | pending commit | 11-chunk execution manifest and failure test: 1 completed, 1 failed, 9 never sent; original error retained |
| A3 | fixed offline | pending commit | Upstream `incomplete` and completed-empty responses classify as incomplete/empty |
| A4 | partial offline | pending commit | Zero budget makes zero provider calls; whole-call reservation/race coverage remains T6 |
| A5 | fixed offline | pending commit | Complete consult answer retained in summary artifact |
| A6 | fixed offline | pending commit | Parse/truncation/loss facts force partial status and nonzero exit |
| A7 | fixed offline | pending commit | Provenance fields are authoritative from captured scope, not model text |
| A8 | fixed offline | pending commit | Partial coverage is explicit in nested/top-level artifacts |
| A9 | fixed offline | pending commit | Plan cap cannot silently become complete |
| A10 | pending | — | — |
| G1 | pending | — | — |
| G2 | pending | — | — |
| G3 | pending | — | — |
| G4 | pending | — | — |
| G5 | pending | — | — |
| G6 | pending | — | — |
| G7 | pending | — | — |
| G8 | pending | — | — |
| R1 | pending | — | — |
| R2 | pending | — | — |
| D1 | pending | — | — |

## Verification boundary

This report will record exact commands, commits, artifacts, residual risks, and
live/offline boundaries. Green tests alone will not be treated as release,
provider, or complete-review approval.
