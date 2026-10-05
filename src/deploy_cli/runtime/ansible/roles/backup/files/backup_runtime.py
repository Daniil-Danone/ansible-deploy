#!/usr/bin/env python3
"""Root-owned backup runtime. Configuration contains no secret values."""

from __future__ import annotations

import argparse
import datetime as dt
import glob
import hashlib
import importlib
import io
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
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Any

BACKUP_STAMP = "%Y-%m-%dT%H%M%SZ"
BACKUP_ID_RE = re.compile(r"^(?P<stamp>\d{4}-\d{2}-\d{2}T\d{6}Z)(?:-[0-9a-f]{16})?$")
ARTIFACT_SUFFIX = ".tar.gz.age"
SIDECAR_SUFFIX = f"{ARTIFACT_SUFFIX}.sha256"
MANIFEST_SUFFIX = ".json"
ARCHIVE_MAPPING = "metadata/source-map.json"


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


def _source_id(source: dict[str, Any]) -> str:
    declaration = json.dumps(source, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(declaration).hexdigest()[:32]


def _archive_source_map(config: dict[str, Any]) -> dict[str, Any]:
    sources: list[dict[str, Any]] = []
    identifiers: set[str] = set()
    for source in config["include"]:
        identifier = _source_id(source)
        if identifier in identifiers:
            raise BackupFailure("backup source declarations must be unique")
        identifiers.add(identifier)
        if source["type"] == "postgres":
            mapped = {
                "database": source["database"],
                "id": identifier,
                "service": source["service"],
                "type": "postgres",
                "user": source["user"],
            }
        else:
            mapped = {
                "id": identifier,
                "restore_destination": source["restore_destination"],
                "type": source["type"],
            }
        sources.append(mapped)
    return {"schema_version": 1, "sources": sources}


def create_archive(config: dict[str, Any], destination: Path) -> None:
    compose = config["compose_file"]
    source_map = _archive_source_map(config)
    path_entries: dict[str, list[tuple[Path, PurePosixPath]]] = {}
    for source in config["include"]:
        if source["type"] != "postgres":
            path_entries[_source_id(source)] = _source_entries(source)
    with tempfile.TemporaryDirectory(prefix="ansible-deploy-backup-") as directory:
        workspace = Path(directory)
        with tarfile.open(destination, "w:gz", format=tarfile.PAX_FORMAT) as archive:
            mapping_bytes = (
                json.dumps(source_map, sort_keys=True, separators=(",", ":")) + "\n"
            ).encode()
            mapping_member = tarfile.TarInfo(ARCHIVE_MAPPING)
            mapping_member.size = len(mapping_bytes)
            mapping_member.mode = 0o600
            mapping_member.mtime = int(dt.datetime.now(dt.UTC).timestamp())
            archive.addfile(mapping_member, io.BytesIO(mapping_bytes))
            for source in config["include"]:
                source_id = _source_id(source)
                if source["type"] == "postgres":
                    dump = workspace / f"{source_id}.sql"
                    run([
                        "docker", "compose", "-f", compose, "exec", "-T",
                        source["service"], "pg_dump", "--clean", "--if-exists",
                        "--no-owner", "--no-privileges", "-U", source["user"],
                        "-d", source["database"],
                    ], stdout=dump)
                    archive.add(dump, arcname=f"database/{source_id}.sql")
                    continue
                for path, relative in path_entries[source_id]:
                    archive.add(
                        path,
                        arcname=str(PurePosixPath("files") / source_id / relative),
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


def _parse_sidecar(value: str | None, artifact_name: str) -> str | None:
    if value is None:
        return None
    match = re.fullmatch(r"([0-9a-f]{64})  ([A-Za-z0-9._-]+)\r?\n?", value)
    if match is None or match.group(2) != artifact_name:
        return None
    return match.group(1)


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
        sidecar_digest = _parse_sidecar(
            _read_remote_text(config, sidecar_name), artifact_name
        )
        if sidecar_digest != manifest["sha256"]:
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
        # Remove the completion marker first. Any later failure leaves a
        # non-listable orphan, never a deceptively complete damaged backup.
        for suffix in (MANIFEST_SUFFIX, ARTIFACT_SUFFIX, SIDECAR_SUFFIX):
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
                lock_api: Any = importlib.import_module("msvcrt")

                stream.seek(0)
                stream.write(b"\0")
                stream.flush()
                stream.seek(0)
                lock_api.locking(stream.fileno(), lock_api.LK_NBLCK, 1)
            else:
                lock_api = importlib.import_module("fcntl")
                lock_api.flock(stream.fileno(), lock_api.LOCK_EX | lock_api.LOCK_NB)
        except (BlockingIOError, OSError) as exc:
            raise BackupFailure("another backup operation is already running") from exc
        try:
            yield
        finally:
            if os.name == "nt":
                lock_api = importlib.import_module("msvcrt")
                stream.seek(0)
                lock_api.locking(stream.fileno(), lock_api.LK_UNLCK, 1)
            else:
                lock_api = importlib.import_module("fcntl")
                lock_api.flock(stream.fileno(), lock_api.LOCK_UN)


def backup(config: dict[str, Any]) -> dict[str, Any]:
    with _backup_lock(config):
        identifier = _new_backup_id()
        names = (
            f"{identifier}{ARTIFACT_SUFFIX}",
            f"{identifier}{SIDECAR_SUFFIX}",
            f"{identifier}{MANIFEST_SUFFIX}",
        )
        completed = False
        try:
            with tempfile.TemporaryDirectory(prefix="ansible-deploy-backup-") as directory:
                workspace = Path(directory)
                archive = workspace / f"{identifier}.tar.gz"
                encrypted = workspace / names[0]
                create_archive(config, archive)
                # Re-open and validate the finished archive. This closes the
                # preflight-to-tar.add race before encryption or remote writes.
                with tarfile.open(archive, "r:gz") as prepared:
                    prepared_members = safe_members(prepared)
                    prepared_map = _load_source_map(prepared, prepared_members)
                    _validate_mapped_members(prepared_members, prepared_map)
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
                completed = True
                removed = apply_retention(config, current=identifier)
                return {
                    "backup_id": identifier, "sha256": digest,
                    "size": encrypted.stat().st_size,
                    "retention_removed": removed, "status": "ok",
                }
        except Exception:
            if not completed:
                for name in names:
                    _delete_quiet(config, name)
                    _delete_quiet(config, f".partial/{name}.partial")
            raise


def safe_members(archive: tarfile.TarFile) -> list[tarfile.TarInfo]:
    members = archive.getmembers()
    for member in members:
        path = PurePosixPath(member.name)
        metadata_member = member.name == ARCHIVE_MAPPING and member.isfile()
        if (
            path.is_absolute() or ".." in path.parts or not path.parts
            or (path.parts[0] not in {"database", "files"} and not metadata_member)
            or (path.parts[0] == "metadata" and not metadata_member)
            or not (member.isfile() or member.isdir())
            or member.uid < 0 or member.gid < 0
            or member.uid > 2**31 - 1 or member.gid > 2**31 - 1
            or member.mode & ~0o777 or member.mtime < 0
        ):
            raise BackupFailure("archive contains an unsafe member or metadata")
    return members


def _safe_relative_destination(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    path = PurePosixPath(value)
    return (
        not path.is_absolute()
        and value == str(path)
        and value not in {"", "."}
        and all(part not in {"", ".", ".."} for part in path.parts)
    )


def _load_source_map(
    archive: tarfile.TarFile, members: list[tarfile.TarInfo]
) -> dict[str, dict[str, Any]]:
    mapping_members = [member for member in members if member.name == ARCHIVE_MAPPING]
    if len(mapping_members) != 1 or mapping_members[0].size > 1024 * 1024:
        raise BackupFailure("archive source mapping is missing or ambiguous")
    stream = archive.extractfile(mapping_members[0])
    if stream is None:
        raise BackupFailure("archive source mapping is unreadable")
    try:
        document = json.loads(stream.read().decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise BackupFailure("archive source mapping is malformed") from exc
    if (
        not isinstance(document, dict)
        or set(document) != {"schema_version", "sources"}
        or document.get("schema_version") != 1
        or not isinstance(document.get("sources"), list)
        or not document["sources"]
    ):
        raise BackupFailure("archive source mapping has an unsupported schema")
    mapped: dict[str, dict[str, Any]] = {}
    for source in document["sources"]:
        if not isinstance(source, dict):
            raise BackupFailure("archive source mapping contains an invalid entry")
        identifier = source.get("id")
        source_type = source.get("type")
        if not isinstance(identifier, str) or re.fullmatch(r"[0-9a-f]{32}", identifier) is None:
            raise BackupFailure("archive source mapping contains an invalid identifier")
        if identifier in mapped:
            raise BackupFailure("archive source mapping contains duplicate identifiers")
        if source_type == "postgres":
            if (
                set(source) != {"database", "id", "service", "type", "user"}
                or not isinstance(source.get("service"), str)
                or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", source["service"])
                is None
                or any(
                    not isinstance(source.get(field), str)
                    or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$-]{0,62}", source[field])
                    is None
                    for field in ("database", "user")
                )
            ):
                raise BackupFailure("archive PostgreSQL mapping is invalid")
        elif source_type in {"file", "directory", "glob"}:
            valid_destination = _safe_relative_destination(
                source.get("restore_destination")
            )
            if set(source) != {"id", "restore_destination", "type"} or not valid_destination:
                raise BackupFailure("archive file mapping is invalid")
        else:
            raise BackupFailure("archive source mapping contains an unsupported type")
        mapped[identifier] = source
    return mapped


def _validate_mapped_members(
    members: list[tarfile.TarInfo], source_map: dict[str, dict[str, Any]]
) -> None:
    represented: set[str] = set()
    database_ids: set[str] = set()
    member_names: set[str] = set()
    destinations: set[tuple[str, str]] = set()
    rooted_sources: set[str] = set()
    for member in members:
        path = PurePosixPath(member.name)
        if member.name in member_names:
            raise BackupFailure("archive contains duplicate member names")
        member_names.add(member.name)
        if path.parts[0] == "files":
            if len(path.parts) < 4:
                raise BackupFailure("archive file member has an invalid mapping")
            source = source_map.get(path.parts[1])
            if source is None or source["type"] == "postgres":
                raise BackupFailure("archive file source identifier is unknown")
            candidate = path.parts[2]
            if not candidate.isdigit() or str(int(candidate)) != candidate:
                raise BackupFailure("archive file candidate identifier is invalid")
            relative = PurePosixPath(*path.parts[3:])
            if source["type"] == "file":
                if candidate != "0" or len(relative.parts) != 1 or not member.isfile():
                    raise BackupFailure("archive file source has invalid contents")
                destination_key = "."
                rooted_sources.add(source["id"])
            elif source["type"] == "directory":
                if candidate != "0":
                    raise BackupFailure("archive directory source has invalid contents")
                destination_key = str(PurePosixPath(*relative.parts[1:]))
                if destination_key == ".":
                    if not member.isdir():
                        raise BackupFailure("archive directory root is not a directory")
                    rooted_sources.add(source["id"])
            else:
                destination_key = str(relative)
            mapped_destination = (source["id"], destination_key)
            if mapped_destination in destinations:
                raise BackupFailure("archive file members collide at restore destination")
            destinations.add(mapped_destination)
            represented.add(source["id"])
        elif path.parts[0] == "database":
            if len(path.parts) != 2 or not path.name.endswith(".sql"):
                raise BackupFailure("archive database member has an invalid mapping")
            source_id = path.name[:-4]
            source = source_map.get(source_id)
            if source is None or source["type"] != "postgres" or source_id in database_ids:
                raise BackupFailure("archive database source mapping is invalid")
            database_ids.add(source_id)
            represented.add(source_id)
    if represented != set(source_map):
        raise BackupFailure("archive contents do not match authenticated source mapping")
    required_roots = {
        identifier
        for identifier, source in source_map.items()
        if source["type"] in {"file", "directory"}
    }
    if rooted_sources != required_roots:
        raise BackupFailure("archive is missing a mapped file or directory root")


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
    if len(parts) < 4 or parts[1] != source["id"]:
        raise BackupFailure("archive file member has an invalid mapping")
    relative = PurePosixPath(*parts[3:])
    restore_root = Path(config["restore_root"])
    root = restore_root.joinpath(*PurePosixPath(source["restore_destination"]).parts)
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
    # Do not truncate until the opened descriptor itself has passed the
    # regular-file and single-link checks; this closes the validation/open race.
    flags = os.O_WRONLY | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(target, flags, 0o600)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise BackupFailure("restore destination is not a safe regular file")
        os.ftruncate(descriptor, 0)
        os.lseek(descriptor, 0, os.SEEK_SET)
        with os.fdopen(descriptor, "wb", closefd=False) as output:
            shutil.copyfileobj(extracted, output)
    finally:
        os.close(descriptor)
    _apply_metadata(target, member)


def _wait_for_postgres(config: dict[str, Any], source: dict[str, Any]) -> None:
    last_error: BackupFailure | None = None
    for attempt in range(30):
        try:
            run([
                "docker", "compose", "-f", config["compose_file"], "exec", "-T",
                source["service"], "pg_isready", "-U", source["user"],
                "-d", source["database"],
            ])
            return
        except BackupFailure as exc:
            last_error = exc
            if attempt < 29:
                time.sleep(2)
    raise BackupFailure("PostgreSQL did not become ready for restore") from last_error


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
        try:
            expected = _parse_sidecar(
                sidecar.read_text(encoding="ascii"), encrypted.name
            )
        except (OSError, UnicodeError) as exc:
            raise BackupFailure("backup checksum sidecar is unreadable") from exc
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise BackupFailure("backup manifest verification failed") from exc
        if (
            expected is None
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
            source_map = _load_source_map(archive, members)
            _validate_mapped_members(members, source_map)
            restored_sources: set[str] = set()
            directories: list[tuple[Path, tarfile.TarInfo]] = []
            for member in members:
                path = PurePosixPath(member.name)
                if path.parts[0] != "files":
                    continue
                source = source_map.get(path.parts[1]) if len(path.parts) > 1 else None
                if source is None:
                    raise BackupFailure("archive file source identifier is unknown")
                if source["type"] == "postgres":
                    raise BackupFailure("archive file source mapping is invalid")
                restored_sources.add(source["id"])
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
            dumps: dict[str, tarfile.TarInfo] = {}
            for member in members:
                path = PurePosixPath(member.name)
                if path.parts[0] != "database":
                    continue
                if len(path.parts) != 2 or not path.name.endswith(".sql"):
                    raise BackupFailure("archive database member has an invalid mapping")
                source_id = path.name[:-4]
                source = source_map.get(source_id)
                if source is None or source["type"] != "postgres" or source_id in dumps:
                    raise BackupFailure("archive database source mapping is invalid")
                dumps[source_id] = member
            expected_sources = set(source_map)
            if restored_sources | set(dumps) != expected_sources:
                raise BackupFailure("archive contents do not match authenticated source mapping")
            for source_id, member in dumps.items():
                source_config = source_map[source_id]
                dump = workspace / Path(member.name).name
                extracted = archive.extractfile(member)
                if extracted is None:
                    raise BackupFailure("unable to read database dump")
                with dump.open("wb") as output:
                    shutil.copyfileobj(extracted, output)
                _wait_for_postgres(config, source_config)
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
