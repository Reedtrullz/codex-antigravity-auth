import ctypes
import json
import os
from pathlib import Path
import sys
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "codex_antigravity_auth" / "skills" / "anti" / "scripts"))
from anti_lib.file_protection import ensure_private_directory, protect_existing_file, file_lock

def acl(path):
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    from ctypes import wintypes as w
    pp = ctypes.POINTER(ctypes.c_void_p)
    advapi.GetNamedSecurityInfoW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, pp, pp, pp, pp, pp]
    advapi.GetNamedSecurityInfoW.restype = w.DWORD
    advapi.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(w.LPWSTR)]
    advapi.ConvertSidToStringSidW.restype = w.BOOL
    advapi.GetSecurityDescriptorControl.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ushort), ctypes.POINTER(ctypes.c_ulong)]
    advapi.GetSecurityDescriptorControl.restype = w.BOOL
    advapi.GetAclInformation.argtypes = [ctypes.c_void_p, ctypes.c_void_p, w.DWORD, w.DWORD]
    advapi.GetAclInformation.restype = w.BOOL
    advapi.GetAce.argtypes = [ctypes.c_void_p, w.DWORD, ctypes.POINTER(ctypes.c_void_p)]
    advapi.GetAce.restype = w.BOOL
    owner = ctypes.c_void_p(); dacl = ctypes.c_void_p(); sec = ctypes.c_void_p()
    code = advapi.GetNamedSecurityInfoW(str(path), 1, 0x5, ctypes.byref(owner), None, ctypes.byref(dacl), None, ctypes.byref(sec))
    if code: raise ctypes.WinError(code)
    sid = w.LPWSTR()
    advapi.ConvertSidToStringSidW(owner, ctypes.byref(sid))
    control = ctypes.c_ushort(); rev = ctypes.c_ulong()
    advapi.GetSecurityDescriptorControl(sec, ctypes.byref(control), ctypes.byref(rev))
    entries = []
    if dacl:
        info = (ctypes.c_ulong * 3)()
        advapi.GetAclInformation(dacl, info, ctypes.sizeof(info), 2)
        for i in range(info[0]):
            ace = ctypes.c_void_p()
            advapi.GetAce(dacl, i, ctypes.byref(ace))
            hdr = (ctypes.c_ubyte * 4).from_address(ace.value)
            mask = ctypes.c_ulong.from_address(ace.value + 4).value
            s = w.LPWSTR()
            advapi.ConvertSidToStringSidW(ctypes.c_void_p(ace.value + 8), ctypes.byref(s))
            entries.append({"type": hdr[0], "mask": hex(mask), "sid": s.value, "inherited": bool(hdr[1] & 0x10)})
    return {"protected": bool(control.value & 0x1000), "owner": sid.value, "entries": entries}

def test_ci_windows_probe2(tmp_path):
    if os.name != "nt":
        pytest.skip("windows-only production flow probe")
    report = {}
    try:
        target = tmp_path / "flow"
        target.mkdir()
        data = target / "accounts.json"
        data.write_bytes(b"fixture")
        data.chmod(0o600)
        report["pre_dir"] = acl(target)
        report["pre_file"] = acl(data)
        ensure_private_directory(target)
        report["post_ensure_dir"] = acl(target)
        protect_existing_file(data)
        report["post_protect_file"] = acl(data)
        fd = os.open(data, os.O_RDONLY)
        os.close(fd)
        report["os_open_after"] = "OK"
        data.read_bytes()
        report["pathlib_read_after"] = "OK"
    except BaseException as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
    try:
        target2 = tmp_path / "lockflow"
        target2.mkdir()
        ld = target2 / "accounts.json"
        ld.write_bytes(b"fixture")
        ld.chmod(0o600)
        with file_lock(ld):
            report["lock"] = "OK"
        report["lock_read"] = "OK"
        ld.read_bytes()
    except BaseException as exc:
        report["lock_error"] = f"{type(exc).__name__}: {exc}"
    try:
        target3 = tmp_path / "moveflow"
        target3.mkdir()
        src = target3 / "anti"
        src.mkdir()
        (src / "a.txt").write_bytes(b"x")
        bak = target3 / "backups"
        bak.mkdir()
        report["move_pre"] = acl(target3)
        src.rename(bak / "anti.backup")
        report["move"] = "OK"
    except BaseException as exc:
        report["move_error"] = f"{type(exc).__name__}: {exc}"
    pytest.fail("CI_PROBE2: " + json.dumps(report, sort_keys=True))
