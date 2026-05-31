# Codex Antigravity Auth Integration & Reliability Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Create a highly reliable, robust, secure and production-ready Google Antigravity to OpenAI Codex responses-api translation proxy.

**Architecture:** Build on the current modular structures. Introduce secure credential storage, clean rotation strategies, rigorous test suites, edge case streaming event-delta handlers, and detailed diagnostics to guarantee maximum reliability.

**Tech Stack:** Python 3.10+, FastAPI, Uvicorn, httpx, cryptography/keyring.

---

## Phase 1: Authentication & Token Management Hardening

### Task 1: Add Secure Credential and Account Storage Encryption

**Objective:** Encrypt the `~/.codex/antigravity-accounts.json` and `~/.codex/antigravity-credentials.json` at rest using simple AES encryption via standard Python libraries (e.g., standard symmetric encryption like PBKDF2/cryptography or a clean fernet fallback using a local machine-specific salt/key) to prevent plaintext token leaks.

**Files:**
- Modify: `codex_antigravity_auth/storage.py`
- Modify: `codex_antigravity_auth/constants.py`
- Test: `tests/test_storage.py`

**Step 1: Write failing test**
Create `tests/test_storage.py` to assert that stored accounts are not saved in plaintext on disk but are still correctly loaded as parsed dictionaries.
```python
import tempfile
import os
import json
from pathlib import Path
from unittest.mock import patch
from codex_antigravity_auth.storage import save_accounts, load_accounts

def test_encrypted_accounts_storage():
    with tempfile.TemporaryDirectory() as tmp:
        # Override the accounts JSON path to temp file
        tmp_path = Path(tmp) / "accounts.json"
        with patch("codex_antigravity_auth.storage.get_accounts_json_path", return_value=tmp_path):
            test_data = {"accounts": [{"email": "test@gmail.com", "accessToken": "secret_token"}]}
            save_accounts(test_data)
            
            # Read raw bytes to verify it's encrypted (not standard JSON plaintext)
            with open(tmp_path, "rb") as f:
                raw_content = f.read()
            try:
                json.loads(raw_content.decode("utf-8"))
                assert False, "File is not encrypted"
            except json.JSONDecodeError:
                pass # Expected: not a plaintext JSON

            # Verify decrypted load succeeds
            loaded = load_accounts()
            assert loaded["accounts"][0]["email"] == "test@gmail.com"
```

**Step 2: Run test to verify failure**
Run: `pytest tests/test_storage.py`
Expected: FAIL - File is plaintext JSON.

**Step 3: Write minimal implementation**
Implement symmetric encryption in `storage.py` using a machine/user-specific fallback key (e.g. hash of system UUID + username) so that it is secure but transparent to the user without requiring manual password prompts.
Use `cryptography` Fernet encryption or `hashlib` + standard cipher. Since we want minimal non-stdlib dependencies unless declared in pyproject.toml, we can add `cryptography` to pyproject.toml dependencies. Let's patch `pyproject.toml` to declare `cryptography`.

```python
# In codex_antigravity_auth/storage.py
import sys
import base64
import hashlib
import getpass
import uuid
from cryptography.fernet import Fernet

def get_encryption_key() -> bytes:
    # Stable machine-specific unique identifier combined with username
    machine_id = str(uuid.getnode()) + getpass.getuser()
    key_hash = hashlib.sha256(machine_id.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(key_hash)

def encrypt_data(data: bytes) -> bytes:
    f = Fernet(get_encryption_key())
    return f.encrypt(data)

def decrypt_data(data: bytes) -> bytes:
    f = Fernet(get_encryption_key())
    return f.decrypt(data)
```
Update `save_accounts` to encrypt the payload before writing to disk, and update `load_accounts` to decrypt. If decryption fails (e.g. machine change), return default dictionary empty struct gracefully to avoid hard crashes.

**Step 4: Run test to verify pass**
Run: `pytest tests/test_storage.py`
Expected: PASS

**Step 5: Commit**
```bash
git add pyproject.toml codex_antigravity_auth/storage.py tests/test_storage.py
git commit -m "feat: add robust AES-256 Fernet encryption to token storage at rest"
```

---

### Task 2: Implement Smarter Account Rotation and Detailed Quota Cooldowns

**Objective:** Enhance `AccountManager` to track detailed failure categories, support model-specific health states, and serialize state cleanly without race conditions.

**Files:**
- Modify: `codex_antigravity_auth/accounts.py`
- Test: `tests/test_accounts.py`

**Step 1: Write failing test**
Create `tests/test_accounts.py` verifying that rotation skips accounts marked as failing / rate-limited for specific families, and respects different cooldown reasons (e.g. exponential backoff on 429).
```python
import time
from codex_antigravity_auth.accounts import AccountManager

def test_rotation_cooldowns():
    manager = AccountManager()
    # Populate stub accounts
    # Verify rotation skips cooling down accounts
    # Assert selection recovers when cooldown expires
```

**Step 2: Run test to verify failure**
Run: `pytest tests/test_accounts.py`
Expected: FAIL - health scoring or backoffs not implemented.

**Step 3: Write minimal implementation**
Improve `AccountManager` to:
- Track consecutive failure counts per account and scale cooldown durations.
- Gracefully handle `429` rate limit exceptions with precise cooldown calculations.
- Persist failure/cooldown state transactionally inside the storage wrapper.

**Step 4: Run test to verify pass**
Run: `pytest tests/test_accounts.py`
Expected: PASS

**Step 5: Commit**
```bash
git add codex_antigravity_auth/accounts.py tests/test_accounts.py
git commit -m "feat: add detailed rate-limiting backoffs and rotation strategy to AccountManager"
```

---

## Phase 2: Responses API Gateway Transformation Fidelity

### Task 3: Handle Structured Tool Parameters & Empty Required Validation

**Objective:** Cleanly validate and sanitize function declarations in `transform_request`, automatically converting complex OpenAPI-compatible JSON Schemas to simplified Antigravity forms, and ensuring the `_placeholder` parameter exists for VALIDATED mode tools.

**Files:**
- Modify: `codex_antigravity_auth/transform.py`
- Modify: `codex_antigravity_auth/schema.py`
- Test: `tests/test_schema_sanitization.py`

**Step 1: Write failing test**
Write a test verifying that when structured tool definitions are transformed, complex parameters are recursively cleaned (stripping `$ref`, `const`, `additionalProperties` etc.), and empty objects are auto-injected with `_placeholder` fields.
```python
from codex_antigravity_auth.schema import clean_json_schema

def test_clean_json_schema():
    raw_schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "minLength": 1, "format": "email"},
            "nested": {"type": "object", "$ref": "#/definitions/nested_type"}
        },
        "required": []
    }
    cleaned = clean_json_schema(raw_schema)
    assert "_placeholder" in cleaned["required"]
    assert "minLength" not in cleaned["properties"]["query"]
```

**Step 2: Run test to verify failure**
Run: `pytest tests/test_schema_sanitization.py`
Expected: FAIL - `$ref` or empty required constraints are not fully resolved.

**Step 3: Write minimal implementation**
Advance `clean_json_schema` in `schema.py` to recursively clean sub-properties, strip unsupported keywords/constraints, rewrite `$ref` declarations to object definitions, and enforce non-empty required properties.

**Step 4: Run test to verify pass**
Run: `pytest tests/test_schema_sanitization.py`
Expected: PASS

**Step 5: Commit**
```bash
git add codex_antigravity_auth/schema.py tests/test_schema_sanitization.py
git commit -m "feat: complete recursive schema sanitization and VALIDATED tool mode support"
```

---

### Task 4: Enhance SSE Streaming Event-Delta Translation

**Objective:** Fully implement standard Responses API streaming chunk transformation, resolving partial JSON structures, correctly isolating thinking/reasoning blocks, and emitting accurate `response.content_part.delta` and `response.reasoning.delta` events.

**Files:**
- Modify: `codex_antigravity_auth/server.py`
- Test: `tests/test_streaming.py`

**Step 1: Write failing test**
Create a test that parses mock Google Antigravity stream lines (containing text parts, thinking parts, and function calls) and asserts they get converted into precise, valid Responses API event strings.
```python
from codex_antigravity_auth.server import sse_generator # or mock stream test
```

**Step 2: Run test to verify failure**
Run: `pytest tests/test_streaming.py`
Expected: FAIL - missing detailed event mappings.

**Step 3: Write minimal implementation**
Refine the `sse_generator` within `server.py` to perfectly translate SSE event lines, handle partial buffered text, output standard reasoning deltas, and correctly aggregate tool call blocks.

**Step 4: Run test to verify pass**
Run: `pytest tests/test_streaming.py`
Expected: PASS

**Step 5: Commit**
```bash
git add codex_antigravity_auth/server.py tests/test_streaming.py
git commit -m "feat: add robust SSE translation mapping delta chunks and thinking blocks"
```

---

## Phase 3: Diagnostics & Verification

### Task 5: Enhance Diagnostics & Doctor Commands

**Objective:** Improve `codex-antigravity doctor` subcommand to test real internet connectivity to Google APIs, check the decryption status of accounts, and report detailed token health metrics.

**Files:**
- Modify: `codex_antigravity_auth/cli.py`
- Test: `tests/test_cli.py`

**Step 1: Write failing test**
Create a CLI diagnostic integration test asserting that `doctor` lists all details correctly.

**Step 2: Run test to verify failure**
Run: `pytest tests/test_cli.py`
Expected: FAIL - lack of connection checks.

**Step 3: Write minimal implementation**
Enhance the `run_doctor` function in `cli.py` to attempt a brief HEAD connection request to `https://cloudcode-pa.googleapis.com` (gracefully handling offline states), and verify that all accounts can be cleanly decrypted and checked for expiration.

**Step 4: Run test to verify pass**
Run: `pytest tests/test_cli.py`
Expected: PASS

**Step 5: Commit**
```bash
git add codex_antigravity_auth/cli.py tests/test_cli.py
git commit -m "feat: enhance CLI doctor command with connection tests and token decryption checks"
```
