# Gateway process-log privacy and retention

All supported `codex-antigravity start` entry points install the same logging
boundary before Uvicorn starts. Foreground runs additionally mirror sanitized
records to the console. Background runs and installed service commands write
through the bounded application handler; their raw stdout/stderr are not routed
to append-only files.

## Limits and ownership

Per port, `~/.codex/antigravity-process-logs/gateway-<port>.log` holds at most
2 MiB; `.1` and `.2` retain at most 2 MiB each, for 6 MiB total log content.
A record is at most 4 KiB including its newline. Truncation happens after
redaction and respects UTF-8 boundaries. Size checks, rotation and writes share
a cross-process lock. Lock/policy metadata is separate from those content bounds.
Disk/rotation failures drop records without failing gateway requests.

The directory's `.policy-v1` marker identifies this retention policy. An existing
nonempty directory without the expected marker is refused, not silently adopted.
Existing symlinked or multiply linked log files are refused. POSIX files use
0600 and the owned log directory uses 0700. Windows uses the user's inherited
filesystem access policy; native Windows execution is not claimed by local macOS
test results.

This policy manages only the new process-log directory. Older
`antigravity-gateway-<port>.log` and `antigravity-service-<port>.out.log`/`.err.log`
are neither read, rotated nor deleted. They may contain identities or credentials
and remain under the owner's manual retention policy. Reinstall existing services
to update launchd/systemd output routing. Foreground terminal history and files
created by shell redirection are externally managed, with no application-enforced
retention limit. Launching `uvicorn` directly bypasses this CLI policy.

## Privacy boundary

Account runtime messages use per-process HMAC references. References correlate
messages within a process and change on restart; they are not persistent account
identifiers. Project discovery reports success without logging the project ID.
Account refresh/rotation errors retain categories and exception types, not raw
exception values. Explicit local account-list/reset/remove commands continue to
accept or display emails.

Every complete logging record passes through shared credential redaction and
email masking. Control characters, including newlines, are escaped. Exception
traces include exception type and a bounded list of basename/line/function frames,
without exception values, source code, locals or full paths. Raw unstructured
Python stdout/stderr fragments are suppressed with a one-time notice per stream;
fragments can split sensitive values and cannot be safely treated as independent
messages. Uvicorn URL access logging is disabled; use sanitized structured request
telemetry for requests. Arbitrary values that do not match credential/email forms
are not promised to be discoverable as secrets; runtime call sites avoid emitting
raw provider bodies, account IDs, project IDs and exception values.

Background and service bootstrap wrapper output is discarded, including 1Password
wrapper diagnostics. Startup exit/readiness failures remain visible to the
invoking command or service manager. If log initialization itself fails, startup
fails with a fixed message instead of printing a raw exception. No credentials
are read to configure logging.

## Diagnosis

`codex-antigravity status --json` and `service status --json` distinguish
`process_log` from `request_log`, report the limits and preserved legacy paths,
and never include raw process-log contents. These commands do not bundle logs.
Do not attach legacy/raw process logs to support reports; report the structured
request evidence and fixed runtime error category instead.
