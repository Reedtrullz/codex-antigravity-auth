# Immutable diff locations

Anti captures ordinary unified Git patches once for staged, working-tree and
revision-diff reviews. It requests full blob IDs and fixed, uncolored prefixes;
external diff helpers and text conversion remain disabled. Non-UTF-8 patch text
fails before generation instead of introducing replacement-based evidence.
Explicit selections are matched literally and classified against the selected
diff, so deleted and rename-old paths remain diff-backed even after leaving the
current index. They are not reread as missing working-tree files.

The coordinate index records old/new paths and blob IDs, rename mappings, hunk
ranges, and the exact changed/context lines in the captured patch. Subsequent
finding enrichment uses this index without rereading the working tree. A
`sourceCommit` is the review's contextual commit, while `diffProvenance.blob` is
the identity of the specific side supplied by the captured patch. The diff's
SHA-256 identifies the patch itself. This does not turn concurrent Git/filesystem
activity into an atomic transaction.

Findings may specify `diffSide: old` or `diffSide: new`; omission means `new` for
compatibility. The finding path must match that side's path, including the original
path for a renamed old-side location. Missing, invalid or ambiguous coordinates
become `line: null`, `locationStatus: unknown`, with a fixed `locationReason`.
Combined patches, binary files, submodules, symlinks, malformed/truncated hunks,
missing full blob identities and lines outside captured hunks are not fabricated
into source locations.

`locationStatus: mapped` means the line is supported by the captured patch and a
complete row was within a submitted source range. Single-prompt truncation and
chunk caps cannot authorize locations in the omitted tail; planning a chunk is
not submission. Chunk IDs refer to the source chunk containing that row. A
failed model's submitted input can still have a known location while its coverage
remains partial. Model claims remain advisory and unverified; mapping does not
establish correctness or runtime behavior. Separate file checks may inspect the
current workspace, but do not replace this captured location evidence.

`sourceExcerpt` is the captured line after credential redaction, with
`excerptRedacted` indicating any change. `excerptSha256` hashes the original UTF-8
line without its patch prefix or final LF (a source CR is retained). Blank lines
therefore have a valid empty-string hash. The newline marker and old/new side
remain in `diffProvenance`. No model-supplied excerpt, hash or verification label
is trusted. Saved findings follow full/summary/never recording policy; summary
retention may omit or shorten evidence.

Coordinate indexing is bounded to 4 Mi characters of patch text, 5,000 files,
5,000 hunks, 50,000 source rows, 16,384 characters per row and signed 32-bit line
coordinates. Unsupported or over-limit indexing yields explicit unknowns and does
not upgrade or erase review coverage. Existing source/prompt limits still govern
the review payload itself. The private line lookup stays local; public metadata
retains identity/range summaries, and judge prompts do not duplicate that index.

Tests use synthetic local repositories and mocked generation, including staged
versus unstaged changes, renames, empty/deleted/context lines, quoted paths,
multiple hunks, subsequent mutation, unsupported formats, partial submission and
saved-artifact round trips. No live-provider acceptance is claimed.

The parser follows the ordinary patch headers and line conventions documented by
[Git's diff format reference](https://git-scm.com/docs/diff-format), checked
2026-10-01. Combined-diff syntax remains explicitly unsupported.
