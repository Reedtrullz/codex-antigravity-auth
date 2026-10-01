# Saved Anti publications

Saved files use separate versions from the console JSON response. JSON schemas ship in [schemas/](schemas/): [run index v1](schemas/run-index-v1.json), [saved result v2](schemas/saved-result-v2.json), and [raw lane v1](schemas/raw-lane-v1.json). Additional fields are allowed in content publications; never-mode indexes and their metadata use closed lifecycle allowlists. The standalone `anti_lib.artifacts` validator also enforces cross-file identity, paths, lifecycle/scope agreement, retention declarations and checksums; JSON Schema alone cannot establish those relationships.

A run index lives at `<runs>/<runId>.json`. Summary/full indexes contain:

```json
{
  "recordSchemaVersion": 1,
  "publication": {
    "revision": "32 lowercase hexadecimal characters",
    "result": {"path": "<runId>/revisions/<revision>/result.json", "sha256": "64 lowercase hexadecimal characters"},
    "lanes": []
  }
}
```

This is a field illustration, not a complete valid record. The index also carries the ID, writer ID, lifecycle, scope, retention mode and `resultPath`. Every result and raw lane carries the same run/revision identity. References are canonical paths relative to the run store, contained under that revision and free of symlink traversal; compatibility `resultPath`/`rawLanePaths` fields remain absolute local paths. Moving a store requires updating those local aliases and checksums; the reader will not follow old paths outside the store.

## Publication and reading

Under the existing per-run lock, the writer creates a fresh revision directory, writes full-mode lane files, then writes the result. It records SHA-256 checksums of their exact saved bytes, synchronizes files/directories where supported, validates the publication, and atomically replaces the index last. Existing revisions are never overwritten. The index replacement is the commit point; a readable unreferenced result is not a completed run.

Readers must open `<runId>.json` and follow its committed `resultPath`, rather than construct `<runId>/result.json`. `runs show <id>` validates the committed files before returning the index. `runs list --json` reports publication errors instead of showing a damaged current publication as successful. Cleanup retains invalid current publications. A failed later publication leaves the previous index/revision readable; an orphan initial revision with no index reports `incomplete_publication`. Unreferenced revisions are preserved until the owning run is explicitly cleaned.

| Reader result | Meaning |
| --- | --- |
| `publicationStatus: validated` | The current index, referenced files, identities, checksums and status relationships agree. |
| `publicationStatus: lifecycle_only` | A current never-mode index has no content artifact references. |
| `publicationStatus: legacy_unverified` | An unversioned index, optionally with a v1 result, was adapted without adding checksum assurance or inventing scope. |
| `unsupported_version` | The index/result/lane version is unknown; no guess or rewrite is made. |
| `incomplete_publication`, `checksum_mismatch`, `identity_mismatch`, `conflicting_status`, `retention_mismatch`, `invalid_reference` | The publication cannot be trusted as complete. Preserve it for inspection. |

Legacy references are checked for containment, presence and understood shape when supplied. Older metadata-only indexes remain readable without inventing result files. Legacy reads never rewrite history. Raw JSON/shape/read failures also return explicit errors.

## Limits of the contract

Publication validation establishes file consistency, **not correctness of model findings**. Lifecycle success and scope completeness are independent: a successful execution may have partial scope, and positive failed/omitted/not-sent counts, incomplete detailed coverage and nonempty loss lists require partial coverage and scope. Known index omissions also require partial scope. Verification retains its own `not_run`, `completed_no_evidence`, `tool_checks` or `unknown` state; matching checksums never promote that state.

The recording policy still applies. Never mode publishes only its lifecycle index. Summary mode publishes bounded previews with `retention.contentComplete=false`, independent of scope coverage, and no raw lanes. Index and result retention declarations must agree (`summary`/`contentComplete=false` or `full`/`contentComplete=true`); this describes retention independently of scope. Full mode may publish up to 10,000 referenced lane files and retains redacted output. Schemas do not authorize saving prompts or secrets. The ownership and cleanup contracts in [SKILL.md](SKILL.md#operational-fallbacks) still apply.

Optional `verification.checks` records carry structured file-check outcomes (`passed`, `failed`, `skipped`, `error`), captured file hashes, command/cwd identities, bounded output and duration. Their presence does not change the finding claim verdict. Full retention stores the details; summary stores check counts and `checksRetained=false` so preview clipping cannot create malformed check descriptors.

ESLint check identity is invocation-scoped: `identityContext` records a fresh observation ID and explicit unknown effective tool/config markers, and `comparableAcrossRuns=false` prevents treating command-path equality as stable tool/config identity. The same observation may be reused for duplicate findings within one batch.

## Finding adjudication and review exports

Reflection history now retains full redacted advisory/check fields when full
recording was selected. Summary recording keeps bounded previews and original
finding counts; never mode still writes no reflections. Capture carries actual
models/providers and known scope gaps. Legacy records have unknown provenance;
a requested model is never silently presented as an actual model.

`runs export --repo PATH [--run-id ID] --format json|sarif|markdown` reads retained
reflection history without modifying it. JSON follows
[review report v1](schemas/review-report-v1.json). SARIF uses the
[OASIS 2.1.0 contract](https://docs.oasis-open.org/sarif/sarif/v2.1.0/os/sarif-v2.1.0-os.html);
offline tests validate against the official schema in `tests/fixtures/`. Every
SARIF result remains `kind: review`, and rejected findings remain present without
SARIF suppressions. Anti's provenance, scope, advisory/check data and separate
local verdict appear in `properties`. Markdown carries the same evidence in
readable form. No command uploads or publishes a report.

Use the exported `findingKey` with `runs finding --repo PATH --run-id ID
--finding KEY --verdict confirmed|rejected|unresolved --author LABEL
--source-file RELATIVE_PATH --evidence TEXT` (or `--evidence-file UTF8_FILE`).
The inspected source must remain within the repo and be at most 512 KiB. Only its
SHA-256 and relative path are stored, along with the explicit author, current
UTC epoch timestamp and redacted evidence. Author labels are at most 120
characters; evidence is at most 4000 characters (input files at most 16000 bytes).
A new local verdict replaces that finding's prior local verdict; model claim,
model evidence and file-check outcome stay visible and unchanged in meaning.
The legacy run-level verdict never propagates to individual findings.

Manual adjudication is a separate explicit local write, with at most one bounded
annotation per retained finding; it does not enlarge or restore automatic model
previews. The summary `content_preview` budget applies to captured model content,
while explicit manual evidence has the separate 4000-character limit above.
Unretained findings cannot be adjudicated or reconstructed. Exports disclose
retained versus declared normalized counts and `contentComplete=false` for summaries;
missing provenance remains unknown. If a combined report exceeds the shared
structured-redaction budget, export an individual `--run-id` instead of silently
truncating the result.

Recurring summaries separate confirmed, rejected and unresolved cohorts and
retain their evidence/occurrences. The inspected source hash (or captured check
hash when no local verdict exists) allows content identity across changed paths.
Equal content does not prove a rename rather than identical copies. Neither
repetition, a passing syntax check, artifact checksums nor manual annotation
turns the original model claim into an automatically verified fact.

`--output PATH` creates a complete new owner-only file using same-directory
staging and atomic no-replace publication; existing files and symlinks are
refused. Unsupported hard-link filesystems fail explicitly. Exports redact
credentials, omit the workspace root from their envelope, and use relative
owned locations. Explicit author/evidence labels are still user-authored content;
inspect reports before sharing. There is no automatic false-positive suppression,
routing change or GitHub publication.

Parser normalization loss has separate `parserFindingTotal`,
`parserFindingsDropped` and `parserLossStatus` fields in every export format.
These count rows before normalization and rows removed by normalization or
deduplication; they do not redefine `contentComplete`, which only describes
retention of normalized findings. Legacy missing parser counters are `null` and
`unknown`, never invented zeros. Markdown includes run timestamps, source-record
hashes and the same verdict/content-identity cohort summaries as JSON and SARIF.
