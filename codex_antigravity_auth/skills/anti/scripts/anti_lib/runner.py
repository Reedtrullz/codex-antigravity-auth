from __future__ import annotations

from typing import Any, Callable


def presentable_result(
    *, text: str, caveats: list[str], metadata: dict[str, Any], sanitizer: Callable[[Any], Any]
) -> tuple[str, list[str], dict[str, Any]]:
    """Sanitize all model-controlled fields immediately before presentation."""
    safe = sanitizer({"text": text, "caveats": caveats, "metadata": metadata})
    return str(safe["text"]), list(safe["caveats"]), dict(safe["metadata"])
