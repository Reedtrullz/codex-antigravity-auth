# Key Storage Isolation Design

**Date:** 2026-07-14

## Problem

The test suite can call encrypted-storage code without `ANTIGRAVITY_STORAGE_KEY`. In that state, `_get_encryption_key()` falls through to the system keyring and may open a macOS Keychain dialog. Test execution must never read or write a user's real credential store.

The operator uses 1Password for durable secret material. The gateway already gives `ANTIGRAVITY_STORAGE_KEY` precedence over the system keyring, so the durable service can avoid Apple Keychain by receiving that variable through the existing `op run --env-file` boundary.

## Decisions

### Test boundary

- The pytest process receives a deterministic, test-only `ANTIGRAVITY_STORAGE_KEY` through the existing autouse test fixture.
- The fixture blocks unmocked `keyring.get_password` and `keyring.set_password` calls with an immediate assertion. Tests that intentionally exercise keyring integration must provide their own explicit mocks inside the test.
- A regression test calls the real storage-key selection function while keyring access is forbidden and proves that the returned value is a valid Fernet key.
- Production storage code is not given pytest-specific branches.

### Durable runtime boundary

- The later service-environment task will add `ANTIGRAVITY_STORAGE_KEY` with an operator-supplied `op://` reference to the same private mode-0600 reference file planned for `DEEPSEEK_API_KEY`.
- The reference file contains only `op://` references. Secret values are resolved by `op run` into the gateway process environment and are never written to the LaunchAgent manifest, repository, argv, logs, or Codex configuration.
- Creating or editing that external reference file remains a separate authorization gate. This test-isolation change does not inspect 1Password contents or mutate the running service.

### Compatibility

- The system-keyring and machine-local fallback paths remain available for other installations that do not inject `ANTIGRAVITY_STORAGE_KEY`.
- Existing tests that deliberately clear the environment remain responsible for mocking or short-circuiting external credential-store access.
- No provider routing, OAuth storage format, fallback policy, acting agent, judge, or model identity changes.

## Verification

1. Add the regression test first and observe it fail because the suite does not yet supply a test storage key.
2. Add the minimal fixture isolation and observe the focused test pass.
3. Run storage, secure-store, CLI, and security-hardening tests with the Keychain guard active.
4. Run the full suite only after the guarded focused tests pass. Any accidental system-keyring access must fail in-process instead of opening a macOS dialog.
5. Confirm `~/.codex/config.toml` is unchanged and no credential, provider, service, or 1Password state was accessed.

## Non-goals

- Removing Apple Keychain support for all users.
- Adding a direct 1Password SDK dependency.
- Discovering, printing, or creating the operator's 1Password secret reference during the test fix.
- Restarting or upgrading the running gateway as part of test isolation.
