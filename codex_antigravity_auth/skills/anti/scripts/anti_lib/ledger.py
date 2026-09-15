from __future__ import annotations

import hashlib
from typing import Any


def execution_entry(*, stage: str, prompt: str, output: str, model: str, generation: dict[str, Any]) -> dict[str, Any]:
    return {
        "stage": stage,
        # Prompts can contain repository source and user input.  Keep enough
        # provenance to compare the executed call without persisting either.
        "promptSha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        "promptChars": len(prompt),
        "output": output,
        "model": model,
        "generation": generation,
    }


def prompts_as_text(entries: list[dict[str, Any]]) -> str:
    return "\n\n".join(
        f"## {entry['stage']}\n[prompt omitted; sha256={entry.get('promptSha256')}; chars={entry.get('promptChars', 0)}]"
        for entry in entries
    )
