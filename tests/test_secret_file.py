import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from deploy_cli import secret_file
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
