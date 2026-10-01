# Encryption-key identity and recovery

The gateway encrypts its account and BYOK provider stores with the existing
Fernet implementation. Key operations preserve the store's raw JSON bytes;
they do not normalize account-state versions or discard unknown fields.
`ANTIGRAVITY_STATE_HOME` owns these files independently of client `CODEX_HOME`.

A private `antigravity-storage-key.json` selection record uses schemaVersion 1,
a backend (`environment`, `file`, `keyring`), a SHA-256 identity of decoded key
bytes, and an optional keyring slot (`legacy` or `id`). Ordinary use records an
existing compatible identity or initializes a new identity only when no encrypted
store requires an unavailable key. Conflicting unrecorded file/keyring material
is refused even when an environment key is supplied. Ordinary file-key use
protects the existing key file and rechecks its identity before returning it;
read-only inspection never changes permissions. Environment keys must match a recorded identity; removing a required
environment key never silently selects another backend.

Existing legacy keyring entries remain readable. New keyring entries use
`storage-encryption-key:<keyId>` and are never substituted for another key's
slot or written over the legacy entry. Other namespaces' keyring entries are
not enumerated or deleted. A recorded file backend remains selected when an
inactive keyring backend returns. An unavailable or mismatched recorded backend
fails explicitly rather than generating a replacement key.

## Read-only diagnosis

```sh
codex-antigravity storage keys
```

This prints source availability, fingerprints, the recorded selection and
recovery state, never key values or decrypted tokens. Inspection does not create,
chmod, migrate or reset files or keyring entries. It checks whether available
material decrypts the managed stores, but does not claim provider generation
readiness. An absent namespace remains absent. Unsafe paths, unsupported
selection metadata, conflicting identities and interrupted transitions are
reported explicitly. The ordinary `doctor` encryption check uses the same
read-only selected-backend report, so inactive legacy keys cannot mask a missing
or mismatched selected key.

## Backup and re-encryption

Supply a valid Fernet key through an environment variable managed by your secret
manager. Keep that value independently: the command does not print or recover a
lost backup key. The target/backup key must be a valid Fernet key; the existing
legacy `ANTIGRAVITY_STORAGE_KEY` normalization remains supported for old stores.
Additional old Fernet keys can be supplied with repeated `--source-key-env NAME`
(up to eight). Stop gateway/configuration writers before operating.

Every command defaults to a no-write plan. Repeat the same command with `--write`
to apply it:

```sh
codex-antigravity storage backup --key-env RECOVERY_KEY --output before.agbackup
codex-antigravity storage backup --key-env RECOVERY_KEY --output before.agbackup --write

codex-antigravity storage reencrypt --key-env NEXT_STORAGE_KEY --backend file --backup before-change.agbackup
codex-antigravity storage reencrypt --key-env NEXT_STORAGE_KEY --backend file --backup before-change.agbackup --write
```

Re-encryption uses the supplied key for both the recovery archive and the new
store encryption. `--backend keyring` selects a key-identity slot instead of the
private local key file. File-backend key material is intentionally stored in its
owner-protected key file; OAuth/API tokens remain encrypted. Neither operation
prints keys or writes cleartext tokens into backup/staging files. Inactive old
keyring entries are retained. A stale `ANTIGRAVITY_STORAGE_KEY` environment must
be cleared or updated after changing identity; the result reports this condition.

The two managed store locks are acquired before the initialization lock, matching
normal writer order. Source bytes and their needed keys are captured together.
A complete encrypted recovery archive is durably published at a new path before
any key/store mutation. The staging hardlink is removed and the directory synced
before the transition marker is written. Backup paths cannot alias managed
store/key/lock paths, and existing files or symlinks are never overwritten.

## Interrupted operations and restore

A durable `antigravity-storage-transition.json` marker blocks ordinary store
access until stores, key backend and selection have all been verified. This also
blocks plaintext legacy reads/mutations; diagnostics report recovery required.
A failed transition keeps its encrypted backup and marker rather than guessing
which partial state is authoritative. Original ciphertext and local key/selection
bytes remain inside the encrypted archive.

```sh
codex-antigravity storage restore --backup before-change.agbackup --key-env NEXT_STORAGE_KEY
codex-antigravity storage restore --backup before-change.agbackup --key-env NEXT_STORAGE_KEY --safety-backup before-restore.agbackup --write
```

Restore validates a matching backup key, format/version, manifests, checksums and
store decryptability before mutation. `--write` always requires a new encrypted
safety backup of current state, even for a fresh namespace. A backup captured
while recovering is labelled as such. Recovery can use the original backup's
keys to understand a mixed interrupted state. Restored payloads are re-encrypted
under the supplied backup key and selected target backend; raw token/schema data
is unchanged, though ciphertext IVs change. Restore can also target a fresh
`ANTIGRAVITY_STATE_HOME` without access to the original OS keyring.

If final marker-removal directory synchronization fails after validation, the
command reports `finalization_sync_failed`: data/key verification completed and
the backup remains, but final directory durability was not confirmed. Failures
before marker removal report recovery required. No claim is made against a
process already running as the same account owner or an administrator.

## Portable backup format v1

The outer UTF-8 JSON object has exactly these fields:

| Field | Value |
| --- | --- |
| `format` | `antigravity-storage-backup` |
| `version` | integer 1 |
| `encryption` | `fernet` |
| `keyId` | SHA-256 identity of the required external backup key |
| `payload` | authenticated Fernet ciphertext containing the manifest |

The encrypted manifest contains the same format name, `schemaVersion:1`, a
`sourceTransitionPending` boolean, `files`, `keys` and `keyRequirements`. File
names are a fixed allowlist: `accounts`, `providers`, `key_file`, `selection`.
Absent files are null; present files contain base64 `data` and a SHA-256 checksum
of their exact original bytes. `keys` maps needed source-key identities to their
key values **inside the encrypted payload**. Unneeded inactive keyring material
is not exported. The key requirements state that the matching backup key is
required and no external store keys are required to restore this archive.

Archives are limited to 96 MiB; each managed store to 16 MiB for backup/recovery and
bounded diagnosis; local key/selection files to 4 KiB. Normal key selection does
not add a size cap to otherwise supported existing stores. Unknown versions,
malformed keys/checksums, missing required fields and unrecognized file names
fail before restoration. No manifest path is used as a filesystem destination.
The archive is not a general backup of client auth, Google OAuth client settings,
logs or Anti history.

Namespace copies carry the key-selection record, require same-machine keyring or
environment availability as before, and refuse any pending transition. They are
separate from these portable encrypted archives.
