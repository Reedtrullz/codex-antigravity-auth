# Service intent, drift and explicit repair

New per-user service installs record a versioned, nonsecret intent under
`ANTIGRAVITY_STATE_HOME/services/gateway-PORT.json`. The intent records the Python
executable, CLI module, package version, host/port, client/state roots, picker
mode, and optional 1Password CLI/path/environment references. It never copies
credential values or reads an env file's contents. File size and modification
time detect ordinary env-file changes; equal metadata is not proof of unchanged
contents or successful secret resolution.

`service status` and `status` compare that intent with the current executable,
package version, namespace selection and secret-runtime references, and with the
installed service definition. The macOS/Linux comparison is deliberately exact:
even a manual comment or formatting edit reports drift. Windows queries the task's
single executable action as XML and compares its command/arguments; it does not
claim equivalence of unrelated task triggers or scheduler policies.

A reachable model catalog alone cannot make a service ready. The loopback-only
`/health/runtime` endpoint reports a process ID and an opaque installation marker,
launch-settings hash and startup package version, without loading account or
provider stores. The marker/hash must match the recorded intent, the service
manager must report active, the gateway must be reachable, and no configuration
drift may remain. A different or missing identity is `foreign_or_unverified` and
service state is `degraded`. This is operational identity evidence, not an
authentication boundary against another program running as the same user.

## Commands

```sh
codex-antigravity service status --port 51122 --json
# Preview the update to the current interpreter/package, retaining saved settings:
codex-antigravity service repair --port 51122 --json
codex-antigravity service repair --port 51122 --write --json
# Explicit changes during repair:
codex-antigravity service repair --port 51122 --unified-model-picker --write
codex-antigravity service repair --port 51122 --no-unified-model-picker --write
codex-antigravity service repair --port 51122 --op-env-file /absolute/refs.env --write
codex-antigravity service repair --port 51122 --clear-secret-runtime --write
# A restart requires verified installed settings; repair drift first:
codex-antigravity service restart --port 51122
codex-antigravity service restart --port 51122 --write
```

Repair and restart default to previews. They may query service-manager status and
the local runtime, but previews do not create/modify service files or intent.
`service install` remains an explicit write/start operation. Mutating user-service
installs must run as that user, without `sudo`, to preserve file ownership. Host
policy remains loopback-only for service commands. Picker mode and namespace roots
are explicit in the service command; changing the invoking environment alone does
not silently reconfigure the running service.

Repairs retain saved host, picker and wrapper-reference choices unless overridden;
they select the invoking interpreter, package version and namespace paths. A new
installation marker distinguishes the repaired registration from its old process.
Restart uses launchd `kickstart`, systemd `restart`, or scheduled-task `End`/`Run`
for the exact owned service name. No command discovers or signals an arbitrary PID
on the port. A failed operation cannot claim owned readiness merely because some
other gateway answers there.

Legacy registrations without intent are reported as unrecorded. They cannot be
adopted or overwritten automatically. Inspect the existing registration and its
settings, explicitly uninstall it, then install with the desired settings. Keep
custom definitions separately before uninstalling. Malformed/future-version
intent and foreign service actions require manual inspection, not guessed repair.

## Publication and recovery

Cooperating mutations lock the intent and definition. Previous intent and service
bytes are retained privately under `services/backups/ID/`. Individual files are
published with a same-directory temporary file, fsync and atomic replacement.
Registration is journaled as `prepared` before definition publication and becomes
`applied` only after service-manager activation is observed. The manager and files
cannot form one atomic transaction: failed reload/start/registration or interrupted
publication leaves explicit incomplete state and recovery paths. It does not
pretend to roll a running service back.

On failure, preserve the reported intent, backup directory and service definition.
Inspect the retained bytes and platform service status before restoring or
reinstalling. A failure before definition replacement may leave an old definition
with newer incomplete intent; automatic repair then refuses mismatched ownership.
Backups are not automatically deleted or exported. A manually edited legacy
service file might contain private environment values, so retained original bytes
remain protected even though generated intent contains no credentials.

The Windows inspection uses the documented
[`schtasks /Query /TN ... /XML` contract](https://learn.microsoft.com/en-us/windows-server/administration/windows-commands/schtasks-query).
Task action quoting uses Python's Windows command-line renderer. Local validation
uses temporary files, simulated launchd/systemd/schtasks managers and a synthetic
loopback runtime server, including failure injection and identity mismatches.
Native service registration and Windows scheduler/ACL execution have not been
verified by those fixtures; platform CI or explicitly authorized host tests are
still needed before claiming native success.
