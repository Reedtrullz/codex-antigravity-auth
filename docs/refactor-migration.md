# Gateway Refactor Migration Notes

This refactor preserves the public `/health`, `/v1/models`, and `/v1/responses` routes, existing CLI command names, model aliases, encrypted store paths, and the executable bundled Anti entrypoint.

## Account state schema

`accountState.schemaVersion` is now `2`. Cooldowns and failure counters are scoped separately to the whole account, the Claude family, or the Gemini family. Legacy account-wide numeric values and millisecond epoch timestamps are normalized in memory and migrated transactionally on the next normal store write.

Use `codex-antigravity doctor --codex-ready --json` to inspect `diagnostics.account_store`. A `migration` value of `pending` means the store is readable but still plaintext or uses the legacy account-state schema. Diagnostics are read-only: they do not create keys, chmod files, migrate content, or rewrite configuration.

Before upgrading a production-like local setup, copy the encrypted account/provider files while the gateway is stopped. To roll back, stop the gateway, reinstall the previous package, and restore the matching pre-upgrade store copies. Do not hand-edit Fernet ciphertext. A previous package may not understand schema-version `2`, so restoring the matching backup is the safe rollback path.

## Persistence behavior

Account, provider, xAI OAuth, and model-overlay writes use a locked temporary file, `fsync`, atomic replacement, and private permissions. Plaintext JSON is still accepted for compatibility and is encrypted on a normal mutating load. Wrong-key encrypted data is reported as a decryption failure and is not reinterpreted as plaintext.

## Response terminal behavior

Provider responses are classified as `completed`, `incomplete`, or `failed` through a shared protocol contract.

- Refusals with a real refusal output are completed responses.
- Token-limit finishes are incomplete and include `incomplete_details.reason`.
- Empty or malformed provider HTTP 200 payloads now fail instead of being reported as completed.
- Streams emit exactly one terminal event followed by `[DONE]`; disconnects release leases and record cancellation.

Clients that previously treated every HTTP 200 as success must inspect the response `status` or streaming terminal event.

## Compatibility shims

The legacy transform functions, secure-storage helpers, account-manager methods, service dictionary serializers, and server request-construction exports remain as thin compatibility wrappers because repository and downstream callers still use them. New work should target `response_protocol.py`, `google_transport.py`, `openai_transport.py`, `account_state.py`, `secure_store.py`, and `service_manager.py` directly.

The bundled `anti.py` script remains executable and re-exports its existing test-facing functions. Its implementation modules now live under `scripts/anti_lib/` and are included in wheels and installed-skill verification.

## Service states

Service commands report observed state, not requested intent:

- `absent`: no installed service was observed.
- `installed`: a manifest/task exists but is not active.
- `active`: the service manager reports it active but the gateway is not reachable.
- `ready`: installed, active, and reachable.
- `failed`: the action or observation failed.

Install and uninstall commands exit non-zero when post-action observation does not match the requested result.

## Python compatibility

The declared minimum remains Python 3.10. CI continues to cover 3.10, 3.11, and 3.12, plus a blocking Python 3.14 evaluation lane. Python 3.14 was locally validated against the full suite before that lane was added; this change does not raise the minimum version.

