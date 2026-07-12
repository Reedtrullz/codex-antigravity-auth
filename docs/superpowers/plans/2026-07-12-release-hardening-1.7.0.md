# Release Hardening 1.7.0 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Complete the production consolidation of the gateway refactor, close all confirmed runtime and publishing blockers, and produce a verified `1.7.0` release candidate without performing a release.

**Architecture:** Provider-neutral protocol objects own terminal and event lifecycle rules; provider transports own execution and parsing; `server.py` owns routing, leases, and logging. `AccountManager` delegates policy to `AccountState`, all encrypted stores use one shared secure lock, and PyPI publishing depends on the complete exact-SHA test matrix.

**Tech Stack:** Python 3.10+, FastAPI, HTTPX, `cryptography` Fernet, keyring, pytest/unittest, setuptools/build/Twine, GitHub Actions.

---

## Task 1: Complete the provider-neutral event and native-output contracts

**Files:**
- Modify: `codex_antigravity_auth/response_protocol.py`
- Modify: `tests/test_response_protocol.py`

- [ ] **Step 1: Add failing incremental lifecycle tests**

Add tests that use this public API and assert stable IDs/indices, monotonic sequence numbers, one terminal, and one done marker:

```python
builder = ResponseEventBuilder(response_id="resp_1", model="model", created_at=1)
created = builder.created()
message_events = builder.add_text_delta("hello")
call_events = builder.add_function_call("lookup", '{"q":"x"}', call_id="call_1")
terminal = builder.terminal(completed_result)
assert builder.done_marker() == "[DONE]"
assert [event["sequence_number"] for event in [created, *message_events, *call_events, terminal]] == list(range(1 + len(message_events) + len(call_events) + 1))
```

Also add table tests for `meaningful_output_items()` rejecting `{}`, empty messages, malformed refusals, incomplete function calls, and unknown item types while accepting text, refusal, reasoning, and complete function calls.

- [ ] **Step 2: Run the focused tests and observe RED**

Run:

```bash
.venv/bin/python -m pytest -q tests/test_response_protocol.py
```

Expected: failures because `add_text_delta`, `add_function_call`, and `meaningful_output_items` do not exist.

- [ ] **Step 3: Implement the incremental builder and meaningful-output validator**

Implement these interfaces in `response_protocol.py`:

```text
meaningful_output_items(output: Sequence[dict[str, Any]]) -> Sequence[dict[str, Any]]
ResponseEventBuilder.add_text_delta(delta: str) -> list[dict[str, Any]]
ResponseEventBuilder.finish_text() -> list[dict[str, Any]]
ResponseEventBuilder.add_reasoning_delta(delta: str) -> list[dict[str, Any]]
ResponseEventBuilder.finish_reasoning() -> list[dict[str, Any]]
ResponseEventBuilder.add_function_call(name: str, arguments: str, call_id: str | None = None) -> list[dict[str, Any]]
```

The builder stores one message state, one reasoning state, and ordered function-call states. It raises `ProtocolStateError` for invalid names, output after terminal, duplicate finishes, duplicate terminal, or duplicate `[DONE]`.

- [ ] **Step 4: Run protocol and transport contract tests**

```bash
.venv/bin/python -m pytest -q tests/test_response_protocol.py tests/test_openai_transport.py tests/test_google_transport.py
```

Expected: all pass.

- [ ] **Step 5: Commit the protocol checkpoint**

```bash
git add codex_antigravity_auth/response_protocol.py tests/test_response_protocol.py
git commit -m "refactor: complete response lifecycle contracts"
```

## Task 2: Validate native Responses execution and streaming

**Files:**
- Modify: `codex_antigravity_auth/openai_transport.py`
- Modify: `codex_antigravity_auth/server.py`
- Modify: `tests/test_openai_transport.py`
- Modify: `tests/test_server_streaming.py`

- [ ] **Step 1: Add the failing native terminal matrix**

Add non-stream tests asserting `validate_native_response()` converts structurally empty output to `status="failed"`. Add async stream tests with a fake HTTP client for:

```python
cases = {
    "empty_http_200": ([], "response.failed"),
    "done_without_terminal": ([b"data: [DONE]\n\n"], "response.failed"),
    "partial_eof": ([b'data: {"type":"response.output_text.delta","delta":"x"}\n\n'], "response.failed"),
    "duplicate_terminal": ([completed_event, completed_event, done], "response.failed"),
    "completed": ([created_event, completed_event, done], "response.completed"),
    "incomplete": ([created_event, incomplete_event, done], "response.incomplete"),
}
```

Assert every case emits exactly one gateway terminal and one `[DONE]`, provider error messages are redacted, a `401` retries once before visible output, and a `401` after visible output does not retry.

- [ ] **Step 2: Run the native focused tests and observe RED**

```bash
.venv/bin/python -m pytest -q tests/test_openai_transport.py tests/test_server_streaming.py -k 'native or xai_oauth'
```

Expected: empty/partial/duplicate terminal cases fail against byte forwarding.

- [ ] **Step 3: Implement `NativeResponsesStreamAdapter`**

Add a transport-owned adapter:

```text
NativeResponsesStreamAdapter(display_model: str)
  consume_bytes(chunk: bytes) -> list[dict[str, Any]]
  finish() -> list[dict[str, Any]]
  visible_output_started: bool
  terminal_seen: bool
```

It buffers split SSE lines, parses `data:` JSON, validates event types/order, rewrites terminal response model fields to `display_model`, and uses `ResponseEventBuilder` to synthesize sanitized failures for malformed, duplicate, empty, and unterminated streams.

Change `OpenAICompatibleTransport.validate_native_response()` to filter through `meaningful_output_items()` and fail a completed response with no meaningful output.

- [ ] **Step 4: Replace raw xAI byte forwarding**

Change `xai_oauth_responses_sse_generator()` to create the adapter, feed every provider byte chunk through it, emit normalized SSE events, retry one `401` only when `visible_output_started` is false, call `finish()` on EOF, and emit one `[DONE]` after the adapter terminal.

- [ ] **Step 5: Run native and full streaming regressions**

```bash
.venv/bin/python -m pytest -q tests/test_openai_transport.py tests/test_server_streaming.py tests/test_xai_oauth.py tests/test_byok_providers.py
```

Expected: all pass.

- [ ] **Step 6: Commit the native Responses checkpoint**

```bash
git add codex_antigravity_auth/openai_transport.py codex_antigravity_auth/server.py tests/test_openai_transport.py tests/test_server_streaming.py
git commit -m "fix: validate native responses streams"
```

## Task 3: Enforce route-specific capabilities from one catalog

**Files:**
- Modify: `codex_antigravity_auth/models.py`
- Modify: `codex_antigravity_auth/byok.py`
- Modify: `codex_antigravity_auth/response_protocol.py`
- Modify: `codex_antigravity_auth/server.py`
- Modify: `codex_antigravity_auth/cli.py`
- Modify: `tests/test_response_protocol.py`
- Modify: `tests/test_server_streaming.py`
- Modify: `tests/test_cli.py`
- Modify: `tests/test_byok_providers.py`

- [ ] **Step 1: Add failing catalog-parity and preflight tests**

Add integration tests for a native overlay with `supports_parallel_tool_calls=false` and a BYOK provider with explicit capability overrides. Patch credential resolution and `httpx.AsyncClient` to raise if called, then assert unsupported requests return HTTP 400 before either patch is invoked.

Assert `/v1/models`, `doctor --codex-ready --json`, and `capabilities_for_route()` report the same parallel-tool setting.

- [ ] **Step 2: Run capability tests and observe RED**

```bash
.venv/bin/python -m pytest -q tests/test_response_protocol.py tests/test_server_streaming.py tests/test_cli.py tests/test_byok_providers.py -k capabil
```

Expected: requests pass the current global all-capable boundary or capability parity assertions fail.

- [ ] **Step 3: Implement normalized route capabilities**

Add:

```text
capabilities_for_native_model(model: str) -> ProviderCapabilities
capabilities_for_provider(provider: dict[str, Any]) -> ProviderCapabilities
capabilities_for_route(model: str, provider: dict[str, Any] | None) -> ProviderCapabilities
```

Native defaults preserve current behavior except `supports_parallel_tool_calls`. BYOK defaults derive from `kind`; optional stored `capabilities` accepts only known boolean fields and a validated list of tool-choice modes.

- [ ] **Step 4: Move validation after exact route resolution**

Keep type/shape validation in `validate_response_request_body()`, but call:

```python
validate_capabilities(codex_req, capabilities_for_route(model, provider))
```

inside `create_response()` after model/provider lookup and before API-key/OAuth resolution, account acquisition, or HTTP preparation. Convert `CapabilityError` to HTTP 400 with code `capability_not_supported` in request logs.

- [ ] **Step 5: Use the shared catalog in models and doctor**

Populate model metadata and `provider_capability_mismatches()` from the same normalized capabilities instead of independent kind/auth checks.

- [ ] **Step 6: Run capability and route regressions**

```bash
.venv/bin/python -m pytest -q tests/test_response_protocol.py tests/test_server_streaming.py tests/test_cli.py tests/test_byok_providers.py tests/test_security_hardening.py
```

Expected: all pass.

- [ ] **Step 7: Commit the capability checkpoint**

```bash
git add codex_antigravity_auth/models.py codex_antigravity_auth/byok.py codex_antigravity_auth/response_protocol.py codex_antigravity_auth/server.py codex_antigravity_auth/cli.py tests/test_response_protocol.py tests/test_server_streaming.py tests/test_cli.py tests/test_byok_providers.py
git commit -m "fix: enforce selected route capabilities"
```

## Task 4: Make `AccountState` the production policy owner

**Files:**
- Modify: `codex_antigravity_auth/account_state.py`
- Modify: `codex_antigravity_auth/accounts.py`
- Modify: `codex_antigravity_auth/server.py`
- Modify: `tests/test_account_state.py`
- Modify: `tests/test_accounts.py`
- Modify: `tests/test_server_streaming.py`

- [ ] **Step 1: Add failing facade and exactly-once tests**

Add tests asserting `AccountManager` delegates selection, in-flight counts, scoped cooldowns, counters, and release to `AccountState`. Add route tests asserting each acquired email gets exactly one typed outcome and one release on success, incomplete, translation failure, connection failure, rotation, cancellation, and disconnect.

- [ ] **Step 2: Run account tests and observe RED**

```bash
.venv/bin/python -m pytest -q tests/test_account_state.py tests/test_accounts.py tests/test_server_streaming.py -k 'account or lease or attempt or disconnect or cancel'
```

Expected: delegation assertions fail because `AccountManager` owns duplicate dictionaries.

- [ ] **Step 3: Extend `AccountState` for persisted facade use**

Implement snapshot-safe methods:

```text
AccountState.select(family: str, acquire: bool) -> Lease | None
AccountState.record_email(email: str, family: str, outcome: AttemptOutcome, usage: dict[str, Any] | None = None) -> None
AccountState.mark_legacy_failure(email: str, family: str, account_scoped: bool, retry_after_seconds: float | None) -> float
AccountState.persisted_payload() -> dict[str, Any]
```

Use existing schema-version `2` field names and preserve migration compatibility.

- [ ] **Step 4: Convert `AccountManager` into a facade**

During each `update_accounts()` transaction, instantiate/synchronize `AccountState`, perform refresh as needed, delegate policy, and persist `state.persisted_payload()`. Retain public methods and compatibility properties, but remove independent selection/backoff/counter algorithms.

- [ ] **Step 5: Remove legacy double-recording from server routes**

Replace paired `mark_account_failure()` plus `record_attempt_outcome()` calls with one typed outcome whose scope is chosen by `outcome_for_http_status()` or `outcome_for_backend_error()`. Keep account-scoped authentication failures and family-scoped quota/rate-limit failures; transport and cancellation outcomes use scope `none`.

- [ ] **Step 6: Run account, storage, and streaming regressions**

```bash
.venv/bin/python -m pytest -q tests/test_account_state.py tests/test_accounts.py tests/test_storage.py tests/test_server_streaming.py tests/test_regressions.py
```

Expected: all pass.

- [ ] **Step 7: Commit the account-state checkpoint**

```bash
git add codex_antigravity_auth/account_state.py codex_antigravity_auth/accounts.py codex_antigravity_auth/server.py tests/test_account_state.py tests/test_accounts.py tests/test_server_streaming.py
git commit -m "refactor: make account state authoritative"
```

## Task 5: Consolidate secure locking and read-only runtime catalogs

**Files:**
- Modify: `codex_antigravity_auth/secure_store.py`
- Modify: `codex_antigravity_auth/storage.py`
- Modify: `codex_antigravity_auth/byok.py`
- Modify: `codex_antigravity_auth/server.py`
- Modify: `codex_antigravity_auth/models.py`
- Modify: `tests/test_secure_store.py`
- Modify: `tests/test_storage.py`
- Modify: `tests/test_security_hardening.py`

- [ ] **Step 1: Add failing lock and clean-home tests**

Add same-path serialization, different-path concurrency, failed-acquisition exception preservation, failed-body exception preservation, and POSIX subprocess contention tests. On Windows CI, exercise `msvcrt` acquisition/unlock. Add a clean temporary-home test proving app startup plus `/health` and `/v1/models` create no account/provider/OAuth lock or key files.

- [ ] **Step 2: Run the storage tests and observe RED**

```bash
.venv/bin/python -m pytest -q tests/test_secure_store.py tests/test_storage.py tests/test_security_hardening.py
```

Expected: different-path concurrency, acquisition cleanup, or clean-home endpoint assertions fail.

- [ ] **Step 3: Implement one path-scoped lock**

Expose only:

```text
file_lock(path: Path) -> ContextManager[None]
```

Use a guarded registry of `threading.RLock` values keyed by canonical lock path. Set `acquired = True` only after the platform lock succeeds; unlock only when acquired; close once in an outer `finally`. Remove `_exclusive_file_lock` from `storage.py` and import the shared implementation.

- [ ] **Step 4: Separate keyring fallback from lock failures**

Make `_get_encryption_key()` fall back only for keyring availability/storage errors. Lock acquisition and cleanup errors propagate without selecting a different key source.

- [ ] **Step 5: Use read-only catalogs on read endpoints**

Change `/health` and `/v1/models` catalog helpers to `all_provider_configs_read_only()` and read-only account paths. Preserve environment-enabled providers and keyless loopback presets without creating encrypted stores or lock files.

- [ ] **Step 6: Run storage, models, CLI, and endpoint regressions**

```bash
.venv/bin/python -m pytest -q tests/test_secure_store.py tests/test_storage.py tests/test_security_hardening.py tests/test_cli.py tests/test_server_streaming.py tests/test_byok_providers.py
```

Expected: all pass.

- [ ] **Step 7: Commit the secure-store checkpoint**

```bash
git add codex_antigravity_auth/secure_store.py codex_antigravity_auth/storage.py codex_antigravity_auth/byok.py codex_antigravity_auth/server.py codex_antigravity_auth/models.py tests/test_secure_store.py tests/test_storage.py tests/test_security_hardening.py
git commit -m "refactor: consolidate secure store locking"
```

## Task 6: Move Google and Chat streaming onto transport/event contracts

**Files:**
- Modify: `codex_antigravity_auth/google_transport.py`
- Modify: `codex_antigravity_auth/openai_transport.py`
- Modify: `codex_antigravity_auth/server.py`
- Modify: `codex_antigravity_auth/transform.py`
- Modify: `tests/test_google_transport.py`
- Modify: `tests/test_openai_transport.py`
- Modify: `tests/test_server_streaming.py`
- Modify: `tests/test_transform.py`

- [ ] **Step 1: Add failing production-ownership tests**

Patch legacy manual parser helpers to raise and prove `/v1/responses` still handles Google and Chat non-streaming/streaming completed, incomplete, refusal, function-call, malformed, empty, cancellation, and post-output failure cases through transports and `ResponseEventBuilder`.

- [ ] **Step 2: Run ownership tests and observe RED**

```bash
.venv/bin/python -m pytest -q tests/test_google_transport.py tests/test_openai_transport.py tests/test_server_streaming.py -k 'transport or terminal or stream or lifecycle'
```

Expected: route tests hit server-owned manual parsing/event code.

- [ ] **Step 3: Add transport streaming iterators**

Implement:

```text
GoogleTransport.stream_events(request, lease, response_id, display_model) -> AsyncIterator[dict[str, Any] | str]
OpenAICompatibleTransport.stream_chat_events(prepared, response_id, display_model) -> AsyncIterator[dict[str, Any] | str]
```

Both use their accumulators for provider parsing and `ResponseEventBuilder` for gateway events. They yield event dictionaries and the final string `[DONE]`; they do not acquire accounts, write logs, or choose retries.

- [ ] **Step 4: Reduce server routes to orchestration**

Replace manual SSE parsing/event construction with a shared serializer over transport events. Keep lease rotation before visible output, exactly-once attempt recording, disconnect cancellation, and request logging in `server.py`.

Keep `transform_response()` and `transform_chat_response()` as thin non-stream compatibility wrappers over transport parsers.

- [ ] **Step 5: Run the complete protocol/transport/route matrix**

```bash
.venv/bin/python -m pytest -q tests/test_response_protocol.py tests/test_google_transport.py tests/test_openai_transport.py tests/test_server_streaming.py tests/test_transform.py tests/test_fidelity_transforms.py tests/test_fidelity_edge_cases.py tests/test_regressions.py
```

Expected: all pass.

- [ ] **Step 6: Commit the transport consolidation checkpoint**

```bash
git add codex_antigravity_auth/google_transport.py codex_antigravity_auth/openai_transport.py codex_antigravity_auth/server.py codex_antigravity_auth/transform.py tests/test_google_transport.py tests/test_openai_transport.py tests/test_server_streaming.py tests/test_transform.py
git commit -m "refactor: consolidate provider stream execution"
```

## Task 7: Gate publishing and prepare `1.7.0` evidence

**Files:**
- Modify: `.github/workflows/publish.yml`
- Modify: `pyproject.toml`
- Modify: `AGENTS.md`
- Modify: `README.md`
- Modify: `USAGE.md`
- Modify: `STATUS.md`
- Modify: `VERIFICATION.md`
- Modify: `docs/refactor-migration.md`
- Modify: `docs/refactor-release-checklist.md`
- Modify: `codex_antigravity_auth/skills/anti/scripts/anti_lib/__init__.py`
- Modify: `codex_antigravity_auth/skills/anti/scripts/anti_lib/context.py`
- Modify: `codex_antigravity_auth/skills/anti/scripts/anti_lib/ledger.py`
- Modify: `codex_antigravity_auth/skills/anti/scripts/anti_lib/runner.py`
- Test: `tests/test_release_workflow.py`

- [ ] **Step 1: Add failing release-workflow tests**

Parse `.github/workflows/publish.yml` and assert the PyPI job depends on package plus test jobs covering Ubuntu 3.10/3.11/3.12/3.14 and Windows 3.12. Assert the tag/version check remains and project version equals `1.7.0`.

- [ ] **Step 2: Run release tests and observe RED**

```bash
.venv/bin/python -m pytest -q tests/test_release_workflow.py
```

Expected: missing test matrix dependency and version `1.6.4` failures.

- [ ] **Step 3: Gate the Publish workflow**

Add a `test` matrix job equivalent to CI. Make `publish.needs` contain both `build` and `test`. Keep artifact download and Trusted Publishing unchanged. Use bounded runner temporary directories and fail-fast shell commands.

- [ ] **Step 4: Bump and refresh release documentation**

Set `pyproject.toml` version to `1.7.0`. Update suite counts only after final verification. Mark old `1.6.4` evidence historical, update current GitHub/PyPI state, list authorization-dependent smokes as unverified, and describe rollback/state schema compatibility.

- [ ] **Step 5: Remove committed EOF whitespace errors**

Remove extra blank lines at EOF from the five files reported by:

```bash
git diff --check v1.6.4..HEAD
```

- [ ] **Step 6: Run workflow/docs/package-focused tests**

```bash
.venv/bin/python -m pytest -q tests/test_release_workflow.py tests/test_cli.py codex_antigravity_auth/skills/anti/tests/test_anti.py
git diff --check
git diff --check v1.6.4..HEAD
```

Expected: all commands exit zero.

- [ ] **Step 7: Commit the release-preparation checkpoint**

```bash
git add .github/workflows/publish.yml pyproject.toml AGENTS.md README.md USAGE.md STATUS.md VERIFICATION.md docs/refactor-migration.md docs/refactor-release-checklist.md codex_antigravity_auth/skills/anti/scripts/anti_lib tests/test_release_workflow.py
git commit -m "chore: prepare verified 1.7.0 release gates"
```

## Task 8: Run the complete non-credentialed release matrix

**Files:**
- Modify: `docs/refactor-release-checklist.md`

- [ ] **Step 1: Run Python 3.10 source verification**

```bash
df -h /System/Volumes/Data
.venv/bin/python -m compileall -q codex_antigravity_auth tests
.venv/bin/python -m pytest -q
git diff --check
git diff --check v1.6.4..HEAD
```

Record exact counts and warning output.

- [ ] **Step 2: Run isolated Python 3.14 verification**

Create one bounded `mktemp -d` directory with cleanup trap, install `.[dev]`, run compileall and the full suite, and record exact counts.

- [ ] **Step 3: Build and verify artifacts**

In one bounded scratch directory, build wheel/sdist with `SOURCE_DATE_EPOCH` set to the HEAD commit time, run Twine, verify package contents, and record SHA-256 hashes.

- [ ] **Step 4: Run clean Python 3.12 wheel and Anti smoke**

Install the wheel into a clean Python 3.12 venv; run dependency check, CLI help, doctor help, provider presets, temporary-home `install-skill --verify`, and installed Anti help/tests.

- [ ] **Step 5: Audit dependencies and running-wheel behavior**

Audit the installed runtime site-packages with `pip-audit`. Start the wheel under an empty environment on a temporary loopback port and read `/health`, `/v1/models`, and non-live doctor diagnostics. Confirm read-only paths do not create secure-store lock/key files.

- [ ] **Step 6: Update evidence with exact source proof**

Append final source SHA, commands, counts, artifact hashes, clean-home results, and explicit non-claims to `docs/refactor-release-checklist.md`. Do not claim CI, live providers, service mutation, tag, publish, or release unless actually performed.

- [ ] **Step 7: Commit final local evidence**

```bash
git add docs/refactor-release-checklist.md
git commit -m "docs: record 1.7.0 release verification"
```

- [ ] **Step 8: Verify the clean final branch**

```bash
git status --short
git log --oneline --decorate -12
```

Expected: clean branch with all eight tasks committed. Then invoke `superpowers:finishing-a-development-branch`; do not push, tag, publish, call credentialed providers, mutate services, deploy, or edit real Codex config without the corresponding user choice or authorization.
