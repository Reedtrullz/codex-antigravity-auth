"""Codex Antigravity Auth package."""

__all__ = ["app", "main"]


def __getattr__(name):
    if name == "app":
        from .server import app

        return app
    if name == "main":
        from .cli import main

        return main
    raise AttributeError(name)
