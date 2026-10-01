"""Bounded, redacted runtime logging. Explicit account-management CLI stays separate."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import hmac
import io
import logging
import os
from pathlib import Path
import re
import stat
import sys
import traceback

from .redaction import redact_secret_text
from .secure_store import file_lock

MAX_BYTES = 2 * 1024 * 1024
BACKUPS = 2
MAX_RECORD_BYTES = 4096
DIRECTORY = "antigravity-process-logs"
POLICY = b'{"version":1,"maxBytes":2097152,"backups":2}\n'
_SALT = os.urandom(32)
_EMAIL = re.compile(r"(?<![^\s<>\"'/:=,;()])[^\s<>\"'/:=,;()]+@[^\s<>\"'/:=,;()]+")
_CONTROLS = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029\u202a-\u202e\u2066-\u2069]")


def account_ref(value: object) -> str:
    return "acct_" + hmac.new(_SALT, str(value).encode("utf-8", "replace"), hashlib.sha256).hexdigest()[:12]


def runtime_text(value: object) -> str:
    text = redact_secret_text(str(value))
    text = _EMAIL.sub("[ACCOUNT]", text)
    return _CONTROLS.sub(lambda match: f"\\u{ord(match[0]):04x}", text)


def log_path(home: Path, port: int) -> Path:
    return home / DIRECTORY / f"gateway-{int(port)}.log"


def process_log_info(home: Path, port: int) -> dict:
    path = log_path(home, port)
    return {
        "path": str(path), "max_bytes": MAX_BYTES, "backups": BACKUPS,
        "max_total_bytes": MAX_BYTES * (BACKUPS + 1), "max_record_bytes": MAX_RECORD_BYTES,
        "kind": "process", "contents_included": False,
        "legacy_paths": [str(home / f"antigravity-gateway-{port}.log"),
                         str(home / f"antigravity-service-{port}.out.log"),
                         str(home / f"antigravity-service-{port}.err.log")],
        "legacy_policy": "unmanaged; preserved without reading or deleting",
    }


def _regular(path: Path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or getattr(info, "st_file_attributes", 0) & 0x400:
        raise OSError("Unsafe process-log path")
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise OSError("Process-log owner differs")
    return info


def _open_private(path: Path, *, exclusive=False):
    info = _regular(path)
    flags = os.O_WRONLY | os.O_CREAT | (os.O_EXCL if exclusive else os.O_APPEND)
    fd = os.open(path, flags | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0), 0o600)
    try:
        opened = os.fstat(fd)
        current = _regular(path)
        if current is None or not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1 or (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
            raise OSError("Process-log path changed")
        if info and (info.st_dev, info.st_ino) != (opened.st_dev, opened.st_ino):
            raise OSError("Process-log identity changed")
        if hasattr(os, "fchmod"):
            os.fchmod(fd, 0o600)
        return fd
    except BaseException:
        os.close(fd)
        raise


def _prepare_directory(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if directory.is_symlink() or not directory.is_dir():
        raise OSError("Unsafe process-log directory")
    marker = directory / ".policy-v1"
    if not marker.exists():
        # Never adopt unrelated existing files into a deletion/rotation policy.
        if any(directory.iterdir()):
            raise OSError("Process-log directory has no ownership marker")
        try:
            fd = _open_private(marker, exclusive=True)
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd, "wb") as stream:
                stream.write(POLICY)
                stream.flush()
                os.fsync(stream.fileno())
    info = _regular(marker)
    if info is None or info.st_size != len(POLICY):
        raise OSError("Unknown process-log retention policy")
    descriptor = os.open(marker, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as stream:
        opened = os.fstat(stream.fileno())
        if (info.st_dev, info.st_ino) != (opened.st_dev, opened.st_ino) or stream.read(len(POLICY) + 1) != POLICY:
            raise OSError("Unknown process-log retention policy")
    if os.name != "nt":
        os.chmod(directory, 0o700)


def prepare_log(path: Path) -> None:
    directory = path.parent
    # Serialize first publication across ports without adopting lock files in an
    # unknown directory. The fixed-size initialization lock lives beside it.
    with file_lock(directory.parent / "antigravity-process-log-init"):
        _prepare_directory(directory)
    with file_lock(path):
        fd = _open_private(path)
        os.close(fd)


class RuntimeFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        # Uvicorn access logs carry arbitrary request URLs. Do not retain them.
        if record.name == "uvicorn.access":
            return "INFO request access record omitted; use structured request telemetry"
        try:
            message = record.getMessage()
        except Exception:
            message = "unformattable runtime record omitted"
        if record.exc_info and record.exc_info[0]:
            frames = traceback.extract_tb(record.exc_info[2], limit=8)
            # Never include exception values, source lines, local variables or full paths.
            message += " | " + record.exc_info[0].__name__ + " " + " <- ".join(
                f"{Path(frame.filename).name}:{frame.lineno} ({frame.name})" for frame in frames
            )
        return runtime_text(f"{self.formatTime(record)} {record.levelname} {record.name}: {message}")


class BoundedProcessHandler(logging.Handler):
    def __init__(self, path: Path, *, console=None, max_bytes=MAX_BYTES, backups=BACKUPS):
        super().__init__()
        if max_bytes < 32 or not 0 <= backups <= BACKUPS:
            raise ValueError("Invalid process-log limits")
        self.path = path
        self.console = console
        self.max_bytes = max_bytes
        self.backups = backups
        self.dropped = 0
        self.setFormatter(RuntimeFormatter())
        prepare_log(path)

    def emit(self, record):
        try:
            formatted = self.format(record)
            limit = min(MAX_RECORD_BYTES, self.max_bytes) - 1
            encoded = formatted.encode("utf-8", "replace")
            if len(encoded) > limit:
                encoded = encoded[:max(0, limit - 14)].decode("utf-8", "ignore").encode() + b" [truncated]"
            payload = encoded + b"\n"
            if self.console is not None:
                try:
                    self.console.write(payload.decode("utf-8"))
                    self.console.flush()
                except Exception:
                    pass
            with file_lock(self.path):
                paths = [self.path] + [Path(f"{self.path}.{index}") for index in range(1, self.backups + 1)]
                infos = [_regular(path) for path in paths]
                if infos[0] is not None and infos[0].st_size + len(payload) > self.max_bytes:
                    if self.backups:
                        paths[-1].unlink(missing_ok=True)
                        for index in range(self.backups, 0, -1):
                            if paths[index - 1].exists():
                                os.replace(paths[index - 1], paths[index])
                    else:
                        paths[0].unlink()
                fd = _open_private(self.path)
                with os.fdopen(fd, "ab") as stream:
                    stream.write(payload)
        except Exception:
            # Never recurse through logging.handleError (which prints raw records).
            self.dropped += 1


class _SuppressedBinaryStream(io.RawIOBase):
    def __init__(self, owner):
        self.owner = owner

    def write(self, value):
        if self.closed:
            raise ValueError("write to closed stream")
        size = memoryview(value).nbytes
        self.owner._notice(size)
        return size

    def writable(self):
        return True

    def fileno(self):
        if self.closed:
            raise ValueError("operation on closed stream")
        return self.owner.fileno()


class SuppressedRuntimeStream(io.TextIOBase):
    """Raw writes have no complete-message boundary, so never persist their content."""
    def __init__(self, label):
        self.label = label
        self.reported = False
        self.buffer = _SuppressedBinaryStream(self)
        self._null_descriptor = None

    def _notice(self, size):
        if self.closed:
            raise ValueError("write to closed stream")
        if size and not self.reported:
            self.reported = True
            logging.getLogger(__name__).warning("Unstructured %s output suppressed", self.label)

    def write(self, value):
        if not isinstance(value, str):
            raise TypeError("text stream requires str")
        self._notice(len(value))
        return len(value)

    def writable(self):
        return True

    def fileno(self):
        self._notice(1)
        if self._null_descriptor is None:
            self._null_descriptor = os.open(os.devnull, os.O_WRONLY)
        return self._null_descriptor

    def close(self):
        if self._null_descriptor is not None:
            os.close(self._null_descriptor)
            self._null_descriptor = None
        self.buffer.close()
        super().close()

    def flush(self):
        pass

    @property
    def encoding(self):
        return "utf-8"

    @property
    def errors(self):
        return "replace"


@contextmanager
def runtime_logging(path: Path, *, console=True):
    old_stdout, old_stderr = sys.stdout, sys.stderr
    handler = BoundedProcessHandler(path, console=old_stderr if console else None)
    root = logging.getLogger()
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logging.getLogger(name)
    loggers = [root] + [item for item in logging.Logger.manager.loggerDict.values() if isinstance(item, logging.Logger)]
    saved = [(item, list(item.handlers), item.level, item.propagate, item.disabled) for item in loggers]
    streams = (SuppressedRuntimeStream("stdout"), SuppressedRuntimeStream("stderr"))
    try:
        for item in loggers:
            item.handlers = []
            item.propagate = True
            item.disabled = False
        root.handlers = [handler]
        root.setLevel(logging.INFO)
        for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
            logging.getLogger(name).setLevel(logging.INFO)
        sys.stdout, sys.stderr = streams
        yield handler
    finally:
        sys.stdout, sys.stderr = old_stdout, old_stderr
        for stream in streams:
            try:
                stream.close()
            except OSError:
                pass
        for item, handlers, level, propagate, disabled in saved:
            item.handlers, item.level, item.propagate, item.disabled = handlers, level, propagate, disabled
        handler.close()


def run_gateway(host: str, port: int, *, path: Path, console: bool = True):
    try:
        with runtime_logging(path, console=console):
            logging.getLogger(__name__).info("Starting gateway on port %d", port)
            try:
                import uvicorn
                uvicorn.run("codex_antigravity_auth.server:app", host=host, port=port,
                            log_level="info", log_config=None, access_log=False)
            except Exception:
                logging.getLogger(__name__).exception("Gateway runtime failed")
                raise SystemExit(1) from None
    except OSError:
        raise SystemExit("Could not initialize private process logging; inspect the process-log directory") from None
