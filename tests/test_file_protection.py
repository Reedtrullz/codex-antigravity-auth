"""Private-file fixtures only; no real stores, keyring, service or network use."""
import os
from pathlib import Path
import stat
import subprocess
import sys
import time
from unittest.mock import MagicMock, Mock
from typing import Any

import pytest

from codex_antigravity_auth import secure_store
from codex_antigravity_auth.skills.anti.scripts.anti_lib import file_protection as protection
from standalone import without_installed_packages


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


def test_windows_backend_locks_after_descriptor_protection_without_initialization_write(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    events = []
    original_protect = protection.protect_descriptor
    def protect(fd, **kwargs):
        assert os.fstat(fd).st_size == 0
        original_protect(fd, **kwargs)
        events.append("protected")
    monkeypatch.setattr(protection, "protect_descriptor", protect)
    def forbidden_write(*args):
        raise AssertionError("lock acquisition must not initialize the file")
    monkeypatch.setattr(protection.os, "write", forbidden_write)
    backend = Mock(LK_LOCK=1, LK_UNLCK=2)
    def lock(fd, operation, count):
        assert events and events[0] == "protected"
        assert os.fstat(fd).st_size == 0 and count == 1
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
            child = subprocess.Popen([sys.executable, "-c", without_installed_packages(code), str(scripts), str(target), str(started), str(entered)], cwd=str(scripts),
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
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


def _native_acl_snapshot(path) -> dict[str, Any]:
    import ctypes
    from ctypes import wintypes as w
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    pointer = ctypes.c_void_p
    pp = ctypes.POINTER(pointer)
    advapi.GetNamedSecurityInfoW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, pp, pp, pp, pp, pp]
    advapi.GetNamedSecurityInfoW.restype = w.DWORD
    advapi.ConvertSidToStringSidW.argtypes = [pointer, ctypes.POINTER(w.LPWSTR)]
    advapi.ConvertSidToStringSidW.restype = w.BOOL
    advapi.GetSecurityDescriptorControl.argtypes = [pointer, ctypes.POINTER(w.WORD), ctypes.POINTER(w.DWORD)]
    advapi.GetSecurityDescriptorControl.restype = w.BOOL
    advapi.GetSecurityDescriptorDacl.argtypes = [pointer, ctypes.POINTER(w.BOOL), pp, ctypes.POINTER(w.BOOL)]
    advapi.GetSecurityDescriptorDacl.restype = w.BOOL
    advapi.GetAclInformation.argtypes = [pointer, ctypes.c_void_p, w.DWORD, w.DWORD]
    advapi.GetAclInformation.restype = w.BOOL
    advapi.GetAce.argtypes = [pointer, w.DWORD, pp]
    advapi.GetAce.restype = w.BOOL
    advapi.OpenProcessToken.argtypes = [w.HANDLE, w.DWORD, ctypes.POINTER(w.HANDLE)]
    advapi.OpenProcessToken.restype = w.BOOL
    advapi.GetTokenInformation.argtypes = [w.HANDLE, w.DWORD, ctypes.c_void_p, w.DWORD, ctypes.POINTER(w.DWORD)]
    advapi.GetTokenInformation.restype = w.BOOL
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentProcess.argtypes = []
    kernel.GetCurrentProcess.restype = w.HANDLE
    kernel.LocalFree.argtypes = [pointer]
    kernel.LocalFree.restype = pointer
    kernel.CloseHandle.argtypes = [w.HANDLE]
    kernel.CloseHandle.restype = w.BOOL

    def sid_text(value):
        text = w.LPWSTR()
        assert advapi.ConvertSidToStringSidW(value, ctypes.byref(text))
        try:
            return text.value
        finally:
            kernel.LocalFree(text)

    token = w.HANDLE()
    assert advapi.OpenProcessToken(kernel.GetCurrentProcess(), 0x0008, ctypes.byref(token))
    try:
        size = w.DWORD()
        advapi.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))
        buffer = ctypes.create_string_buffer(size.value)
        assert advapi.GetTokenInformation(token, 1, buffer, size, ctypes.byref(size))
        current = sid_text(ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0])
    finally:
        kernel.CloseHandle(token)
    owner, _group, dacl, _sacl, descriptor = (pointer(),) * 5
    code = advapi.GetNamedSecurityInfoW(str(path), 1, 0x5,
                                        ctypes.byref(owner), None, ctypes.byref(dacl), None, ctypes.byref(descriptor))
    assert not code, ctypes.WinError(code)
    try:
        control, revision = w.WORD(), w.DWORD()
        assert advapi.GetSecurityDescriptorControl(descriptor, ctypes.byref(control), ctypes.byref(revision))
        entries = []
        present, defaulted = w.BOOL(), w.BOOL()
        acl = pointer()
        assert advapi.GetSecurityDescriptorDacl(descriptor, ctypes.byref(present), ctypes.byref(acl), ctypes.byref(defaulted))
        if present.value and acl.value:
            information = (w.DWORD * 3)()
            assert advapi.GetAclInformation(acl, information, ctypes.sizeof(information), 2)
            for index in range(information[0]):
                ace = pointer()
                assert advapi.GetAce(acl, index, ctypes.byref(ace))
                assert ace.value is not None
                ace_address: int = ace.value
                header = (ctypes.c_ubyte * 4).from_address(ace_address)
                mask = w.DWORD.from_address(ace_address + 4).value
                entries.append({
                    "Sid": sid_text(pointer(ace_address + 8)),
                    "Rights": mask,
                    "Allow": header[0] == 0,
                    "Inherited": bool(header[1] & 0x10),
                })
        return {"Protected": bool(control.value & 0x1000), "Owner": sid_text(owner),
                "Current": current, "Entries": entries}
    finally:
        kernel.LocalFree(descriptor)


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
def test_native_windows_directory_protection_shields_existing_child_acl(tmp_path):
    directory = tmp_path / "parent"
    directory.mkdir()
    child = directory / "unrelated-fixture"
    child.write_bytes(b"synthetic unrelated content")
    protection.ensure_private_directory(directory, enforce_existing=True)
    child_acl = _native_acl_snapshot(child)
    assert child.read_bytes() == b"synthetic unrelated content"
    assert child_acl["Protected"] and child_acl["Owner"] == child_acl["Current"]
    assert len(child_acl["Entries"]) == 1
    entry = child_acl["Entries"][0]
    assert entry["Sid"] == child_acl["Current"] and entry["Rights"] == 0x1F01FF
    assert entry["Allow"] and not entry["Inherited"]
    assert _native_acl_snapshot(directory)["Protected"]


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


def test_windows_directory_protection_uses_exclusive_handle_for_the_directory(monkeypatch):
    from codex_antigravity_auth.skills.anti.scripts.anti_lib.windows_file_security import WindowsFileSecurity
    monkeypatch.setattr(os, "scandir", lambda _path: MagicMock(__enter__=MagicMock(return_value=iter([])), __exit__=Mock(return_value=False)))
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
    monkeypatch.setattr(os, "scandir", lambda _path: MagicMock(__enter__=MagicMock(return_value=iter([])), __exit__=Mock(return_value=False)))
    security._handle = Mock(side_effect=[busy, 123])
    security._protect = Mock()
    monkeypatch.setattr(windows.time, "sleep", Mock())
    security.protect_directory(Path("fixture-directory"))
    assert security.kernel.CreateFileW.call_count == 2
    security._protect.assert_called_once_with(123, directory=True)
    security.kernel.CloseHandle.assert_called_once_with(123)


def test_windows_directory_protection_shields_existing_file_children(tmp_path, monkeypatch):
    from unittest.mock import call
    from codex_antigravity_auth.skills.anti.scripts.anti_lib.windows_file_security import WindowsFileSecurity
    security = WindowsFileSecurity.__new__(WindowsFileSecurity)
    security.kernel = Mock()
    security.kernel.CreateFileW.return_value = 123
    security._handle = lambda result: result
    security._protect = Mock()
    file_entry = Mock(path=str(tmp_path / "state.json"))
    file_entry.stat = lambda follow_symlinks=False: Mock(st_file_attributes=0x20)
    directory_entry = Mock(path=str(tmp_path / "nested"))
    directory_entry.stat = lambda follow_symlinks=False: Mock(st_file_attributes=0x10)
    reparse_entry = Mock(path=str(tmp_path / "link"))
    reparse_entry.stat = lambda follow_symlinks=False: Mock(st_file_attributes=0x400)
    scans = [[file_entry, directory_entry, reparse_entry], []]
    monkeypatch.setattr(os, "scandir", lambda _path: MagicMock(__enter__=MagicMock(return_value=iter(scans.pop(0))), __exit__=Mock(return_value=False)))
    security.protect_directory(tmp_path)
    assert security.kernel.CreateFileW.call_args_list[0].args == (str(tmp_path / "state.json"), 0xE0080, 7, None, 3, 0x02200000, None)
    assert security._protect.call_args_list == [call(123), call(123, directory=True), call(123, directory=True)]
    assert security.kernel.CreateFileW.call_count == 3
    assert security.kernel.CloseHandle.call_count == 3


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
