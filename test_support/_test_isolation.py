"""Process-wide test guard. Stdlib only; install before importing application code.

This is an accident guard for the test suite, not a sandbox for hostile Python.
"""
from __future__ import annotations

import atexit
import base64
from contextlib import contextmanager
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile

_installed = False
_allowed_endpoints: set[tuple[str, int]] = set()
_binding = False
_violations: list[str] = []
_SAFE_ENV = {"PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "LANG", "LC_ALL",
             "TMPDIR", "TEMP", "TMP", "VIRTUAL_ENV"}
_STORAGE_KEY = base64.urlsafe_b64encode(b"\0" * 32).decode("ascii")


def _deny(message):
    _violations.append(message)
    raise AssertionError(message)


@contextmanager
def expected_denial():
    """A negative guard test consumes only the violation it deliberately caused."""
    before = len(_violations)
    try:
        yield
    finally:
        del _violations[before:]


def assert_no_violations():
    if _violations:
        messages = list(_violations)
        _violations.clear()
        raise AssertionError("Unexpected test isolation violation(s): " + "; ".join(messages))


def allow_listener(sock):
    """Bind an owned TCP listener; authorization lives only as long as the fixture."""
    global _binding
    _binding = True
    try:
        sock.bind(("127.0.0.1", 0))
    finally:
        _binding = False
    endpoint = sock.getsockname()[:2]
    _allowed_endpoints.add(endpoint)
    return endpoint


def remove_listener(endpoint):
    _allowed_endpoints.discard(endpoint)


def install():
    global _installed
    if _installed:
        return
    _installed = True
    inherited_root = os.environ.get("ANTIGRAVITY_TEST_ROOT")
    original_home = Path(os.environ.get("ANTIGRAVITY_TEST_ORIGINAL_HOME") or Path.home())
    root = Path(inherited_root or tempfile.mkdtemp(prefix="antigravity-tests-"))
    root.mkdir(parents=True, exist_ok=True)
    support = str(Path(__file__).resolve().parent)
    safe = {k: v for k, v in os.environ.items() if k.upper() in _SAFE_ENV}
    safe.update({
        "HOME": str(root), "USERPROFILE": str(root),
        "XDG_CONFIG_HOME": str(root / "config"), "XDG_DATA_HOME": str(root / "data"),
        "XDG_CACHE_HOME": str(root / "cache"), "APPDATA": str(root / "appdata"),
        "LOCALAPPDATA": str(root / "localappdata"),
        "ANTIGRAVITY_TEST_ROOT": str(root),
        "ANTIGRAVITY_TEST_ORIGINAL_HOME": str(original_home),
        "ANTIGRAVITY_STORAGE_KEY": _STORAGE_KEY,
        "PYTHON_KEYRING_BACKEND": "keyring.backends.fail.Keyring",
        "CODEX_ANTIGRAVITY_NO_UPDATE_CHECK": "1",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        "PYTHONPATH": support,
        "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
    })
    # Do not carry credential variables or explicit user state roots into collection.
    os.environ.clear()
    os.environ.update(safe)
    protected = [original_home / p for p in (".codex", ".config", ".local/share/keyrings", "Library/Keychains")]

    original_expanduser = os.path.expanduser

    def safe_expanduser(path):
        # Tests sometimes clear the whole environment. Do not fall back to pwd's
        # real user home when HOME is absent, including in child processes.
        marker = b"~" if isinstance(path, bytes) else "~"
        separator = b"/" if isinstance(path, bytes) else "/"
        if path == marker or path.startswith(marker + separator):
            home = os.environ.get("HOME") or os.environ.get("USERPROFILE") or str(root)
            if isinstance(path, bytes):
                home = os.fsencode(home)
            return home + path[1:]
        return original_expanduser(path)

    os.path.expanduser = safe_expanduser
    original_popen = subprocess.Popen

    class IsolatedPopen(original_popen):
        def __init__(self, args, *positional, **kwargs):
            if positional or kwargs.get("shell") or not isinstance(args, (list, tuple)):
                _deny("tests require an explicit argv subprocess with keyword options")
            argv = [os.fspath(a) for a in args]
            command = Path(argv[0]).name.lower()
            env = dict(os.environ if kwargs.get("env") is None else kwargs["env"])
            # Children retain synthetic per-test env, but cannot omit the guard.
            for key in ("ANTIGRAVITY_TEST_ROOT", "ANTIGRAVITY_TEST_ORIGINAL_HOME", "PYTHONPATH", "PYTHON_KEYRING_BACKEND",
                        "GIT_CONFIG_NOSYSTEM", "GIT_CONFIG_GLOBAL", "GIT_TERMINAL_PROMPT"):
                env[key] = safe[key]
            for key, value in safe.items():
                env.setdefault(key, value)
            if command in {"python", "python3", "python.exe", "python3.exe"} or Path(argv[0]).resolve() == Path(sys.executable).resolve():
                argv[0] = sys.executable
                if any(a in {"-I", "-E", "-S"} for a in argv[1:]):
                    _deny("Python subprocess must load the test startup guard")
            elif command in {"git", "git.exe"}:
                # Git is needed for local fixture repositories, never remote access.
                permitted = {"init", "config", "add", "commit", "diff", "rev-parse", "ls-files", "status", "show", "log", "mv"}
                index = 1
                while index < len(argv) and argv[index] == "-c":
                    index += 2
                if index == len(argv) or argv[index] not in permitted:
                    _deny("test subprocess is not an allowed local Git operation")
                argv[1:1] = ["-c", "core.hooksPath=" + str(root / "empty-hooks"), "-c", "core.fsmonitor=false", "-c", "credential.helper="]
            else:
                _deny("test subprocess must be the guarded Python interpreter or local Git")
            kwargs["env"] = env
            super().__init__(argv, **kwargs)

    subprocess.Popen = IsolatedPopen

    def audit(event, args):
        if event in {"socket.connect", "socket.sendto"}:
            address = args[1] if event == "socket.connect" else args[-1]
            if not isinstance(address, tuple) or address[:2] not in _allowed_endpoints:
                _deny("test network denied: destination is not an owned fixture listener")
        elif event == "socket.getaddrinfo":
            if (args[0], args[1]) not in _allowed_endpoints:
                _deny("test DNS denied: destination is not an owned fixture listener")
        elif event == "socket.bind":
            if not _binding:
                _deny("test listener must be created by the fake-upstream fixture")
        elif event in {"os.system", "os.exec", "os.posix_spawn", "os.spawn"}:
            _deny("unguarded subprocess creation is forbidden in tests")
        elif event == "open" and isinstance(args[0], (str, bytes, os.PathLike)):
            path = Path(os.fsdecode(args[0])).absolute()
            if any(path == p or p in path.parents for p in protected):
                _deny("test attempted to open a real user credential/configuration path")

    sys.addaudithook(audit)

    def finish():
        if not inherited_root:
            shutil.rmtree(root, ignore_errors=True)
        if _violations:
            sys.stderr.write("Unexpected test isolation violation(s): " + "; ".join(_violations) + "\n")
            sys.stderr.flush()
            os._exit(98)
    atexit.register(finish)
