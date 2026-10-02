"""Process-wide test guard. Stdlib only; install before importing application code.

This is an accident guard for the test suite, not a sandbox for hostile Python.
"""
from __future__ import annotations

import atexit
import base64
from contextlib import contextmanager
from contextvars import ContextVar
import os
from pathlib import Path
import shutil
import _socket
import socket
import subprocess
import sys
import tempfile

_installed = False
_allowed_endpoints: set[tuple[str, int]] = set()
_binding = False
_violations: list[str] = []
_protected_paths: list[Path] = []
_validated_spawn = ContextVar("validated_test_spawn", default=None)
_allowed_ruff_check = ContextVar("allowed_test_ruff_check", default=None)
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


@contextmanager
def allow_ruff_check():
    """Authorize only the installed lint checker for one explicit fixture."""
    from ruff import find_ruff_bin

    executable = str(Path(find_ruff_bin()).resolve())
    token = _allowed_ruff_check.set(executable)
    try:
        yield executable
    finally:
        _allowed_ruff_check.reset(token)


def assert_no_violations():
    if _violations:
        messages = list(_violations)
        _violations.clear()
        raise AssertionError("Unexpected test isolation violation(s): " + "; ".join(messages))


def allow_listener(sock, host="127.0.0.1"):
    """Bind an owned TCP listener; authorization lives only as long as the fixture."""
    global _binding
    if host == "127.0.0.1" and sock.family == socket.AF_INET:
        address = (host, 0)
    elif host == "::1" and sock.family == socket.AF_INET6:
        address = (host, 0, 0, 0)
    else:
        raise ValueError("test listeners may bind only to their matching IPv4/IPv6 loopback address")
    _binding = True
    try:
        sock.bind(address)
    finally:
        _binding = False
    endpoint = sock.getsockname()[:2]
    _allowed_endpoints.add(endpoint)
    return endpoint


def remove_listener(endpoint):
    _allowed_endpoints.discard(endpoint)


def guarded_socketpair(family=socket.AF_INET, type=socket.SOCK_STREAM, proto=0):
    """Owned TCP pair for platforms without native socketpair (Windows)."""
    if family not in {socket.AF_INET, socket.AF_INET6} or type != socket.SOCK_STREAM:
        raise ValueError("test socketpair requires an IP stream")
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM, proto)
    endpoint = allow_listener(listener)
    client = None
    try:
        listener.listen(1)
        client = socket.socket(socket.AF_INET, socket.SOCK_STREAM, proto)
        client.connect(endpoint)
        peer, _ = listener.accept()
        return client, peer
    except BaseException:
        if client is not None:
            client.close()
        raise
    finally:
        listener.close()
        remove_listener(endpoint)


def install():
    global _installed
    if _installed:
        return
    _installed = True
    inherited_root = os.environ.get("ANTIGRAVITY_TEST_ROOT")
    original_home = Path(os.environ.get("ANTIGRAVITY_TEST_ORIGINAL_HOME") or Path.home())
    root = Path(inherited_root or tempfile.mkdtemp(prefix="antigravity-tests-"))
    root.mkdir(parents=True, exist_ok=True)
    child_home = os.environ.get("ANTIGRAVITY_TEST_CHILD_HOME") if inherited_root else None
    home = Path(child_home) if child_home else root
    support = str(Path(__file__).resolve().parent)
    safe = {k: v for k, v in os.environ.items() if k.upper() in _SAFE_ENV}
    safe.update({
        "HOME": str(home), "USERPROFILE": str(home),
        "XDG_CONFIG_HOME": str(home / "config"), "XDG_DATA_HOME": str(home / "data"),
        "XDG_CACHE_HOME": str(home / "cache"), "APPDATA": str(home / "appdata"),
        "LOCALAPPDATA": str(home / "localappdata"),
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
    _protected_paths.extend(original_home / p for p in (".codex", ".config", ".ssh", ".aws", ".azure", ".netrc", "_netrc", ".local/share/keyrings", "Library/Keychains", "AppData/Roaming/Python Keyring"))

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
    Path.home = classmethod(lambda cls: cls(safe_expanduser("~")))
    original_path_expanduser = Path.expanduser

    def safe_path_expanduser(path):
        if path.parts and path.parts[0] == "~":
            return type(path)(safe_expanduser("~")).joinpath(*path.parts[1:])
        return original_path_expanduser(path)

    Path.expanduser = safe_path_expanduser
    original_popen = subprocess.Popen

    class IsolatedPopen(original_popen):
        def __init__(self, args, *positional, **kwargs):
            if positional or kwargs.get("shell") or not isinstance(args, (list, tuple)):
                _deny("tests require an explicit argv subprocess with keyword options")
            argv = [os.fspath(a) for a in args]
            command = Path(argv[0]).name.lower()
            env = dict(os.environ if kwargs.get("env") is None else kwargs["env"])
            # Capture HOME from this already-isolated process, not a caller's
            # explicit child env. Child startup may only preserve this value.
            private_home = Path(os.environ.get("HOME") or str(home)).resolve()
            if private_home == original_home.resolve() or original_home / ".codex" in private_home.parents:
                _deny("Python child HOME must remain isolated")
            env["ANTIGRAVITY_TEST_CHILD_HOME"] = str(private_home)
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
            elif _allowed_ruff_check.get() == str(Path(argv[0]).resolve()) and argv[1:2] == ["check"]:
                pass
            else:
                _deny("test subprocess must be guarded Python, local Git or an authorized Ruff check")
            kwargs["env"] = env
            token = _validated_spawn.set((argv[0], argv, env))
            try:
                super().__init__(argv, **kwargs)
            finally:
                _validated_spawn.reset(token)

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
            if event == "os.posix_spawn" and _validated_spawn.get() is not None:
                executable, argv, env = args
                if (os.fsdecode(executable), [os.fsdecode(a) for a in argv], env) == _validated_spawn.get():
                    return
            _deny("unguarded subprocess creation is forbidden in tests")
        elif event in {"open", "os.remove", "os.rmdir", "os.mkdir", "os.rename", "os.chmod", "os.chown", "os.truncate", "os.listdir", "os.scandir", "os.link", "os.symlink"}:
            paths = args[:2] if event in {"os.rename", "os.link", "os.symlink"} else args[:1]
            for value in paths:
                if not isinstance(value, (str, bytes, os.PathLike)):
                    continue
                path = Path(os.fsdecode(value)).resolve()
                if any(path == p or p in path.parents for p in _protected_paths):
                    _deny("test attempted to access a real user credential/configuration path")

    sys.addaudithook(audit)
    if not hasattr(_socket, "socketpair"):
        socket.socketpair = guarded_socketpair

    def finish():
        if not inherited_root:
            shutil.rmtree(root, ignore_errors=True)
        if _violations:
            sys.stderr.write("Unexpected test isolation violation(s): " + "; ".join(_violations) + "\n")
            sys.stderr.flush()
            os._exit(98)
    atexit.register(finish)
