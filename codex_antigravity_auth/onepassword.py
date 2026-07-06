from __future__ import annotations

import re
import shutil
from pathlib import Path


OP_ENVIRONMENT_RE = re.compile(r"^[A-Za-z0-9_-]{8,128}$")


def normalize_op_env_file(path: str | None) -> str | None:
    if not path:
        return None
    expanded = Path(path).expanduser()
    if not expanded.is_file():
        raise ValueError(f"1Password env file does not exist or is not a regular file: {expanded}")
    return str(expanded)


def normalize_op_environment(environment_id: str | None) -> str | None:
    if not environment_id:
        return None
    if not OP_ENVIRONMENT_RE.fullmatch(environment_id):
        raise ValueError("1Password environment id must be 8-128 letters, numbers, underscores, or hyphens")
    return environment_id


def onepassword_command_prefix(
    *,
    op_env_file: str | None = None,
    op_environment: str | None = None,
) -> list[str]:
    env_file = normalize_op_env_file(op_env_file)
    environment = normalize_op_environment(op_environment)
    if env_file and environment:
        raise ValueError("Use only one of --op-env-file or --op-environment")
    if not env_file and not environment:
        return []
    op = shutil.which("op")
    if not op:
        raise ValueError(
            "1Password CLI (op) was not found on PATH; install it or omit --op-env-file/--op-environment"
        )
    if env_file:
        return [op, "run", "--env-file", env_file, "--"]
    return [op, "run", "--environment", environment or "", "--"]


def wrap_with_onepassword(
    command: list[str],
    *,
    op_env_file: str | None = None,
    op_environment: str | None = None,
) -> list[str]:
    return [*onepassword_command_prefix(op_env_file=op_env_file, op_environment=op_environment), *command]


def onepassword_runtime_description(*, op_env_file: str | None = None, op_environment: str | None = None) -> str | None:
    env_file = normalize_op_env_file(op_env_file)
    environment = normalize_op_environment(op_environment)
    if env_file and environment:
        raise ValueError("Use only one of --op-env-file or --op-environment")
    if env_file:
        return f"1Password env file {env_file}"
    if environment:
        return f"1Password Environment {environment}"
    return None
