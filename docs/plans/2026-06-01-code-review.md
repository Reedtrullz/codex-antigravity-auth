# Comprehensive Codebase Review — `codex-antigravity-auth`

## Critical Issues (Should Fix)

### 1. Non-streaming response text is swallowed — `transform.py:176-178`
When a Gemini candidate part has `"thoughtSignature"` but also contains `"text"`, the text is treated as reasoning and removed from the output. The live backend returns parts like:
```json
{"thoughtSignature": "...", "text": "Hello! I am Antigravity..."}
```
The current logic checks `"thoughtSignature" in part` FIRST, consumes the text as reasoning, and `continue`s — so the actual text never reaches `output_parts`. This is why non-streaming responses return empty `content: []` with the real text buried in `step_by_step_summary`.

**Fix**: Only treat a part as reasoning if `"thought"` is True or `"type"` is `"thinking"`. Remove the `"thoughtSignature" in part` check — instead, handle `thoughtSignature` by stripping it (or moving it to `providerMetadata`) while keeping the text in output.

### 2. Streaming delta for reasoning is empty string — `server.py:195-198`
When `"thoughtSignature" in part` triggers, the code emits a `response.reasoning.delta` with `delta: ""` because `part.get("text", "")` returns empty (the thought text in these parts is the actual model response, not reasoning). This produces spurious empty reasoning deltas in the stream.

**Fix**: Related to #1 — stop classifying `thoughtSignature` parts as reasoning.

### 3. Non-streaming: `created_at` uses wrong time epoch — `transform.py:250`
`int(time.time())` is only reached when `"time" in globals()` is True — but `time` is never imported in `transform.py`. The fallback `int(uuid.uuid4().time / 10000000)` produces UUID-based timestamps (e.g., `49154830507`) that look like future dates from the year 3500+. This may confuse Codex's token accounting or conversation threading.

**Fix**: Add `import time` at the top of `transform.py`.

### 4. `codex-shim` model slugs get dash-normalized — `server.py:15-18`
Our model IDs use dotted notation (`gemini-3.5-flash-high`) but `codex-shim` normalizes to hyphens (`gemini-3-5-flash-high`). If a user switches models via the shim, the slug sent to our gateway won't match `MODEL_MAP`. When using our gateway directly (no shim), this is fine. But it's a documented gotcha.

**Fix**: Accept both forms in `models.py:resolve_backend_model` by normalizing hyphens to dots.

### 5. Streaming: function call emits duplicate `output_index: 1` — `server.py:205-206`
Each function call yields TWO events (`output_item.added` + `output_item.done`) with `output_index: 1`. If multiple function calls occur in a single stream, all of them get index `1`, overwriting each other. Codex expects unique incrementing indices.

**Fix**: Track a counter for output indices and increment per tool call.

---

## High-Impact Improvements

### 6. Streaming: account rotation missing for streaming path — `server.py:158-163`
The non-streaming path retries with a rotated account on failure (lines 110-116). The streaming path does NOT — it just yields an error and gives up. A 401/429 on a streaming request should also trigger rotation and retry.

### 7. `AccountManager` in-memory cooldowns lost on restart — `accounts.py:13-14`
`self._failures` and `self._cooldowns` are stored only in memory. If the server restarts, cooldown state is lost and accounts that were recently rate-limited may be tried again immediately. The rate-limit reset times ARE persisted in the accounts JSON (`rateLimitResetTimes`) but the exponential backoff counters are not.

**Fix**: Persist `_failures` and `_cooldowns` in `accounts.py` to the storage JSON, or derive cooldown duration from `rateLimitResetTimes` on load.

### 8. `doctor` connectivity check fires a `HEAD` request — `cli.py:158-163`
Google's `cloudcode-pa.googleapis.com` returns 404 for `HEAD` requests (no root endpoint). This causes `[FAIL] Google Antigravity Connectivity: OFFLINE / TIMEOUT` even when the backend is perfectly reachable (confirmed via successful POSTs). 

**Fix**: Use a `GET` instead, or note that `HEAD` returns 404 as expected and treat it as "reachable".

### 9. `doctor` incorrectly marks accounts as EXPIRED — `cli.py:178-183`
When `expiresAt` is in milliseconds (epoch from some accounts migrated from hermes), comparing against `time.time()` (seconds) marks them as expired when they're actually valid. The hermes accounts JSON uses millisecond timestamps (`1780234094503`), but our code stores `time.time()` (seconds).

**Fix**: Normalize all timestamps to seconds on storage, or detect millisecond values and compare accordingly.

### 10. `build_headers` includes `deviceId`/`sessionToken` as separate headers — `server.py:53-54`
These are NOT standard HTTP headers that Google's Cloud Code API expects. The `deviceId` and `sessionToken` should be in the `Client-Metadata` JSON or as part of the fingerprint envelope, not as standalone headers. They're silently ignored by the backend (no harm) but add noise.

---

## Medium Issues

### 11. `transform_request` sends `VALIDATED` mode even without tools — `transform.py:107-112`
When using Claude models, `toolConfig.functionCallingConfig.mode = "VALIDATED"` is set regardless of whether any tools are in the request. This may cause validation issues with simple text prompts.

**Fix**: Only set VALIDATED mode when `gemini_tools` is non-empty.

### 12. `select_active_account` triggers `save_accounts` on every selection — `accounts.py:52,66,75`
Every call to `select_active_account` writes to disk up to 3 times (fingerprint generation, token refresh, and index update). Under heavy streaming load, this creates unnecessary disk I/O.

**Fix**: Batch writes or use a dirty flag with async debounced saves.

### 13. `fingerprint.py` uses `uuid.uuid4().time / 10000` for `createdAt` — `fingerprint.py:44`
`uuid4()` time is a random-ish field in UUID v1, NOT a reliable timestamp. This produces arbitrary values.

**Fix**: Import `time` and use `int(time.time() * 1000)` as done elsewhere.

---

## Minor / Polish

### 14. `sse_generator` swallows `json.JSONDecodeError` silently — `server.py:207`
Invalid SSE chunks are silently skipped with `except Exception: pass`. While resilience is good, a debug log would help diagnose backend changes.

### 15. `schema.py` placeholder injection happens unconditionally — `schema.py:34-42`
Every object-typed schema without `required` gets a `_placeholder` injected, even for non-tool schemas (e.g., response format schemas). This is harmless but may confuse model output.

### 16. `clin.py` has `import json` on line 8 but re-uses it later with urllib — no issue, just redundant.

### 17. No `time` import in `transform.py` — same as issue #3 but worth double-noting.

### 18. Test coverage gap: no tests for the `/v1/models` endpoint or `model_provider` dispatch.

---

## Security Review

- **OAuth flow**: Correctly uses PKCE with state parameter. Verifier stored in-memory with TTL. ✅
- **Token storage**: Fernet encryption with OS keyring. Backward-compatible plaintext fallback. ✅
- **Credential exposure**: `_credentials.py` in hermes-auth has real secrets; codex-auth uses external JSON. ✅
- **No secrets in logs**: `doctor` redacts client IDs partially. Token values are not printed. ✅
- **No input sanitization on `model` parameter**: The `model` value from the Codex request is passed directly to URL construction and header building. While `resolve_backend_model` maps known values, unknown values pass through. This is low risk since the backend validates models, but worth noting.

---

## Summary

| Severity | Count |
|----------|-------|
| Critical | 5 |
| High | 5 |
| Medium | 3 |
| Minor | 5 |
| **Total** | **18** |

The top priority fix is **#1** (text swallowed in non-streaming responses) — this is why Codex shows empty responses. **#3** (timestamp epoch) and **#5** (duplicate output indices) directly affect Codex compatibility. **#8** (doctor HEAD request) causes a permanent false negative in diagnostics.
