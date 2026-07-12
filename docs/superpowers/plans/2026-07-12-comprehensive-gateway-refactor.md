# Comprehensive Gateway Refactor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Refactor the gateway around explicit protocol, transport, state, persistence, service, and Anti-helper contracts while preserving the public CLI, `/v1` API, encrypted data, model aliases, and installed-skill behavior.

**Architecture:** Introduce provider-neutral response and outcome types first, then move Google and OpenAI-compatible execution behind adapters while `server.py` keeps temporary compatibility wrappers. Move account-state policy, transactional persistence, service observation, and Anti internals into focused modules only after characterization tests lock current public behavior. Remove old paths only after route-level parity tests pass.

**Tech Stack:** Python 3.10+, FastAPI, HTTPX, Pydantic/JSON-compatible dictionaries, `cryptography` Fernet, OS keyring, file locks, pytest/unittest, setuptools, GitHub Actions.

**Approved design:** `docs/superpowers/specs/2026-07-12-comprehensive-gateway-refactor-design.md`

**Review provenance:** This plan incorporates the local code review and the successful Anti/Opus architecture review `20260712T030017Z-f4417dc0` (`claude-opus-4-6`, one attempt, no fallback, no omitted files). Opus's concrete corrections—refusal as completed output, epoch-second cooldown units and migration, explicit counter fields, complete tool-choice semantics, repository-relative documentation, and a deliberate Python compatibility evaluation—are reflected below. A broader 13-chunk planning attempt timed out on Opus chunk 1 and was interrupted after one Sonnet fallback chunk; none of that incomplete fallback output is treated as authoritative.

---

## Global constraints and execution rules

- Preserve the public routes `/health`, `/v1/models`, and `/v1/responses`; existing CLI command names and safe defaults; model aliases; config paths; encrypted account/provider data; and bundled Anti installation.
- Treat `server.py`, `accounts.py`, `storage.py`, `service.py`, and `skills/anti/scripts/anti.py` exports as compatibility surfaces until their callers and tests have migrated.
- Never classify a clean EOF or an empty HTTP 200 as successful completion.
- Refusal is output content inside a completed response, not a fourth terminal status.
- Retry or rotate only before any user-visible output. Release leases on success, failure, cancellation, and disconnect.
- Reject a requested capability with HTTP 400 before provider execution when it cannot be honored faithfully; never silently discard it.
- Persist cooldown timestamps as Unix epoch seconds. Normalize zero away and divide legacy millisecond values by 1000 during migration.
- Do not run credentialed live tests in CI or modify a real `~/.codex/config.toml` in tests.
- Before long test loops, run `df -h /System/Volumes/Data` and stop if free space is below 50 GiB.
- At every task boundary run the focused test first, then the named regression set. Commit only when both are green.

## Dependency order

```text
protocol contract
  ├── Google transport ──┐
  └── OpenAI transport ──┼── thin server shell ── diagnostics/release proof
account state ───────────┤
secure store ────────────┤
service manager ─────────┘
Anti modularization ────────────────────────────── package/release proof
```

Tasks 2 and 3 depend on Task 1. Tasks 4, 5, 6, and 7 can proceed after Task 1 without depending on each other. Task 8 integrates Tasks 2–6. Tasks 9–10 require all preceding implementation work.

---

## Task 1: Define the provider-neutral response protocol

**Files:**

- Create: `codex_antigravity_auth/response_protocol.py`
- Create: `tests/test_response_protocol.py`
- Modify: `codex_antigravity_auth/server.py:764-1002`
- Reference: `codex_antigravity_auth/transform.py:173-237,532-667,878-956`

**Interfaces:**

```python
class TerminalKind(str, Enum):
    COMPLETED = "completed"
    INCOMPLETE = "incomplete"
    FAILED = "failed"

@dataclass(frozen=True)
class ProviderTerminal:
    kind: TerminalKind
    reason: str
    incomplete_reason: str | None = None
    error_code: str | None = None
    error_message: str | None = None

@dataclass(frozen=True)
class ProviderResult:
    output: tuple[dict[str, Any], ...]
    usage: dict[str, int]
    terminal: ProviderTerminal
    provider_response_id: str | None = None

class ResponseEventBuilder:
    def created(self) -> dict[str, Any]: ...
    def add_output_item(self, item: dict[str, Any]) -> list[dict[str, Any]]: ...
    def terminal(self, result: ProviderResult) -> dict[str, Any]: ...
    def done_marker(self) -> str: ...

def classify_terminal(*, output: Sequence[dict[str, Any]], finish_reason: str | None,
                      safety_block: dict[str, Any] | None,
                      malformed: bool = False) -> ProviderTerminal: ...
def validate_capabilities(request: dict[str, Any], capabilities: ProviderCapabilities) -> None: ...
```

`ResponseEventBuilder` owns sequence numbers, stable item IDs, output indices, exactly one terminal event, and one `[DONE]`. It raises an internal state error if a caller emits twice or adds output after terminal.

- [ ] Add table-driven failing tests for meaningful text, reasoning, function calls, explicit refusal, max-token truncation, safety block, malformed payload, and empty HTTP 200. Assert refusal produces `completed` plus a refusal item; empty produces `failed`.
- [ ] Add failing event-builder tests for monotonic sequence numbers, stable function-call IDs/indices, exactly-once terminal events, output-after-terminal rejection, and `[DONE]` ordering.
- [ ] Add failing capability tests for `tool_choice` values `auto`, `none`, `required`, and function-specific choice; unknown functions; and `parallel_tool_calls=False` on an incapable route.
- [ ] Run `python3 -m pytest -q tests/test_response_protocol.py`; expect failures because the module does not exist.
- [ ] Implement the dataclasses, classifier, capability contract, event builder, usage normalizer, and sanitized failure constructor. Keep these pure: no HTTP, account, credential, or filesystem imports.
- [ ] Replace only the reusable validation helpers in `server.py` with delegating wrappers. Do not move network execution yet.
- [ ] Run `python3 -m pytest -q tests/test_response_protocol.py tests/test_server_streaming.py tests/test_fidelity_edge_cases.py`; expect all green.
- [ ] Commit: `git add codex_antigravity_auth/response_protocol.py codex_antigravity_auth/server.py tests/test_response_protocol.py && git commit -m "refactor: define shared response protocol"`

---

## Task 2: Extract Google request and terminal translation

**Files:**

- Create: `codex_antigravity_auth/google_transport.py`
- Create: `tests/test_google_transport.py`
- Modify: `codex_antigravity_auth/transform.py:274-667`
- Modify: `codex_antigravity_auth/server.py:1035-1969`
- Modify: `tests/test_server_streaming.py`
- Modify: `tests/test_regressions.py`

**Interfaces:**

```python
@dataclass(frozen=True)
class AccountLease:
    email: str
    project_id: str | None
    access_token: str

@dataclass(frozen=True)
class AttemptOutcome:
    scope: Literal["none", "family", "account"]
    category: Literal["success", "rate_limit", "quota", "auth", "invalid_request", "transport", "cancelled"]
    retry_after_seconds: float | None = None

class GoogleTransport:
    async def execute(self, request: dict[str, Any], lease: AccountLease,
                      *, stream: bool) -> ProviderResult | AsyncIterator[dict[str, Any]]: ...
```

The adapter consumes normalized request options and returns protocol results/events plus one typed attempt outcome to the account layer. `transform_request()` and `transform_response()` remain wrappers until Task 8.

- [ ] Add failing non-streaming fixtures for wrapped/unwrapped candidates, empty candidates, `MAX_TOKENS`, safety `promptFeedback`, malformed parts, text carrying `thoughtSignature`, reasoning, function calls, and usage.
- [ ] Add failing streaming fixtures for empty clean EOF, finish-only chunks, truncated output, safety refusal, malformed JSON/SSE, unique function-call indices, disconnect cancellation, and provider failure after visible output.
- [ ] Assert retry/rotation signals occur only before the first emitted text/reasoning/function-call delta. After visible output, assert one failed/incomplete terminal and no replay.
- [ ] Run `python3 -m pytest -q tests/test_google_transport.py`; expect import/behavior failures.
- [ ] Move Google envelope creation, HTTP execution, payload parsing, `finishReason`/`promptFeedback` handling, and stream conversion into `GoogleTransport`. Preserve `thoughtSignature` text behavior.
- [ ] Make `server.create_response()` select/acquire/release the account but delegate Google execution. Retain the old server helpers as wrappers during this task.
- [ ] Run `python3 -m pytest -q tests/test_google_transport.py tests/test_server_streaming.py tests/test_transform.py tests/test_regressions.py`.
- [ ] Commit: `git add codex_antigravity_auth/google_transport.py codex_antigravity_auth/transform.py codex_antigravity_auth/server.py tests/test_google_transport.py tests/test_server_streaming.py tests/test_regressions.py && git commit -m "refactor: extract Google transport"`

---

## Task 3: Extract OpenAI-compatible transports and capability fidelity

**Files:**

- Create: `codex_antigravity_auth/openai_transport.py`
- Create: `tests/test_openai_transport.py`
- Modify: `codex_antigravity_auth/transform.py:56-75,669-956`
- Modify: `codex_antigravity_auth/server.py:696-763,985-1033,1972-2372`
- Modify: `tests/test_byok_providers.py`
- Modify: `tests/test_xai_oauth.py`

**Interfaces:**

```python
@dataclass(frozen=True)
class ProviderCapabilities:
    native_responses: bool
    parallel_tool_calls: bool
    structured_output: bool
    stop_sequences: bool
    reasoning: bool
    streaming_usage: bool

class OpenAICompatibleTransport:
    async def execute_chat(self, request: dict[str, Any], provider: dict[str, Any],
                           *, stream: bool) -> ProviderResult | AsyncIterator[dict[str, Any]]: ...
    async def execute_responses(self, request: dict[str, Any], provider: dict[str, Any],
                                *, stream: bool) -> ProviderResult | AsyncIterator[dict[str, Any]]: ...
```

- [ ] Add failing tests proving `parallel_tool_calls` is forwarded, `false` is rejected when unsupported, and all four tool-choice semantics map faithfully or fail before HTTP execution.
- [ ] Add Chat Completions tests for empty 200/EOF, `finish_reason=length`, refusal/content-filter outcomes, malformed chunks, streaming usage, structured output, stop sequences, reasoning, and multiple indexed tool calls.
- [ ] Add native Responses tests that validate terminal-event ordering, sanitize provider errors, and never forward gateway-only metadata or credentials.
- [ ] Run `python3 -m pytest -q tests/test_openai_transport.py tests/test_byok_providers.py tests/test_xai_oauth.py`; expect new tests to fail.
- [ ] Move URL/header construction, request translation, Chat SSE parsing, native Responses validation, and terminal conversion into the adapter. Add `parallel_tool_calls` to `transform_request_to_chat()`.
- [ ] Keep `create_openai_compatible_response()`, `openai_compatible_sse_generator()`, `create_xai_oauth_response()`, and `xai_oauth_responses_sse_generator()` as delegating compatibility wrappers until Task 8.
- [ ] Run `python3 -m pytest -q tests/test_openai_transport.py tests/test_byok_providers.py tests/test_xai_oauth.py tests/test_fidelity_transforms.py tests/test_fidelity_edge_cases.py`.
- [ ] Commit: `git add codex_antigravity_auth/openai_transport.py codex_antigravity_auth/transform.py codex_antigravity_auth/server.py tests/test_openai_transport.py tests/test_byok_providers.py tests/test_xai_oauth.py && git commit -m "refactor: extract OpenAI compatible transports"`

---

## Task 4: Introduce scoped account state and migrate persisted data

**Files:**

- Create: `codex_antigravity_auth/account_state.py`
- Create: `tests/test_account_state.py`
- Modify: `codex_antigravity_auth/accounts.py`
- Modify: `codex_antigravity_auth/storage.py:28-64`
- Modify: `tests/test_accounts.py`

**Interfaces:**

```python
@dataclass(frozen=True)
class Lease:
    account: dict[str, Any]
    family: str

class AccountState:
    def acquire(self, family: str) -> Lease | None: ...
    def release(self, lease: Lease) -> None: ...
    def record(self, lease: Lease, outcome: AttemptOutcome) -> None: ...
    def clear_failures(self, email: str, family: str | None = None) -> None: ...

def migrate_account_state(data: dict[str, Any], *, now: float) -> tuple[dict[str, Any], bool]: ...
```

Normalized state stores `activeIndex` by family, `failures[email].account`, `failures[email].families[family]`, matching cooldown maps, and sanitized counters (`attempts`, `successes`, `failures`, `lastAttemptAt`, `lastSuccessAt`, `lastFailureAt`, `lastCategory`).

- [ ] Add failing migration tests for legacy account-wide maps, millisecond timestamps, zero/negative/NaN values, unknown fields, repeated idempotent loads, and no reauthentication requirement.
- [ ] Add selection tests proving a Claude-family cooldown does not block Gemini, an auth failure blocks the account globally, active indices are family-specific, and in-flight counts release on exceptions/cancellation.
- [ ] Add counter tests proving exactly one outcome per attempted account and no double count for internal retries.
- [ ] Run `python3 -m pytest -q tests/test_account_state.py tests/test_accounts.py`; expect failures.
- [ ] Implement `AccountState`; adapt `AccountManager` as the backward-compatible facade. Map current `mark_failure()` calls to typed outcomes, but keep the old signature until all callers migrate in Task 8.
- [ ] Persist migration only through the storage transaction callback and only when normalization changed data.
- [ ] Run `python3 -m pytest -q tests/test_account_state.py tests/test_accounts.py tests/test_server_streaming.py`.
- [ ] Commit: `git add codex_antigravity_auth/account_state.py codex_antigravity_auth/accounts.py codex_antigravity_auth/storage.py tests/test_account_state.py tests/test_accounts.py && git commit -m "refactor: scope account cooldown state"`

---

## Task 5: Centralize transactional secure persistence

**Files:**

- Create: `codex_antigravity_auth/secure_store.py`
- Create: `tests/test_secure_store.py`
- Modify: `codex_antigravity_auth/storage.py`
- Modify: `codex_antigravity_auth/models.py:106-110,276-354`
- Modify: `codex_antigravity_auth/byok.py`
- Modify: `tests/test_storage.py`
- Modify: `tests/test_security_hardening.py`

**Interfaces:**

```python
class StoreError(Exception): ...
class StoreNotFound(StoreError): ...
class StoreInvalidData(StoreError): ...
class StoreDecryptionError(StoreError): ...
class StorePermissionError(StoreError): ...

class SecureStore:
    def load_json(self, path: Path, *, default: Callable[[], dict[str, Any]]) -> dict[str, Any]: ...
    def save_json(self, path: Path, data: dict[str, Any]) -> None: ...
    def update_json(self, path: Path, mutator: Callable[[dict[str, Any]], T],
                    *, default: Callable[[], dict[str, Any]]) -> T: ...
    def atomic_write_text(self, path: Path, text: str, *, mode: int = 0o600) -> None: ...
```

- [ ] Add a multiprocessing failing test where two first-run processes request the storage key simultaneously; both must receive the same valid key and neither may overwrite the other.
- [ ] Add failing tests for symlink rejection, private modes, lock coverage across read-modify-write, `fsync` before atomic replace, interrupted temp writes, wrong-key/corruption distinction, and explicit legacy plaintext migration.
- [ ] Add model-overlay concurrency tests proving readers see either the old or complete new TOML, never a partial file.
- [ ] Run `python3 -m pytest -q tests/test_secure_store.py tests/test_storage.py tests/test_security_hardening.py`; expect failures.
- [ ] Implement key initialization under a cross-process lock with create-if-absent semantics. Implement same-directory temporary writes, file flush/`fsync`, atomic replace, directory `fsync` where supported, and cleanup on failure.
- [ ] Make `storage.py` a compatibility facade over `SecureStore`; route provider JSON and model overlays through the same transaction/atomic-text primitives.
- [ ] Run `python3 -m pytest -q tests/test_secure_store.py tests/test_storage.py tests/test_security_hardening.py tests/test_byok_providers.py tests/test_cli.py -k 'overlay or provider or storage or credential'`.
- [ ] Commit: `git add codex_antigravity_auth/secure_store.py codex_antigravity_auth/storage.py codex_antigravity_auth/models.py codex_antigravity_auth/byok.py tests/test_secure_store.py tests/test_storage.py tests/test_security_hardening.py && git commit -m "refactor: centralize secure transactional storage"`

---

## Task 6: Make service actions structured and truthful

**Files:**

- Create: `codex_antigravity_auth/service_manager.py`
- Create: `tests/test_service_manager.py`
- Modify: `codex_antigravity_auth/service.py`
- Modify: `codex_antigravity_auth/cli.py:827-910`
- Modify: `tests/test_cli.py:2820-2990`

**Interfaces:**

```python
class ServiceState(str, Enum):
    NOT_INSTALLED = "not_installed"
    INSTALLED_INACTIVE = "installed_inactive"
    ACTIVE_UNREACHABLE = "active_unreachable"
    READY = "ready"
    DEGRADED = "degraded"
    FAILED = "failed"

@dataclass(frozen=True)
class ServiceResult:
    action: Literal["install", "uninstall", "status"]
    state: ServiceState
    installed: bool
    active: bool
    reachable: bool
    changed: bool
    commands: tuple[dict[str, Any], ...]
    error: str | None = None
```

- [ ] Add failing macOS/Linux/Windows tests for command failure, manifest written but inactive, active but unreachable, ready, uninstall failure, and idempotent install/uninstall.
- [ ] Add CLI tests proving `installed=false, active=false` never prints a positive installed message and exits non-zero for a requested install that was not observed.
- [ ] Run `python3 -m pytest -q tests/test_service_manager.py tests/test_cli.py -k service`; expect failures.
- [ ] Implement platform adapters and post-action observation. Sanitize command evidence; never include environment values or secrets.
- [ ] Keep `install_service()`, `uninstall_service()`, and `service_status()` returning their existing dictionary shapes by serializing `ServiceResult`, then make CLI presentation branch on `state` rather than the requested action.
- [ ] Run `python3 -m pytest -q tests/test_service_manager.py tests/test_cli.py -k service`.
- [ ] Commit: `git add codex_antigravity_auth/service_manager.py codex_antigravity_auth/service.py codex_antigravity_auth/cli.py tests/test_service_manager.py tests/test_cli.py && git commit -m "fix: report observed service state truthfully"`

---

## Task 7: Modularize Anti and make evidence/redaction trustworthy

**Files:**

- Create: `codex_antigravity_auth/skills/anti/scripts/anti_lib/__init__.py`
- Create: `codex_antigravity_auth/skills/anti/scripts/anti_lib/redaction.py`
- Create: `codex_antigravity_auth/skills/anti/scripts/anti_lib/context.py`
- Create: `codex_antigravity_auth/skills/anti/scripts/anti_lib/chunking.py`
- Create: `codex_antigravity_auth/skills/anti/scripts/anti_lib/ledger.py`
- Create: `codex_antigravity_auth/skills/anti/scripts/anti_lib/runner.py`
- Modify: `codex_antigravity_auth/skills/anti/scripts/anti.py`
- Modify: `codex_antigravity_auth/skills/anti/tests/test_anti.py`
- Modify: `pyproject.toml:50-56`

**Compatibility rule:** `anti.py` remains the executable entrypoint and re-exports functions used by current tests/installations. The split modules must use relative imports when packaged and a guarded local-path bootstrap when `anti.py` is executed directly from an installed skill.

- [ ] Add secret-sentinel tests covering model text, exceptions, progress lines, JSON output, summaries, ledgers, and manifests. Assert the sentinel never appears unless an explicit unsafe raw-output flag exists and is selected.
- [ ] Add failing chunk-cap tests asserting manifest `included_files`, `omitted_files`, chunk count, and caveats reflect the prompts actually sent—not the pre-cap plan.
- [ ] Add failing full-ledger tests asserting saved prompts equal every actual chunk and synthesis prompt in order; do not save the stale unchunked prompt.
- [ ] Add failing tests for user-prompt ordering, negative/zero numeric CLI values, retry/fallback provenance, interrupted runs, and deterministic run IDs/correlation metadata.
- [ ] Run `python3 -m pytest -q codex_antigravity_auth/skills/anti/tests/test_anti.py`; expect new failures.
- [ ] Move redaction/sanitization first and apply it at the final presentation and persistence boundaries. Then extract context, chunk planning, ledger, and gateway runner without changing CLI flags.
- [ ] Update package data to include `skills/anti/scripts/anti_lib/*.py`. Verify a direct installed copy can import without the source checkout on `PYTHONPATH`.
- [ ] Run `python3 -m pytest -q codex_antigravity_auth/skills/anti/tests/test_anti.py tests/test_cli.py -k 'anti or skill'`.
- [ ] Build and install a wheel into an isolated venv, then run `codex-antigravity install-skill --verify`; expected exit 0 and all installed-skill tests pass.
- [ ] Commit: `git add codex_antigravity_auth/skills/anti pyproject.toml tests/test_cli.py && git commit -m "refactor: modularize and harden Anti helper"`

---

## Task 8: Integrate the contracts and thin the server/CLI shells

**Files:**

- Modify: `codex_antigravity_auth/server.py`
- Modify: `codex_antigravity_auth/cli.py`
- Modify: `codex_antigravity_auth/transform.py`
- Modify: `codex_antigravity_auth/accounts.py`
- Modify: `codex_antigravity_auth/storage.py`
- Modify: `codex_antigravity_auth/service.py`
- Modify: `tests/test_server_streaming.py`
- Modify: `tests/test_regressions.py`

- [ ] Add route-level matrix tests covering Google, BYOK Chat Completions, and native Responses in streaming/non-streaming modes for completed, incomplete, refusal, empty, malformed, auth exhaustion, and cancellation outcomes.
- [ ] Add a lease spy to every route matrix case; assert one acquire/release pair and one typed attempt record per attempted account even on disconnect or exception.
- [ ] Run the new matrix and preserve its failing output as the integration baseline.
- [ ] Change `create_response()` to: validate body → resolve route/capabilities → acquire lease → invoke adapter → record outcome → release in `finally` → return protocol response/stream.
- [ ] Delete duplicated SSE construction, terminal inference, transport URL/header logic, and cooldown policy from `server.py` only after the matrix passes through the new modules.
- [ ] Remove compatibility wrappers from `transform.py`, `accounts.py`, `storage.py`, and `service.py` only when `rg` proves there are no callers outside explicitly supported exports; otherwise retain thin deprecated wrappers.
- [ ] Keep CLI limited to parser/dispatch/presentation for service and storage-related commands. No CLI branch may infer success from the requested action.
- [ ] Run `python3 -m pytest -q tests/test_response_protocol.py tests/test_google_transport.py tests/test_openai_transport.py tests/test_account_state.py tests/test_server_streaming.py tests/test_regressions.py`.
- [ ] Run `python3 -m pytest -q`; expected full suite green with no xfails added to mask regressions.
- [ ] Commit: `git add codex_antigravity_auth tests && git commit -m "refactor: integrate gateway module boundaries"`

---

## Task 9: Add diagnostics, metrics, and migration observability

**Files:**

- Modify: `codex_antigravity_auth/observability.py`
- Modify: `codex_antigravity_auth/server.py`
- Modify: `codex_antigravity_auth/cli.py`
- Modify: `tests/test_cli.py`
- Modify: `tests/test_security_hardening.py`
- Modify: `README.md`
- Modify: `VERIFICATION.md`

- [ ] Add failing tests for sanitized request-log fields: route, terminal kind/reason, attempt count, rotation count, cooldown scope/category, latency, usage, cancellation, and Anti correlation ID. Assert no prompt, account email, OAuth token, provider key, raw body, or unsafe provider message is recorded.
- [ ] Add doctor/status tests reporting account-state schema version, pending/completed migration, store accessibility, service observed state, and provider capability mismatches without mutating configuration.
- [ ] Run `python3 -m pytest -q tests/test_cli.py tests/test_security_hardening.py -k 'doctor or log or diagnostic or migration'`; expect failures.
- [ ] Emit structured sanitized records at the orchestration boundary after terminal classification and attempt recording. Keep bounded JSONL retention behavior.
- [ ] Update `doctor --json`, text presentation, README diagnostics, and verification instructions. Clearly distinguish local, mocked, package, and live-provider evidence.
- [ ] Run the focused tests, then `python3 -m pytest -q tests/test_cli.py tests/test_security_hardening.py`.
- [ ] Commit: `git add codex_antigravity_auth/observability.py codex_antigravity_auth/server.py codex_antigravity_auth/cli.py tests/test_cli.py tests/test_security_hardening.py README.md VERIFICATION.md && git commit -m "feat: expose sanitized gateway diagnostics"`

---

## Task 10: Strengthen CI, packaging, documentation, and release gates

**Files:**

- Modify: `.github/workflows/ci.yml`
- Modify: `.github/workflows/publish.yml`
- Modify: `pyproject.toml`
- Modify: `README.md`
- Modify: `USAGE.md`
- Modify: `VERIFICATION.md`
- Create: `docs/refactor-migration.md`
- Create: `docs/refactor-release-checklist.md`

- [ ] Update the CI matrix to keep declared Python 3.10–3.12 coverage and add a non-blocking or blocking evaluation lane for the newest supported CPython after local dependency compatibility is confirmed. Do not silently expand `requires-python` first.
- [ ] Add CI gates for `python -m compileall`, the full suite, wheel/sdist build, `twine check`, isolated wheel install, CLI help, provider presets, bundled Anti import/tests, and `install-skill --verify`.
- [ ] Add a package-content assertion for every `anti_lib` module and required skill asset.
- [ ] Document schema migration/rollback, compatibility shims, terminal behavior changes, service state meanings, and the fact that empty HTTP 200 now fails instead of completing.
- [ ] Run `python3 -m compileall -q codex_antigravity_auth tests`.
- [ ] Run `python3 -m pytest -q`; expected all tests and subtests green.
- [ ] Run `python3 -m build && python3 -m twine check dist/*` in a clean bounded build directory.
- [ ] Install the wheel into a fresh venv and run: `codex-antigravity --help`, `codex-antigravity doctor --help`, `codex-antigravity provider presets`, and `codex-antigravity install-skill --verify`.
- [ ] Run `git diff --check` and inspect `git status --short`; expect no generated build artifacts staged.
- [ ] Commit: `git add .github pyproject.toml README.md USAGE.md VERIFICATION.md docs/refactor-migration.md docs/refactor-release-checklist.md && git commit -m "ci: enforce refactor release gates"`

---

## Task 11: Final compatibility and live release evidence

**Files:**

- Modify: `docs/refactor-release-checklist.md`
- Modify: project release notes/changelog if present at execution time

- [ ] Run the full local gate from Task 10 on a clean checkout and record exact Python versions, test counts, artifact hashes, and package contents.
- [ ] Exercise CLI compatibility in a temporary home: setup/check/doctor help, model overlays, provider configuration with dummy keys, service rendering, and Anti installation. Do not touch real user config.
- [ ] With explicit credentialed-live authorization and existing local credentials, run one minimal Google non-streaming response, one Google streaming response, and one configured BYOK route. Record only model/route, terminal state, latency, usage, sanitized request-log correlation ID, and success/failure—not prompts or credentials.
- [ ] Verify a max-token or controlled truncation fixture yields `incomplete`, an empty mocked stream yields `failed`, and a refusal fixture yields completed refusal output.
- [ ] Verify service install truthfulness on the current host only if service mutation is explicitly in scope; otherwise mark it as not claimed.
- [ ] Compare public route payloads, CLI flags/help, model aliases, existing encrypted files, and installed Anti behavior against the pre-refactor compatibility contract.
- [ ] Update the release checklist with exact evidence and explicit non-claims. Do not publish or tag without a separate user instruction.
- [ ] Stage `docs/refactor-release-checklist.md` and the repository's changelog if one exists at execution time, then commit with `git commit -m "docs: record refactor verification evidence"`.

---

## Plan self-review and completion criteria

Before beginning implementation:

- [ ] Search for placeholders: `rg -n 'TB[D]|TO[D]O|FIXM[E]|implemen[t] later|simila[r] to|appropriate erro[r] handling|write test[s] for' docs/superpowers/plans/2026-07-12-comprehensive-gateway-refactor.md`; expected no matches.
- [ ] Verify every approved design area maps to at least one task: terminal fidelity (1–3, 8), capability validation (1, 3), account state (4), secure persistence (5), service truthfulness (6), Anti reliability (7), diagnostics (9), CI/package/docs/live gates (10–11).
- [ ] Verify type and naming consistency: `ProviderResult`, `ProviderTerminal`, `AttemptOutcome`, `ProviderCapabilities`, `Lease`, `ServiceResult`, and `ServiceState` each have one owning module.
- [ ] Confirm every extraction retains a compatibility shim until route-level tests prove parity.
- [ ] Confirm every behavior-changing task starts with a failing test and ends with focused plus regression tests and a commit.

The refactor is complete only when:

- all streaming and non-streaming routes share the terminal classifier;
- no empty clean EOF can produce `response.completed`;
- tool choice and parallel-tool behavior are forwarded or explicitly rejected;
- account cooldown scope and migration are proven across restarts;
- all mutable local state uses transaction/atomic-write primitives appropriate to its format;
- service output reflects observed state;
- Anti manifests, ledgers, prompt order, and redaction match actual execution;
- the full suite, packaging checks, isolated install, and installed-skill verification pass;
- live-provider claims, if any, are backed by sanitized evidence and all untested surfaces are explicit non-claims.
