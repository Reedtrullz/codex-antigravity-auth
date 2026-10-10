# Gateway identity rotation fix - verification report

## Root cause

Ground truth from current `origin/main` (`c33c30e`), verified before edits:

1. The retry path after a 401/403/429 acquired an alternate account and
   set `rotation_attempted = True` before confirming the new account was
   distinct, so a same-account retry counted as rotation.
2. The streaming terminal failure log omitted `attempt_count` and
   `rotation_count` entirely.
3. Provider rejection bodies were classified with ad-hoc string checks.
   The nested `{"status": "PERMISSION_DENIED", "message":
   "VALIDATION_REQUIRED..."}` shape classified as `permission_denied`
   because string order beat provider intent.
4. `RESTRICTED_AGE` accounts churned through escalating cooldowns (120s,
   240s, 480s) plus auth strikes before the 3-strike disable. The
   `age_rejection` entry in `CURABLE_AUTH_ERROR_CLASSES` was dead code.
5. Query-parameter stripping covered only `accounts.google.com` validation
   URLs; other Google domains leaked.

## What changed

Commit `4c60f1b` on branch `codex/fix-rotation-diagnostics`:

- **New `provider_diagnostics.py`**: bounded `parse_provider_error()` and
  `provider_error_class()`. Fixed field set (http_status, reason, domain,
  error_number, 300-char message). Query params stripped before parsing.
  Priority-ordered reason extraction: VALIDATION_REQUIRED first, then
  RESTRICTED_AGE, then permission/rate-limit families.
- **`server.py`**: distinct-email check before `rotation_attempted=True`
  in non-streaming retry paths; streaming final failure log now includes
  `attempt_count` and `rotation_count`; terminal paths use
  `provider_error_class` with reason-conditional distinct classes;
  unstructured 401/403 bodies preserve the legacy `"auth"` fallback.
- **`account_state.py`**: `record_email` with `error_class="age_ineligible"`
  disables the account immediately (no cooldown, no strikes) and records
  `last_failure_class="age_ineligible"` so the terminal state is durable
  and visible.
- **`response_protocol.py`**: removed dead `age_rejection` from
  `CURABLE_AUTH_ERROR_CLASSES`.
- **`secret_redaction.py`**: Google validation URL regex widened to cover
  google.com, googleapis.com, and gstatic.com.

## Binding contract and panel - verified intact

- Every rotation gate checks `account_binding is None` first; bound
  requests never rotate, never fall back, never refresh.
- `acquire_bound_account` fail-closed validation of gatewayInstance,
  inventorySha256, and accountRef is untouched.
- Panel fail-closed behavior (`degraded_single_model`,
  min-successes enforcement) is untouched.

## Synthetic verification

All tests use temporary stores, no real credentials.

Focused suites: `tests/test_server_streaming.py` and
`tests/test_provider_diagnostics.py` - 52 passed, 17 subtests.

Full suite (`4c60f1b`): 3791 passed, 9 failed, 3 skipped, 236 subtests
passed in 142.62s.

The same 9 failures reproduce on pristine `c33c30e` (A/B in the same
worktree, same interpreter): 3 in account-state/request-log retention and
6 in codex-toml/installed-contract/orchestration-ownership/quality-gates.
All 9 are pre-existing environment artifacts: isolated subprocesses spawn
with a bare interpreter and cannot import user-site packages
(`cryptography`, `tomlkit`) because this checkout's `.venv` is a
bin-only shim without package directories. Not introduced by this change.

Coverage: rotation with succeeding alternate (truthful counts), age
ineligibility terminal disable, pool exhaustion without
`rotation_attempted`, nested VALIDATION_REQUIRED classification, URL
redaction, panel fail-closed source assertions.

## Bounded live probe

Two bounded probes, one request each, no fallback, no rotation:

1. Installed gateway (v2.4.2, pre-fix binary) via the anti sidecar smoke:
   transport + generation passed (312 output chars, gemini-3.8-flash).
   Proves provider transport only; it does not exercise this PR's code.
2. PR code (this branch, commit `4c60f1b`) served the request itself:
   HTTP 200, response text `probe-ok`, upstream 200, request log shows
   `attempt_count: 1`, `rotation_count: 0`, `rotation_attempted:
   false`, `terminal_kind: completed`.

The PR-code probe ran against a temporary isolated
`ANTIGRAVITY_STATE_HOME` with a copy of the account store and the
keyring key supplied via `ANTIGRAVITY_STORAGE_KEY`. The production store
was never written (mtime verified unchanged before/after) and the key was
never printed or persisted.

Probe context: the default `~/.codex` state home currently has a
fail-closed `key_conflict` between the file fallback key and the legacy
keyring key (`antigravity-storage-key.json` selection is absent). The
PR-code gateway correctly refuses to run there. The installed 2.4.2
gateway predates that gate, which is why its smoke still works. This is a
pre-existing local state issue for the owner to resolve separately (see
recommendations below); it is not a regression from this branch.

What the probe proves: PR code starts, resolves accounts from the real
schema, completes a real generation, and records truthful counts on
success. What it does not prove: rotation behavior under 401/403 (that
path is covered by synthetic tests only), provider-side VALIDATION_REQUIRED
recovery, or production admission.

## Remaining scope

Persistent 403 after this fix is an account-eligibility problem for the
account owner to resolve with Google. No rotation-to-evade, no validation
bypass, no User-Agent spoofing was implemented or used.
