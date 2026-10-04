#!/usr/bin/env python3
"""Root-owned backup runtime. Configuration contains no secret values."""

from __future__ import annotations

import argparse
import datetime as dt
import glob
import hashlib
import json
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

BACKUP_ID = "%Y-%m-%dT%H%M%SZ"


class BackupFailure(RuntimeError):
    pass


def run(command: list[str], *, stdin: Path | None = None, stdout: Path | None = None) -> str:
    input_stream = stdin.open("rb") if stdin else None
    output_stream = stdout.open("wb") if stdout else subprocess.PIPE
    try:
        result = subprocess.run(  # noqa: S603 - validated argv, never a shell
            command,
            stdin=input_stream,
            stdout=output_stream,
            stderr=subprocess.PIPE,
            check=False,
        )
    except OSError as exc:
        raise BackupFailure(f"unable to execute {Path(command[0]).name}") from exc
    finally:
        if input_stream:
            input_stream.close()
        if stdout:
            output_stream.close()  # type: ignore[union-attr]
    if result.returncode:
        detail = result.stderr.decode("utf-8", "replace").strip()
        raise BackupFailure(f"{Path(command[0]).name} failed: {detail[-1000:]}")
    if stdout:
        return ""
    return bytes(result.stdout).decode("utf-8", "replace")


def checksum(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_members(archive: tarfile.TarFile) -> list[tarfile.TarInfo]:
    members = archive.getmembers()
    for member in members:
        path = PurePosixPath(member.name)
        if (
            path.is_absolute()
            or ".." in path.parts
            or not path.parts
            or path.parts[0] not in {"database", "files"}
            or member.issym()
            or member.islnk()
            or member.isdev()
        ):
            raise BackupFailure("archive contains an unsafe member")
    return members


def _selected_paths(source: dict[str, Any]) -> list[Path]:
    raw = source["path"]
    candidates = glob.glob(raw, recursive=True) if source["type"] == "glob" else [raw]
    if not candidates:
        raise BackupFailure(f"backup source matched no paths: {raw}")
    selected: list[Path] = []
    for candidate in sorted(set(candidates)):
        path = Path(candidate)
        if not path.is_absolute() or path.is_symlink() or not path.exists():
            raise BackupFailure(f"backup source is missing or unsafe: {candidate}")
        if source["type"] == "file" and not path.is_file():
            raise BackupFailure(f"backup file source is not a regular file: {candidate}")
        if source["type"] == "directory" and not path.is_dir():
            raise BackupFailure(f"backup directory source is not a directory: {candidate}")
        selected.append(path)
    return selected


def create_archive(config: dict[str, Any], destination: Path) -> None:
    compose = config["compose_file"]
    with tempfile.TemporaryDirectory(prefix="ansible-deploy-backup-") as directory:
        workspace = Path(directory)
        with tarfile.open(destination, "w:gz", format=tarfile.PAX_FORMAT) as archive:
            for index, source in enumerate(config["include"]):
                if source["type"] == "postgres":
                    dump = workspace / f"{index}-{source['database']}.sql"
                    run(
                        [
                            "docker",
                            "compose",
                            "-f",
                            compose,
                            "exec",
                            "-T",
                            source["service"],
                            "pg_dump",
                            "--clean",
                            "--if-exists",
                            "--no-owner",
                            "--no-privileges",
                            "-U",
                            source["user"],
                            "-d",
                            source["database"],
                        ],
                        stdout=dump,
                    )
                    archive.add(dump, arcname=f"database/{index}-{source['database']}.sql")
                    continue
                for path in _selected_paths(source):
                    for nested in [path] if path.is_file() else [path, *path.rglob("*")]:
                        if nested.is_symlink():
                            raise BackupFailure(f"backup source contains a symlink: {nested}")
                    archive.add(path, arcname=f"files/{str(path).lstrip('/')}", recursive=True)


def _rclone(config: dict[str, Any], *arguments: str) -> str:
    return run(["rclone", "--config", config["rclone_config"], *arguments])


def _remote_size(config: dict[str, Any], remote: str) -> int:
    document = json.loads(_rclone(config, "size", remote, "--json"))
    return int(document["bytes"])


def upload_verified(config: dict[str, Any], local: Path, remote_name: str) -> None:
    final = f"{config['remote'].rstrip('/')}/{remote_name}"
    partial = f"{config['remote'].rstrip('/')}/.partial/{remote_name}.partial"
    _rclone(config, "copyto", str(local), partial)
    if _remote_size(config, partial) != local.stat().st_size:
        raise BackupFailure("remote partial upload size does not match local artifact")
    _rclone(config, "moveto", partial, final)
    if _remote_size(config, final) != local.stat().st_size:
        raise BackupFailure("remote artifact verification failed")


def retention_keep(ids: list[str], daily: int, weekly: int, monthly: int) -> set[str]:
    parsed = sorted((dt.datetime.strptime(value, BACKUP_ID), value) for value in set(ids))
    keep: set[str] = set()
    for count, key in (
        (daily, lambda value: value.date().isoformat()),
        (weekly, lambda value: f"{value.isocalendar().year}-W{value.isocalendar().week:02d}"),
        (monthly, lambda value: value.strftime("%Y-%m")),
    ):
        buckets: dict[str, str] = {}
        for stamp, identifier in parsed:
            buckets[key(stamp)] = identifier
        keep.update(list(buckets.values())[-count:] if count else [])
    return keep


def remote_ids(config: dict[str, Any]) -> list[str]:
    listing = json.loads(_rclone(config, "lsjson", config["remote"], "--files-only"))
    suffix = ".tar.gz.age"
    return sorted(
        item["Name"][: -len(suffix)]
        for item in listing
        if isinstance(item, dict)
        and isinstance(item.get("Name"), str)
        and item["Name"].endswith(suffix)
        and _valid_id(item["Name"][: -len(suffix)])
    )


def apply_retention(config: dict[str, Any]) -> list[str]:
    identifiers = remote_ids(config)
    policy = config["retention"]
    keep = retention_keep(identifiers, policy["daily"], policy["weekly"], policy["monthly"])
    removed = [identifier for identifier in identifiers if identifier not in keep]
    for identifier in removed:
        for suffix in (".tar.gz.age", ".tar.gz.age.sha256", ".json"):
            _rclone(config, "deletefile", f"{config['remote'].rstrip('/')}/{identifier}{suffix}")
    return removed


def backup(config: dict[str, Any]) -> dict[str, Any]:
    identifier = dt.datetime.now(dt.UTC).strftime(BACKUP_ID)
    with tempfile.TemporaryDirectory(prefix="ansible-deploy-backup-") as directory:
        workspace = Path(directory)
        archive = workspace / f"{identifier}.tar.gz"
        encrypted = workspace / f"{identifier}.tar.gz.age"
        create_archive(config, archive)
        run(["age", "-r", config["age_recipient"], "-o", str(encrypted), str(archive)])
        digest = checksum(encrypted)
        sidecar = workspace / f"{encrypted.name}.sha256"
        sidecar.write_text(f"{digest}  {encrypted.name}\n", encoding="ascii")
        manifest = workspace / f"{identifier}.json"
        manifest.write_text(
            json.dumps(
                {"backup_id": identifier, "sha256": digest, "size": encrypted.stat().st_size},
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        for item in (encrypted, sidecar, manifest):
            upload_verified(config, item, item.name)
        removed = apply_retention(config)
        return {
            "backup_id": identifier,
            "sha256": digest,
            "size": encrypted.stat().st_size,
            "retention_removed": removed,
            "status": "ok",
        }


def _valid_id(value: str) -> bool:
    try:
        return dt.datetime.strptime(value, BACKUP_ID).strftime(BACKUP_ID) == value
    except ValueError:
        return False


def _safe_destination(root: Path, relative: PurePosixPath) -> Path:
    target = root.joinpath(*relative.parts)
    current = root
    for part in relative.parts[:-1]:
        current /= part
        if current.exists() and current.is_symlink():
            raise BackupFailure("restore destination contains a symlink")
    if target.exists() and target.is_symlink():
        raise BackupFailure("restore destination is a symlink")
    return target


def restore(config: dict[str, Any], identifier: str) -> dict[str, Any]:
    if not _valid_id(identifier):
        raise BackupFailure("invalid backup identifier")
    with tempfile.TemporaryDirectory(prefix="ansible-deploy-restore-") as directory:
        workspace = Path(directory)
        encrypted = workspace / f"{identifier}.tar.gz.age"
        sidecar = workspace / f"{encrypted.name}.sha256"
        archive_path = workspace / f"{identifier}.tar.gz"
        for item in (encrypted, sidecar):
            _rclone(config, "copyto", f"{config['remote'].rstrip('/')}/{item.name}", str(item))
        expected = sidecar.read_text(encoding="ascii").split()[0]
        if len(expected) != 64 or checksum(encrypted) != expected:
            raise BackupFailure("backup checksum verification failed")
        run(["age", "-d", "-i", config["age_identity"], "-o", str(archive_path), str(encrypted)])
        files_root = Path("/")
        with tarfile.open(archive_path, "r:gz") as archive:
            members = safe_members(archive)
            for member in members:
                path = PurePosixPath(member.name)
                if path.parts[0] == "files":
                    relative = PurePosixPath(*path.parts[1:])
                    target = _safe_destination(files_root, relative)
                    if member.isdir():
                        target.mkdir(parents=True, exist_ok=True)
                    elif member.isfile():
                        target.parent.mkdir(parents=True, exist_ok=True)
                        source = archive.extractfile(member)
                        if source is None:
                            raise BackupFailure("unable to read archive member")
                        with target.open("wb") as output:
                            shutil.copyfileobj(source, output)
            postgres = [item for item in config["include"] if item["type"] == "postgres"]
            dumps = [item for item in members if PurePosixPath(item.name).parts[0] == "database"]
            if len(postgres) != len(dumps):
                raise BackupFailure("backup database manifest does not match restore config")
            for source_config, member in zip(postgres, dumps, strict=True):
                dump = workspace / Path(member.name).name
                extracted = archive.extractfile(member)
                if extracted is None:
                    raise BackupFailure("unable to read database dump")
                with dump.open("wb") as output:
                    shutil.copyfileobj(extracted, output)
                run(
                    [
                        "docker",
                        "compose",
                        "-f",
                        config["compose_file"],
                        "exec",
                        "-T",
                        source_config["service"],
                        "psql",
                        "-v",
                        "ON_ERROR_STOP=1",
                        "-U",
                        source_config["user"],
                        "-d",
                        source_config["database"],
                    ],
                    stdin=dump,
                )
        return {"backup_id": identifier, "sha256": expected, "status": "restored"}


def write_result(config: dict[str, Any], result: dict[str, Any]) -> None:
    result_path = Path(
        config.get("result_file", "/var/lib/ansible-deploy/backup/last-result.json")
    )
    result_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = result_path.with_suffix(".tmp")
    temporary.write_text(json.dumps(result, sort_keys=True) + "\n", encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(result_path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["run", "list", "restore", "retention"])
    parser.add_argument("--config", default="/etc/ansible-deploy/backup/config.json")
    parser.add_argument("--backup")
    args = parser.parse_args()
    config: dict[str, Any] | None = None
    try:
        config = json.loads(Path(args.config).read_text(encoding="utf-8"))
        if args.action == "run":
            result: Any = backup(config)
        elif args.action == "list":
            result = {"backups": remote_ids(config)}
        elif args.action == "retention":
            result = {"retention_removed": apply_retention(config)}
        else:
            if not args.backup:
                raise BackupFailure("--backup is required")
            result = restore(config, args.backup)
        write_result(config, result)
        print(json.dumps(result, sort_keys=True))
        return 0
    except (
        BackupFailure,
        OSError,
        ValueError,
        KeyError,
        json.JSONDecodeError,
        tarfile.TarError,
    ) as exc:
        failure = {"error": str(exc), "status": "failed"}
        if config is not None:
            try:
                write_result(config, failure)
            except OSError:
                print("unable to persist backup failure state", file=sys.stderr)
        print(json.dumps(failure, sort_keys=True), file=sys.stderr)
        return 8


if __name__ == "__main__":
    raise SystemExit(main())
