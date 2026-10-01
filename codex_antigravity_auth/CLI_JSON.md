# Operational JSON and local support evidence

`--json` emits one versioned result on stdout. Progress goes to stderr. The
supported operations are setup inspection/plan, doctor readiness, status,
service install/uninstall/status, logs show/summary/clean, accounts list, models
list and provider list/presets. Human output remains available without `--json`.
`doctor --json` implies the readiness inspector; generation requires explicit
`--live`. JSON readiness skips the package update lookup and cache write.

This changes the previous unversioned CLI JSON layout: scripts must read command
payloads under `data`, rather than at the root. Existing internal Python helpers
keep their return shapes. Schemas ship in [schemas/cli-result-v1.json](schemas/cli-result-v1.json)
and [schemas/support-bundle-v1.json](schemas/support-bundle-v1.json).

```json
{"schemaVersion":1,"command":"doctor","status":"ready","ok":true,"exitCode":0,"warnings":[],"errors":[],"data":{"ok":true,"checks":[]}}
```

| Outcome | Status / exit | Meaning |
| --- | --- | --- |
| Successful inspection/action | ready / 0 | No reported warning or blocker |
| Informational warning | degraded / 0 | Evidence available, limitations in warnings |
| Blocking check or execution failure | failed / 1 | Action/readiness not established |
| Invalid invocation | failed / 2 | Correct arguments before retrying |
| Cancelled collection | failed / 130 | No successful result claimed |

An unreachable `status`/`service status` observation is informational; failed
readiness is blocking. Check-level detail remains in `data.checks`; envelope
codes are stable categories. Exceptions never become raw error messages in JSON.
`logs --json --follow` is refused because it cannot produce a single result.
JSON log inspection is bounded; summary retains the requested-window gap flag.
Accounts/provider list project metadata and credential-presence booleans without
account emails, keys or custom provider endpoint/header values. Indices are
positions in this inspection, not durable account/provider identities. Other
operational payloads may contain local paths or custom labels, so use a support
bundle for sharing.

## Support bundles

```bash
codex-antigravity support-bundle --since 24h
codex-antigravity support-bundle --since 24h --request-id REQUEST_ID
codex-antigravity support-bundle --output ./support.json --write
```

The default is a JSON preview. `--output` alone does not write; export requires
both the explicit path and `--write`. The parent directory must already exist.
Files are created privately and exclusively, so existing files and symlinks are
never overwritten. An interrupted/failed write may retain the newly created
partial file; inspect/remove it explicitly before retrying. There is no upload.

Bundles contain package/Python/platform versions, config structure and selected
provider protocol/endpoint category, account/provider store readability (without
decryption), aggregate request counts, requested-window gaps and optionally
selected sanitized request events. Route observation is structural/offline: no
claim of authentication or live provider readiness. No provider, gateway, update
service, keyring or OAuth request is made. Empty namespaces are not created.
Reading existing request history uses its cooperating file lock; lock metadata
and managed directory permissions may be maintained by that existing primitive.

Only fixed fields, booleans, bounded numbers, normalized timestamps and enums
enter a bundle. Config values, source files, prompts, raw process logs, provider
URLs/names, account emails, auth stores and keys are excluded. Store files are
opened for metadata verification but their bytes are not read. Config is bounded
to 1 MiB; history to 2 MiB and 2,000 rows; selected requests to 20 IDs and 100
rows; serialized bundles to 256 KiB. An oversized history segment is omitted as
a whole, with an explicit gap. Unknown/omitted coverage never becomes a complete
window claim. The tool does not recursively collect arbitrary files.

Selected IDs are matched locally and replaced with per-bundle keyed references;
the key is discarded. `selectedInputIndex` identifies the position of the input
selector. References cannot correlate separate exports, and an unobserved ID is
reported as unobserved, not assumed successful. Selection follows the requested
time window; records without timestamps cannot establish membership.

Tests use temporary namespaces, synthetic credentials and blocked connections.
Local POSIX private-file checks are not native Windows ACL evidence; the shared
protection helper's platform limitations still apply.
