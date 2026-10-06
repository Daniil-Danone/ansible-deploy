"""Materialize a GitHub secret JSON map into a temporary external secret store."""

from __future__ import annotations

import base64
import binascii
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath


def main() -> None:
    # This is a one-shot helper process. A restrictive umask also covers
    # intermediate directories implicitly created by mkdir(parents=True).
    os.umask(0o077)
    root_value = os.environ.get("ANSIBLE_DEPLOY_SECRETS_DIR", "")
    payload = os.environ.pop("DEPLOY_SECRET_STORE_JSON", "")
    if not root_value or not Path(root_value).is_absolute() or not payload:
        raise SystemExit("CI secret store inputs are missing")
    root = Path(root_value)
    root.mkdir(mode=0o700, parents=True, exist_ok=False)
    try:
        entries = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise SystemExit("CI secret store payload is invalid JSON") from exc
    if not isinstance(entries, dict) or not entries:
        raise SystemExit("CI secret store payload must be a non-empty object")
    for name, encoded in entries.items():
        if not isinstance(name, str) or not isinstance(encoded, str):
            raise SystemExit("CI secret store entries must be base64 string pairs")
        posix = PurePosixPath(name)
        windows = PureWindowsPath(name)
        if (
            not name
            or posix.is_absolute()
            or windows.is_absolute()
            or bool(windows.drive)
            or name != str(posix)
            or any(part in {"", ".", ".."} for part in posix.parts)
        ):
            raise SystemExit("CI secret store contains an unsafe file name")
        try:
            content = base64.b64decode(encoded, validate=True)
        except binascii.Error as exc:
            raise SystemExit("CI secret store contains invalid base64") from exc
        target = root.joinpath(*posix.parts)
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)


if __name__ == "__main__":
    main()
