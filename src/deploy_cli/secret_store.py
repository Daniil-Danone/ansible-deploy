import ctypes
import os
import stat
import sys
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from .secret_file import (
    SecretFileError,
    secure_secret_permissions,
    validate_secret_permissions,
    windows_current_sid,
)

PROJECT_ID_RELATIVE_PATH = Path(".deploy/project-id")
EXTERNAL_STORE_ENV = "ANSIBLE_DEPLOY_SECRETS_DIR"
_IDENTITY_NAME = "project-id"
_MAX_IDENTITY_SIZE = 38
_O_DIRECTORY = int(getattr(os, "O_DIRECTORY", 0))
_O_NOFOLLOW = int(getattr(os, "O_NOFOLLOW", 0))


class SecretStoreError(ValueError):
    """Raised when project identity or secret-store location is unsafe."""


def _windows_library(name: str) -> Any:
    windows_ctypes: Any = ctypes
    return windows_ctypes.WinDLL(name, use_last_error=True)


def _windows_last_error() -> int:
    windows_ctypes: Any = ctypes
    return int(windows_ctypes.get_last_error())


@dataclass(frozen=True)
class ExternalSecretLocation:
    root: Path
    trusted_base: Path
    validate_trusted_base: bool


def _is_reparse(metadata: os.stat_result) -> bool:
    attributes = getattr(metadata, "st_file_attributes", 0)
    return bool(attributes & 0x00000400)


def _portable_relative_name(configured: Path) -> PurePosixPath:
    value = configured.as_posix()
    posix = PurePosixPath(value)
    windows = PureWindowsPath(value)
    if (
        not value
        or value == "."
        or posix.is_absolute()
        or windows.is_absolute()
        or bool(windows.drive)
        or value != str(posix)
        or any(part in {"", ".", ".."} for part in posix.parts)
    ):
        raise SecretStoreError("External file name must be a normalized relative path")
    return posix


def _current_uid() -> int:
    getuid = getattr(os, "getuid", None)
    if getuid is None:
        raise SecretStoreError("Unable to resolve the current filesystem owner")
    return int(getuid())


def _close_posix_descriptors(descriptors: list[int]) -> None:
    failed = False
    for descriptor in reversed(descriptors):
        try:
            os.close(descriptor)
        except OSError:
            failed = True
    if failed:
        raise SecretStoreError("Unable to close external security descriptor") from None


def _close_windows_security_handles(handles: list[int]) -> None:
    failed = False
    for handle in reversed(handles):
        try:
            _windows_close(handle)
        except OSError:
            failed = True
    if failed:
        raise SecretStoreError("Unable to close external security handle") from None


def _validate_posix_metadata(
    metadata: os.stat_result,
    *,
    directory: bool,
    secret: bool,
    owner_only_directory: bool = False,
) -> None:
    expected_type = stat.S_ISDIR if directory else stat.S_ISREG
    if not expected_type(metadata.st_mode):
        raise SecretStoreError("External path component has an invalid file type")
    if metadata.st_uid != _current_uid():
        raise SecretStoreError("External path components must be owned by the current user")
    mode = stat.S_IMODE(metadata.st_mode)
    if directory:
        forbidden = 0o077 if owner_only_directory else 0o022
        if mode & forbidden:
            requirement = "owner-only" if owner_only_directory else "not group/other writable"
            raise SecretStoreError(f"External directories must be {requirement}")
    elif secret:
        if mode & 0o077:
            raise SecretStoreError("External secret file permissions are not owner-only")
        if metadata.st_nlink != 1:
            raise SecretStoreError("External secret file must not have hardlink aliases")
    elif mode & 0o022:
        raise SecretStoreError("External public file must not be group/other writable")


def _validate_posix_ancestry(
    root: Path,
    relative: PurePosixPath,
    *,
    secret: bool,
    require_file: bool,
) -> None:
    descriptors: list[int] = []
    try:
        try:
            root_descriptor = os.open(root, _posix_directory_flags())
        except FileNotFoundError:
            if require_file:
                raise SecretStoreError("Required external file is missing") from None
            return
        descriptors.append(root_descriptor)
        _validate_posix_metadata(
            os.fstat(root_descriptor),
            directory=True,
            secret=False,
            owner_only_directory=True,
        )
        parent = root_descriptor
        for index, part in enumerate(relative.parts):
            final = index == len(relative.parts) - 1
            flags = os.O_RDONLY | _O_NOFOLLOW | (0 if final else _O_DIRECTORY)
            try:
                descriptor = os.open(part, flags, dir_fd=parent)
            except FileNotFoundError:
                if require_file:
                    raise SecretStoreError("Required external file is missing") from None
                return
            except OSError:
                raise SecretStoreError(
                    "External path contains an unsafe link or component"
                ) from None
            descriptors.append(descriptor)
            _validate_posix_metadata(
                os.fstat(descriptor),
                directory=not final,
                secret=secret and final,
                owner_only_directory=not final,
            )
            parent = descriptor
    finally:
        _close_posix_descriptors(descriptors)


def _validate_windows_component(path: Path, *, directory: bool, secret: bool) -> int:
    try:
        handle = _windows_open(path, directory=directory)
    except FileNotFoundError:
        raise
    except OSError:
        raise SecretStoreError("Unable to open external path component safely") from None
    try:
        _windows_validate_handle(handle, directory=directory)
        if _windows_owner_sid(handle) != windows_current_sid():
            raise SecretStoreError("External path components must be owned by the current user")
        validate_secret_permissions(path)
        if not directory and secret:
            metadata = path.stat()
            if metadata.st_nlink != 1:
                raise SecretStoreError("External secret file must not have hardlink aliases")
        return handle
    except (OSError, SecretFileError):
        _windows_close(handle)
        raise SecretStoreError("External path permissions are not owner-only") from None
    except Exception:
        _windows_close(handle)
        raise


def _validate_windows_ancestry(
    root: Path,
    relative: PurePosixPath,
    *,
    secret: bool,
    require_file: bool,
) -> None:
    handles: list[int] = []
    try:
        try:
            handles.append(_validate_windows_component(root, directory=True, secret=False))
        except FileNotFoundError:
            if require_file:
                raise SecretStoreError("Required external file is missing") from None
            return
        current = root
        for index, part in enumerate(relative.parts):
            current /= part
            final = index == len(relative.parts) - 1
            try:
                handles.append(
                    _validate_windows_component(
                        current, directory=not final, secret=secret and final
                    )
                )
            except FileNotFoundError:
                if require_file:
                    raise SecretStoreError("Required external file is missing") from None
                return
    finally:
        _close_windows_security_handles(handles)


def _validate_external_ancestry(
    root: Path,
    relative: PurePosixPath,
    *,
    secret: bool,
    require_file: bool,
) -> None:
    if os.name == "nt":
        _validate_windows_ancestry(
            root, relative, secret=secret, require_file=require_file
        )
    else:
        _validate_posix_ancestry(
            root, relative, secret=secret, require_file=require_file
        )


def _validate_posix_directory_chain(
    base: Path,
    relative: PurePosixPath,
    *,
    validate_base: bool,
    require_root: bool,
) -> None:
    descriptors: list[int] = []
    try:
        try:
            base_descriptor = os.open(base, _posix_directory_flags())
        except (FileNotFoundError, OSError):
            raise SecretStoreError("External secret trusted base is unavailable") from None
        descriptors.append(base_descriptor)
        if validate_base:
            _validate_posix_metadata(os.fstat(base_descriptor), directory=True, secret=False)
        parent = base_descriptor
        for part in relative.parts:
            try:
                descriptor = os.open(
                    part, _posix_directory_flags(), dir_fd=parent
                )
            except FileNotFoundError:
                if require_root:
                    raise SecretStoreError("External secret root is missing") from None
                return
            except OSError:
                raise SecretStoreError("External secret ancestry is unsafe") from None
            descriptors.append(descriptor)
            _validate_posix_metadata(
                os.fstat(descriptor),
                directory=True,
                secret=False,
                owner_only_directory=True,
            )
            parent = descriptor
    finally:
        _close_posix_descriptors(descriptors)


def _validate_windows_directory_chain(
    base: Path,
    relative: PurePosixPath,
    *,
    validate_base: bool,
    require_root: bool,
) -> None:
    handles: list[int] = []
    try:
        if validate_base:
            try:
                handles.append(
                    _validate_windows_component(base, directory=True, secret=False)
                )
            except FileNotFoundError:
                raise SecretStoreError(
                    "External secret trusted base is unavailable"
                ) from None
        current = base
        for part in relative.parts:
            current /= part
            try:
                handles.append(
                    _validate_windows_component(current, directory=True, secret=False)
                )
            except FileNotFoundError:
                if require_root:
                    raise SecretStoreError("External secret root is missing") from None
                return
    finally:
        _close_windows_security_handles(handles)


def validate_external_root_ancestry(
    location: ExternalSecretLocation, *, require_root: bool
) -> None:
    try:
        lexical = location.root.relative_to(location.trusted_base)
    except ValueError:
        raise SecretStoreError("External secret root escapes its trusted base") from None
    relative = PurePosixPath(lexical.as_posix())
    if os.name == "nt":
        _validate_windows_directory_chain(
            location.trusted_base,
            relative,
            validate_base=location.validate_trusted_base,
            require_root=require_root,
        )
    else:
        _validate_posix_directory_chain(
            location.trusted_base,
            relative,
            validate_base=location.validate_trusted_base,
            require_root=require_root,
        )


def _git_worktree_root(project: Path) -> Path:
    for current in (project, *project.parents):
        marker = current / ".git"
        try:
            metadata = os.lstat(marker)
        except FileNotFoundError:
            continue
        except OSError:
            raise SecretStoreError("Unable to validate Git worktree boundary") from None
        if stat.S_ISLNK(metadata.st_mode) or _is_reparse(metadata):
            raise SecretStoreError("Git worktree metadata must not be a link")
        if not (stat.S_ISDIR(metadata.st_mode) or stat.S_ISREG(metadata.st_mode)):
            raise SecretStoreError("Git worktree metadata has an invalid file type")
        return current
    return project


def _reject_root_project_overlap(project: Path, canonical_root: Path) -> Path:
    worktree = _git_worktree_root(project)
    try:
        canonical_root.relative_to(worktree)
    except ValueError:
        pass
    else:
        raise SecretStoreError("External secret root must be outside the Git worktree")
    try:
        worktree.relative_to(canonical_root)
    except ValueError:
        return worktree
    raise SecretStoreError("External secret root must not contain the Git worktree")


def resolve_external_file(
    project_dir: Path,
    configured: Path,
    *,
    secret: bool = True,
) -> Path:
    """Resolve a schema-v2 portable name without exposing it in validation errors."""
    project = project_dir.resolve()
    relative = _portable_relative_name(configured)
    location = external_secret_location(project)
    root = location.root
    canonical_root = root.resolve(strict=False)
    worktree = _reject_root_project_overlap(project, canonical_root)
    validate_external_root_ancestry(location, require_root=False)
    candidate = root.joinpath(*relative.parts)
    canonical = candidate.resolve(strict=False)
    try:
        canonical.relative_to(canonical_root)
    except ValueError:
        raise SecretStoreError("External file path escapes the project secret root") from None
    try:
        canonical.relative_to(worktree)
    except ValueError:
        pass
    else:
        raise SecretStoreError("External file path must not enter the Git worktree")
    _validate_external_ancestry(
        root, relative, secret=secret, require_file=False
    )
    return candidate


def validate_external_file_for_use(
    project_dir: Path,
    root: Path,
    path: Path,
    *,
    secret: bool = True,
    trusted_base: Path | None = None,
    validate_trusted_base: bool = True,
) -> None:
    """Revalidate a v2 path immediately before read/mount.

    The boundary excludes a same-user attacker: that user can already read or replace
    owner-only secrets. Descriptor/handle checks plus owner-only ancestry prevent a
    different local user from substituting a path between configuration load and use.
    """
    project = project_dir.resolve()
    location = ExternalSecretLocation(
        root,
        root.parent if trusted_base is None else trusted_base,
        validate_trusted_base,
    )
    canonical_root = root.resolve(strict=False)
    worktree = _reject_root_project_overlap(project, canonical_root)
    validate_external_root_ancestry(location, require_root=True)
    try:
        relative_path = path.relative_to(root)
    except ValueError:
        raise SecretStoreError("External file is outside its configured root") from None
    relative = _portable_relative_name(relative_path)
    canonical = path.resolve(strict=False)
    try:
        canonical.relative_to(canonical_root)
    except ValueError:
        raise SecretStoreError("External file path escapes its configured root") from None
    try:
        canonical.relative_to(worktree)
    except ValueError:
        pass
    else:
        raise SecretStoreError("External file path must not enter the Git worktree")
    _validate_external_ancestry(root, relative, secret=secret, require_file=True)


def ensure_external_parent_for_write(
    project_dir: Path,
    root: Path,
    path: Path,
    *,
    trusted_base: Path,
    validate_trusted_base: bool,
) -> None:
    """Create only owner-controlled directories below a validated trust anchor."""
    project = project_dir.resolve()
    canonical_root = root.resolve(strict=False)
    _reject_root_project_overlap(project, canonical_root)
    try:
        relative_parent = path.parent.relative_to(root)
    except ValueError:
        raise SecretStoreError("External write target is outside its configured root") from None
    location = ExternalSecretLocation(root, trusted_base, validate_trusted_base)
    validate_external_root_ancestry(location, require_root=False)

    try:
        root_parts = root.relative_to(trusted_base).parts
    except ValueError:
        raise SecretStoreError("External secret root escapes its trusted base") from None
    current = trusted_base
    for part in (*root_parts, *relative_parent.parts):
        current /= part
        if current.exists():
            continue
        try:
            # Python gives mode=0700 special ACL semantics on modern Windows;
            # inherit the already-private parent first, then install our one-ACE DACL.
            current.mkdir(mode=0o777 if os.name == "nt" else 0o700)
            secure_secret_permissions(current)
        except (OSError, SecretFileError):
            raise SecretStoreError("Unable to create protected external directory") from None

    validate_external_root_ancestry(location, require_root=True)
    parent_location = ExternalSecretLocation(path.parent, root, True)
    validate_external_root_ancestry(parent_location, require_root=True)


def _after_directory_open(path: Path) -> None:
    """Test seam executed only after the directory is held against replacement."""


def _parse_project_id(raw: bytes) -> uuid.UUID:
    if raw.endswith(b"\r\n"):
        normalized = raw[:-2] + b"\n"
    else:
        normalized = raw
    if len(normalized) != 37:
        raise SecretStoreError("Project identity is not a canonical UUID")
    try:
        value = normalized.decode("ascii").removesuffix("\n")
    except UnicodeError:
        raise SecretStoreError("Project identity is not a canonical UUID") from None
    if normalized != f"{value}\n".encode():
        raise SecretStoreError("Project identity is not a canonical UUID")
    try:
        project_id = uuid.UUID(value)
    except ValueError:
        raise SecretStoreError("Project identity is not a canonical UUID") from None
    if str(project_id) != value:
        raise SecretStoreError("Project identity is not a canonical UUID")
    return project_id


def _read_bounded(descriptor: int) -> bytes:
    try:
        return os.read(descriptor, _MAX_IDENTITY_SIZE + 1)
    except OSError:
        raise SecretStoreError("Unable to read project identity") from None


def _write_all(descriptor: int, content: bytes) -> None:
    written = 0
    while written < len(content):
        count = os.write(descriptor, content[written:])
        if count <= 0:
            raise OSError("Unable to write complete project identity")
        written += count


def _posix_directory_flags() -> int:
    if not _O_DIRECTORY or not _O_NOFOLLOW:
        raise SecretStoreError("This platform cannot safely open the project metadata directory")
    return os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW


@contextmanager
def _posix_deploy_directory(project: Path, *, create: bool) -> Iterator[int]:
    project_descriptor = -1
    deploy_descriptor = -1
    try:
        project_descriptor = os.open(project, _posix_directory_flags())
        if create:
            try:
                os.mkdir(".deploy", dir_fd=project_descriptor)
            except FileExistsError:
                pass
        try:
            deploy_descriptor = os.open(
                ".deploy", _posix_directory_flags(), dir_fd=project_descriptor
            )
        except FileNotFoundError:
            raise SecretStoreError("Project identity is missing; use --new-project-id") from None
        _after_directory_open(project / ".deploy")
        yield deploy_descriptor
    except SecretStoreError:
        raise
    except OSError:
        raise SecretStoreError(
            "Project metadata directory must not be a symlink or reparse point"
        ) from None
    finally:
        if deploy_descriptor >= 0:
            os.close(deploy_descriptor)
        if project_descriptor >= 0:
            os.close(project_descriptor)


def _posix_load_project_id(project: Path) -> uuid.UUID:
    with _posix_deploy_directory(project, create=False) as deploy_descriptor:
        try:
            identity_descriptor = os.open(
                _IDENTITY_NAME,
                os.O_RDONLY | _O_NOFOLLOW,
                dir_fd=deploy_descriptor,
            )
        except FileNotFoundError:
            raise SecretStoreError("Project identity is missing; use --new-project-id") from None
        except OSError:
            raise SecretStoreError(
                "Project identity path must not be a symlink or reparse point"
            ) from None
        try:
            if not stat.S_ISREG(os.fstat(identity_descriptor).st_mode):
                raise SecretStoreError("Project identity must be a regular file")
            return _parse_project_id(_read_bounded(identity_descriptor))
        finally:
            os.close(identity_descriptor)


def _posix_create_project_id(project: Path) -> uuid.UUID:
    with _posix_deploy_directory(project, create=True) as deploy_descriptor:
        try:
            current = os.stat(_IDENTITY_NAME, dir_fd=deploy_descriptor, follow_symlinks=False)
        except FileNotFoundError:
            current = None
        except OSError:
            raise SecretStoreError("Unable to validate project identity path") from None
        if current is not None and not stat.S_ISREG(current.st_mode):
            raise SecretStoreError("Project identity path must be a regular file")

        project_id = uuid.uuid4()
        temporary_name = f".project-id-{project_id}.tmp"
        temporary_descriptor = -1
        try:
            temporary_descriptor = os.open(
                temporary_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW,
                0o644,
                dir_fd=deploy_descriptor,
            )
            _write_all(temporary_descriptor, f"{project_id}\n".encode())
            os.fsync(temporary_descriptor)
            os.close(temporary_descriptor)
            temporary_descriptor = -1
            os.replace(
                temporary_name,
                _IDENTITY_NAME,
                src_dir_fd=deploy_descriptor,
                dst_dir_fd=deploy_descriptor,
            )
        except OSError:
            try:
                os.unlink(temporary_name, dir_fd=deploy_descriptor)
            except OSError:
                raise SecretStoreError(
                    "Unable to write project identity and rollback failed"
                ) from None
            raise SecretStoreError("Unable to write project identity") from None
        finally:
            if temporary_descriptor >= 0:
                os.close(temporary_descriptor)
        return project_id


class _FileAttributeTagInfo(ctypes.Structure):
    _fields_ = [("file_attributes", wintypes.DWORD), ("reparse_tag", wintypes.DWORD)]


def _windows_open(path: Path, *, directory: bool) -> int:
    kernel32 = _windows_library("kernel32")
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create_file.restype = wintypes.HANDLE
    generic_read = 0x80000000
    share_read = 0x00000001
    share_write = 0x00000002
    open_existing = 3
    open_reparse_point = 0x00200000
    backup_semantics = 0x02000000 if directory else 0
    handle = create_file(
        str(path),
        generic_read,
        share_read | share_write if directory else share_read,
        None,
        open_existing,
        open_reparse_point | backup_semantics,
        None,
    )
    invalid_handle = ctypes.c_void_p(-1).value
    if handle == invalid_handle:
        error = _windows_last_error()
        if error in {2, 3}:
            raise FileNotFoundError(path)
        raise OSError(error, f"Unable to open {path.name}")
    return int(handle)


def _windows_close(handle: int) -> None:
    kernel32 = _windows_library("kernel32")
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL
    if not close_handle(wintypes.HANDLE(handle)):
        raise OSError(_windows_last_error(), "Unable to close Windows file handle")


def _windows_attributes(handle: int) -> int:
    kernel32 = _windows_library("kernel32")
    get_information = kernel32.GetFileInformationByHandleEx
    get_information.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    ]
    get_information.restype = wintypes.BOOL
    information = _FileAttributeTagInfo()
    file_attribute_tag_info = 9
    if not get_information(
        wintypes.HANDLE(handle),
        file_attribute_tag_info,
        ctypes.byref(information),
        ctypes.sizeof(information),
    ):
        raise OSError(_windows_last_error(), "Unable to inspect Windows file handle")
    return int(information.file_attributes)


def _windows_owner_sid(handle: int) -> str:
    advapi32 = _windows_library("advapi32")
    kernel32 = _windows_library("kernel32")
    owner = ctypes.c_void_p()
    descriptor = ctypes.c_void_p()
    get_security = advapi32.GetSecurityInfo
    get_security.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    get_security.restype = wintypes.DWORD
    se_file_object = 1
    owner_security_information = 0x00000001
    result = get_security(
        wintypes.HANDLE(handle),
        se_file_object,
        owner_security_information,
        ctypes.byref(owner),
        None,
        None,
        None,
        ctypes.byref(descriptor),
    )
    if result != 0:
        raise OSError(result, "Unable to inspect external path owner")
    sid_text = wintypes.LPWSTR()
    convert_sid = advapi32.ConvertSidToStringSidW
    convert_sid.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
    convert_sid.restype = wintypes.BOOL
    local_free = kernel32.LocalFree
    local_free.argtypes = [wintypes.HLOCAL]
    local_free.restype = wintypes.HLOCAL
    try:
        if not convert_sid(owner, ctypes.byref(sid_text)):
            raise OSError(_windows_last_error(), "Unable to format external path owner")
        value = sid_text.value
        if value is None:
            raise OSError("Unable to format external path owner")
        return value
    finally:
        if sid_text:
            local_free(ctypes.cast(sid_text, wintypes.HLOCAL))
        if descriptor:
            local_free(ctypes.cast(descriptor, wintypes.HLOCAL))


def _windows_validate_handle(handle: int, *, directory: bool) -> None:
    attributes = _windows_attributes(handle)
    reparse_point = 0x00000400
    directory_attribute = 0x00000010
    if attributes & reparse_point:
        raise SecretStoreError("Project identity path must not be a symlink or reparse point")
    if bool(attributes & directory_attribute) != directory:
        kind = "metadata directory" if directory else "identity"
        raise SecretStoreError(f"Project {kind} has an invalid file type")


@contextmanager
def _windows_deploy_directory(project: Path, *, create: bool) -> Iterator[int]:
    deploy_dir = project / ".deploy"
    if create:
        try:
            deploy_dir.mkdir()
        except FileExistsError:
            pass
        except OSError:
            raise SecretStoreError("Unable to create project metadata directory") from None
    try:
        handle = _windows_open(deploy_dir, directory=True)
    except FileNotFoundError:
        raise SecretStoreError("Project identity is missing; use --new-project-id") from None
    except OSError:
        raise SecretStoreError("Unable to open project metadata directory safely") from None
    try:
        _windows_validate_handle(handle, directory=True)
        _after_directory_open(deploy_dir)
        yield handle
    finally:
        _windows_close(handle)


def _windows_load_project_id(project: Path) -> uuid.UUID:
    identity = project / PROJECT_ID_RELATIVE_PATH
    with _windows_deploy_directory(project, create=False):
        try:
            handle = _windows_open(identity, directory=False)
        except FileNotFoundError:
            raise SecretStoreError("Project identity is missing; use --new-project-id") from None
        except OSError:
            raise SecretStoreError("Unable to open project identity safely") from None
        try:
            _windows_validate_handle(handle, directory=False)
            buffer = ctypes.create_string_buffer(_MAX_IDENTITY_SIZE + 1)
            read = wintypes.DWORD()
            kernel32 = _windows_library("kernel32")
            read_file = kernel32.ReadFile
            read_file.argtypes = [
                wintypes.HANDLE,
                wintypes.LPVOID,
                wintypes.DWORD,
                ctypes.POINTER(wintypes.DWORD),
                wintypes.LPVOID,
            ]
            read_file.restype = wintypes.BOOL
            if not read_file(
                wintypes.HANDLE(handle),
                buffer,
                len(buffer),
                ctypes.byref(read),
                None,
            ):
                raise OSError(_windows_last_error(), "Unable to read project identity")
            return _parse_project_id(buffer.raw[: read.value])
        except OSError:
            raise SecretStoreError("Unable to read project identity") from None
        finally:
            _windows_close(handle)


def _windows_existing_identity_is_safe(identity: Path) -> None:
    try:
        handle = _windows_open(identity, directory=False)
    except FileNotFoundError:
        return
    except OSError:
        raise SecretStoreError("Unable to validate project identity path") from None
    try:
        _windows_validate_handle(handle, directory=False)
    finally:
        _windows_close(handle)


def _windows_create_project_id(project: Path) -> uuid.UUID:
    deploy_dir = project / ".deploy"
    identity = project / PROJECT_ID_RELATIVE_PATH
    with _windows_deploy_directory(project, create=True):
        _windows_existing_identity_is_safe(identity)
        project_id = uuid.uuid4()
        temporary = deploy_dir / f".project-id-{project_id}.tmp"
        try:
            with temporary.open("xb") as stream:
                stream.write(f"{project_id}\n".encode())
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, identity)
        except OSError:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                raise SecretStoreError(
                    "Unable to write project identity and rollback failed"
                ) from None
            raise SecretStoreError("Unable to write project identity") from None
        return project_id


def load_project_id(project_dir: Path) -> uuid.UUID:
    project = project_dir.resolve()
    if sys.platform == "win32":
        return _windows_load_project_id(project)
    return _posix_load_project_id(project)


def create_project_id(project_dir: Path) -> uuid.UUID:
    project = project_dir.resolve()
    if sys.platform == "win32":
        return _windows_create_project_id(project)
    return _posix_create_project_id(project)


def external_secret_location(
    project_dir: Path,
    *,
    environ: Mapping[str, str] | None = None,
    platform: str | None = None,
    home: Path | None = None,
) -> ExternalSecretLocation:
    project_id = load_project_id(project_dir)
    environment = os.environ if environ is None else environ
    override = environment.get(EXTERNAL_STORE_ENV)
    if override is not None:
        if not override:
            raise SecretStoreError(f"{EXTERNAL_STORE_ENV} must not be empty")
        override_path = Path(override).expanduser()
        if not override_path.is_absolute():
            raise SecretStoreError(f"{EXTERNAL_STORE_ENV} must be an absolute path")
        # Keep the lexical root so handle-based validation can still observe a
        # symlink/junction at the root itself instead of resolving it away.
        root = Path(os.path.abspath(override_path))
        return ExternalSecretLocation(root, root.parent, True)

    current_platform = sys.platform if platform is None else platform
    user_home = Path.home() if home is None else home
    if current_platform == "win32":
        local_app_data = environment.get("LOCALAPPDATA")
        if not local_app_data:
            raise SecretStoreError("LOCALAPPDATA is required to locate the secret store")
        base = Path(local_app_data)
        if not base.is_absolute():
            raise SecretStoreError("LOCALAPPDATA must be an absolute path")
        trusted_base = base
        validate_trusted_base = False
    elif current_platform == "darwin":
        base = user_home / "Library/Application Support"
        trusted_base = user_home
        validate_trusted_base = False
    else:
        xdg_data_home = environment.get("XDG_DATA_HOME")
        if xdg_data_home:
            base = Path(xdg_data_home)
            if not base.is_absolute():
                raise SecretStoreError("XDG_DATA_HOME must be an absolute path")
            trusted_base = base.parent
            validate_trusted_base = True
        else:
            base = user_home / ".local/share"
            trusted_base = user_home
            validate_trusted_base = False
    lexical_base = Path(os.path.abspath(base))
    trusted_base = Path(os.path.abspath(trusted_base))
    root = Path(os.path.abspath(lexical_base / "ansible-deploy/projects" / str(project_id)))
    return ExternalSecretLocation(root, trusted_base, validate_trusted_base)


def external_secret_root(
    project_dir: Path,
    *,
    environ: Mapping[str, str] | None = None,
    platform: str | None = None,
    home: Path | None = None,
) -> Path:
    return external_secret_location(
        project_dir, environ=environ, platform=platform, home=home
    ).root
