from __future__ import annotations

from typing import Iterable


def ordered_prompt(pieces: Iterable[str | None]) -> str:
    """Join prompt sources in their declared precedence order."""
    return "\n\n".join(piece.strip() for piece in pieces if piece and piece.strip()).strip()

