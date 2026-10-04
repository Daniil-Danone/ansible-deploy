import ctypes
import os
import stat
import sys
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from ctypes import wintypes
from pathlib import Path

PROJECT_ID_RELATIVE_PATH = Path(".deploy/project-id")
EXTERNAL_STORE_ENV = "ANSIBLE_DEPLOY_SECRETS_DIR"
_IDENTITY_NAME = "project-id"
_MAX_IDENTITY_SIZE = 38
_O_DIRECTORY = int(getattr(os, "O_DIRECTORY", 0))
_O_NOFOLLOW = int(getattr(os, "O_NOFOLLOW", 0))


class SecretStoreError(ValueError):
    """Raised when project identity or secret-store location is unsafe."""


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
    except UnicodeError as exc:
        raise SecretStoreError("Project identity is not a canonical UUID") from exc
    if normalized != f"{value}\n".encode():
        raise SecretStoreError("Project identity is not a canonical UUID")
    try:
        project_id = uuid.UUID(value)
    except ValueError as exc:
        raise SecretStoreError("Project identity is not a canonical UUID") from exc
    if str(project_id) != value:
        raise SecretStoreError("Project identity is not a canonical UUID")
    return project_id


def _read_bounded(descriptor: int) -> bytes:
    try:
        return os.read(descriptor, _MAX_IDENTITY_SIZE + 1)
    except OSError as exc:
        raise SecretStoreError("Unable to read project identity") from exc


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
        except FileNotFoundError as exc:
            raise SecretStoreError("Project identity is missing; use --new-project-id") from exc
        _after_directory_open(project / ".deploy")
        yield deploy_descriptor
    except SecretStoreError:
        raise
    except OSError as exc:
        raise SecretStoreError(
            "Project metadata directory must not be a symlink or reparse point"
        ) from exc
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
        except FileNotFoundError as exc:
            raise SecretStoreError("Project identity is missing; use --new-project-id") from exc
        except OSError as exc:
            raise SecretStoreError(
                "Project identity path must not be a symlink or reparse point"
            ) from exc
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
        except OSError as exc:
            raise SecretStoreError("Unable to validate project identity path") from exc
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
        except OSError as exc:
            try:
                os.unlink(temporary_name, dir_fd=deploy_descriptor)
            except OSError:
                pass
            raise SecretStoreError("Unable to write project identity") from exc
        finally:
            if temporary_descriptor >= 0:
                os.close(temporary_descriptor)
        return project_id


class _FileAttributeTagInfo(ctypes.Structure):
    _fields_ = [("file_attributes", wintypes.DWORD), ("reparse_tag", wintypes.DWORD)]


def _windows_open(path: Path, *, directory: bool) -> int:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
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
        error = ctypes.get_last_error()
        if error in {2, 3}:
            raise FileNotFoundError(path)
        raise OSError(error, f"Unable to open {path.name}")
    return int(handle)


def _windows_close(handle: int) -> None:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL
    if not close_handle(wintypes.HANDLE(handle)):
        raise OSError(ctypes.get_last_error(), "Unable to close Windows file handle")


def _windows_attributes(handle: int) -> int:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
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
        raise OSError(ctypes.get_last_error(), "Unable to inspect Windows file handle")
    return int(information.file_attributes)


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
        except OSError as exc:
            raise SecretStoreError("Unable to create project metadata directory") from exc
    try:
        handle = _windows_open(deploy_dir, directory=True)
    except FileNotFoundError as exc:
        raise SecretStoreError("Project identity is missing; use --new-project-id") from exc
    except OSError as exc:
        raise SecretStoreError("Unable to open project metadata directory safely") from exc
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
        except FileNotFoundError as exc:
            raise SecretStoreError("Project identity is missing; use --new-project-id") from exc
        except OSError as exc:
            raise SecretStoreError("Unable to open project identity safely") from exc
        try:
            _windows_validate_handle(handle, directory=False)
            buffer = ctypes.create_string_buffer(_MAX_IDENTITY_SIZE + 1)
            read = wintypes.DWORD()
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
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
                raise OSError(ctypes.get_last_error(), "Unable to read project identity")
            return _parse_project_id(buffer.raw[: read.value])
        except OSError as exc:
            raise SecretStoreError("Unable to read project identity") from exc
        finally:
            _windows_close(handle)


def _windows_existing_identity_is_safe(identity: Path) -> None:
    try:
        handle = _windows_open(identity, directory=False)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise SecretStoreError("Unable to validate project identity path") from exc
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
        except OSError as exc:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise SecretStoreError("Unable to write project identity") from exc
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


def external_secret_root(
    project_dir: Path,
    *,
    environ: Mapping[str, str] | None = None,
    platform: str | None = None,
    home: Path | None = None,
) -> Path:
    project_id = load_project_id(project_dir)
    environment = os.environ if environ is None else environ
    override = environment.get(EXTERNAL_STORE_ENV)
    if override is not None:
        if not override:
            raise SecretStoreError(f"{EXTERNAL_STORE_ENV} must not be empty")
        override_path = Path(override).expanduser()
        if not override_path.is_absolute():
            raise SecretStoreError(f"{EXTERNAL_STORE_ENV} must be an absolute path")
        return override_path.resolve()

    current_platform = sys.platform if platform is None else platform
    user_home = Path.home() if home is None else home
    if current_platform == "win32":
        local_app_data = environment.get("LOCALAPPDATA")
        if not local_app_data:
            raise SecretStoreError("LOCALAPPDATA is required to locate the secret store")
        base = Path(local_app_data)
        if not base.is_absolute():
            raise SecretStoreError("LOCALAPPDATA must be an absolute path")
    elif current_platform == "darwin":
        base = user_home / "Library/Application Support"
    else:
        xdg_data_home = environment.get("XDG_DATA_HOME")
        if xdg_data_home:
            base = Path(xdg_data_home)
            if not base.is_absolute():
                raise SecretStoreError("XDG_DATA_HOME must be an absolute path")
        else:
            base = user_home / ".local/share"
    return (base / "ansible-deploy/projects" / str(project_id)).resolve()
