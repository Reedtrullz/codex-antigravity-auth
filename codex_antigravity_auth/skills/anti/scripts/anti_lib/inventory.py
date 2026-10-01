"""Bounded, literal Git inventory and no-follow repository file capture."""
from __future__ import annotations

import os
from pathlib import Path
import stat
import subprocess
import threading

MAX_PATH_BYTES = 4 * 1024 * 1024
MAX_PATHS = 20_000
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_SOURCE_BYTES = 16 * 1024 * 1024
GIT_TIMEOUT = 10
MANIFEST_NAMES = {'pyproject.toml', 'setup.py', 'setup.cfg', 'package.json', 'Cargo.toml',
                  'go.mod', 'Gemfile', 'pom.xml', 'build.gradle'}


class InventoryError(ValueError):
    pass


def relative_path(root: Path, raw: str) -> str:
    path = Path(raw)
    if not raw or '\0' in raw or '..' in path.parts:
        raise InventoryError('Review paths must be literal paths inside the repository.')
    path = path if path.is_absolute() else root / path
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        raise InventoryError('Review paths must stay inside the repository.') from None


def path_kind(root: Path, rel: str) -> str:
    path = root
    try:
        info = path.lstat()
        for part in Path(rel).parts:
            path /= part
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0):
                return 'symlink'
        if stat.S_ISREG(info.st_mode): return 'file'
        if stat.S_ISDIR(info.st_mode): return 'directory'
        return 'special_file'
    except FileNotFoundError:
        return 'missing'
    except OSError:
        return 'unreadable'


def git_paths(root: Path, options: list[str], roots: list[str]) -> list[str]:
    """Never accept a truncated inventory as a complete list of candidates."""
    process = subprocess.Popen(['git', 'ls-files', '-z', *options, '--', *(':(literal)' + root for root in roots)],
                               cwd=root, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    expired = threading.Event()
    def timeout():
        expired.set()
        if process.poll() is None:
            try: process.kill()
            except ProcessLookupError: pass
    timer = threading.Timer(GIT_TIMEOUT, timeout)
    timer.daemon = True
    timer.start()
    try:
        raw = process.stdout.read(MAX_PATH_BYTES + 1)
        if len(raw) > MAX_PATH_BYTES:
            raise InventoryError('Git inventory exceeds its byte limit; narrow --review-root.')
        code = process.wait()
        if expired.is_set(): raise InventoryError('Git inventory timed out; narrow --review-root.')
        if code: raise InventoryError('Git inventory failed; no complete file selection is available.')
        if raw and not raw.endswith(b'\0'):
            raise InventoryError('Git returned an incomplete inventory.')
        if raw.count(b'\0') > MAX_PATHS:
            raise InventoryError('Git inventory exceeds its path limit; narrow --review-root.')
        try:
            return sorted(set(part.decode('utf-8') for part in raw.split(b'\0') if part))
        except UnicodeError:
            raise InventoryError('Git inventory includes a non-UTF-8 path.') from None
    finally:
        timer.cancel()
        if process.poll() is None:
            process.kill()
        process.wait()
        process.stdout.close()
        timer.join()


def within(path: str, selected: str) -> bool:
    return selected == '.' or path == selected or path.startswith(selected.rstrip('/') + '/')


def collect(root: Path, *, roots=(), exclusions=(), include_untracked=False, selected=(), excluded_path):
    roots = list(dict.fromkeys(relative_path(root, raw) for raw in roots)) or ['.']
    for rel in roots:
        if path_kind(root, rel) != 'directory':
            raise InventoryError('Each --review-root must be an existing directory without symlinks.')
    exclusions = list(dict.fromkeys(relative_path(root, raw) for raw in exclusions))
    selected = set(relative_path(root, raw) for raw in selected)
    tracked = git_paths(root, ['--cached'], roots)
    untracked = git_paths(root, ['--others', '--exclude-standard'], roots)
    ignored = git_paths(root, ['--others', '--ignored', '--exclude-standard', '--directory', '--no-empty-directory'], roots)
    all_paths = sorted(set(tracked) | set(untracked) | set(ignored))
    if len(all_paths) > MAX_PATHS:
        raise InventoryError('Combined inventory exceeds its path limit; narrow --review-root.')
    candidates, excluded = [], []
    ignored_set, untracked_set = set(ignored), set(untracked)
    for raw in all_paths:
        rel = relative_path(root, raw.rstrip('/'))
        reason = None
        if selected and rel not in selected: reason = 'not_selected'
        elif any(within(rel, item) for item in exclusions): reason = 'user_excluded'
        elif raw in ignored_set: reason = 'git_ignored'
        elif excluded_path(rel): reason = 'sensitive_cache_or_binary'
        elif rel in untracked_set and not include_untracked: reason = 'untracked_not_requested'
        elif path_kind(root, rel) not in {'file', 'missing', 'unreadable'}: reason = path_kind(root, rel)
        if reason:
            excluded.append({'path':raw, 'reason':reason})
        else:
            candidates.append(rel)
    unknown = selected - set(all_paths)
    if unknown:
        raise InventoryError('Explicit files are absent from the selected repository inventory: ' + ', '.join(sorted(unknown)))
    packages = sorted({str(Path(path).parent) for path in candidates
                       if Path(path).name in MANIFEST_NAMES or Path(path).suffix == '.csproj'})
    return candidates, {'scope':'repository', 'roots':roots, 'exclusions':exclusions,
                        'include_untracked':include_untracked, 'excluded':excluded,
                        'tracked_count':len(tracked), 'untracked_count':len(untracked),
                        'package_roots':packages, 'inventory_complete':True}


def read_file(root: Path, rel: str, budget: int):
    """Read only regular files, with bounded bytes and path identity rechecks."""
    kind = path_kind(root, rel)
    if kind != 'file': return None, 0, kind
    path = root / rel
    try:
        before = path.lstat()
        if before.st_size > MAX_FILE_BYTES: return None, before.st_size, 'file_byte_limit'
        if before.st_size > budget: return None, before.st_size, 'total_byte_limit'
        flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
        with os.fdopen(_open_file(root, rel, flags), 'rb') as stream:
            opened = os.fstat(stream.fileno())
            if not stat.S_ISREG(opened.st_mode) or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
                return None, before.st_size, 'changed_during_read'
            if path_kind(root, rel) != 'file': return None, before.st_size, 'changed_during_read'
            raw = stream.read(min(MAX_FILE_BYTES, budget) + 1)
            after = os.fstat(stream.fileno())
        final = path.lstat()
        identity = lambda info: (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        if identity(before) != identity(after) or identity(after) != identity(final):
            return None, before.st_size, 'changed_during_read'
        if len(raw) > min(MAX_FILE_BYTES, budget): return None, len(raw), 'source_byte_limit'
        return raw, len(raw), None
    except OSError:
        return None, 0, 'unreadable'


def _open_file(root: Path, rel: str, flags: int):
    # POSIX anchors each parent directory without following links; platforms
    # without dir_fd retain the surrounding path/descriptor identity checks.
    if os.open not in os.supports_dir_fd or not hasattr(os, 'O_NOFOLLOW'):
        return os.open(root / rel, flags)
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptor = os.open(root, directory_flags)
    try:
        parts = Path(rel).parts
        for part in parts[:-1]:
            next_descriptor = os.open(part, directory_flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
        return os.open(parts[-1], flags, dir_fd=descriptor)
    finally:
        os.close(descriptor)
