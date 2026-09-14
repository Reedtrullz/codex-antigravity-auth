# T15 Synthetic Fixture

This fixture is non-sensitive and exists only for one bounded Anti acceptance check.

## Contract

The parser accepts a short markdown document.
The parser rejects an empty document.
The parser preserves line order.
The parser reports a stable diagnostic code.

## Implementation sketch

```python
def parse_document(text):
    if not text.strip():
        return {"ok": False, "code": "EMPTY"}
    lines = text.splitlines()
    return {"ok": True, "line_count": len(lines), "lines": lines}
```

## Acceptance notes

Expected status is deterministic for this fixture.
No credentials, tokens, personal data, or provider configuration are present.
The review should distinguish contract behavior from implementation preference.
The review should report only evidence grounded in this file.
