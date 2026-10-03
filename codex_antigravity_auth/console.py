"""Shared console boundary, also bundled for standalone Anti installations."""

from .skills.anti.scripts.anti_lib.console import (
    ConsoleArgumentParser,
    console_print,
    safe_terminal_text,
)

__all__ = ["ConsoleArgumentParser", "console_print", "safe_terminal_text"]
