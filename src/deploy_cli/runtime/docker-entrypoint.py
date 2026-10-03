#!/usr/bin/env python3
import os
import shutil
import stat
import sys

SSH_KEY_SOURCE = "/run/secrets-source/ssh_key"
SSH_KEY_DESTINATION = "/run/ansible-deploy-secrets/ssh_key"
MAX_SSH_KEY_BYTES = 64 * 1024


def prepare_ssh_key() -> None:
    """Copy a bind-mounted key to a container-native file with strict permissions."""
    destination_parent = os.path.dirname(SSH_KEY_DESTINATION)
    os.makedirs(destination_parent, mode=0o700, exist_ok=True)
    if not stat.S_ISDIR(os.lstat(destination_parent).st_mode):
        raise SystemExit("SSH private key destination is not a directory")
    source_flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    destination_flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
    )
    source_descriptor = os.open(SSH_KEY_SOURCE, source_flags)
    try:
        source_stat = os.fstat(source_descriptor)
        if not stat.S_ISREG(source_stat.st_mode):
            raise SystemExit("SSH private key source is not a regular file")
        if source_stat.st_size < 1 or source_stat.st_size > MAX_SSH_KEY_BYTES:
            raise SystemExit("SSH private key source has an invalid size")
        destination_descriptor = os.open(SSH_KEY_DESTINATION, destination_flags, 0o600)
        try:
            fchmod = getattr(os, "fchmod", None)
            if fchmod is not None:
                fchmod(destination_descriptor, 0o600)
            with os.fdopen(source_descriptor, "rb", closefd=False) as source_stream:
                destination_stream = os.fdopen(destination_descriptor, "wb", closefd=True)
                destination_descriptor = -1
                with destination_stream:
                    shutil.copyfileobj(source_stream, destination_stream)
                    destination_stream.flush()
                    os.fsync(destination_stream.fileno())
            if fchmod is None:
                os.chmod(SSH_KEY_DESTINATION, 0o600)
        except BaseException:
            try:
                os.unlink(SSH_KEY_DESTINATION)
            except FileNotFoundError:
                pass
            raise
        finally:
            if destination_descriptor >= 0:
                os.close(destination_descriptor)
    finally:
        os.close(source_descriptor)


def main() -> None:
    prepare_ssh_key()
    if os.environ.pop("ANSIBLE_BOOTSTRAP_PASSWORD_STDIN", None) == "1":
        password = sys.stdin.buffer.read()
        if not password or any(character in password for character in (b"\0", b"\r", b"\n")):
            raise SystemExit("invalid bootstrap password input")
        credential_path = "/dev/shm/ansible-bootstrap-password"  # noqa: S108
        descriptor = os.open(
            credential_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
        )
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(password)
            stream.flush()
            os.fsync(stream.fileno())
        os.environ["ANSIBLE_CONNECTION_PASSWORD_FILE"] = (  # noqa: S105
            "/usr/local/bin/password-once"  # noqa: S105
        )
    executable = "/usr/local/bin/ansible-playbook"
    os.execv(executable, [executable, *sys.argv[1:]])  # noqa: S606


if __name__ == "__main__":
    main()
