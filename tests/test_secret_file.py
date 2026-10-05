import os
import shutil
import subprocess
from pathlib import Path, PurePosixPath
from typing import Any

import pytest

from deploy_cli import secret_file, secret_store
from deploy_cli.secret_file import SecretFileError


def _forbid_path_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    def which(*args: Any, **kwargs: Any) -> str:
        raise AssertionError("Windows system tools must not be resolved through PATH")

    monkeypatch.setattr(shutil, "which", which)


def _system_root(tmp_path: Path, *tools: str) -> Path:
    system32 = tmp_path / "Windows" / "System32"
    system32.mkdir(parents=True)
    for tool in tools:
        (system32 / tool).write_bytes(b"")
    return tmp_path / "Windows"


def test_system_executable_resolves_from_system_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _system_root(tmp_path, "whoami.exe", "icacls.exe")
    monkeypatch.setenv("SystemRoot", str(root))
    _forbid_path_lookup(monkeypatch)

    assert secret_file._system_executable("whoami.exe") == str(root / "System32/whoami.exe")
    assert secret_file._system_executable("icacls.exe") == str(root / "System32/icacls.exe")


def test_system_directory_falls_back_to_default_windows_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SystemRoot", raising=False)

    assert secret_file._windows_system_directory() == Path(r"C:\Windows") / "System32"


def test_missing_system_tool_fails_even_when_path_has_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _system_root(tmp_path)
    monkeypatch.setenv("SystemRoot", str(root))
    shadow = tmp_path / "usr/bin/whoami"
    shadow.parent.mkdir(parents=True)
    shadow.write_bytes(b"")
    monkeypatch.setattr(shutil, "which", lambda *args, **kwargs: str(shadow))
    monkeypatch.setenv("PATH", str(shadow.parent))

    with pytest.raises(SecretFileError, match="whoami.exe"):
        secret_file._system_executable("whoami.exe")


def test_current_sid_runs_whoami_from_system32(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _system_root(tmp_path, "whoami.exe")
    monkeypatch.setenv("SystemRoot", str(root))
    _forbid_path_lookup(monkeypatch)
    commands: list[list[str]] = []

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, '"host\\user","S-1-5-21-1-2-3-1001"\n', "")

    monkeypatch.setattr(subprocess, "run", run)

    assert secret_file.windows_current_sid() == "S-1-5-21-1-2-3-1001"
    assert commands == [[str(root / "System32/whoami.exe"), "/user", "/fo", "csv", "/nh"]]


_SID = "S-1-5-21-1-2-3-1001"
_OTHER_SID = "S-1-5-32-545"


@pytest.mark.parametrize(
    ("dacl", "directory", "accepted"),
    [
        # Explicit ACE installed by secure_secret_permissions.
        (f"D:P(A;;FA;;;{_SID})", False, True),
        (f"D:P(A;OICI;FA;;;{_SID})", True, True),
        # Inherited from an owner-only parent: Explorer, editors, New-Item, mkdir.
        (f"D:AI(A;ID;FA;;;{_SID})", False, True),
        (f"D:AI(A;OICIID;FA;;;{_SID})", True, True),
        (f"D:(A;ID;FA;;;{_SID})", False, True),
        # Wrong inheritance shape for the object kind.
        (f"D:AI(A;OICIID;FA;;;{_SID})", False, False),
        (f"D:AI(A;ID;FA;;;{_SID})", True, False),
        (f"D:P(A;OICIIO;FA;;;{_SID})", True, False),
        (f"D:P(A;NP;FA;;;{_SID})", False, False),
        # Any extra ACE, another SID, deny ACEs or partial rights stay rejected.
        (f"D:AI(A;ID;FA;;;{_SID})(A;ID;FR;;;{_OTHER_SID})", False, False),
        (f"D:P(A;;FA;;;{_SID})(A;;FA;;;{_SID})", False, False),
        (f"D:AI(A;ID;FA;;;{_OTHER_SID})", False, False),
        (f"D:AI(D;ID;FA;;;{_SID})", False, False),
        (f"D:AI(D;;FA;;;{_OTHER_SID})(A;ID;FA;;;{_SID})", False, False),
        (f"D:AI(A;ID;FR;;;{_SID})", False, False),
        (f"D:AI(A;ID;0x1f01ff;;;{_SID})", False, False),
        (f"D:AI(A;ID;FA;;;{_SID}0)", False, False),
        ("D:NO_ACCESS_CONTROL", False, False),
        ("D:", False, False),
    ],
)
def test_owner_only_dacl_accepts_only_a_single_owner_full_access_entry(
    dacl: str, directory: bool, accepted: bool
) -> None:
    assert secret_file.is_owner_only_windows_dacl(dacl, _SID, directory=directory) is accepted


def _icacls(*args: str) -> None:
    icacls = secret_file._system_executable("icacls.exe")
    subprocess.run(  # noqa: S603 - fixed Windows ACL utility and arguments
        [icacls, *args], capture_output=True, check=True
    )


@pytest.mark.skipif(os.name != "nt", reason="Windows ACL inheritance contract")
def test_windows_items_created_normally_inside_owner_only_directory_are_accepted(
    tmp_path: Path,
) -> None:
    root = tmp_path / "store"
    root.mkdir()
    secret_file.secure_secret_permissions(root)
    # Plain creation inherits the single owner ACE, like Explorer or an editor would.
    nested = root / "project/environments/stage"
    nested.mkdir(parents=True)
    secret = nested / "app.env"
    secret.write_text("APP_ENV=stage\n", encoding="utf-8")

    for path in (root / "project", nested, secret):
        secret_file.validate_secret_permissions(path)
    secret_store._validate_external_ancestry(
        root,
        PurePosixPath("project/environments/stage/app.env"),
        secret=True,
        require_file=True,
    )

    _icacls(str(secret), "/grant", f"*{_OTHER_SID}:(R)")
    with pytest.raises(secret_file.SecretPermissionError, match="owner-only Windows ACL"):
        secret_file.validate_secret_permissions(secret)
    with pytest.raises(secret_store.SecretStorePathError) as raised:
        secret_store._validate_external_ancestry(
            root,
            PurePosixPath("project/environments/stage/app.env"),
            secret=True,
            require_file=True,
        )
    message = str(raised.value)
    assert message.startswith("Secret file is not owner-only; fix: icacls")
    assert str(tmp_path) not in message

    _icacls(str(nested), "/grant", f"*{_OTHER_SID}:(OI)(CI)(R)")
    with pytest.raises(secret_store.SecretStorePathError, match="Secret store directory"):
        secret_store._validate_external_ancestry(
            root,
            PurePosixPath("project/environments/stage/app.env"),
            secret=True,
            require_file=True,
        )


@pytest.mark.skipif(os.name != "nt", reason="Windows ACL repair hint")
def test_windows_owner_only_hint_command_repairs_acl_inherited_from_shared_parent(
    tmp_path: Path,
) -> None:
    # tmp_path is not owner-only: its children inherit several ACEs and are rejected.
    directory = tmp_path / "secrets"
    directory.mkdir()
    secret = tmp_path / "app.env"
    secret.write_text("APP_ENV=stage\n", encoding="utf-8")
    for path in (directory, secret):
        with pytest.raises(secret_file.SecretPermissionError):
            secret_file.validate_secret_permissions(path)

    user = os.environ["USERNAME"]
    _icacls(str(directory), "/inheritance:r", "/grant:r", f"{user}:(OI)(CI)F")
    _icacls(str(secret), "/inheritance:r", "/grant:r", f"{user}:F")

    secret_file.validate_secret_permissions(directory)
    secret_file.validate_secret_permissions(secret)


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode contract")
def test_posix_permission_error_names_kind_and_expected_mode(tmp_path: Path) -> None:
    secret = tmp_path / "app.env"
    secret.write_text("APP_ENV=stage\n", encoding="utf-8")
    secret.chmod(0o644)
    directory = tmp_path / "store"
    directory.mkdir(mode=0o755)
    directory.chmod(0o755)

    with pytest.raises(secret_file.SecretPermissionError, match=r"Secret file .*0600"):
        secret_file.validate_secret_permissions(secret)
    with pytest.raises(secret_file.SecretPermissionError, match=r"Secret directory .*0700"):
        secret_file.validate_secret_permissions(directory)
