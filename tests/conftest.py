import os
import socket
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

import pytest


def _package_index_reachable() -> bool:
    index = urlparse(os.environ.get("PIP_INDEX_URL", "https://pypi.org/simple"))
    host = index.hostname or "pypi.org"
    port = index.port or (80 if index.scheme == "http" else 443)
    try:
        with socket.create_connection((host, port), timeout=5):
            return True
    except OSError:
        return False


def _pip(python: Path, *arguments: str, timeout: int) -> None:
    result = subprocess.run(  # noqa: S603 - isolated venv executable and fixed pip arguments
        [str(python), "-m", "pip", "--disable-pip-version-check", *arguments],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.fixture(scope="session")
def installed_wheel_python(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Python of a hermetic venv with the project wheel and its dependencies installed.

    The venv never sees the global site-packages, so the build backend and runtime
    dependencies are fetched from the package index; without network the tests skip.
    """
    if not _package_index_reachable():
        pytest.skip("Package index is unreachable: wheel install tests need network access")
    source = Path(__file__).parents[1]
    root = tmp_path_factory.mktemp("wheel-install")
    venv = root / "venv"
    subprocess.run(  # noqa: S603 - fixed interpreter and argument vector
        [sys.executable, "-m", "venv", str(venv)],
        check=True,
        timeout=120,
    )
    python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    wheel_dir = root / "wheel"
    _pip(python, "wheel", "--no-deps", "--wheel-dir", str(wheel_dir), str(source), timeout=300)
    wheel = next(wheel_dir.glob("*.whl"))
    _pip(python, "install", str(wheel), timeout=300)
    return python
