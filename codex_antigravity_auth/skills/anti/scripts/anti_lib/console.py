"""Console-only escaping; stored text and serialized JSON values stay unchanged."""

from __future__ import annotations

import argparse
import builtins


_CONTROL_ESCAPES = {
    code: f"\\x{code:02x}"
    for code in (*range(32), *range(0x7F, 0xA0))
    if code not in (9, 10)  # Keep intended tabs and newlines.
}


def safe_terminal_text(value: str) -> str:
    return value.translate(_CONTROL_ESCAPES)


def console_print(*values, sep=" ", end="\n", file=None, flush=False) -> None:
    """Print display text without interpreting provider-controlled C0/C1 codes.

    CLI JSON is serialized with normal JSON escaping before reaching this
    boundary; no source or data structures are modified here.
    """
    builtins.print(
        *(safe_terminal_text(str(value)) for value in values),
        sep=safe_terminal_text(sep) if sep is not None else None,
        end=safe_terminal_text(end) if end is not None else None,
        file=file,
        flush=flush,
    )


class ConsoleArgumentParser(argparse.ArgumentParser):
    def _print_message(self, message, file=None):
        if message:
            super()._print_message(safe_terminal_text(message), file)
