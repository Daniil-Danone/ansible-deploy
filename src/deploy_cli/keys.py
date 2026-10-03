import os
import stat
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from .config import ConfigurationError
from .models import EnvironmentConfig

if sys.platform == "win32":
    import msvcrt

    def _lock_descriptor(descriptor: int) -> None:
        if os.fstat(descriptor).st_size == 0:
            os.write(descriptor, b"0")
        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_LOCK, 1)

    def _unlock_descriptor(descriptor: int) -> None:
        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _lock_descriptor(descriptor: int) -> None:
        fcntl.flock(descriptor, fcntl.LOCK_EX)

    def _unlock_descriptor(descriptor: int) -> None:
        fcntl.flock(descriptor, fcntl.LOCK_UN)


def ensure_deploy_key(config: EnvironmentConfig) -> str:
    """Create or complete the configured key pair without replacing any path."""
    private_key = _canonical_key_path(config.server.ssh_key)
    public_key = _canonical_key_path(config.server.public_key)
    config.server.ssh_key = private_key
    config.server.public_key = public_key
    _reject_aliased_paths(private_key, public_key)
    private_key.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    public_key.parent.mkdir(mode=0o700, parents=True, exist_ok=True)

    lock_path = private_key.parent / f".{private_key.name}.ansible-deploy.lock"
    with _pair_lock(lock_path):
        _reject_aliased_paths(private_key, public_key)
        private_exists = private_key.is_file()
        public_exists = public_key.is_file()
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
            _write_exclusive(public_key, public_bytes, 0o644)
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
            try:
                os.link(generated_private, private_key)
            except FileExistsError as exc:
                raise ConfigurationError(
                    f"Refusing to replace deploy private key created concurrently: {private_key}"
                ) from exc
            except OSError as exc:
                raise ConfigurationError(f"Unable to publish deploy private key: {exc}") from exc
            _write_exclusive(public_key, generated_public.read_bytes(), 0o644)

        _restrict_mode(private_key, 0o600)
        _verify_pair(private_key, public_key)
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
def _pair_lock(path: Path) -> Iterator[None]:
    if path.is_symlink():
        raise ConfigurationError(f"Deploy key lock cannot be a symlink: {path}")
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor: int | None = None
    try:
        descriptor = os.open(path, flags, 0o600)
        opened = os.fstat(descriptor)
        current = os.lstat(path)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)
        ):
            raise ConfigurationError("Deploy key lock must be one non-aliased regular file")
        _lock_descriptor(descriptor)
    except OSError as exc:
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


def _write_exclusive(path: Path, content: bytes, mode: int) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, mode)
    except FileExistsError as exc:
        raise ConfigurationError(f"Refusing to replace key created concurrently: {path}") from exc
    except OSError as exc:
        raise ConfigurationError(f"Unable to publish key {path}: {exc}") from exc
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as exc:
        raise ConfigurationError(f"Unable to write key {path}: {exc}") from exc
    _restrict_mode(path, mode)


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
