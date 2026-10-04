#!/usr/bin/env python3
"""Root-owned backup runtime. Configuration contains no secret values."""

from __future__ import annotations

import argparse
import datetime as dt
import glob
import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Any

BACKUP_STAMP = "%Y-%m-%dT%H%M%SZ"
BACKUP_ID_RE = re.compile(r"^(?P<stamp>\d{4}-\d{2}-\d{2}T\d{6}Z)(?:-[0-9a-f]{16})?$")
ARTIFACT_SUFFIX = ".tar.gz.age"
SIDECAR_SUFFIX = f"{ARTIFACT_SUFFIX}.sha256"
MANIFEST_SUFFIX = ".json"


class BackupFailure(RuntimeError):
    pass


def run(command: list[str], *, stdin: Path | None = None, stdout: Path | None = None) -> str:
    input_stream = stdin.open("rb") if stdin else None
    output_stream = stdout.open("wb") if stdout else subprocess.PIPE
    try:
        result = subprocess.run(  # noqa: S603 - validated argv, never a shell
            command, stdin=input_stream, stdout=output_stream,
            stderr=subprocess.PIPE, check=False,
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


def _valid_id(value: str) -> bool:
    match = BACKUP_ID_RE.fullmatch(value)
    if match is None:
        return False
    try:
        return dt.datetime.strptime(match.group("stamp"), BACKUP_STAMP).strftime(
            BACKUP_STAMP
        ) == match.group("stamp")
    except ValueError:
        return False


def _backup_stamp(identifier: str) -> dt.datetime:
    match = BACKUP_ID_RE.fullmatch(identifier)
    if match is None:
        raise BackupFailure("invalid backup identifier")
    return dt.datetime.strptime(match.group("stamp"), BACKUP_STAMP)


def _new_backup_id() -> str:
    return f"{dt.datetime.now(dt.UTC).strftime(BACKUP_STAMP)}-{secrets.token_hex(8)}"


def _safe_source(path: Path, *, expected: str | None = None) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise BackupFailure(f"backup source is missing or unsafe: {path}") from exc
    mode = metadata.st_mode
    if stat.S_ISLNK(mode):
        raise BackupFailure(f"backup source contains a symlink: {path}")
    if stat.S_ISREG(mode):
        if metadata.st_nlink != 1:
            raise BackupFailure(f"backup source has multiple hard links: {path}")
    elif not stat.S_ISDIR(mode):
        raise BackupFailure(f"backup source is not a regular file or directory: {path}")
    if expected == "file" and not stat.S_ISREG(mode):
        raise BackupFailure(f"backup file source is not a regular file: {path}")
    if expected == "directory" and not stat.S_ISDIR(mode):
        raise BackupFailure(f"backup directory source is not a directory: {path}")
    return metadata


def _selected_paths(source: dict[str, Any]) -> list[Path]:
    raw = source["path"]
    candidates = glob.glob(raw, recursive=True) if source["type"] == "glob" else [raw]
    if not candidates:
        raise BackupFailure(f"backup source matched no paths: {raw}")
    selected: list[Path] = []
    for candidate in sorted(set(candidates)):
        path = Path(candidate)
        if not path.is_absolute():
            raise BackupFailure(f"backup source is missing or unsafe: {candidate}")
        _safe_source(path, expected=source["type"] if source["type"] != "glob" else None)
        if any(parent in selected for parent in path.parents):
            continue
        selected.append(path)
    if source["type"] == "glob":
        basenames = [path.name for path in selected]
        if len(basenames) != len(set(basenames)):
            raise BackupFailure("backup glob sources collide at restore destination")
    return selected


def _source_entries(source: dict[str, Any]) -> list[tuple[Path, PurePosixPath]]:
    entries: list[tuple[Path, PurePosixPath]] = []
    for candidate_index, path in enumerate(_selected_paths(source)):
        nested_paths = [path]
        if path.is_dir():
            nested_paths.extend(sorted(path.rglob("*")))
        for nested in nested_paths:
            _safe_source(nested)
            relative = PurePosixPath(*nested.relative_to(path.parent).parts)
            entries.append((nested, PurePosixPath(str(candidate_index)) / relative))
    return entries


def create_archive(config: dict[str, Any], destination: Path) -> None:
    compose = config["compose_file"]
    path_entries: dict[int, list[tuple[Path, PurePosixPath]]] = {}
    for index, source in enumerate(config["include"]):
        if source["type"] != "postgres":
            path_entries[index] = _source_entries(source)
    with tempfile.TemporaryDirectory(prefix="ansible-deploy-backup-") as directory:
        workspace = Path(directory)
        with tarfile.open(destination, "w:gz", format=tarfile.PAX_FORMAT) as archive:
            for index, source in enumerate(config["include"]):
                if source["type"] == "postgres":
                    dump = workspace / f"{index}-{source['database']}.sql"
                    run([
                        "docker", "compose", "-f", compose, "exec", "-T",
                        source["service"], "pg_dump", "--clean", "--if-exists",
                        "--no-owner", "--no-privileges", "-U", source["user"],
                        "-d", source["database"],
                    ], stdout=dump)
                    archive.add(dump, arcname=f"database/{index}-{source['database']}.sql")
                    continue
                for path, relative in path_entries[index]:
                    archive.add(
                        path,
                        arcname=str(PurePosixPath("files") / str(index) / relative),
                        recursive=False,
                    )


def _rclone(config: dict[str, Any], *arguments: str) -> str:
    return run(["rclone", "--config", config["rclone_config"], *arguments])


def _remote_size(config: dict[str, Any], remote: str) -> int:
    document = json.loads(_rclone(config, "size", remote, "--json"))
    return int(document["bytes"])


def _remote_names(config: dict[str, Any]) -> set[str]:
    listing = json.loads(
        _rclone(config, "lsjson", config["remote"], "--files-only", "--recursive")
    )
    return {
        item["Path"] if isinstance(item.get("Path"), str) else item["Name"]
        for item in listing
        if isinstance(item, dict)
        and (isinstance(item.get("Path"), str) or isinstance(item.get("Name"), str))
    }


def _remote_path(config: dict[str, Any], name: str) -> str:
    return f"{config['remote'].rstrip('/')}/{name}"


def _delete_quiet(config: dict[str, Any], name: str) -> None:
    try:
        _rclone(config, "deletefile", _remote_path(config, name))
    except (BackupFailure, OSError, ValueError, json.JSONDecodeError):
        pass


def upload_verified(config: dict[str, Any], local: Path, remote_name: str) -> None:
    final = _remote_path(config, remote_name)
    partial_name = f".partial/{remote_name}.partial"
    partial = _remote_path(config, partial_name)
    if remote_name in _remote_names(config):
        raise BackupFailure("refusing to overwrite immutable remote backup artifact")
    moved = False
    try:
        _rclone(config, "copyto", str(local), partial)
        if _remote_size(config, partial) != local.stat().st_size:
            raise BackupFailure("remote partial upload size does not match local artifact")
        _rclone(config, "moveto", partial, final, "--immutable")
        moved = True
        if _remote_size(config, final) != local.stat().st_size:
            raise BackupFailure("remote artifact size verification failed")
        with tempfile.TemporaryDirectory(prefix="ansible-deploy-remote-verify-") as directory:
            downloaded = Path(directory) / local.name
            _rclone(config, "copyto", final, str(downloaded))
            if checksum(downloaded) != checksum(local):
                raise BackupFailure("remote artifact checksum verification failed")
    except Exception:
        _delete_quiet(config, partial_name)
        if moved:
            _delete_quiet(config, remote_name)
        raise


def retention_keep(ids: list[str], daily: int, weekly: int, monthly: int) -> set[str]:
    if daily == weekly == monthly == 0:
        raise BackupFailure("backup retention must keep at least one backup")
    parsed = sorted((_backup_stamp(value), value) for value in set(ids))
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


def _read_remote_json(config: dict[str, Any], name: str) -> dict[str, Any] | None:
    with tempfile.TemporaryDirectory(prefix="ansible-deploy-manifest-") as directory:
        target = Path(directory) / "manifest.json"
        try:
            _rclone(config, "copyto", _remote_path(config, name), str(target))
            value = json.loads(target.read_text(encoding="utf-8"))
        except (BackupFailure, OSError, ValueError, json.JSONDecodeError):
            return None
    return value if isinstance(value, dict) else None


def _read_remote_text(config: dict[str, Any], name: str) -> str | None:
    with tempfile.TemporaryDirectory(prefix="ansible-deploy-metadata-") as directory:
        target = Path(directory) / "metadata"
        try:
            _rclone(config, "copyto", _remote_path(config, name), str(target))
            return target.read_text(encoding="ascii")
        except (BackupFailure, OSError, ValueError, UnicodeError):
            return None


def remote_ids(config: dict[str, Any]) -> list[str]:
    names = _remote_names(config)
    complete: list[str] = []
    for name in sorted(names):
        if not name.endswith(MANIFEST_SUFFIX):
            continue
        identifier = name[: -len(MANIFEST_SUFFIX)]
        if not _valid_id(identifier):
            continue
        artifact_name = f"{identifier}{ARTIFACT_SUFFIX}"
        sidecar_name = f"{identifier}{SIDECAR_SUFFIX}"
        if artifact_name not in names or sidecar_name not in names:
            continue
        manifest = _read_remote_json(config, name)
        if (
            manifest is None
            or manifest.get("backup_id") != identifier
            or manifest.get("artifact") != artifact_name
            or not isinstance(manifest.get("sha256"), str)
            or re.fullmatch(r"[0-9a-f]{64}", manifest["sha256"]) is None
            or not isinstance(manifest.get("size"), int)
            or manifest["size"] <= 0
        ):
            continue
        sidecar = _read_remote_text(config, sidecar_name)
        if sidecar is None or sidecar.split()[0] != manifest["sha256"]:
            continue
        try:
            if _remote_size(config, _remote_path(config, artifact_name)) != manifest["size"]:
                continue
        except (BackupFailure, OSError, ValueError, KeyError, json.JSONDecodeError):
            continue
        complete.append(identifier)
    return complete


def apply_retention(config: dict[str, Any], *, current: str | None = None) -> list[str]:
    identifiers = remote_ids(config)
    policy = config["retention"]
    keep = retention_keep(identifiers, policy["daily"], policy["weekly"], policy["monthly"])
    if current is not None:
        keep.add(current)
    removed = [identifier for identifier in identifiers if identifier not in keep]
    for identifier in removed:
        for suffix in (ARTIFACT_SUFFIX, SIDECAR_SUFFIX, MANIFEST_SUFFIX):
            _rclone(config, "deletefile", _remote_path(config, f"{identifier}{suffix}"))
    return removed


@contextmanager
def _backup_lock(config: dict[str, Any]) -> Iterator[None]:
    result_path = Path(
        config.get("result_file", "/var/lib/ansible-deploy/backup/last-result.json")
    )
    result_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_path = result_path.parent / "backup.lock"
    with lock_path.open("a+b") as stream:
        try:
            if os.name == "nt":
                import msvcrt

                stream.seek(0)
                stream.write(b"\0")
                stream.flush()
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(  # type: ignore[attr-defined]
                    stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB  # type: ignore[attr-defined]
                )
        except (BlockingIOError, OSError) as exc:
            raise BackupFailure("another backup operation is already running") from exc
        try:
            yield
        finally:
            if os.name == "nt":
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)  # type: ignore[attr-defined]


def backup(config: dict[str, Any]) -> dict[str, Any]:
    with _backup_lock(config):
        identifier = _new_backup_id()
        names = (
            f"{identifier}{ARTIFACT_SUFFIX}",
            f"{identifier}{SIDECAR_SUFFIX}",
            f"{identifier}{MANIFEST_SUFFIX}",
        )
        try:
            with tempfile.TemporaryDirectory(prefix="ansible-deploy-backup-") as directory:
                workspace = Path(directory)
                archive = workspace / f"{identifier}.tar.gz"
                encrypted = workspace / names[0]
                create_archive(config, archive)
                run(["age", "-r", config["age_recipient"], "-o", str(encrypted), str(archive)])
                digest = checksum(encrypted)
                sidecar = workspace / names[1]
                sidecar.write_text(f"{digest}  {encrypted.name}\n", encoding="ascii")
                manifest = workspace / names[2]
                manifest.write_text(json.dumps({
                    "artifact": encrypted.name,
                    "backup_id": identifier,
                    "sha256": digest,
                    "size": encrypted.stat().st_size,
                }, sort_keys=True) + "\n", encoding="utf-8")
                # The manifest is the completion marker and is uploaded last.
                upload_verified(config, encrypted, encrypted.name)
                upload_verified(config, sidecar, sidecar.name)
                upload_verified(config, manifest, manifest.name)
                removed = apply_retention(config, current=identifier)
                return {
                    "backup_id": identifier, "sha256": digest,
                    "size": encrypted.stat().st_size,
                    "retention_removed": removed, "status": "ok",
                }
        except Exception:
            for name in names:
                _delete_quiet(config, name)
                _delete_quiet(config, f".partial/{name}.partial")
            raise


def safe_members(archive: tarfile.TarFile) -> list[tarfile.TarInfo]:
    members = archive.getmembers()
    for member in members:
        path = PurePosixPath(member.name)
        if (
            path.is_absolute() or ".." in path.parts or not path.parts
            or path.parts[0] not in {"database", "files"}
            or not (member.isfile() or member.isdir())
            or member.uid < 0 or member.gid < 0
            or member.uid > 2**31 - 1 or member.gid > 2**31 - 1
            or member.mode & ~0o777 or member.mtime < 0
        ):
            raise BackupFailure("archive contains an unsafe member or metadata")
    return members


def _safe_destination(root: Path, relative: PurePosixPath) -> Path:
    if not root.is_absolute() or relative.is_absolute() or ".." in relative.parts:
        raise BackupFailure("restore destination is unsafe")
    target = root.joinpath(*relative.parts)
    current = Path(root.anchor)
    for part in target.parts[1:-1]:
        current /= part
        if not current.exists():
            continue
        metadata = current.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise BackupFailure("restore destination contains an unsafe path component")
    if target.exists():
        metadata = target.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            raise BackupFailure("restore destination is a symlink")
        if stat.S_ISREG(metadata.st_mode) and metadata.st_nlink != 1:
            raise BackupFailure("restore destination has multiple hard links")
        if not (stat.S_ISREG(metadata.st_mode) or stat.S_ISDIR(metadata.st_mode)):
            raise BackupFailure("restore destination is not a regular file or directory")
    return target


def _member_destination(
    config: dict[str, Any], source: dict[str, Any], member: tarfile.TarInfo
) -> Path:
    parts = PurePosixPath(member.name).parts
    if len(parts) < 4:
        raise BackupFailure("archive file member has an invalid mapping")
    relative = PurePosixPath(*parts[3:])
    root = Path(source["restore_destination"])
    restore_root = Path(config["restore_root"])
    try:
        root.relative_to(restore_root)
    except ValueError as exc:
        raise BackupFailure("restore destination escapes the configured restore root") from exc
    if source["type"] == "file":
        if len(parts) != 4 or parts[2] != "0":
            raise BackupFailure("archive file source has an invalid mapping")
        return _safe_destination(root.parent, PurePosixPath(root.name))
    if source["type"] == "directory":
        suffix = PurePosixPath(*relative.parts[1:])
        return _safe_destination(root, suffix)
    return _safe_destination(root, relative)


def _apply_metadata(path: Path, member: tarfile.TarInfo) -> None:
    if hasattr(os, "chown"):
        os.chown(path, member.uid, member.gid, follow_symlinks=False)
    try:
        os.chmod(path, member.mode & 0o777, follow_symlinks=False)
        os.utime(path, (member.mtime, member.mtime), follow_symlinks=False)
    except NotImplementedError:
        # Windows test hosts do not support follow_symlinks for these calls. The
        # production runtime is Linux and always takes the race-safe branch above.
        if os.name != "nt":
            raise
        os.chmod(path, member.mode & 0o777)
        os.utime(path, (member.mtime, member.mtime))


def _restore_file(archive: tarfile.TarFile, member: tarfile.TarInfo, target: Path) -> None:
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    target = _safe_destination(target.parent, PurePosixPath(target.name))
    extracted = archive.extractfile(member)
    if extracted is None:
        raise BackupFailure("unable to read archive member")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(target, flags, 0o600)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise BackupFailure("restore destination is not a safe regular file")
        with os.fdopen(descriptor, "wb", closefd=False) as output:
            shutil.copyfileobj(extracted, output)
    finally:
        os.close(descriptor)
    _apply_metadata(target, member)


def restore(config: dict[str, Any], identifier: str) -> dict[str, Any]:
    if not _valid_id(identifier) or identifier not in remote_ids(config):
        raise BackupFailure("backup is not a complete verified set")
    with tempfile.TemporaryDirectory(prefix="ansible-deploy-restore-") as directory:
        workspace = Path(directory)
        encrypted = workspace / f"{identifier}{ARTIFACT_SUFFIX}"
        sidecar = workspace / f"{identifier}{SIDECAR_SUFFIX}"
        manifest_path = workspace / f"{identifier}{MANIFEST_SUFFIX}"
        archive_path = workspace / f"{identifier}.tar.gz"
        for item in (encrypted, sidecar, manifest_path):
            _rclone(config, "copyto", _remote_path(config, item.name), str(item))
        expected = sidecar.read_text(encoding="ascii").split()[0]
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise BackupFailure("backup manifest verification failed") from exc
        if (
            re.fullmatch(r"[0-9a-f]{64}", expected) is None
            or manifest.get("backup_id") != identifier
            or manifest.get("artifact") != encrypted.name
            or manifest.get("sha256") != expected
            or manifest.get("size") != encrypted.stat().st_size
            or checksum(encrypted) != expected
        ):
            raise BackupFailure("backup checksum verification failed")
        run(["age", "-d", "-i", config["age_identity"], "-o", str(archive_path), str(encrypted)])
        with tarfile.open(archive_path, "r:gz") as archive:
            members = safe_members(archive)
            directories: list[tuple[Path, tarfile.TarInfo]] = []
            for member in members:
                path = PurePosixPath(member.name)
                if path.parts[0] != "files":
                    continue
                try:
                    source_index = int(path.parts[1])
                    source = config["include"][source_index]
                except (IndexError, ValueError) as exc:
                    raise BackupFailure("archive file source index is invalid") from exc
                if source["type"] == "postgres":
                    raise BackupFailure("archive file source mapping is invalid")
                target = _member_destination(config, source, member)
                if member.isdir():
                    target.mkdir(mode=0o700, parents=True, exist_ok=True)
                    directories.append((target, member))
                else:
                    _restore_file(archive, member, target)
            for target, member in sorted(
                directories, key=lambda item: len(item[0].parts), reverse=True
            ):
                _apply_metadata(target, member)
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
                run([
                    "docker", "compose", "-f", config["compose_file"], "exec", "-T",
                    source_config["service"], "psql", "-v", "ON_ERROR_STOP=1", "-U",
                    source_config["user"], "-d", source_config["database"],
                ], stdin=dump)
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
        BackupFailure, OSError, ValueError, KeyError, json.JSONDecodeError, tarfile.TarError,
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
