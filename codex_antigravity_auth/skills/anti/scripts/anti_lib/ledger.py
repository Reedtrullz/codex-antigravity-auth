from __future__ import annotations

from typing import Any


def execution_entry(*, stage: str, prompt: str, output: str, model: str, generation: dict[str, Any]) -> dict[str, Any]:
    return {
        "stage": stage,
        "prompt": prompt,
        "output": output,
        "model": model,
        "generation": generation,
    }


def prompts_as_text(entries: list[dict[str, Any]]) -> str:
    return "\n\n".join(f"## {entry['stage']}\n{entry['prompt']}" for entry in entries)
