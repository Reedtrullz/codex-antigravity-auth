"""Portable evidence paths for the qualification fixture manifest.

Standard library only, so it can be tested without the dispatcher's numpy and
Pillow dependencies.

Schema 1 manifests stored absolute, machine-specific ``clipPath`` and
``captureReceiptPath`` values. Schema 2 stores them relative to one
``evidenceRoot``. The root is written ``~``-based so it carries no user name,
and a relative root is taken relative to the manifest's own directory. A
dispatcher may override the root (``--evidence-root``) when the evidence tree
lives somewhere else. File identity never depends on the path: the dispatcher
still verifies every clip against its recorded ``clipSha256``.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import Any, Mapping

PATH_FIELDS = ("clipPath", "captureReceiptPath")


class ManifestPathError(ValueError):
    """A manifest path is missing, malformed or escapes its evidence root."""


def portable_root(root: Path, home: Path | None = None) -> str:
    """Render an absolute evidence root with the home directory as ``~``."""
    root = Path(root)
    home = Path.home() if home is None else Path(home)
    try:
        relative = root.relative_to(home)
    except ValueError:
        return root.as_posix()
    return "~" if relative == Path(".") else "~/" + relative.as_posix()


def relative_evidence_path(path: Path, root: Path) -> str:
    """Return ``path`` relative to ``root`` as a POSIX string, or fail."""
    try:
        return Path(path).relative_to(root).as_posix()
    except ValueError as exc:
        raise ManifestPathError(f"{path} is outside the evidence root {root}") from exc


def evidence_root(manifest: Mapping[str, Any], manifest_path: Path, override: Path | None = None) -> Path:
    """Resolve the evidence root: explicit override, else the manifest's ``evidenceRoot``."""
    if override is not None:
        root = Path(override).expanduser()
    else:
        value = manifest.get("evidenceRoot")
        if not isinstance(value, str) or not value.strip():
            raise ManifestPathError("manifest has no evidenceRoot; pass --evidence-root")
        root = Path(value).expanduser()
        if not root.is_absolute():
            root = Path(manifest_path).parent / root
    return root.resolve()


def resolve_fixture_path(manifest: Mapping[str, Any], manifest_path: Path, value: Any,
                         override: Path | None = None) -> Path:
    """Resolve one fixture path field for the manifest's schema version."""
    if not isinstance(value, str) or not value:
        raise ManifestPathError("fixture path must be a non-empty string")
    schema = manifest.get("schemaVersion")
    if schema == 1:
        # Legacy manifests carry absolute, machine-specific paths.
        path = Path(value).expanduser()
        if not path.is_absolute():
            raise ManifestPathError(f"schema 1 fixture path must be absolute: {value}")
        return path
    if schema != 2:
        raise ManifestPathError(f"unsupported manifest schemaVersion: {schema!r}")
    pure = PurePosixPath(value)
    if pure.is_absolute() or value.startswith("~") or "\\" in value or ".." in pure.parts:
        raise ManifestPathError(f"schema 2 fixture path must be relative to evidenceRoot: {value}")
    root = evidence_root(manifest, manifest_path, override)
    path = (root / Path(*pure.parts)).resolve()
    if path != root and root not in path.parents:
        raise ManifestPathError(f"fixture path escapes evidenceRoot: {value}")
    return path
