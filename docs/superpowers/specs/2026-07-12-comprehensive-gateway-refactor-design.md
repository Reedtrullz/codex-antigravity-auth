# Comprehensive Gateway Refactor Design

**Status:** Approved design

**Date:** 2026-07-12

**Repository:** `/Users/reidar/Projectos/codex-antigravity-auth`

## Summary

Refactor Codex Antigravity Auth around explicit protocol, transport, state, service, and Anti-helper boundaries while preserving the current CLI, configuration, encrypted data, model aliases, and `/v1` API. The refactor resolves the verified review findings and establishes durable foundations for provider fidelity, diagnostics, migrations, testing, and future feature work.

The work is comprehensive in scope but not a big-bang rewrite. Existing behavior moves behind stable interfaces in independently testable milestones. The gateway must remain usable after every milestone.

## Goals

- Translate provider terminal states into accurate Responses API outcomes.
- Eliminate false `response.completed` events for empty, blocked, truncated, or malformed provider responses.
- Apply one terminal-state model to streaming and non-streaming Google, BYOK Chat Completions, and native Responses routes.
- Forward supported request capabilities such as `parallel_tool_calls`; reject unsupported capabilities explicitly.
- Separate routing, transport, protocol translation, persistence, service control, CLI presentation, and Anti orchestration.
- Make cooldowns family-aware while retaining account-wide handling for credential-wide failures.
- Make secure-store key initialization and all mutable local state cross-process safe.
- Make service installation and status output truthful across macOS, Linux, and Windows.
- Make Anti review scope, manifests, ledgers, prompt ordering, and output redaction trustworthy.
- Improve diagnostics, observability, CI, packaging proof, documentation truth, and release evidence.

## Non-Goals

- Replacing FastAPI, HTTPX, Fernet, keyring, or the current CLI framework.
- Changing existing user-facing model IDs or configuration file locations.
- Requiring users to reauthenticate or recreate BYOK provider configuration.
- Adding new providers while the refactor is in progress.
- Adding gateway-side recursive agents, virtual `panel:*` models, or automatic model swarms.
- Modifying a user's real Codex configuration as part of tests.
- Treating local, CI, package, or mocked evidence as credentialed live-provider proof.

## Chosen Approach

Use a contract-first modular refactor.

This approach was selected over a big-bang rewrite, which would maximize regression risk, and patching the current monoliths, which would leave the structural causes intact. New modules define explicit contracts first; existing behavior is migrated behind them with characterization tests and compatibility shims. Old internal functions may remain temporarily, but public routes and commands continue to use stable behavior throughout the transition.

## Compatibility Contract

The refactor preserves:

- `codex-antigravity` command names, flags, exit-code expectations, and safe defaults.
- `/v1/models`, `/v1/responses`, and `/health` routes.
- Current native model aliases and backend mappings.
- `~/.codex/antigravity-accounts.json` and `~/.codex/antigravity-providers.json` data.
- `~/.codex/antigravity-credentials.json`, storage-key sources, request logs, model overlays, and Anti run directories.
- Existing loopback-first and remote-token security boundaries.
- Existing config-write behavior: read-only by default where currently read-only, explicit write flags where currently required, backups, and preservation of unrelated TOML.

Any intentional behavior change must be described in release notes and covered by a regression test.

## Target Architecture

### `response_protocol.py`

Owns provider-neutral Responses API behavior:

- Request option validation and capability checks.
- Terminal-state types and classification.
- Responses output items and SSE event construction.
- Sequence numbers, output indices, item IDs, and exactly-once terminal events.
- Mapping of completion, incomplete, refusal, and failure outcomes.
- Shared usage normalization.

This module must not perform network I/O, read credentials, select accounts, or write state.

### `google_transport.py`

Owns Google Antigravity execution:

- Request envelope construction for the selected account and project.
- Non-streaming and streaming HTTP execution.
- Google payload parsing, including `finishReason`, `promptFeedback`, usage, content, reasoning, and function calls.
- Retry and rotation signals before visible output begins.
- Transport results expressed through provider-neutral protocol events and outcomes.

It depends on HTTPX, the protocol contract, and an account lease interface. It does not persist account state directly.

### `openai_transport.py`

Owns OpenAI-compatible routes:

- Chat Completions request translation and SSE parsing.
- Native Responses pass-through with validation and sanitized failures.
- Capability-aware forwarding for `parallel_tool_calls`, structured output, tool choice, stop sequences, reasoning, and streaming usage.
- Provider result conversion into the shared protocol contract.

Unsupported fields are rejected with a client error or reported as unsupported capability; they are not silently discarded.

### `account_state.py`

Owns Google account selection and outcomes:

- Account leases and process-local in-flight counts.
- Family-specific active indices.
- Family-scoped quota/rate-limit cooldowns.
- Account-wide authentication and credential cooldowns.
- Sanitized counters and attempt outcomes.
- Persisted schema migration.

Transport code reports a typed outcome. Account state decides whether the outcome affects one family or the entire account.

### `secure_store.py`

Owns mutable local persistence:

- Encryption-key resolution and cross-process-safe first initialization.
- Private file and directory modes.
- Symlink-safe opens where supported.
- File locking, validation, temporary writes, `fsync`, and atomic replacement.
- Explicit legacy-plaintext migration.
- Transactional update functions shared by accounts, providers, overlays, version cache, PID files, and other mutable state where appropriate.

Storage APIs distinguish not-found, invalid-data, wrong-key/corruption, permission, and migration errors without exposing secrets.

### `service_manager.py`

Owns durable gateway lifecycle behavior:

- Platform-specific manifest rendering and command execution.
- Explicit `installed`, `active`, `reachable`, `degraded`, and `failed` states.
- Action results containing command evidence and sanitized errors.
- No positive success message unless the requested state is observed.

The CLI presents these results but does not infer success independently.

### CLI and server shells

`server.py` becomes route validation, dependency selection, orchestration, and response return. `cli.py` becomes parser construction, command dispatch, and presentation. Provider translation, service manipulation, storage transactions, and protocol event assembly move out of these shells.

### Anti helper modules

The bundled helper is divided into focused internal modules or sections with equivalent boundaries:

- Context and path collection.
- Prompt and chunk planning.
- Model/gateway execution.
- Panel and synthesis contracts.
- Manifest and ledger generation.
- Sanitized terminal/JSON presentation.
- Workflow expansion.

The installed skill remains a self-contained packaged artifact. Packaging and `install-skill --verify` must continue to work.

## Request Lifecycle

Every `/v1/responses` request follows the same lifecycle:

1. Parse JSON and validate the Responses request.
2. Normalize the model and determine the provider route.
3. Validate requested capabilities against the selected route.
4. Acquire provider/account capacity through a lease.
5. Translate into a provider request.
6. Execute through the provider transport.
7. Convert provider output into shared protocol events or a non-streaming result.
8. Classify the terminal state.
9. Commit account counters, cooldowns, and sanitized observability records.
10. Release the account lease in all success, failure, cancellation, and disconnect paths.

Request validation and deterministic translation failures happen before provider selection where possible and never trigger account rotation.

## Terminal-State Contract

### Completed

Use `completed` only when the provider produced meaningful assistant text, reasoning, valid function calls, or an explicit refusal item. A response with no meaningful output is not completed successfully, even if the transport returned HTTP 200 or ended cleanly.

### Incomplete

Use `incomplete` for max-token termination, provider truncation, interrupted generation with a provider-declared resumable condition, or another terminal condition that maps to Responses `incomplete_details`.

### Refused

Use a refusal output when the provider exposes a clear safety or policy block with enough structured information to distinguish it from transport failure. Sanitized provider context may be included without raw prompts or policy internals.

### Failed

Use `failed` for empty HTTP 200 streams without a valid terminal signal, malformed provider payloads, transport exhaustion, authentication exhaustion, invalid streaming chunks, or unrecoverable provider errors.

Streaming and non-streaming paths use the same classifier. A clean socket EOF is not a successful terminal signal by itself.

## Streaming Contract

- Emit `response.created` once.
- Use monotonically increasing sequence numbers.
- Preserve stable item IDs and output indices from `added` through `done`.
- Emit meaningful deltas only after their containing item lifecycle starts.
- Emit exactly one of `response.completed`, `response.incomplete`, or `response.failed`.
- Emit exactly one `[DONE]` after the terminal response event.
- Do not retry after any user-visible text, reasoning, or function-call output has been emitted.
- On client disconnect, cancel provider work, release leases, and record a sanitized cancelled outcome without attempting replay.

## Retry and Rotation Policy

- Retry connection and retryable provider failures only before visible output.
- Rotate accounts only for account-scoped authentication, quota, rate-limit, or entitlement failures.
- Apply family-scoped cooldowns to model-family quota and rate-limit failures.
- Apply account-wide cooldowns to refresh-token, revoked-credential, or account-wide authentication failures.
- Never rotate for invalid request options, schemas, model IDs, unsupported capabilities, deterministic provider validation errors, or content-independent translation errors.
- Respect bounded `Retry-After` hints without allowing unbounded persisted cooldowns.
- Persist one attempt outcome per account actually attempted; do not double-count retries.

## Account-State Schema

The persisted state evolves from account-wide maps to scoped state. The normalized conceptual form is:

```json
{
  "accountState": {
    "failures": {
      "user@example.com": {
        "account": 0,
        "claude": 0,
        "gemini": 0
      }
    },
    "cooldowns": {
      "user@example.com": {
        "account": 0,
        "claude": 0,
        "gemini": 0
      }
    },
    "counters": {}
  }
}
```

Legacy numeric failure and cooldown entries migrate conservatively into `account` scope so upgrades do not prematurely retry a previously cooled-down account. Expired and malformed state is pruned. Migration is idempotent and occurs inside one locked transaction.

## Persistence and Migration Safety

- Existing encrypted account/provider files remain readable.
- Legacy plaintext migration remains explicit and immediately rewrites private encrypted storage.
- Encryption-key creation serializes first initialization and re-reads the persisted winner before encryption.
- Wrong-key or corrupted ciphertext is not silently reclassified as arbitrary plaintext unless it matches a narrowly defined legacy JSON shape.
- Model overlay writes use locked atomic replacement.
- Failed validation leaves the original file untouched.
- Migration round trips prove semantic equivalence for accounts, providers, active indices, cooldowns, counters, fingerprints, and OAuth tokens without logging values.

## Service Management Contract

Service operations return structured results rather than inferred strings. Installation is successful only when the manifest/task exists and the platform reports it enabled or loaded. Runtime readiness is a separate reachable state.

Examples:

- Installed and active but gateway booting: `installed=true`, `active=true`, `reachable=false`, state `degraded`.
- Manifest written but bootstrap failed: state `failed`, with a sanitized command error.
- Windows scheduled task exists but is not running: `installed=true`, `active=false`; existence alone is not active status.

Human output and JSON output derive from the same result object.

## Anti Helper Contract

- Every requested path is represented individually as reviewed, omitted, excluded, unreadable, or truncated.
- Byte/line ranges identify partial-file chunks.
- Chunk caps produce `status=incomplete` and never contradictory included/omitted lists.
- Synthesis receives an authoritative manifest of actual chunk prompts.
- `--save-output full` records the sanitized prompts actually sent, including chunk and synthesis prompts, or explicitly states which content was intentionally not persisted.
- Model output passes through output redaction before terminal or JSON display.
- `debug-consensus --prompt-file` prepends workflow instructions before user file content.
- Negative retry and prompt-budget values are rejected except the documented zero-as-unlimited value.
- Primitive and workflow commands use consistent validation, privacy disclosures, and run correlation IDs.

## Diagnostics and Observability

`doctor --json` reports independent sections for:

- Package/version state.
- Codex configuration.
- Gateway reachability.
- Model catalog and route resolution.
- Provider capability support.
- Google family/account readiness.
- BYOK credential readiness without revealing credential values.
- Secure-store readability and migration status.
- Durable service installation and runtime state.
- Installed Anti skill version and bundled-copy match.
- Optional credentialed live generation evidence.

Request logs retain stable request, run, and attempt IDs; route, provider/family, terminal state, latency, sanitized error class, rotation, retry-after source, and normalized usage. They never contain prompts, input bodies, tool arguments, credentials, authorization headers, provider payloads, or raw account identifiers.

## Test Strategy

### Characterization and unit tests

Before moving behavior, add characterization tests for current public routes, commands, model mappings, transforms, storage, and Anti output. New pure modules receive focused unit tests.

### Protocol contract tests

A shared fixture matrix runs against Google and OpenAI-compatible adapters for:

- Text, reasoning, single/multiple tool calls, and mixed output.
- Empty HTTP 200 payloads and streams.
- Safety/policy blocks.
- Max-token and provider truncation.
- Malformed chunks followed by valid chunks.
- Pre-output retry and rotation.
- Post-output failures with no replay.
- Usage-only terminal chunks.
- Client cancellation and lease release.
- `parallel_tool_calls` true and false.

Golden SSE tests assert the entire ordered event sequence, not substring presence.

### Storage and concurrency tests

- Simultaneous first-key initialization.
- Concurrent account/provider/overlay updates.
- Legacy plaintext and encrypted migration.
- Wrong-key, corrupted, truncated, symlinked, and permission-denied files.
- Interrupted writes and atomic rollback behavior.
- Family-scoped and account-scoped cooldown selection.

### Platform and package tests

- macOS launchd, Linux systemd user units, and Windows Task Scheduler result semantics.
- Clean wheel/sdist build and installation.
- Packaged Anti installation and its full test suite.
- Python 3.10, 3.11, and 3.12, plus the supported Windows job.

### Static and security gates

- Ruff lint/format checks.
- Static typing for new public contracts and transport/state boundaries.
- Dependency audit separated into runtime and development environments.
- Secret-shaped output regression tests.
- Diff hygiene, compile checks, package metadata, and license checks.

## Implementation Milestones

The implementation plan will express these as TDD tasks with independent reviewer gates:

1. Characterize public behavior and define protocol contracts.
2. Implement terminal-state classification and shared SSE construction.
3. Extract Google transport and correct streaming/non-streaming fidelity.
4. Extract OpenAI-compatible transports and capability validation.
5. Introduce scoped account state and safe migration.
6. Consolidate secure-store transactions and atomic overlays.
7. Replace service control with structured truthful outcomes.
8. Refactor Anti manifests, ledgers, prompt ordering, and output redaction.
9. Reduce server/CLI shells and remove obsolete internal paths.
10. Upgrade diagnostics, CI, documentation truth, packaging, and release evidence.

Each milestone must keep the full suite green and leave the gateway usable.

## Release Gate

Release requires all of the following evidence from the same candidate commit:

- Full local test suite and static gates.
- Clean wheel and sdist build, metadata check, install, and `pip check` in an isolated environment.
- Account/provider/overlay migration round trip from representative legacy and v1.6.4 fixtures.
- Real macOS service install, status, gateway reachability, and uninstall lifecycle.
- Linux and Windows CI platform results.
- Credentialed Google non-streaming and streaming generation.
- At least one BYOK provider non-streaming and streaming generation.
- Native Codex model-picker, text, reasoning, and tool-loop smoke.
- Packaged and installed Anti smoke plus manifest/redaction verification.
- Updated README, USAGE, STATUS, VERIFICATION, AGENTS, and release notes.
- Explicit separation of local, CI, package, live-provider, service, and non-claim evidence.

## Risks and Mitigations

### Protocol regression during extraction

Mitigate with characterization tests, shared fixtures, compatibility shims, and per-milestone full-suite runs.

### Migration damages user state

Mitigate with fixture-based round trips, locked atomic writes, validation before replacement, preserved originals on failure, and value-free live-store diagnostics.

### Refactor becomes an unreviewable rewrite

Mitigate with interface-first tasks, small commits, reviewer gates, and removal of obsolete paths only after parity tests pass.

### Provider differences are forced into a leaky abstraction

Keep transport-specific parsing and capability declarations in adapters. Share only provider-neutral protocol outcomes and lifecycle rules.

### Anti leaks sensitive model output

Apply central output sanitization before all presentation and ledger paths, and test terminal plus JSON output with secret-shaped sentinels.

### Release proof becomes stale or ambiguous

Generate or verify counts/version/tag facts in CI and require exact candidate-SHA evidence for every release gate.

## Success Criteria

- Verified review defects have regression tests and are resolved.
- Empty or blocked provider responses cannot appear as successful completed output.
- Streaming and non-streaming terminal semantics match.
- Supported capabilities are forwarded; unsupported capabilities fail explicitly.
- Account capacity is not unnecessarily disabled across model families.
- Concurrent first use and local-state updates cannot lose or orphan encryption state.
- Service output never claims success when the requested state was not observed.
- Anti scope and ledger metadata accurately describe what models received.
- Anti output cannot display known secret shapes without redaction.
- `server.py`, `cli.py`, and the Anti helper have materially narrower responsibilities.
- Existing installations upgrade without reconfiguration or credential loss.
- The release candidate satisfies the complete evidence gate from one commit.

## Deferred Work

After the comprehensive refactor is released and verified:

- `/v1/responses/compact` support.
- Additional BYOK provider live-smoke profiles.
- New provider integrations.
- Optional richer operator dashboards or GUI surfaces.
- Broader Responses API features not required by current Codex behavior.
