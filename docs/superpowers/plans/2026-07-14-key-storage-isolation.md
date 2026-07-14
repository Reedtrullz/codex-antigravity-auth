# Key Storage Isolation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent pytest from reading or writing the real system keyring, and carry the approved 1Password storage-key boundary into the later service rollout.

**Architecture:** An autouse pytest fixture injects a deterministic Fernet-compatible test key and replaces unmocked keyring reads and writes with immediate assertions. Production storage code remains unchanged. The existing provider-lane rollout plan is tightened so the future 1Password reference file must provide both the DeepSeek API key and `ANTIGRAVITY_STORAGE_KEY` before the service is installed.

**Tech Stack:** Python 3.10+, pytest 8, unittest.mock, cryptography Fernet, keyring, 1Password CLI `op run --env-file`.

## Global Constraints

- Tests must never read or write the user's Apple Keychain, 1Password vault, encrypted provider store, or encrypted OAuth store.
- Do not inspect, print, create, or resolve any real secret or `op://` reference during this implementation.
- Do not edit `~/.codex/config.toml`, provider configuration, LaunchAgents, services, or the running gateway.
- Production key-selection behavior remains environment first, then system keyring, then machine-local fallback for backward compatibility.
- The future durable service must receive `ANTIGRAVITY_STORAGE_KEY` through `op run --env-file`; no secret value may appear in a repository file, LaunchAgent manifest, argv, log, or Codex configuration.
- No external model calls, pushes, pull requests, releases, tags, deployments, or global package changes.

---

### Task 1: Isolate the entire pytest process from the system keyring

**Files:**

- Modify: `tests/conftest.py`
- Modify: `tests/test_secure_store.py`

**Interfaces:**

- Consumes: `codex_antigravity_auth.storage._get_encryption_key() -> str`, which already checks `ANTIGRAVITY_STORAGE_KEY` before keyring access.
- Produces: an autouse fixture named `_isolate_test_storage_from_system_keyring` that supplies a deterministic Fernet key and rejects unmocked `keyring.get_password` and `keyring.set_password` calls.

- [ ] **Step 1: Record safety baselines**

Run:

```bash
df -h /System/Volumes/Data
shasum -a 256 ~/.codex/config.toml
if [[ -v ANTIGRAVITY_STORAGE_KEY ]]; then
  echo ANTIGRAVITY_STORAGE_KEY_PRESENT
else
  echo ANTIGRAVITY_STORAGE_KEY_ABSENT
fi
git status --short
```

Expected: at least 30 GiB free; the config hash is recorded without printing content; the current shell reports the variable name's presence only; the only unrelated worktree entry is untracked `.superpowers/` scratch.

- [ ] **Step 2: Write a safe failing regression test**

Add this method to `TestKeyInitialization` in `tests/test_secure_store.py`:

```python
def test_pytest_storage_key_bypasses_system_keyring(self):
    blocked = AssertionError("tests must not access the system keyring")
    with patch(
        "codex_antigravity_auth.storage.keyring.get_password",
        side_effect=blocked,
    ) as get_password:
        with patch(
            "codex_antigravity_auth.storage.keyring.set_password",
            side_effect=blocked,
        ) as set_password:
            key = _get_encryption_key()

    Fernet(key.encode("utf-8"))
    get_password.assert_not_called()
    set_password.assert_not_called()
```

The explicit mocks make the RED run safe: failure happens in-process before any real Keychain operation.

- [ ] **Step 3: Run the regression test and verify RED**

Run:

```bash
source .venv/bin/activate
python3 -m pytest -q \
  tests/test_secure_store.py::TestKeyInitialization::test_pytest_storage_key_bypasses_system_keyring
```

Expected: FAIL with `AssertionError: tests must not access the system keyring`. No macOS dialog opens.

- [ ] **Step 4: Add the minimal suite-wide isolation fixture**

Replace `tests/conftest.py` with this complete guarded fixture file:

```python
import base64

import pytest


_TEST_STORAGE_KEY = base64.urlsafe_b64encode(b"\0" * 32).decode("ascii")


def _reject_system_keyring(*_args, **_kwargs):
    raise AssertionError("tests must not access the system keyring")


@pytest.fixture(autouse=True)
def _isolate_test_storage_from_system_keyring(monkeypatch):
    monkeypatch.setenv("ANTIGRAVITY_STORAGE_KEY", _TEST_STORAGE_KEY)
    monkeypatch.setattr("keyring.get_password", _reject_system_keyring)
    monkeypatch.setattr("keyring.set_password", _reject_system_keyring)


@pytest.fixture(autouse=True)
def _disable_gateway_refresh_ahead(monkeypatch):
    monkeypatch.setattr(
        "codex_antigravity_auth.server.schedule_refresh_accounts_ahead",
        lambda *args, **kwargs: False,
        raising=False,
    )
```

Tests that intentionally exercise keyring behavior may continue to install narrower explicit mocks inside their own test body; those mocks override the guard for that test and are restored afterward.

- [ ] **Step 5: Run the regression test and verify GREEN**

Run:

```bash
python3 -m pytest -q \
  tests/test_secure_store.py::TestKeyInitialization::test_pytest_storage_key_bypasses_system_keyring
```

Expected: `1 passed`; neither patched keyring function is called.

- [ ] **Step 6: Run credential-storage and CLI focused tests under the guard**

Run:

```bash
python3 -m pytest -q \
  tests/test_secure_store.py \
  tests/test_storage.py \
  tests/test_cli.py \
  tests/test_security_hardening.py
```

Expected: all selected tests and subtests pass. Any unisolated keyring access fails with `tests must not access the system keyring` instead of opening a dialog.

- [ ] **Step 7: Run the guarded full suite and hygiene checks**

Run:

```bash
python3 -m pytest -q
python3 -m compileall -q codex_antigravity_auth tests
git diff --check
shasum -a 256 ~/.codex/config.toml
```

Expected: all tests and subtests pass; only the pre-existing Starlette/httpx deprecation warning may remain; compileall and diff hygiene pass; the config hash matches Step 1; no Keychain dialog opens.

- [ ] **Step 8: Commit only the guarded test boundary**

Run:

```bash
git add tests/conftest.py tests/test_secure_store.py
git commit -m "test: isolate suite from system keyring"
```

Expected: one commit containing only the two test files. `.superpowers/` remains untracked.

---

### Task 2: Tighten the future 1Password service-environment gate

**Files:**

- Modify: `docs/superpowers/plans/2026-07-14-operationalize-provider-lanes.md`

**Interfaces:**

- Consumes: the existing Task 6 authorization gate and `--op-env-file` service wrapper.
- Produces: a future rollout contract requiring both `DEEPSEEK_API_KEY` and `ANTIGRAVITY_STORAGE_KEY` as operator-supplied `op://` references.

- [ ] **Step 1: Update Task 6's allowed reference-file contract**

Replace the single-variable dotenv example with prose requiring exactly two variable names, each mapped to an operator-supplied value beginning with `op://`:

```text
DEEPSEEK_API_KEY
ANTIGRAVITY_STORAGE_KEY
```

State explicitly that the storage-key reference prevents the authorized service from using Apple Keychain on this operator's setup. Preserve the existing separate approval requirement before creating or editing the external file.

- [ ] **Step 2: Replace the validation block with exact secret-safe checks**

Document these validations:

```bash
test -f ~/.codex/antigravity-provider.env
test "$(stat -f '%Lp' ~/.codex/antigravity-provider.env)" = 600
test "$(wc -l < ~/.codex/antigravity-provider.env | tr -d ' ')" = 2
rg -q '^DEEPSEEK_API_KEY=op://[^[:space:]]+$' ~/.codex/antigravity-provider.env
rg -q '^ANTIGRAVITY_STORAGE_KEY=op://[^[:space:]]+$' ~/.codex/antigravity-provider.env
! rg -q '^BLUESMINDS_API_KEY=' ~/.codex/antigravity-provider.env
```

The later bounded `op run` check may print only presence and length classes for both variable names, never either value.

- [ ] **Step 3: Verify the rollout plan is specific and placeholder-free**

Run:

```bash
rg -n 'ANTIGRAVITY_STORAGE_KEY|DEEPSEEK_API_KEY|BLUESMINDS_API_KEY|op run' \
  docs/superpowers/plans/2026-07-14-operationalize-provider-lanes.md
! rg -n 'operator-supplied-reference|TBD|TODO' \
  docs/superpowers/plans/2026-07-14-operationalize-provider-lanes.md
git diff --check
```

Expected: Task 6 requires exactly the approved two 1Password-backed variables, keeps BluesMinds absent, contains no unresolved placeholder syntax, and diff hygiene passes.

- [ ] **Step 4: Commit the plan correction separately**

Run:

```bash
git add docs/superpowers/plans/2026-07-14-operationalize-provider-lanes.md
git commit -m "docs: require 1Password storage key for service"
```

Expected: one documentation-only commit. No external reference file, secret, service, provider, LaunchAgent, or Codex configuration is changed.

---

### Task 3: Review and resume the provider-lane plan

**Files:** Verify committed changes; no new source files.

**Interfaces:**

- Consumes: the Task 1 guarded test boundary and Task 2 future service contract.
- Produces: evidence that the original provider-lane execution may resume at Task 3 without risking another Keychain dialog from pytest.

- [ ] **Step 1: Review the two commit ranges independently**

Review Task 1 for test isolation, mock ordering, subprocess inheritance, and absence of production changes. Review Task 2 for exact 1Password variable names, authorization boundaries, and absence of secret material.

Expected: no Critical or Important findings; any finding is fixed and re-reviewed before continuation.

- [ ] **Step 2: Record the execution checkpoint**

Record exact commit SHAs, focused/full test counts, the unchanged `~/.codex/config.toml` hash, and the non-claims that no real Keychain, 1Password, provider, service, or model endpoint was accessed.

- [ ] **Step 3: Resume the original plan at Task 3**

Continue with `docs/superpowers/plans/2026-07-14-operationalize-provider-lanes.md` Task 3. Task 5, Task 6, and Task 7 retain their explicit authorization gates; this implementation grants no advance approval for those actions.
