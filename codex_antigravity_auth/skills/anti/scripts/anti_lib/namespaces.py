"""Read-only client and gateway roots, also usable by the standalone skill."""
from __future__ import annotations

import os
from pathlib import Path

CLIENT_HOME_ENV = "CODEX_HOME"
STATE_HOME_ENV = "ANTIGRAVITY_STATE_HOME"


def root_path(value: str, *, label: str) -> Path:
    if not isinstance(value, str) or not value.strip() or any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise ValueError(f"{label} must be a nonempty absolute directory path without control characters")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ValueError(f"{label} must be an absolute directory path")
    # An absent leaf is a valid future root; existing components must be
    # directories. Broken symlinks are not usable future directories either.
    for component in (path, *path.parents):
        if (component.exists() or component.is_symlink()) and not component.is_dir():
            raise ValueError(f"{label} must select a directory, not an existing non-directory path")
    return path


def _root(name: str, home: Path | None = None) -> Path:
    if name in os.environ:
        return root_path(os.environ[name], label=name)
    return root_path(str((home if home is not None else Path.home()) / ".codex"), label=f"default {name}")


def client_home(*, home: Path | None = None) -> Path:
    return _root(CLIENT_HOME_ENV, home)


def gateway_home(*, home: Path | None = None) -> Path:
    return _root(STATE_HOME_ENV, home)


def client_config_path(value: str | Path = "~/.codex/config.toml") -> Path:
    if str(value) == "~/.codex/config.toml":
        return client_home() / "config.toml"
    return Path(value).expanduser()


def client_skills_path(value: str | Path = "~/.codex/skills") -> Path:
    if str(value) == "~/.codex/skills":
        return client_home() / "skills"
    return Path(value).expanduser()


def gateway_file(value: str, basename: str) -> Path:
    # Preserve explicit file overrides and callers' existing test injection seams.
    if str(value) == f"~/.codex/{basename}":
        return gateway_home() / basename
    return Path(value).expanduser()


def namespace_diagnostics(*, home: Path | None = None) -> dict:
    client = client_home(home=home)
    state = gateway_home(home=home)
    return {
        "client_root_source": CLIENT_HOME_ENV if CLIENT_HOME_ENV in os.environ else "default",
        "gateway_root_source": STATE_HOME_ENV if STATE_HOME_ENV in os.environ else "default",
        "roots_shared": client.resolve() == state.resolve(),
        "client_auth_fallback": False,
        "gateway_shared_default": CLIENT_HOME_ENV in os.environ and STATE_HOME_ENV not in os.environ,
    }
