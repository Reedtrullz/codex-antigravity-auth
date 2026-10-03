# Local read-only HTML reports

Export saved runs or reflection verdicts to a self-contained HTML file. Open it
in your browser yourself; the helper does not start a server, launch a browser,
upload source, install tools, contact models or execute verification commands.

```sh
python scripts/anti.py runs report RUN_ID --output run.html
python scripts/anti.py runs report RUN_A --compare RUN_B --output compare.html
python scripts/anti.py runs export --repo /path/to/repo --run-id RUN_A \
  --compare-run-id RUN_B --format html --output findings.html
```

Omit `--output` to print HTML to stdout. Output creation is atomic, owner-only,
and refuses existing files or symlinks. The commands never rewrite saved runs or
reflection history. Comparison is a display of two records, not a judgement that
a finding disappeared, was resolved, or was reviewed under equivalent scope.

## Choose the evidence source

| Command | Input and meaning |
| --- | --- |
| `runs report ID` | Saved run index and its committed publication. Validates schema, identity, checksums, retention and coverage relationships before rendering a captured byte snapshot. ID prefixes use the same unique-match rule as `runs show`. |
| `runs report ID --compare ID` | Two validated saved publications, side by side on wide screens and stacked on narrow screens. |
| `runs export --format html` | Existing sanitized reflection report, including independently supplied local confirmed/rejected/unresolved verdicts and their evidence. Use `--run-id` to select one, or add `--compare-run-id` for a pair. |

Publication consistency does not validate model claims. The page keeps lifecycle,
code scope, panel integrity, verification, retention and media coverage separate.
Partial, degraded, unverified and unknown states are shown literally. Requested
models are never silently promoted to actual models. Missing legacy metadata
stays unknown; recognized legacy `status` values are displayed when `runStatus`
is absent, with legacy `error` shown as `failed`. Retained result source commits
backfill missing index metadata; conflicting recorded commits remain visible.
Never mode cannot supply output or findings that were not retained.

Reflection history may lack lifecycle or reviewer-lane fields; those stay unknown
or absent in its HTML view. Use the saved publication view for retained terminal
state and lane output.

The saved-run view does **not** join mutable reflection adjudications by matching
an unverified finding ID. Its model findings remain unresolved. Use reflection
HTML export for explicit local verdicts. That export preserves model evidence,
file checks and local evidence separately; a passing syntax check or a confirmed
run-level verdict cannot promote an individual model claim. Source and record
hashes remain visible for comparisons.

## Static and accessible behavior

All model-controlled HTML, Markdown, URLs, commands and paths are rendered as
escaped text. No model-provided hyperlink is made clickable. Only generated
same-document navigation links are active. The page includes no JavaScript,
forms, frames, embedded objects, images, remote fonts/styles or analytics. A
restrictive Content Security Policy disables scripts and network resource loads.
Commands shown in check evidence are inert, not launch buttons.

Semantic headings, definition lists, labeled navigation, table captions, header
cells, a skip link, visible keyboard focus and native `details`/`summary` controls
support keyboard and screen-reader navigation. The two-column view stacks below
850px; long hashes and evidence wrap. No color-only status is used. Browser
accessibility-tree and keyboard tests do not substitute for a human assistive-
technology audit; recorded verification notes state what was actually exercised.

## Bounds and privacy

Saved-run HTML capture permits at most8MiB per file,16MiB per publication and256
referenced lanes, using regular-file/no-follow reads and identity checks. Each
referenced path is captured once and those same bytes are used for checksum
validation and rendering, so a later revision cannot replace the validated view.
A malformed, changed, over-limit or corrupt publication fails without creating an
HTML file. Larger publications remain available to the existing JSON commands.

HTML renders at most20 runs,200 findings or lanes per run and8,000 characters per
displayed field, with explicit shortened/undisplayed notices; the complete page
is capped at4MiB. Retained and declared counts remain distinct. These are view
limits, never a reason to change original scope or retention flags. Select fewer
runs or inspect JSON for details beyond the display limits.

Credential redaction runs before escaping and text shortening. Known local-root
and absolute-file fields are omitted. Free-form evidence can still contain
private code or information not recognizable as a credential; inspect the local
report before sharing. This feature does not upload, persist additional raw
source, or broaden the original recording policy.

Full-retention saved views show the validated raw lane artifacts separately from
reviewer summaries, including their saved SHA-256 and generation/output evidence.
Both use the same explicit display limits; summary/never modes cannot reconstruct
raw lane files. Rendering uses only the captured validated bytes, even if a later
writer changes files while the HTML is being assembled.
