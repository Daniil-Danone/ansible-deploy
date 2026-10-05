import csv
import ctypes
import os
import re
import stat
import subprocess
import tempfile
from ctypes import wintypes
from pathlib import Path
from typing import Any


class SecretFileError(ValueError):
    """A secret file is not protected for the current platform."""


def _windows_library(name: str) -> Any:
    windows_ctypes: Any = ctypes
    return windows_ctypes.WinDLL(name, use_last_error=True)


def secure_secret_permissions(path: Path) -> None:
    if os.name != "nt":
        os.chmod(path, 0o700 if path.is_dir() else 0o600)
        validate_secret_permissions(path)
        return
    sid = windows_current_sid()
    _set_windows_owner_only_acl(path, sid)
    validate_secret_permissions(path)


def _set_windows_owner_only_acl(path: Path, sid: str) -> None:
    advapi32 = _windows_library("advapi32")
    kernel32 = _windows_library("kernel32")
    descriptor = ctypes.c_void_p()
    descriptor_size = wintypes.ULONG()
    convert = advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW
    convert.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.ULONG),
    ]
    convert.restype = wintypes.BOOL
    ace_flags = "OICI" if path.is_dir() else ""
    sddl = f"D:P(A;{ace_flags};FA;;;{sid})"
    if not convert(sddl, 1, ctypes.byref(descriptor), ctypes.byref(descriptor_size)):
        raise SecretFileError("Unable to construct an owner-only Windows ACL")
    dacl_present = wintypes.BOOL()
    dacl_defaulted = wintypes.BOOL()
    dacl = ctypes.c_void_p()
    get_dacl = advapi32.GetSecurityDescriptorDacl
    get_dacl.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.BOOL),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.BOOL),
    ]
    get_dacl.restype = wintypes.BOOL
    local_free = kernel32.LocalFree
    local_free.argtypes = [wintypes.HLOCAL]
    local_free.restype = wintypes.HLOCAL
    try:
        if not get_dacl(
            descriptor,
            ctypes.byref(dacl_present),
            ctypes.byref(dacl),
            ctypes.byref(dacl_defaulted),
        ) or not dacl_present:
            raise SecretFileError("Unable to inspect the owner-only Windows ACL")
        set_security = advapi32.SetNamedSecurityInfoW
        set_security.argtypes = [
            wintypes.LPWSTR,
            ctypes.c_int,
            wintypes.DWORD,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
        ]
        set_security.restype = wintypes.DWORD
        dacl_security_information = 0x00000004
        protected_dacl_security_information = 0x80000000
        result = set_security(
            str(path),
            1,
            dacl_security_information | protected_dacl_security_information,
            None,
            None,
            dacl,
            None,
        )
        if result != 0:
            raise SecretFileError("Unable to apply an owner-only Windows ACL")
    finally:
        local_free(ctypes.cast(descriptor, wintypes.HLOCAL))


def validate_secret_permissions(path: Path) -> None:
    if os.name != "nt":
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & 0o077:
            raise SecretFileError("Registry authentication must have mode 0600")
        return
    current_sid = windows_current_sid()
    icacls = _system_executable("icacls.exe")
    descriptor, acl_name = tempfile.mkstemp(prefix="ansible-deploy-acl-", suffix=".txt")
    os.close(descriptor)
    acl_path = Path(acl_name)
    acl_path.unlink()
    try:
        result = subprocess.run(  # noqa: S603 - fixed Windows ACL utility and arguments
            [icacls, str(path), "/save", str(acl_path), "/c"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise SecretFileError("Unable to verify the Windows ACL")
        descriptor_text = acl_path.read_text(encoding="utf-16-le").replace("\x00", "")
    except (OSError, UnicodeError) as exc:
        raise SecretFileError("Unable to verify the Windows ACL") from exc
    finally:
        acl_path.unlink(missing_ok=True)
    dacl = descriptor_text.splitlines()[-1].strip()
    ace_flags = "OICI" if path.is_dir() else ""
    expected = rf"D:[A-Z]*\(A;{ace_flags};FA;;;{re.escape(current_sid)}\)"
    if re.fullmatch(expected, dacl) is None:
        raise SecretFileError("Registry authentication requires an owner-only Windows ACL")


def windows_current_sid() -> str:
    whoami = _system_executable("whoami.exe")
    result = subprocess.run(  # noqa: S603 - fixed Windows identity utility
        [whoami, "/user", "/fo", "csv", "/nh"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise SecretFileError("Unable to resolve the current Windows user SID")
    try:
        row = next(csv.reader([result.stdout.strip()]))
        sid = row[1]
    except (IndexError, StopIteration, csv.Error) as exc:
        raise SecretFileError("Unable to resolve the current Windows user SID") from exc
    if not re.fullmatch(r"S-\d(?:-\d+)+", sid):
        raise SecretFileError("Unable to resolve the current Windows user SID")
    return sid


def _windows_system_directory() -> Path:
    system_root = os.environ.get("SystemRoot") or r"C:\Windows"
    return Path(system_root) / "System32"


def _system_executable(name: str) -> str:
    # PATH is never consulted: Git Bash and similar shells shadow System32 with
    # look-alike tools (e.g. /usr/bin/whoami) that break SID and ACL checks.
    executable = _windows_system_directory() / name
    if not executable.is_file():
        raise SecretFileError(f"Required Windows security utility is unavailable: {executable}")
    return str(executable)
