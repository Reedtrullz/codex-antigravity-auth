"""Private-file fixtures only; no real stores, keyring, service or network use."""
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import time
from unittest.mock import Mock

import pytest

from codex_antigravity_auth import secure_store
from codex_antigravity_auth.skills.anti.scripts.anti_lib import file_protection as protection
from test_support.standalone import without_installed_packages


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "directory", "fifo"])
def test_unsafe_lock_targets_are_not_chmodded_or_written(tmp_path, kind):
    target = tmp_path / "data.json"
    lock = tmp_path / ".data.json.lock"
    referent = tmp_path / "referent"
    referent.write_bytes(b"synthetic original")
    os.chmod(referent, 0o644)
    if kind == "symlink":
        try:
            lock.symlink_to(referent)
        except OSError:
            pytest.skip("fixture symlink creation unavailable")
    elif kind == "hardlink":
        os.link(referent, lock)
    elif kind == "directory":
        lock.mkdir()
    else:
        if not hasattr(os, "mkfifo"):
            pytest.skip("POSIX FIFO fixture")
        os.mkfifo(lock, 0o640)
    before = referent.stat().st_mode
    started = time.monotonic()
    with pytest.raises((ValueError, OSError)):
        with secure_store.file_lock(target):
            pytest.fail("unsafe lock was accepted")
    assert time.monotonic() - started < 1
    assert referent.read_bytes() == b"synthetic original" and referent.stat().st_mode == before


@pytest.mark.skipif(os.name == "nt", reason="POSIX open-descriptor substitution fixture")
def test_opened_descriptor_must_match_directory_entry_before_chmod(tmp_path, monkeypatch):
    lock = tmp_path / ".data.json.lock"
    different = tmp_path / "different"
    for path in (lock, different):
        path.write_bytes(b"synthetic original")
        os.chmod(path, 0o644)
    before = {path: path.stat().st_mode for path in (lock, different)}
    original_open = os.open
    def substitute(path, flags, *args, **kwargs):
        return original_open(different if Path(path) == lock else path, flags, *args, **kwargs)
    monkeypatch.setattr(protection.os, "open", substitute)
    with pytest.raises(ValueError, match="changed"):
        with secure_store.file_lock(tmp_path / "data.json"):
            pass
    for path in (lock, different):
        assert path.read_bytes() == b"synthetic original" and path.stat().st_mode == before[path]


@pytest.mark.skipif(os.name == "nt", reason="POSIX ownership check")
def test_wrong_owner_descriptor_is_never_modified(tmp_path, monkeypatch):
    path = tmp_path / "fixture"
    path.write_bytes(b"synthetic original")
    os.chmod(path, 0o644)
    uid = os.geteuid()
    monkeypatch.setattr(protection.os, "geteuid", lambda: uid + 1)
    with pytest.raises(ValueError, match="another user"):
        protection.protect_existing_file(path)
    assert path.read_bytes() == b"synthetic original" and stat.S_IMODE(path.stat().st_mode) == 0o644


def test_unsupported_process_lock_refuses_before_creating_files(tmp_path, monkeypatch):
    monkeypatch.setattr(secure_store, "fcntl", None)
    monkeypatch.setattr(secure_store, "msvcrt", None)
    parent = tmp_path / "not-created"
    with pytest.raises(RuntimeError, match="No supported process lock"):
        with secure_store.file_lock(parent / "state.json"):
            pass
    assert not parent.exists()


def test_private_new_directories_files_and_reentrant_lock(tmp_path):
    path = tmp_path / "new" / "nested" / "state.json"
    secure_store.SecureStore().atomic_write_text(path, "synthetic content")
    assert path.read_text() == "synthetic content"
    with secure_store.file_lock(path):
        with secure_store.file_lock(path):
            assert path.read_text() == "synthetic content"
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(path.parent.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(path.with_name(".state.json.lock").stat().st_mode) == 0o600


def test_windows_acl_failure_refuses_before_secret_write(tmp_path, monkeypatch):
    target = tmp_path / "fixture.json"
    target.write_bytes(b"synthetic original")
    fake = Mock()
    fake.protect_descriptor.side_effect = OSError("synthetic ACL refusal")
    fake.open_lock_file.side_effect = lambda path: os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    monkeypatch.setattr(protection, "WINDOWS", True)
    monkeypatch.setattr(protection, "_windows_security", lambda: fake)
    with pytest.raises(OSError, match="ACL refusal"):
        secure_store.SecureStore().atomic_write_text(target, "synthetic replacement")
    assert target.read_bytes() == b"synthetic original"
    assert not list(tmp_path.glob("*.tmp"))
    fake.protect_descriptor.assert_called_once()


def test_windows_backend_sentinel_write_only_after_descriptor_protection(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    events = []
    original_protect = protection.protect_descriptor
    def protect(fd, **kwargs):
        assert os.fstat(fd).st_size == 0
        original_protect(fd, **kwargs)
        events.append("protected")
    monkeypatch.setattr(protection, "protect_descriptor", protect)
    backend = Mock(LK_LOCK=1, LK_UNLCK=2)
    def lock(fd, operation, count):
        assert events and events[0] == "protected"
        assert os.fstat(fd).st_size == 1 and count == 1
        events.append(operation)
    backend.locking.side_effect = lock
    with protection.file_lock(path, posix_backend=None, windows_backend=backend):
        events.append("entered")
    assert events == ["protected", 1, "entered", 2]


def test_packaged_lock_serializes_standalone_child_process(tmp_path):
    target, started, entered = (tmp_path / name for name in ("state.json", "started", "entered"))
    scripts = Path(secure_store.__file__).parent / "skills/anti/scripts"
    code = '''import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from anti_lib.persistence import file_lock
Path(sys.argv[3]).write_text('ready')
with file_lock(Path(sys.argv[2])):
    Path(sys.argv[4]).write_text('entered')
'''
    child = None
    try:
        with secure_store.file_lock(target):
            child = subprocess.Popen(
                [sys.executable, "-c", without_installed_packages(code), str(scripts), str(target), str(started), str(entered)],
                cwd=tmp_path,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            deadline = time.monotonic() + 3
            while not started.exists() and child.poll() is None and time.monotonic() < deadline:
                time.sleep(0.01)
            assert started.exists()
            time.sleep(0.1)
            assert not entered.exists() and child.poll() is None
        _out, error = child.communicate(timeout=5)
        assert child.returncode == 0, error
        assert entered.read_text() == "entered"
    finally:
        if child is not None and child.poll() is None:
            child.kill()
            child.communicate(timeout=5)


def _native_acl_snapshot(path):
    import ctypes
    from ctypes import wintypes as w
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetSystemDirectoryW.argtypes = [w.LPWSTR, w.UINT]
    kernel.GetSystemDirectoryW.restype = w.UINT
    buffer = ctypes.create_unicode_buffer(32768)
    size = kernel.GetSystemDirectoryW(buffer, len(buffer))
    assert 0 < size < len(buffer)
    system_directory = Path(buffer.value)
    shell = system_directory / "WindowsPowerShell/v1.0/powershell.exe"
    script = '''$acl=Get-Acl -LiteralPath $env:ANTIGRAVITY_ACL_FIXTURE;
$sid=[System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value;
$entries=@($acl.GetAccessRules($true,$true,[System.Security.Principal.SecurityIdentifier]));
@{Protected=$acl.AreAccessRulesProtected;Owner=$acl.GetOwner([System.Security.Principal.SecurityIdentifier]).Value;Current=$sid;Entries=@($entries | ForEach-Object {@{Sid=$_.IdentityReference.Value;Rights=[int]$_.FileSystemRights;Allow=($_.AccessControlType -eq 'Allow');Inherited=$_.IsInherited}})} | ConvertTo-Json -Depth 4 -Compress
'''
    env = {**os.environ, "SystemRoot": str(system_directory.parent), "ANTIGRAVITY_ACL_FIXTURE": str(path)}
    result = subprocess.run([str(shell), "-NoProfile", "-NonInteractive", "-Command", script], env=env,
                            capture_output=True, text=True, check=True, timeout=15)
    return json.loads(result.stdout)


@pytest.mark.skipif(os.name != "nt", reason="native Windows ACL inspection")
def test_native_windows_file_dacl_is_current_user_only_and_protected(tmp_path):
    path = tmp_path / "private" / "fixture.json"
    secure_store.SecureStore().atomic_write_text(path, "synthetic private payload")
    acl = _native_acl_snapshot(path)
    assert acl["Protected"] and acl["Owner"] == acl["Current"]
    assert len(acl["Entries"]) == 1
    entry = acl["Entries"][0]
    assert entry["Sid"] == acl["Current"] and entry["Rights"] == 0x1F01FF
    assert entry["Allow"] and not entry["Inherited"]


@pytest.mark.skipif(os.name != "nt", reason="native Windows child ACL inspection")
def test_native_windows_directory_protection_preserves_unrelated_child_acl(tmp_path):
    directory = tmp_path / "parent"
    directory.mkdir()
    child = directory / "unrelated-fixture"
    child.write_bytes(b"synthetic unrelated content")
    before = _native_acl_snapshot(child)
    protection.ensure_private_directory(directory, enforce_existing=True)
    assert _native_acl_snapshot(child) == before
    assert child.read_bytes() == b"synthetic unrelated content"


@pytest.mark.parametrize("failure", ["foreign_owner", "null_dacl", "verification"])
def test_windows_acl_control_refuses_unsafe_owner_null_acl_or_unverified_result(monkeypatch, failure):
    import ctypes
    from ctypes import wintypes as w
    from codex_antigravity_auth.skills.anti.scripts.anti_lib.windows_file_security import WindowsFileSecurity
    security = WindowsFileSecurity.__new__(WindowsFileSecurity)
    security.kernel, security.advapi = Mock(), Mock()
    security.user_sid, security.default_owner_sid = "fixture-user", "fixture-owner-group"
    security._check_object = Mock()
    security._descriptor = Mock(return_value=(ctypes.c_void_p(1), ctypes.c_void_p(2), ctypes.c_void_p(3)))
    security._sid_text = lambda sid: "fixture-foreign" if failure == "foreign_owner" and sid.value == 1 else "fixture-user"
    def descriptor(_sddl, _revision, pointer, _size):
        ctypes.cast(pointer, ctypes.POINTER(ctypes.c_void_p))[0] = 4
        return 1
    def dacl(_descriptor, present, acl, _defaulted):
        ctypes.cast(present, ctypes.POINTER(w.BOOL))[0] = failure != "null_dacl"
        ctypes.cast(acl, ctypes.POINTER(ctypes.c_void_p))[0] = 5 if failure != "null_dacl" else None
        return 1
    def owner(_descriptor, pointer, _defaulted):
        ctypes.cast(pointer, ctypes.POINTER(ctypes.c_void_p))[0] = 6
        return 1
    security.advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.side_effect = descriptor
    security.advapi.GetSecurityDescriptorDacl.side_effect = dacl
    security.advapi.GetSecurityDescriptorOwner.side_effect = owner
    security.advapi.SetSecurityInfo.return_value = 0
    security.verify = Mock(side_effect=OSError("synthetic verification refusal"))
    with pytest.raises(OSError):
        security._protect(123)
    if failure in {"foreign_owner", "null_dacl"}:
        security.advapi.SetSecurityInfo.assert_not_called()
    else:
        security.advapi.SetSecurityInfo.assert_called_once()
        security.verify.assert_called_once_with(123, directory=False)
    assert security.kernel.LocalFree.call_count == (1 if failure == "foreign_owner" else 2)


def test_windows_directory_protection_uses_exclusive_handle_to_prevent_propagation():
    from codex_antigravity_auth.skills.anti.scripts.anti_lib.windows_file_security import WindowsFileSecurity
    security = WindowsFileSecurity.__new__(WindowsFileSecurity)
    security.kernel = Mock()
    security.kernel.CreateFileW.return_value = 123
    security._protect = Mock()
    security.protect_directory(Path("fixture-directory"))
    assert security.kernel.CreateFileW.call_args.args[2] == 0
    security._protect.assert_called_once_with(123, directory=True)
    security.kernel.CloseHandle.assert_called_once_with(123)


def test_windows_directory_protection_retries_transient_sharing_conflict(monkeypatch):
    from codex_antigravity_auth.skills.anti.scripts.anti_lib import windows_file_security as windows
    security = windows.WindowsFileSecurity.__new__(windows.WindowsFileSecurity)
    security.kernel = Mock()
    busy = OSError("synthetic sharing conflict")
    busy.winerror = 32
    security._handle = Mock(side_effect=[busy, 123])
    security._protect = Mock()
    monkeypatch.setattr(windows.time, "sleep", Mock())
    security.protect_directory(Path("fixture-directory"))
    assert security.kernel.CreateFileW.call_count == 2
    security._protect.assert_called_once_with(123, directory=True)
    security.kernel.CloseHandle.assert_called_once_with(123)


def test_windows_native_identity_mismatch_refuses_before_acl_changes(monkeypatch):
    from types import SimpleNamespace
    from codex_antigravity_auth.skills.anti.scripts.anti_lib.windows_file_security import WindowsFileSecurity
    security = WindowsFileSecurity.__new__(WindowsFileSecurity)
    security.kernel = Mock()
    security.advapi = Mock()
    security.kernel.ReOpenFile.return_value = 123
    security.kernel.CreateFileW.return_value = 456
    security._check_object = Mock(side_effect=[(7, b"a" * 16), (7, b"b" * 16)])
    monkeypatch.setitem(sys.modules, "msvcrt", SimpleNamespace(get_osfhandle=lambda fd: 789))
    with pytest.raises(OSError, match="changed"):
        security.verify_descriptor_path(9, Path("synthetic-lock"))
    security.advapi.SetSecurityInfo.assert_not_called()
    assert security.kernel.CloseHandle.call_count == 2
