#!/usr/bin/env python3
import os
import sys


def main() -> None:
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
