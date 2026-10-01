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
