"""Owner-only Windows DACLs, applied and verified on the opened object.

No best-effort chmod fallback: unsupported ACL/handle operations raise OSError.
The caller is responsible for its same-user directory ownership boundary.
"""
from __future__ import annotations

import ctypes
import time
from ctypes import wintypes as w


class WindowsFileSecurity:
    def __init__(self):
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.advapi = ctypes.WinDLL("advapi32", use_last_error=True)
        pointer = ctypes.c_void_p
        pp = ctypes.POINTER(pointer)
        self._bind(self.kernel, "GetCurrentProcess", [], w.HANDLE)
        self._bind(self.kernel, "CloseHandle", [w.HANDLE], w.BOOL)
        self._bind(self.kernel, "LocalFree", [pointer], pointer)
        self._bind(self.kernel, "ReOpenFile", [w.HANDLE, w.DWORD, w.DWORD, w.DWORD], w.HANDLE)
        self._bind(self.kernel, "CreateFileW", [w.LPCWSTR, w.DWORD, w.DWORD, pointer, w.DWORD, w.DWORD, w.HANDLE], w.HANDLE)
        self._bind(self.kernel, "GetFileType", [w.HANDLE], w.DWORD)
        self._bind(self.kernel, "GetFileInformationByHandle", [w.HANDLE, pointer], w.BOOL)
        self._bind(self.kernel, "GetFileInformationByHandleEx", [w.HANDLE, w.DWORD, pointer, w.DWORD], w.BOOL)
        self._bind(self.advapi, "OpenProcessToken", [w.HANDLE, w.DWORD, ctypes.POINTER(w.HANDLE)], w.BOOL)
        self._bind(self.advapi, "GetTokenInformation", [w.HANDLE, w.DWORD, pointer, w.DWORD, ctypes.POINTER(w.DWORD)], w.BOOL)
        self._bind(self.advapi, "ConvertSidToStringSidW", [pointer, ctypes.POINTER(w.LPWSTR)], w.BOOL)
        self._bind(self.advapi, "GetSecurityInfo", [w.HANDLE, w.DWORD, w.DWORD, pp, pp, pp, pp, pp], w.DWORD)
        self._bind(self.advapi, "SetSecurityInfo", [w.HANDLE, w.DWORD, w.DWORD, pointer, pointer, pointer, pointer], w.DWORD)
        self._bind(self.advapi, "SetFileSecurityW", [w.LPCWSTR, w.DWORD, pointer], w.BOOL)
        self._bind(self.advapi, "ConvertStringSecurityDescriptorToSecurityDescriptorW", [w.LPCWSTR, w.DWORD, pp, ctypes.POINTER(w.DWORD)], w.BOOL)
        self._bind(self.advapi, "GetSecurityDescriptorDacl", [pointer, ctypes.POINTER(w.BOOL), pp, ctypes.POINTER(w.BOOL)], w.BOOL)
        self._bind(self.advapi, "GetSecurityDescriptorOwner", [pointer, pp, ctypes.POINTER(w.BOOL)], w.BOOL)
        self._bind(self.advapi, "GetSecurityDescriptorControl", [pointer, ctypes.POINTER(w.WORD), ctypes.POINTER(w.DWORD)], w.BOOL)
        self._bind(self.advapi, "GetAclInformation", [pointer, pointer, w.DWORD, w.DWORD], w.BOOL)
        self._bind(self.advapi, "GetAce", [pointer, w.DWORD, pp], w.BOOL)
        self.user_sid = self._token_sid(1)
        self.default_owner_sid = self._token_sid(4)

    @staticmethod
    def _bind(library, name, arguments, result):
        function = getattr(library, name)
        function.argtypes, function.restype = arguments, result

    @staticmethod
    def _ok(result):
        if not result:
            raise ctypes.WinError(ctypes.get_last_error())

    @staticmethod
    def _handle(result):
        if result in (None, 0, ctypes.c_void_p(-1).value):
            raise ctypes.WinError(ctypes.get_last_error())
        return result

    def _sid_text(self, sid):
        result = w.LPWSTR()
        self._ok(self.advapi.ConvertSidToStringSidW(sid, ctypes.byref(result)))
        try:
            return result.value
        finally:
            self.kernel.LocalFree(result)

    def _token_sid(self, info_class):
        token = w.HANDLE()
        self._ok(self.advapi.OpenProcessToken(self.kernel.GetCurrentProcess(), 0x0008, ctypes.byref(token)))
        try:
            size = w.DWORD()
            self.advapi.GetTokenInformation(token, info_class, None, 0, ctypes.byref(size))  # TokenUser / TokenOwner size query
            if not size.value:
                raise ctypes.WinError(ctypes.get_last_error())
            buffer = ctypes.create_string_buffer(size.value)
            self._ok(self.advapi.GetTokenInformation(token, info_class, buffer, size, ctypes.byref(size)))
            # TOKEN_USER and TOKEN_OWNER both begin with PSID.
            return self._sid_text(ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0])
        finally:
            self.kernel.CloseHandle(token)

    def _check_object(self, handle, directory):
        if self.kernel.GetFileType(handle) != 1:  # FILE_TYPE_DISK
            raise OSError("Private storage requires a disk file or directory")
        info = (w.DWORD * 13)()  # BY_HANDLE_FILE_INFORMATION consists of 13 DWORDs.
        self._ok(self.kernel.GetFileInformationByHandle(handle, info))
        attributes = info[0]
        if attributes & 0x400 or bool(attributes & 0x10) != directory:
            raise OSError("Refusing a reparse point or unexpected file type")
        if not directory and info[10] != 1:
            raise OSError("Refusing a multiply-linked private file")
        class FileIdentity(ctypes.Structure):
            _fields_ = [("volume", ctypes.c_ulonglong), ("identifier", ctypes.c_ubyte * 16)]
        identity = FileIdentity()
        self._ok(self.kernel.GetFileInformationByHandleEx(handle, 18, ctypes.byref(identity), ctypes.sizeof(identity)))
        return identity.volume, bytes(identity.identifier)

    def _descriptor(self, handle):
        owner = ctypes.c_void_p()
        dacl = ctypes.c_void_p()
        descriptor = ctypes.c_void_p()
        code = self.advapi.GetSecurityInfo(handle, 1, 0x5, ctypes.byref(owner), None,
                                          ctypes.byref(dacl), None, ctypes.byref(descriptor))
        if code:
            raise ctypes.WinError(code)
        return owner, dacl, descriptor

    def verify(self, handle, *, directory=False):
        self._check_object(handle, directory)
        owner, dacl, descriptor = self._descriptor(handle)
        try:
            if not owner or self._sid_text(owner) != self.user_sid:
                raise OSError("Private storage object is not owned by the current user")
            control, revision = w.WORD(), w.DWORD()
            self._ok(self.advapi.GetSecurityDescriptorControl(descriptor, ctypes.byref(control), ctypes.byref(revision)))
            if not dacl or not (control.value & 0x1000):  # SE_DACL_PROTECTED
                raise OSError("Owner-only protected DACL was not established")
            information = (w.DWORD * 3)()  # ACL_SIZE_INFORMATION
            self._ok(self.advapi.GetAclInformation(dacl, information, ctypes.sizeof(information), 2))
            if information[0] != 1:
                raise OSError("Private DACL must contain exactly one owner ACE")
            ace = ctypes.c_void_p()
            self._ok(self.advapi.GetAce(dacl, 0, ctypes.byref(ace)))
            header = (ctypes.c_ubyte * 4).from_address(ace.value)
            mask = w.DWORD.from_address(ace.value + 4).value
            expected_flags = 0x03 if directory else 0  # OI|CI for directories; no file inheritance.
            if (header[0] != 0 or header[1] != expected_flags or mask != 0x1F01FF
                    or self._sid_text(ctypes.c_void_p(ace.value + 8)) != self.user_sid):
                raise OSError("Private DACL grants unexpected access")
        finally:
            self.kernel.LocalFree(descriptor)

    def _protect(self, handle, *, directory=False, path=None):
        self._check_object(handle, directory)
        owner, _old_dacl, old_descriptor = self._descriptor(handle)
        try:
            if not owner or self._sid_text(owner) not in {self.user_sid, self.default_owner_sid}:
                raise OSError("Refusing to change a storage object owned by another user")
        finally:
            self.kernel.LocalFree(old_descriptor)
        descriptor = ctypes.c_void_p()
        # Directories grant the owner access inherited by newly created children.
        # SetFileSecurityW is intentionally path-based: unlike SetSecurityInfo,
        # it does not propagate a changed directory DACL to existing children.
        flags = "OICI" if directory else ""
        sddl = f"O:{self.user_sid}D:P(A;{flags};FA;;;{self.user_sid})"
        self._ok(self.advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, ctypes.byref(descriptor), None))
        try:
            present, defaulted = w.BOOL(), w.BOOL()
            dacl = ctypes.c_void_p()
            self._ok(self.advapi.GetSecurityDescriptorDacl(descriptor, ctypes.byref(present), ctypes.byref(dacl), ctypes.byref(defaulted)))
            if not present.value or not dacl:
                raise OSError("Refusing to apply a null DACL")
            new_owner = ctypes.c_void_p()
            self._ok(self.advapi.GetSecurityDescriptorOwner(descriptor, ctypes.byref(new_owner), ctypes.byref(defaulted)))
            if not new_owner or self._sid_text(new_owner) != self.user_sid:
                raise OSError("Cannot establish the current user as storage owner")
            if directory:
                if path is None:
                    raise ValueError("A directory path is required for non-propagating ACL protection")
                self._ok(self.advapi.SetFileSecurityW(str(path), 0x80000005, descriptor))
            else:
                code = self.advapi.SetSecurityInfo(handle, 1, 0x80000005, new_owner, None, dacl, None)
                if code:
                    raise ctypes.WinError(code)
        finally:
            self.kernel.LocalFree(descriptor)
        self.verify(handle, directory=directory)

    def open_lock_file(self, path):
        import os
        import msvcrt
        handle = self._handle(self.kernel.CreateFileW(str(path), 0xC00E0000, 7, None, 4, 0x00200080, None))
        try:
            self._check_object(handle, False)
            descriptor = msvcrt.open_osfhandle(handle, os.O_RDWR | os.O_BINARY)
        except BaseException:
            self.kernel.CloseHandle(handle)
            raise
        return descriptor  # CRT descriptor now owns the handle.

    def verify_descriptor_path(self, descriptor, path=None):
        import msvcrt
        handle = self._handle(self.kernel.ReOpenFile(msvcrt.get_osfhandle(descriptor), 0x80, 7, 0x02200000))
        try:
            identity = self._check_object(handle, False)
            if path is not None:
                entry = self._handle(self.kernel.CreateFileW(str(path), 0x80, 7, None, 3, 0x00200080, None))
                try:
                    if self._check_object(entry, False) != identity:
                        raise OSError("Private file path changed after opening")
                finally:
                    self.kernel.CloseHandle(entry)
        finally:
            self.kernel.CloseHandle(handle)

    def protect_descriptor(self, descriptor):
        import msvcrt
        # Reopen the same object, never its replaceable path, with ACL rights.
        handle = self._handle(self.kernel.ReOpenFile(msvcrt.get_osfhandle(descriptor), 0xE0080, 7, 0x02200000))
        try:
            self._protect(handle)
        finally:
            self.kernel.CloseHandle(handle)

    def protect_directory(self, path):
        # Pin the target identity and deny delete sharing while allowing the
        # current-directory and ordinary read/write handles. SetFileSecurityW
        # updates this same path without converting existing child descriptors.
        deadline = time.monotonic() + 2.0
        while True:
            try:
                handle = self._handle(self.kernel.CreateFileW(str(path), 0x000E0080, 0x3, None, 3, 0x02200000, None))
                break
            except OSError as exc:
                if getattr(exc, "winerror", None) != 32 or time.monotonic() >= deadline:
                    raise
                time.sleep(0.01)
        try:
            self._protect(handle, directory=True, path=path)
        finally:
            self.kernel.CloseHandle(handle)

    def verify_directory(self, path):
        handle = self._handle(self.kernel.CreateFileW(str(path), 0x20080, 7, None, 3, 0x02200000, None))
        try:
            self.verify(handle, directory=True)
        finally:
            self.kernel.CloseHandle(handle)

    def verify_directory_owner(self, path):
        handle = self._handle(self.kernel.CreateFileW(str(path), 0x20080, 7, None, 3, 0x02200000, None))
        try:
            self._check_object(handle, True)
            owner, _dacl, descriptor = self._descriptor(handle)
            try:
                if not owner or self._sid_text(owner) not in {self.user_sid, self.default_owner_sid}:
                    raise OSError("Private storage directory is not owned by the current user")
            finally:
                self.kernel.LocalFree(descriptor)
        finally:
            self.kernel.CloseHandle(handle)

    def verify_descriptor(self, descriptor):
        import msvcrt
        handle = self._handle(self.kernel.ReOpenFile(msvcrt.get_osfhandle(descriptor), 0x20080, 7, 0x02200000))
        try:
            self.verify(handle)
        finally:
            self.kernel.CloseHandle(handle)
