# Release Hardening 1.7.0 Design

## Purpose

Prepare the comprehensive gateway refactor for a safe `1.7.0` release by completing the production consolidation that the refactor introduced, fixing the confirmed protocol and release blockers, and aligning release evidence with the code that will actually ship.

The implementation must preserve the public CLI, `/health`, `/v1/models`, `/v1/responses`, existing model aliases, encrypted account/provider data, installed Anti entrypoint, and stateless `previous_response_id` behavior.

## Chosen approach

Use staged consolidation on `codex/release-hardening-1.7.0`. Each stage moves one production responsibility onto its final contract, retains a thin compatibility wrapper where downstream callers still need it, and lands only after focused RED/GREEN tests plus the relevant regression slice pass.

This approach is preferred over a big-bang rewrite because provider streaming, account rotation, encrypted persistence, and release automation have independent failure modes. It is preferred over blocker-only patches because leaving duplicate state and event implementations would preserve the drift that caused the review findings.

## Scope

### Provider-neutral response lifecycle

`response_protocol.py` is the only owner of terminal classification and Responses event lifecycle rules:

- exactly one `response.created` event;
- monotonic sequence numbers;
- stable item IDs and output indices from `added` through `done`;
- exactly one terminal event: `response.completed`, `response.incomplete`, or `response.failed`;
- exactly one `[DONE]`, after the terminal event;
- no output after terminal;
- empty, malformed, duplicate-terminal, and unterminated streams fail with sanitized errors;
- refusals remain completed output;
- token limits remain incomplete output.

`ResponseEventBuilder` will support incremental message, reasoning, refusal, and function-call events instead of only whole-item events. Google, Chat Completions, and native Responses streaming will all emit through this builder.

### Transport ownership

`GoogleTransport` and `OpenAICompatibleTransport` own provider-specific request construction, HTTP execution, response parsing, streaming accumulation, error normalization, and `AttemptOutcome` creation.

`server.py` owns only:

1. request parsing and loopback/remote access policy;
2. model/provider route resolution;
3. route capability validation;
4. account lease acquisition and release for Google routes;
5. request-log correlation;
6. conversion of transport results into FastAPI responses.

Legacy transform and server helper exports remain delegating compatibility wrappers. They must not contain a second terminal parser, SSE lifecycle, or retry policy.

### Native Responses validation

Native Responses non-streaming validation accepts only meaningful supported output items. A non-empty list containing empty dictionaries, empty message content, malformed refusals, incomplete function calls, or unknown item shapes is not successful output.

Native Responses streaming is parsed as SSE rather than forwarded byte-for-byte. The adapter:

- handles chunk boundaries and multiple events per byte chunk;
- validates event objects and terminal ordering;
- tracks whether user-visible output has started;
- retries a single OAuth `401` only before visible output;
- rewrites the selected model to the gateway display model where required;
- sanitizes provider errors;
- emits a failed terminal plus `[DONE]` for empty or unterminated HTTP 200 streams;
- never replays after visible output.

### Capability resolution

Capabilities are resolved after the exact route and model are known but before credentials, account acquisition, or network execution.

The capability source is shared by `/v1/models`, doctor diagnostics, and request validation. It includes:

- native Responses support;
- parallel tool-call control;
- structured output;
- stop sequences;
- reasoning;
- streaming usage;
- supported `tool_choice` modes.

Native model overlays retain `supports_parallel_tool_calls`. BYOK providers receive normalized capability defaults by provider kind and may store explicit safe boolean or tool-choice overrides. A requested option that cannot be honored returns HTTP 400 without resolving secrets or opening an HTTP client.

### Account state ownership

`AccountState` becomes the sole implementation of:

- family-aware account selection;
- process-local in-flight counts;
- account and family cooldowns;
- failure backoff;
- attempt and usage counters;
- typed `AttemptOutcome` recording;
- lease release.

`AccountManager` remains the public compatibility facade. It loads and transactionally updates account data, delegates policy to `AccountState`, performs token refresh, and preserves existing method signatures. Direct `_failures`, `_cooldowns`, `_counters`, and `_in_flight` policy manipulation is removed from the facade.

Every attempted lease records at most one outcome. Every acquired lease is released on success, incomplete completion, failure, cancellation, disconnect, translation error, and retry rotation. Transport failures use their typed scope instead of separately invoking legacy cooldown policy.

### Secure persistence

`secure_store.py` owns the single shared lock and atomic-write implementation used by account, provider, OAuth, and model-overlay storage.

The lock implementation:

- serializes threads by canonical lock path rather than globally serializing unrelated stores;
- uses `fcntl` on POSIX and `msvcrt` on Windows;
- records whether acquisition succeeded before attempting unlock;
- preserves the original acquisition or body exception;
- closes descriptors exactly once;
- has subprocess contention coverage on supported platforms and Windows CI coverage;
- never falls back to a different encryption-key source merely because lock cleanup masked another error.

Read-only diagnostics must not create store directories, keys, lock files, migrations, or chmod mutations. Runtime endpoints will use read-only provider/account catalog functions when they only inspect state. The intentional version-check cache remains opt-out through `CODEX_ANTIGRAVITY_NO_UPDATE_CHECK=1` and is documented separately from secure-store reads.

### Service and Anti boundaries

Service observation remains based on `ServiceResult`; platform rendering tests remain mutation-free. No real service install/uninstall is part of this implementation without separate authorization.

Anti remains executable as a standalone installed skill. The existing redaction, truthful chunk manifest, actual-call ledger, deterministic run ID, and interruption behavior remain covered during consolidation. Transport/server changes must preserve Anti request-log correlation metadata without persisting prompts or secrets.

### Release automation and metadata

The release version becomes `1.7.0` because `1.6.4` is already public and this is a compatibility-preserving feature release.

Publishing must not begin until the exact tag SHA passes:

- package build and installed-wheel checks;
- Ubuntu Python 3.10, 3.11, 3.12, and 3.14 tests;
- Windows Python 3.12 tests.

The preferred implementation makes the Publish workflow contain or reuse the complete verification jobs and makes the PyPI job depend on all of them. A tag/version match remains mandatory. Artifact upload and Trusted Publishing remain unchanged after the gate.

`STATUS.md`, `AGENTS.md` suite counts, migration notes, release checklist, README release guidance, and artifact evidence will describe the final `1.7.0` source SHA. Historical evidence stays labeled with its historical SHA and must not be rewritten as current proof.

Committed end-of-file whitespace warnings are removed, and the final source-range `git diff --check v1.6.4..HEAD` must exit zero.

## Error handling

Provider-controlled error text is always passed through central redaction and length bounds. Client-visible failures use stable gateway codes such as `empty_response`, `missing_terminal_signal`, `invalid_stream_chunk`, `duplicate_terminal`, `capability_not_supported`, and `provider_error`.

Failures before visible output may retry only when route policy permits. Failures after visible output terminate the existing response and never rotate or replay. Cancellation is recorded distinctly and does not create a provider cooldown unless the typed outcome explicitly requests one.

Storage errors are fail-closed. Wrong-key ciphertext, invalid JSON, unsafe symlinks, lock acquisition failures, and atomic replacement failures do not overwrite existing state.

## Testing strategy

Every production change follows RED, observed failure, minimal GREEN implementation, focused regression, then full regression at natural checkpoints.

Required automated coverage includes:

- native Responses streaming: empty 200, partial EOF, malformed JSON, split chunks, duplicate terminal, terminal without `[DONE]`, provider `[DONE]` without terminal, valid completed/incomplete/failed, `401` retry before output, and no retry after output;
- meaningful native non-stream output validation;
- route-specific capability rejection before credential resolution or HTTP client construction;
- `/v1/models`, doctor, and runtime validation capability parity;
- `AccountManager` facade delegation, scoped cooldowns, exactly-once outcomes, rotation, cancellation, disconnect, and in-flight release;
- same-path thread locking, different-path concurrency, subprocess contention, failed acquisition cleanup, failed body cleanup, and Windows lock execution;
- read-only health/models/doctor behavior in a clean temporary home;
- Google and Chat Completions terminal matrices after moving event construction to `ResponseEventBuilder`;
- installed Anti tests and package asset presence;
- Publish workflow dependency assertions.

Final local gates:

1. Python 3.10 compileall and full pytest with exact test/subtest counts.
2. Isolated Python 3.14 compileall and full pytest.
3. `git diff --check` for the working tree and `v1.6.4..HEAD`.
4. Wheel and sdist build with Twine checks and recorded SHA-256 hashes.
5. Clean Python 3.12 wheel install, dependency check, CLI help, provider presets, and installed Anti verification.
6. Runtime dependency vulnerability audit.
7. Fresh-home setup/readiness and running-wheel `/health` and `/v1/models` readback.
8. Exact-SHA GitHub CI matrix after push.

Credentialed Google non-streaming, Google streaming, and one BYOK live smoke require explicit provider-spend authorization after all non-credentialed gates pass. Real service installation or removal requires separate host-mutation authorization.

## Migration and compatibility

No user action or reauthentication is required for normal upgrade. Existing encrypted store paths and schema-version `2` remain valid. Compatibility wrappers keep public imports and CLI behavior stable during the release.

Rollback requires stopping the gateway, reinstalling the previous package, and restoring matching pre-upgrade encrypted account/provider backups if the new version has written state. The release checklist will distinguish source tests, package tests, running-local proof, CI proof, credentialed proof, and explicit non-claims.

## Completion criteria

The work is ready for a release decision only when all confirmed blockers have regression tests, production uses the consolidated contracts, the full non-credentialed release matrix passes, the tag/PyPI gate cannot outrun tests, version and evidence docs are current, and the branch is clean.

Passing unit tests alone is not sufficient. No tag, PyPI publish, GitHub release, credentialed provider request, service mutation, or real `~/.codex/config.toml` write is authorized by this design.
