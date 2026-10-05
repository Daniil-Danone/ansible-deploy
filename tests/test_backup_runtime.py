from __future__ import annotations

import io
import json
import os
import shutil
import stat
import tarfile
from contextlib import nullcontext
from pathlib import Path, PurePosixPath
from types import ModuleType, SimpleNamespace

import pytest
import yaml
from pydantic import ValidationError

from deploy_cli.cli import run as cli_run
from deploy_cli.config import ConfigurationError, validate_restore_isolation
from deploy_cli.models import BackupConfig, BackupRetention, EnvironmentConfig


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
        if arguments[0] == "lsjson":
            return "[]"
        if arguments[0] == "size":
            return json.dumps({"bytes": artifact.stat().st_size})
        if arguments[0] == "copyto" and arguments[1].startswith("drive:"):
            Path(arguments[2]).write_bytes(artifact.read_bytes())
        return ""

    monkeypatch.setattr(runtime, "_rclone", fake_rclone)
    runtime.upload_verified({"remote": "drive:backups"}, artifact, artifact.name)

    assert calls == [
        ("lsjson", "drive:backups", "--files-only", "--recursive"),
        ("copyto", str(artifact), "drive:backups/.partial/backup.age.partial"),
        ("size", "drive:backups/.partial/backup.age.partial", "--json"),
        (
            "moveto",
            "drive:backups/.partial/backup.age.partial",
            "drive:backups/backup.age",
            "--immutable",
        ),
        ("size", "drive:backups/backup.age", "--json"),
        ("copyto", "drive:backups/backup.age", calls[-1][2]),
    ]


def test_upload_rejects_partial_size_mismatch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runtime = _runtime()
    artifact = tmp_path / "backup.age"
    artifact.write_bytes(b"encrypted")

    def fake_rclone(config: dict[str, object], *arguments: str) -> str:
        if arguments[0] == "lsjson":
            return "[]"
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
            elif target.name.endswith(".json"):
                target.write_text(
                    json.dumps(
                        {
                            "artifact": "2026-10-05T010203Z.tar.gz.age",
                            "backup_id": "2026-10-05T010203Z",
                            "sha256": "0" * 64,
                            "size": len(b"tampered"),
                        }
                    ),
                    encoding="utf-8",
                )
            else:
                target.write_bytes(b"tampered")
        return ""

    monkeypatch.setattr(runtime, "_rclone", fake_rclone)
    monkeypatch.setattr(runtime, "remote_ids", lambda config: ["2026-10-05T010203Z"])
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

    with pytest.raises(runtime.BackupFailure, match="symlink"):
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
                "include": [
                    {
                        "type": "directory",
                        "path": "../uploads",
                        "restore_destination": "shared/uploads",
                    }
                ],
            }
        )


def test_backup_restore_destination_must_be_safe_relative_path() -> None:
    with pytest.raises(ValueError, match="restore_destination"):
        BackupConfig.model_validate(
            {
                "remote": "gdrive:backups/production",
                "credentials_file": "backup/rclone.conf",
                "age_identity_file": "backup/age.key",
                "age_recipient": "age1" + "a" * 58,
                "include": [
                    {
                        "type": "directory",
                        "path": "/srv/prod/uploads",
                        "restore_destination": "../prod/uploads",
                    }
                ],
            }
        )


def test_backup_restore_destinations_must_not_overlap() -> None:
    with pytest.raises(ValueError, match="must not overlap"):
        BackupConfig.model_validate(
            {
                "remote": "gdrive:backups/production",
                "credentials_file": "backup/rclone.conf",
                "age_identity_file": "backup/age.key",
                "age_recipient": "age1" + "a" * 58,
                "include": [
                    {
                        "type": "directory",
                        "path": "/srv/prod/uploads",
                        "restore_destination": "shared",
                    },
                    {
                        "type": "file",
                        "path": "/srv/prod/avatar.png",
                        "restore_destination": "shared/avatar.png",
                    },
                ],
            }
        )


def test_retention_rejects_all_zero_policy() -> None:
    with pytest.raises(ValueError, match="keep at least one"):
        BackupRetention.model_validate({"daily": 0, "weekly": 0, "monthly": 0})


def test_source_environment_is_required_only_for_restore() -> None:
    root = Path(__file__).parents[1] / "src/deploy_cli/templates/project/.deploy/environments"
    restore = yaml.safe_load((root / "restore/config.yml").read_text(encoding="utf-8"))
    stage = yaml.safe_load((root / "stage/config.yml").read_text(encoding="utf-8"))
    restore.pop("source_environment")
    stage["source_environment"] = "prod"

    with pytest.raises(ValidationError, match="requires source_environment"):
        EnvironmentConfig.model_validate(restore)
    with pytest.raises(ValidationError, match="only valid for restore"):
        EnvironmentConfig.model_validate(stage)


def test_runtime_retention_rejects_all_zero_and_protects_current(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime()
    current = "2026-10-05T010203Z-0123456789abcdef"
    old = "2026-09-05T010203Z-fedcba9876543210"
    monkeypatch.setattr(runtime, "remote_ids", lambda config: [old, current])
    deleted: list[str] = []
    monkeypatch.setattr(
        runtime,
        "_rclone",
        lambda config, *args: deleted.append(args[1]) or "",
    )
    config = {
        "remote": "drive:backups",
        "retention": {"daily": 0, "weekly": 0, "monthly": 1},
    }

    runtime.apply_retention(config, current=current)

    assert all(current not in path for path in deleted)


def test_source_hardlink_is_rejected_before_archive(tmp_path: Path) -> None:
    runtime = _runtime()
    source = tmp_path / "source"
    alias = tmp_path / "alias"
    source.write_text("data", encoding="utf-8")
    try:
        os.link(source, alias)
    except OSError:
        pytest.skip("hard links are unavailable")

    with pytest.raises(runtime.BackupFailure, match="hard links"):
        runtime._selected_paths(
            {
                "type": "file",
                "path": str(source),
                "restore_destination": "/srv/restore/source",
            }
        )


def test_existing_destination_hardlink_is_rejected(tmp_path: Path) -> None:
    runtime = _runtime()
    target = tmp_path / "target"
    alias = tmp_path / "alias"
    target.write_text("unchanged", encoding="utf-8")
    try:
        os.link(target, alias)
    except OSError:
        pytest.skip("hard links are unavailable")

    with pytest.raises(runtime.BackupFailure, match="hard links"):
        runtime._safe_destination(tmp_path, runtime.PurePosixPath("target"))
    assert alias.read_text(encoding="utf-8") == "unchanged"


def test_restore_file_preserves_mode_mtime_and_is_writable(tmp_path: Path) -> None:
    runtime = _runtime()
    archive_path = tmp_path / "archive.tar"
    member = tarfile.TarInfo("files/0/0/source.txt")
    member.size = len(b"payload")
    member.mode = 0o660
    member.mtime = 1_700_000_000
    if hasattr(os, "getuid"):
        member.uid = os.getuid()
        member.gid = os.getgid()
    with tarfile.open(archive_path, "w") as archive:
        archive.addfile(member, io.BytesIO(b"payload"))
    target = tmp_path / "restore" / "target.txt"

    with tarfile.open(archive_path) as archive:
        runtime._restore_file(archive, archive.getmembers()[0], target)

    metadata = target.stat()
    if os.name != "nt":
        assert stat.S_IMODE(metadata.st_mode) == 0o660
    assert int(metadata.st_mtime) == member.mtime
    with target.open("a", encoding="utf-8") as stream:
        stream.write("!")
    assert target.read_text(encoding="utf-8") == "payload!"


def test_remote_list_requires_valid_manifest_and_complete_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime()
    complete = "2026-10-05T010203Z-0123456789abcdef"
    orphan = "2026-10-05T020304Z-fedcba9876543210"
    names = {
        f"{complete}.tar.gz.age",
        f"{complete}.tar.gz.age.sha256",
        f"{complete}.json",
        f"{orphan}.tar.gz.age",
    }
    monkeypatch.setattr(runtime, "_remote_names", lambda config: names)
    monkeypatch.setattr(
        runtime,
        "_read_remote_json",
        lambda config, name: {
            "artifact": f"{complete}.tar.gz.age",
            "backup_id": complete,
            "sha256": "a" * 64,
            "size": 10,
        },
    )
    monkeypatch.setattr(
        runtime,
        "_read_remote_text",
        lambda config, name: f"{'a' * 64}  {complete}.tar.gz.age\n",
    )
    monkeypatch.setattr(runtime, "_remote_size", lambda config, name: 10)

    assert runtime.remote_ids({"remote": "drive:backups"}) == [complete]


def test_upload_rejects_existing_final_without_overwrite(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runtime = _runtime()
    artifact = tmp_path / "backup.age"
    artifact.write_bytes(b"new")
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(runtime, "_remote_names", lambda config: {artifact.name})
    monkeypatch.setattr(
        runtime, "_rclone", lambda config, *args: calls.append(args) or ""
    )

    with pytest.raises(runtime.BackupFailure, match="immutable"):
        runtime.upload_verified({"remote": "drive:backups"}, artifact, artifact.name)
    assert calls == []


def test_remote_checksum_detects_same_size_corruption(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runtime = _runtime()
    artifact = tmp_path / "backup.age"
    artifact.write_bytes(b"good")

    def fake_rclone(config: dict[str, object], *arguments: str) -> str:
        if arguments[0] == "lsjson":
            return "[]"
        if arguments[0] == "size":
            return json.dumps({"bytes": 4})
        if arguments[0] == "copyto" and arguments[1].startswith("drive:"):
            Path(arguments[2]).write_bytes(b"evil")
        return ""

    monkeypatch.setattr(runtime, "_rclone", fake_rclone)
    with pytest.raises(runtime.BackupFailure, match="checksum"):
        runtime.upload_verified({"remote": "drive:backups"}, artifact, artifact.name)


def test_new_backup_ids_have_nonce_and_are_unique(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = _runtime()
    values = iter(["0" * 16, "1" * 16])
    monkeypatch.setattr(runtime.secrets, "token_hex", lambda count: next(values))

    first = runtime._new_backup_id()
    second = runtime._new_backup_id()

    assert first != second
    assert runtime._valid_id(first)
    assert runtime._valid_id(second)


def test_archive_source_mapping_survives_config_reorder_add_and_remove(tmp_path: Path) -> None:
    runtime = _runtime()
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    first.write_text("first", encoding="utf-8")
    second.write_text("second", encoding="utf-8")
    sources = [
        {"type": "file", "path": str(first), "restore_destination": "data/first.txt"},
        {"type": "file", "path": str(second), "restore_destination": "data/second.txt"},
    ]
    archive_path = tmp_path / "backup.tar.gz"
    runtime.create_archive({"compose_file": "/unused", "include": sources}, archive_path)

    with tarfile.open(archive_path, "r:gz") as archive:
        members = runtime.safe_members(archive)
        source_map = runtime._load_source_map(archive, members)
        runtime._validate_mapped_members(members, source_map)
        changed_config = {
            "restore_root": str(tmp_path / "restore"),
            "include": [
                {"type": "file", "path": "/new", "restore_destination": "wrong"},
                *reversed(sources),
            ],
        }
        destinations = {
            Path(member.name).name: runtime._member_destination(
                changed_config, source_map[PurePosixPath(member.name).parts[1]], member
            )
            for member in members
            if PurePosixPath(member.name).parts[0] == "files"
        }

    assert destinations == {
        "first.txt": tmp_path / "restore/data/first.txt",
        "second.txt": tmp_path / "restore/data/second.txt",
    }


@pytest.mark.parametrize(
    "mapping",
    [
        None,
        b"not-json",
        json.dumps(
            {
                "schema_version": 1,
                "sources": [
                    {"id": "a" * 32, "type": "file", "restore_destination": "../prod"}
                ],
            }
        ).encode(),
    ],
)
def test_archive_source_mapping_missing_or_tampered_is_rejected(
    tmp_path: Path, mapping: bytes | None
) -> None:
    runtime = _runtime()
    archive_path = tmp_path / "mapping.tar"
    with tarfile.open(archive_path, "w") as archive:
        if mapping is not None:
            member = tarfile.TarInfo(runtime.ARCHIVE_MAPPING)
            member.size = len(mapping)
            member.mode = 0o600
            archive.addfile(member, io.BytesIO(mapping))
    with tarfile.open(archive_path) as archive:
        members = runtime.safe_members(archive)
        with pytest.raises(runtime.BackupFailure, match="mapping"):
            runtime._load_source_map(archive, members)


def test_post_archive_validation_rejects_raced_symlink_before_age_or_upload(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runtime = _runtime()
    calls: list[object] = []

    def unsafe_archive(config: dict[str, object], destination: Path) -> None:
        with tarfile.open(destination, "w:gz") as archive:
            mapping = json.dumps(
                {
                    "schema_version": 1,
                    "sources": [
                        {"id": "a" * 32, "type": "file", "restore_destination": "data/x"}
                    ],
                }
            ).encode()
            mapped = tarfile.TarInfo(runtime.ARCHIVE_MAPPING)
            mapped.size = len(mapping)
            archive.addfile(mapped, io.BytesIO(mapping))
            link = tarfile.TarInfo(f"files/{'a' * 32}/0/x")
            link.type = tarfile.SYMTYPE
            link.linkname = "/etc/shadow"
            archive.addfile(link)

    monkeypatch.setattr(runtime, "_backup_lock", lambda config: nullcontext())
    monkeypatch.setattr(runtime, "create_archive", unsafe_archive)
    monkeypatch.setattr(runtime, "run", lambda *args, **kwargs: calls.append(args) or "")
    monkeypatch.setattr(runtime, "_rclone", lambda *args: calls.append(args) or "[]")
    monkeypatch.setattr(runtime, "_delete_quiet", lambda *args: None)
    config = {
        "age_recipient": "age1" + "a" * 58,
        "include": [{"type": "file", "path": "/unused", "restore_destination": "data/x"}],
        "remote": "drive:backup",
        "retention": {"daily": 1, "weekly": 0, "monthly": 0},
        "result_file": str(tmp_path / "result.json"),
    }

    with pytest.raises(runtime.BackupFailure, match="unsafe"):
        runtime.backup(config)
    assert calls == []


def test_restore_open_race_does_not_truncate_hardlinked_victim(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runtime = _runtime()
    victim = tmp_path / "victim"
    target = tmp_path / "target"
    victim.write_text("do-not-truncate", encoding="utf-8")
    target.write_text("safe-before-race", encoding="utf-8")
    archive_path = tmp_path / "archive.tar"
    member = tarfile.TarInfo("files/0/0/source")
    member.size = 3
    with tarfile.open(archive_path, "w") as archive:
        archive.addfile(member, io.BytesIO(b"new"))
    original_open = runtime.os.open

    def raced_open(path: Path, flags: int, mode: int) -> int:
        Path(path).unlink()
        os.link(victim, path)
        return original_open(path, flags, mode)

    monkeypatch.setattr(runtime.os, "open", raced_open)
    with tarfile.open(archive_path) as archive:
        with pytest.raises(runtime.BackupFailure, match="safe regular file"):
            runtime._restore_file(archive, archive.getmembers()[0], target)
    assert victim.read_text(encoding="utf-8") == "do-not-truncate"


def test_postgres_readiness_retries_before_import(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime()
    attempts = 0
    sleeps: list[int] = []

    def fake_run(command: list[str], **kwargs: object) -> str:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise runtime.BackupFailure("initializing")
        return ""

    monkeypatch.setattr(runtime, "run", fake_run)
    monkeypatch.setattr(runtime.time, "sleep", sleeps.append)
    runtime._wait_for_postgres(
        {"compose_file": "/srv/restore/current/compose.yml"},
        {"service": "db", "user": "app", "database": "app"},
    )

    assert attempts == 3
    assert sleeps == [2, 2]


@pytest.mark.parametrize("sidecar", [None, "", "garbage", "a" * 64, "z" * 64 + "  x\n"])
def test_malformed_checksum_sidecar_is_rejected(sidecar: str | None) -> None:
    runtime = _runtime()
    assert runtime._parse_sidecar(sidecar, "backup.tar.gz.age") is None


def test_retention_failure_keeps_completed_current_backup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runtime = _runtime()
    source = tmp_path / "source"
    source.write_text("payload", encoding="utf-8")
    uploaded: list[str] = []
    deleted: list[str] = []

    def fake_run(command: list[str], **kwargs: object) -> str:
        if command[0] == "age":
            shutil.copyfile(command[-1], command[command.index("-o") + 1])
        return ""

    monkeypatch.setattr(runtime, "_backup_lock", lambda config: nullcontext())
    monkeypatch.setattr(runtime, "run", fake_run)
    monkeypatch.setattr(
        runtime, "upload_verified", lambda config, local, name: uploaded.append(name)
    )
    monkeypatch.setattr(
        runtime,
        "apply_retention",
        lambda config, current=None: (_ for _ in ()).throw(runtime.BackupFailure("retention")),
    )
    monkeypatch.setattr(
        runtime, "_delete_quiet", lambda config, name: deleted.append(name)
    )
    config = {
        "age_recipient": "age1" + "a" * 58,
        "compose_file": "/unused",
        "include": [
            {
                "type": "file",
                "path": str(source),
                "restore_destination": "data/source",
            }
        ],
        "remote": "drive:backup",
        "retention": {"daily": 1, "weekly": 0, "monthly": 0},
        "result_file": str(tmp_path / "result.json"),
    }

    with pytest.raises(runtime.BackupFailure, match="retention"):
        runtime.backup(config)
    assert [Path(name).suffix for name in uploaded] == [".age", ".sha256", ".json"]
    assert deleted == []


def test_retention_removes_completion_marker_before_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime()
    old = "2026-09-01T010203Z-0123456789abcdef"
    current = "2026-10-01T010203Z-fedcba9876543210"
    calls: list[str] = []
    monkeypatch.setattr(runtime, "remote_ids", lambda config: [old, current])

    def fail_mid_delete(config: dict[str, object], *arguments: str) -> str:
        calls.append(arguments[1])
        if len(calls) == 2:
            raise runtime.BackupFailure("remote delete failed")
        return ""

    monkeypatch.setattr(runtime, "_rclone", fail_mid_delete)
    config = {
        "remote": "drive:backup",
        "retention": {"daily": 0, "weekly": 0, "monthly": 1},
    }

    with pytest.raises(runtime.BackupFailure, match="remote delete"):
        runtime.apply_retention(config, current=current)
    assert calls == [
        f"drive:backup/{old}.json",
        f"drive:backup/{old}.tar.gz.age",
    ]


def test_remote_list_ignores_empty_sidecar(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = _runtime()
    identifier = "2026-10-05T010203Z-0123456789abcdef"
    monkeypatch.setattr(
        runtime,
        "_remote_names",
        lambda config: {
            f"{identifier}.json",
            f"{identifier}.tar.gz.age",
            f"{identifier}.tar.gz.age.sha256",
        },
    )
    monkeypatch.setattr(
        runtime,
        "_read_remote_json",
        lambda config, name: {
            "artifact": f"{identifier}.tar.gz.age",
            "backup_id": identifier,
            "sha256": "a" * 64,
            "size": 1,
        },
    )
    monkeypatch.setattr(runtime, "_read_remote_text", lambda config, name: "")
    assert runtime.remote_ids({"remote": "drive:backup"}) == []


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
            if arguments[1].startswith("drive:"):
                shutil.copyfile(remote / Path(arguments[1]).name, arguments[2])
            else:
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
        "include": [
            {
                "type": "file",
                "path": str(source),
                "restore_destination": "restored.txt",
            }
        ],
        "remote": "drive:backups",
        "result_file": str(tmp_path / "last-result.json"),
        "retention": {"daily": 7, "weekly": 4, "monthly": 6},
    }

    first = runtime.backup(config)
    second = runtime.backup(config)

    assert first["status"] == second["status"] == "ok"
    assert any(path.name.endswith(".tar.gz.age") for path in remote.iterdir())
