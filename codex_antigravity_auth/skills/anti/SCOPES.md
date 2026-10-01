# Review inventories and untracked files

`review --scope working-tree` keeps its tracked diff semantics and reports
untracked files it excludes. Add `--include-untracked` to capture their current
file bytes as additional review input. An explicitly selected `--file` remains
explicit. Staged and revision-diff reviews never add untracked content; the flag
is rejected for those scopes. Empty working-tree reviews still report excluded
untracked paths instead of suggesting that no files exist.

For a repository audit, use an explicit inventory:

```sh
python3 scripts/anti.py review --scope repository --review-root packages/api --review-root packages/web --include-untracked --exclude-path packages/web/vendor --required-file packages/api/pyproject.toml --print-prompt --json
```

Paths are relative to the Git repository root, even when invoked from a nested
directory. Repeat `--review-root` for literal existing directories; omit it for
the repository root. `--exclude-path` is a literal file or directory prefix,
not a glob. `--file` / `--files-from` narrow the inventory to exact file paths;
missing selections fail. These root/exclusion options require repository scope.
Repository scope captures current files, not a revision diff, and refuses
`--base` / `--changed-files`.

Inventory uses bounded `git ls-files` queries for tracked, untracked and ignored
paths. Untracked content remains opt-in. Ignored directories are reported as
excluded subtrees without listing every descendant. The usual sensitive/cache
and binary-looking exclusions remain active. Symlinks, including links to files
inside the repository, and nonregular files are excluded. Missing tracked files
and selected files that cannot be read are recorded as omissions. Gitlinks are
not recursively audited. Nested manifest names identify `package_roots` in the
inventory; no package code or configuration is executed.

Each Git query has a ten-second deadline, a 4 MiB path-output cap and a 20,000-path
cap; the combined inventory also has a 20,000-path cap. Overflow or Git failure
refuses the inventory before generation; narrow the roots to proceed. Repository
source capture and opt-in untracked capture have a 2 MiB per-file limit and a
16 MiB combined read budget. Oversized/unreadable files remain explicit coverage
omissions; source is never silently clipped. File capture checks path and
descriptor identity; POSIX additionally opens parent directories without following
links. This is a bounded snapshot, not a filesystem transaction against concurrent
repository mutation. Required files must be selected and fully captured; existing
chunk planning also refuses a cap that omits any required file.

JSON review metadata retains `inventory`: requested roots/exclusions, ignored and
untracked exclusions with reasons, package roots, and enumeration counts. Existing
byte/hash coverage describes captured source and chunk omissions separately.
`inventory_complete` means enumeration completed within its limits; it does not
mean all enumerated files were submitted or reviewed. Preview the selected source
with `--print-prompt --json`; bounded chunk omissions still require the existing
`--allow-partial` opt-in for generation. Saved metadata follows the selected
recording policy and may be reduced in summary/never modes.

The same options work with `panel --mode review` and review workflows, including
`workflow review-ready`. Prompt-only and planning workflows refuse them. Repository
data-routing policies still apply before captured content can be submitted. Tests
use temporary Git repositories and synthetic source; no live provider validation
is implied.
