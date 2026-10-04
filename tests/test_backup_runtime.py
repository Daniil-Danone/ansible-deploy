from __future__ import annotations

import io
import json
import shutil
import tarfile
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from deploy_cli.cli import run as cli_run
from deploy_cli.config import ConfigurationError, validate_restore_isolation
from deploy_cli.models import BackupConfig


def _runtime() -> ModuleType:
    path = Path("ansible/roles/backup/files/backup_runtime.py").resolve()
    module = ModuleType("backup_runtime_test")
    module.__file__ = str(path)
    exec(  # noqa: S102 - load the checked-in standalone runtime without creating pycache
        compile(path.read_bytes(), str(path), "exec"), module.__dict__
    )
    return module


def test_retention_keeps_latest_daily_weekly_and_monthly() -> None:
    runtime = _runtime()
    identifiers = [
        "2026-07-01T010000Z",
        "2026-08-01T010000Z",
        "2026-09-28T010000Z",
        "2026-10-01T010000Z",
        "2026-10-02T010000Z",
        "2026-10-03T010000Z",
    ]

    kept = runtime.retention_keep(identifiers, daily=2, weekly=2, monthly=2)

    assert kept == {
        "2026-08-01T010000Z",
        "2026-09-28T010000Z",
        "2026-10-02T010000Z",
        "2026-10-03T010000Z",
    }


def test_upload_uses_partial_then_verifies_final(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runtime = _runtime()
    artifact = tmp_path / "backup.age"
    artifact.write_bytes(b"encrypted")
    calls: list[tuple[str, ...]] = []

    def fake_rclone(config: dict[str, object], *arguments: str) -> str:
        calls.append(arguments)
        if arguments[0] == "size":
            return json.dumps({"bytes": artifact.stat().st_size})
        return ""

    monkeypatch.setattr(runtime, "_rclone", fake_rclone)
    runtime.upload_verified({"remote": "drive:backups"}, artifact, artifact.name)

    assert calls == [
        ("copyto", str(artifact), "drive:backups/.partial/backup.age.partial"),
        ("size", "drive:backups/.partial/backup.age.partial", "--json"),
        ("moveto", "drive:backups/.partial/backup.age.partial", "drive:backups/backup.age"),
        ("size", "drive:backups/backup.age", "--json"),
    ]


def test_upload_rejects_partial_size_mismatch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runtime = _runtime()
    artifact = tmp_path / "backup.age"
    artifact.write_bytes(b"encrypted")

    def fake_rclone(config: dict[str, object], *arguments: str) -> str:
        return json.dumps({"bytes": 1}) if arguments[0] == "size" else ""

    monkeypatch.setattr(runtime, "_rclone", fake_rclone)
    with pytest.raises(runtime.BackupFailure, match="partial upload size"):
        runtime.upload_verified({"remote": "drive:backups"}, artifact, artifact.name)


def test_restore_rejects_tampered_encrypted_artifact(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runtime = _runtime()

    def fake_rclone(config: dict[str, object], *arguments: str) -> str:
        if arguments[0] == "copyto":
            target = Path(arguments[2])
            if target.name.endswith(".sha256"):
                target.write_text("0" * 64 + "  backup.age\n", encoding="ascii")
            else:
                target.write_bytes(b"tampered")
        return ""

    monkeypatch.setattr(runtime, "_rclone", fake_rclone)
    with pytest.raises(runtime.BackupFailure, match="checksum"):
        runtime.restore(
            {
                "remote": "drive:backups",
                "rclone_config": str(tmp_path / "rclone.conf"),
                "age_identity": str(tmp_path / "identity"),
                "include": [],
            },
            "2026-10-05T010203Z",
        )


def test_archive_member_guard_rejects_traversal(tmp_path: Path) -> None:
    runtime = _runtime()
    archive_path = tmp_path / "unsafe.tar"
    with tarfile.open(archive_path, "w") as archive:
        member = tarfile.TarInfo("files/../../etc/shadow")
        member.size = 1
        archive.addfile(member, io.BytesIO(b"x"))

    with tarfile.open(archive_path) as archive:
        with pytest.raises(runtime.BackupFailure, match="unsafe member"):
            runtime.safe_members(archive)


def test_file_source_rejects_symlink(tmp_path: Path) -> None:
    runtime = _runtime()
    target = tmp_path / "target"
    target.write_text("data", encoding="utf-8")
    link = tmp_path / "link"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlinks are unavailable")

    with pytest.raises(runtime.BackupFailure, match="unsafe"):
        runtime._selected_paths({"type": "file", "path": str(link)})


def test_restore_destination_rejects_existing_symlink(tmp_path: Path) -> None:
    runtime = _runtime()
    target = tmp_path / "target"
    target.write_text("data", encoding="utf-8")
    link = tmp_path / "link"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlinks are unavailable")

    with pytest.raises(runtime.BackupFailure, match="symlink"):
        runtime._safe_destination(tmp_path, runtime.PurePosixPath("link"))


def test_backup_config_requires_safe_declarative_sources() -> None:
    with pytest.raises(ValueError, match="normalized absolute"):
        BackupConfig.model_validate(
            {
                "remote": "gdrive:backups/production",
                "credentials_file": "backup/rclone.conf",
                "age_identity_file": "backup/age.key",
                "age_recipient": "age1" + "a" * 58,
                "include": [{"type": "directory", "path": "../uploads"}],
            }
        )


def test_cli_rejects_production_restore_target_with_exit_8(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    result = cli_run(
        [
            "--project-dir",
            str(tmp_path),
            "backup",
            "restore",
            "prod",
            "--target",
            "prod",
            "--backup",
            "2026-10-05T010203Z",
            "--yes",
        ]
    )

    assert result == 8
    assert "dedicated restore environment" in capsys.readouterr().err


def test_restore_isolation_rejects_same_server_address() -> None:
    source = SimpleNamespace(
        environment="prod",
        domain="prod.example.com",
        application=SimpleNamespace(remote_dir="/srv/prod", compose=Path("prod.yml")),
        server=SimpleNamespace(host="192.0.2.10", ssh_port=22),
    )
    target = SimpleNamespace(
        environment="restore",
        domain="restore.example.com",
        application=SimpleNamespace(remote_dir="/srv/restore", compose=Path("restore.yml")),
        server=SimpleNamespace(host="192.0.2.10", ssh_port=22),
    )

    with pytest.raises(ConfigurationError, match="same address"):
        validate_restore_isolation(source, target)


def test_backup_with_fake_age_and_rclone_is_verified_and_idempotent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runtime = _runtime()
    source = tmp_path / "source.txt"
    source.write_text("payload", encoding="utf-8")
    remote = tmp_path / "remote"
    remote.mkdir()

    original_run = runtime.run

    def fake_run(command: list[str], **kwargs: object) -> str:
        if command[0] == "age":
            shutil.copyfile(command[-1], command[command.index("-o") + 1])
            return ""
        return original_run(command, **kwargs)

    def fake_rclone(config: dict[str, object], *arguments: str) -> str:
        action = arguments[0]
        if action == "copyto":
            target = remote / Path(arguments[2]).name
            shutil.copyfile(arguments[1], target)
        elif action == "size":
            return json.dumps({"bytes": (remote / Path(arguments[1]).name).stat().st_size})
        elif action == "moveto":
            (remote / Path(arguments[1]).name).replace(remote / Path(arguments[2]).name)
        elif action == "lsjson":
            return json.dumps([{"Name": path.name} for path in remote.iterdir()])
        elif action == "deletefile":
            (remote / Path(arguments[1]).name).unlink(missing_ok=True)
        return ""

    monkeypatch.setattr(runtime, "run", fake_run)
    monkeypatch.setattr(runtime, "_rclone", fake_rclone)
    config = {
        "age_recipient": "age1" + "a" * 58,
        "compose_file": "/unused",
        "include": [{"type": "file", "path": str(source)}],
        "remote": "drive:backups",
        "retention": {"daily": 7, "weekly": 4, "monthly": 6},
    }

    first = runtime.backup(config)
    second = runtime.backup(config)

    assert first["status"] == second["status"] == "ok"
    assert any(path.name.endswith(".tar.gz.age") for path in remote.iterdir())
