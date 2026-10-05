import hashlib
import os
import stat
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from .config import ConfigurationError
from .models import EnvironmentConfig, MonitoringConfig
from .secret_file import (
    SecretFileError,
    secure_secret_permissions,
    validate_secret_permissions,
)
from .secret_store import (
    SecretStoreError,
    ensure_external_parent_for_write,
    validate_external_file_for_use,
)

if sys.platform == "win32":
    import ctypes
    import msvcrt
    from ctypes import wintypes

    @contextmanager
    def _pair_initialization_guard(path: Path) -> Iterator[None]:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_mutex = kernel32.CreateMutexW
        create_mutex.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
        create_mutex.restype = wintypes.HANDLE
        wait = kernel32.WaitForSingleObject
        wait.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        wait.restype = wintypes.DWORD
        release = kernel32.ReleaseMutex
        release.argtypes = [wintypes.HANDLE]
        release.restype = wintypes.BOOL
        close = kernel32.CloseHandle
        close.argtypes = [wintypes.HANDLE]
        close.restype = wintypes.BOOL
        identity = hashlib.sha256(
            os.path.normcase(os.path.abspath(path)).encode("utf-8")
        ).hexdigest()
        handle = create_mutex(None, False, f"Local\\ansible-deploy-key-{identity}")
        if not handle:
            raise ConfigurationError("Unable to initialize deploy key locking") from None
        acquired = False
        try:
            result = wait(handle, 0xFFFFFFFF)
            if result not in (0x00000000, 0x00000080):
                raise ConfigurationError("Unable to initialize deploy key locking")
            acquired = True
            yield
        finally:
            if acquired:
                release(handle)
            close(handle)

    def _lock_descriptor(descriptor: int) -> None:
        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_LOCK, 1)
        if os.fstat(descriptor).st_size == 0:
            os.write(descriptor, b"0")
        os.lseek(descriptor, 0, os.SEEK_SET)

    def _unlock_descriptor(descriptor: int) -> None:
        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    @contextmanager
    def _pair_initialization_guard(path: Path) -> Iterator[None]:
        del path
        yield

    def _lock_descriptor(descriptor: int) -> None:
        fcntl.flock(descriptor, fcntl.LOCK_EX)

    def _unlock_descriptor(descriptor: int) -> None:
        fcntl.flock(descriptor, fcntl.LOCK_UN)


class _KeyRollbackFailure(ConfigurationError):
    def __init__(self, artifacts: set[str]) -> None:
        self.artifacts = frozenset(artifacts)
        label = " and ".join(sorted(artifacts))
        super().__init__(
            f"Key publication failed and rollback could not remove the partial {label}"
        )


def ensure_deploy_key(config: EnvironmentConfig | MonitoringConfig) -> str:
    try:
        return _ensure_deploy_key(config)
    except _KeyRollbackFailure as exc:
        label = " and ".join(sorted(exc.artifacts))
        raise ConfigurationError(
            f"Unable to prepare deploy key for {config.environment}; "
            f"rollback failed and a partial {label} may remain"
        ) from None
    except ConfigurationError:
        raise
    except (OSError, SecretFileError):
        raise ConfigurationError(
            f"Unable to prepare deploy key for {config.environment}"
        ) from None


def _ensure_deploy_key(config: EnvironmentConfig | MonitoringConfig) -> str:
    """Create or complete the configured key pair without replacing any path."""
    private_key = _canonical_key_path(config.server.ssh_key)
    public_key = _canonical_key_path(config.server.public_key)
    config.server.ssh_key = private_key
    config.server.public_key = public_key
    _reject_aliased_paths(private_key, public_key)
    if not private_key.is_file() and public_key.is_file():
        raise ConfigurationError(
            f"Deploy public key exists but private key is missing: {private_key}; "
            "restore the matching private key or choose new empty key paths"
        )
    context = config.external_secret_context
    if context is None:
        raise ConfigurationError(
            f"External key store is unavailable for {config.environment}"
        )
    project, root, trusted_base, validate_base = context

    def prepare_external_store() -> None:
        try:
            for path in (private_key, public_key):
                ensure_external_parent_for_write(
                    project,
                    root,
                    path,
                    trusted_base=trusted_base,
                    validate_trusted_base=validate_base,
                )
        except SecretStoreError:
            raise ConfigurationError(
                f"Unable to prepare external key store for {config.environment}"
            ) from None

    lock_path = private_key.parent / f".{private_key.name}.ansible-deploy.lock"
    with _pair_lock(lock_path, prepare=prepare_external_store):
        _reject_aliased_paths(private_key, public_key)
        private_exists = private_key.is_file()
        public_exists = public_key.is_file()
        if context is not None:
            project, root, trusted_base, validate_base = context
            try:
                if private_exists:
                    validate_external_file_for_use(
                        project,
                        root,
                        private_key,
                        trusted_base=trusted_base,
                        validate_trusted_base=validate_base,
                    )
                if public_exists:
                    validate_external_file_for_use(
                        project,
                        root,
                        public_key,
                        secret=False,
                        trusted_base=trusted_base,
                        validate_trusted_base=validate_base,
                    )
            except SecretStoreError:
                raise ConfigurationError(
                    f"Configured key pair is unsafe for {config.environment}"
                ) from None
        if private_exists and public_exists:
            _verify_pair(private_key, public_key)
            return f"Using existing deploy key {private_key}"
        if not private_exists and public_exists:
            raise ConfigurationError(
                f"Deploy public key exists but private key is missing: {private_key}; "
                "restore the matching private key or choose new empty key paths"
            )
        if private_exists:
            public_bytes = (_public_from_private(private_key) + "\n").encode()
            public_published = False
            try:
                _write_exclusive(public_key, public_bytes, 0o644, artifact="public key")
                public_published = True
                if context is not None:
                    secure_secret_permissions(public_key)
            except BaseException as failure:
                _rollback_published_keys(
                    [(public_key, "public key")] if public_published else [],
                    failure=failure,
                )
                raise
            return f"Restored deploy public key {public_key} from the existing private key"

        with tempfile.TemporaryDirectory(
            prefix=".ansible-deploy-key-", dir=private_key.parent
        ) as temp:
            generated_private = Path(temp) / "id_ed25519"
            _ssh_keygen(
                [
                    "-q",
                    "-t",
                    "ed25519",
                    "-N",
                    "",
                    "-C",
                    f"ansible-deploy-{config.environment}",
                    "-f",
                    str(generated_private),
                ]
            )
            generated_public = generated_private.with_suffix(".pub")
            private_published = False
            public_published = False
            try:
                os.link(generated_private, private_key)
                private_published = True
            except FileExistsError as exc:
                raise ConfigurationError(
                    f"Refusing to replace deploy private key created concurrently: {private_key}"
                ) from exc
            except OSError as exc:
                raise ConfigurationError(f"Unable to publish deploy private key: {exc}") from exc
            try:
                _write_exclusive(
                    public_key,
                    generated_public.read_bytes(),
                    0o644,
                    artifact="public key",
                )
                public_published = True
                if context is not None:
                    secure_secret_permissions(private_key)
                    secure_secret_permissions(public_key)
                else:
                    _restrict_mode(private_key, 0o600)
                _verify_pair(private_key, public_key)
            except BaseException as failure:
                published = []
                if public_published:
                    published.append((public_key, "public key"))
                if private_published:
                    published.append((private_key, "private key"))
                _rollback_published_keys(published, failure=failure)
                raise
    return f"Created deploy key {private_key}"


def _canonical_key_path(path: Path) -> Path:
    absolute = Path(os.path.abspath(path))
    current = Path(absolute.anchor)
    for component in absolute.parts[1:]:
        current /= component
        if current.is_symlink():
            raise ConfigurationError(f"Deploy key path cannot contain a symlink: {current}")
    return absolute


def _reject_aliased_paths(private_key: Path, public_key: Path) -> None:
    for path in (private_key, public_key):
        if path.is_symlink():
            raise ConfigurationError(f"Deploy key destination cannot be a symlink: {path}")
    try:
        private_normalized = os.path.normcase(str(private_key.resolve(strict=False)))
        public_normalized = os.path.normcase(str(public_key.resolve(strict=False)))
    except (OSError, RuntimeError) as exc:
        raise ConfigurationError(f"Unable to normalize deploy key paths: {exc}") from exc
    if private_normalized == public_normalized:
        raise ConfigurationError("Deploy private and public key paths must be different")
    if private_key.exists() and public_key.exists():
        try:
            if os.path.samefile(private_key, public_key):
                raise ConfigurationError("Deploy private and public key paths alias the same file")
        except OSError as exc:
            raise ConfigurationError(f"Unable to validate deploy key paths: {exc}") from exc


@contextmanager
def _pair_lock(
    path: Path, *, prepare: Callable[[], None] | None = None
) -> Iterator[None]:
    if path.is_symlink():
        raise ConfigurationError(f"Deploy key lock cannot be a symlink: {path}")
    flags = os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor: int | None = None
    try:
        with _pair_initialization_guard(path):
            if prepare is not None:
                prepare()
            _publish_pair_lock(path)
            descriptor = os.open(path, flags, 0o600)
            _lock_descriptor(descriptor)
            opened = os.fstat(descriptor)
            current = os.lstat(path)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_nlink != 1
                or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)
            ):
                raise ConfigurationError("Deploy key lock must be one non-aliased regular file")
            validate_secret_permissions(path)
    except (OSError, SecretFileError) as exc:
        if descriptor is not None:
            os.close(descriptor)
        raise ConfigurationError(f"Unable to lock deploy key pair: {exc}") from exc
    except BaseException:
        if descriptor is not None:
            os.close(descriptor)
        raise
    try:
        yield
    finally:
        try:
            _unlock_descriptor(descriptor)
        finally:
            os.close(descriptor)


def _publish_pair_lock(path: Path) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError:
        return
    try:
        secure_secret_permissions(path)
    finally:
        os.close(descriptor)


def _rollback_published_keys(
    published: list[tuple[Path, str]], *, failure: BaseException | None = None
) -> None:
    failures = (
        set(failure.artifacts) if isinstance(failure, _KeyRollbackFailure) else set()
    )
    for path, label in published:
        try:
            path.unlink()
        except OSError:
            failures.add(label)
    if failures:
        raise _KeyRollbackFailure(failures) from None


def _write_exclusive(
    path: Path, content: bytes, mode: int, *, artifact: str = "key"
) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, mode)
    except FileExistsError:
        raise ConfigurationError("Refusing to replace a key created concurrently") from None
    except OSError:
        raise ConfigurationError("Unable to create the key file safely") from None
    stream = None
    try:
        stream = os.fdopen(descriptor, "wb")
        descriptor = -1
        with stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        _restrict_mode(path, mode)
    except BaseException:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
        try:
            path.unlink()
        except OSError:
            raise _KeyRollbackFailure({artifact}) from None
        raise ConfigurationError(
            "Key publication failed; the partial file was removed"
        ) from None


def _public_from_private(private_key: Path) -> str:
    return _ssh_keygen(["-y", "-f", str(private_key)]).stdout.strip()


def _verify_pair(private_key: Path, public_key: Path) -> None:
    expected = _public_from_private(private_key).split()[:2]
    try:
        actual = public_key.read_text(encoding="utf-8").split()[:2]
    except (OSError, UnicodeError) as exc:
        raise ConfigurationError(f"Unable to read deploy public key: {exc}") from exc
    if actual != expected:
        raise ConfigurationError("Configured deploy public key does not match its private key")


def _ssh_keygen(arguments: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(  # noqa: S603 - fixed executable and argument vector
            ["ssh-keygen", *arguments],  # noqa: S607 - standard OpenSSH executable
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=True,
        )
    except FileNotFoundError as exc:
        raise ConfigurationError(
            "ssh-keygen is required to create the deploy key; install the OpenSSH client"
        ) from exc
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.strip() or "ssh-keygen rejected the private key or its permissions"
        raise ConfigurationError(f"Unable to prepare deploy key: {detail}") from exc
    except OSError as exc:
        raise ConfigurationError(f"Unable to start ssh-keygen: {exc}") from exc


def _restrict_mode(path: Path, mode: int) -> None:
    try:
        os.chmod(path, mode)
    except OSError as exc:
        raise ConfigurationError(f"Unable to set safe permissions on {path}: {exc}") from exc
