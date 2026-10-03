import csv
import os
import re
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path


class SecretFileError(ValueError):
    """A secret file is not protected for the current platform."""


def secure_secret_permissions(path: Path) -> None:
    if os.name != "nt":
        os.chmod(path, 0o600)
        validate_secret_permissions(path)
        return
    sid = _windows_current_sid()
    icacls = _system_executable("icacls.exe")
    result = subprocess.run(  # noqa: S603 - fixed Windows ACL utility and arguments
        [icacls, str(path), "/inheritance:r", "/grant:r", f"*{sid}:(F)"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise SecretFileError("Unable to apply an owner-only Windows ACL")
    validate_secret_permissions(path)


def validate_secret_permissions(path: Path) -> None:
    if os.name != "nt":
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & 0o077:
            raise SecretFileError("Registry authentication must have mode 0600")
        return
    current_sid = _windows_current_sid()
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
    if re.fullmatch(rf"D:[A-Z]*\(A;;FA;;;{re.escape(current_sid)}\)", dacl) is None:
        raise SecretFileError("Registry authentication requires an owner-only Windows ACL")


def _windows_current_sid() -> str:
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


def _system_executable(name: str) -> str:
    executable = shutil.which(name)
    if executable is None:
        raise SecretFileError("Required Windows security utility is unavailable")
    return executable
