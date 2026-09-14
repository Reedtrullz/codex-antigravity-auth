# Anti and gateway audit remediation

## T0 baseline

- Worktree: `/Users/reidar/.codex/worktrees/dfb9/codex-antigravity-auth`
- Branch: `codex/anti-gateway-remediation`
- Starting SHA: `117b496db568a7c222dd1698912224c36f8264da`
- Primary checkout was not edited.
- Initial finding outcomes are pending until regression evidence is collected.

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
| A1 | pending | — | — |
| A2 | pending | — | — |
| A3 | pending | — | — |
| A4 | pending | — | — |
| A5 | pending | — | — |
| A6 | pending | — | — |
| A7 | pending | — | — |
| A8 | pending | — | — |
| A9 | pending | — | — |
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
