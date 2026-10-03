import shutil
import subprocess
import time
import uuid
from pathlib import Path

import pytest

from deploy_cli.runner import AnsibleRunner

RUNTIME_IMAGE = "ansible-deploy:local"


def test_real_interrupted_runtime_leaves_no_container(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    docker = _require_docker()
    image = subprocess.run(  # noqa: S603 - resolved Docker executable
        [docker, "image", "inspect", RUNTIME_IMAGE],
        capture_output=True,
        check=False,
        timeout=10,
    )
    assert image.returncode == 0, f"build {RUNTIME_IMAGE} before running Docker integration tests"

    container_name = f"ansible-deploy-stage-{uuid.uuid4().hex}"
    assert _inspect_container(docker, container_name) != 0

    class InterruptOnReady:
        def __call__(self, line: str) -> str:
            if "READY" in line:
                raise KeyboardInterrupt
            return line

    runner = AnsibleRunner(tmp_path, InterruptOnReady())  # type: ignore[arg-type]
    monkeypatch.setattr(runner, "_container_name", lambda: container_name)
    started = time.monotonic()
    with pytest.raises(KeyboardInterrupt):
        runner._run(
            [
                "docker",
                "run",
                "--rm",
                "--entrypoint",
                "sh",
                RUNTIME_IMAGE,
                "-c",
                "echo READY; sleep 60",
            ],
            exit_code=5,
        )

    assert time.monotonic() - started < 12
    assert _inspect_container(docker, container_name) != 0


def test_missing_docker_cli_fails_integration_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda _: None)

    with pytest.raises(AssertionError, match="Docker CLI is required"):
        _require_docker()


def test_unavailable_docker_daemon_fails_integration_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/docker")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 1),
    )

    with pytest.raises(AssertionError, match="Docker daemon is required"):
        _require_docker()


def _require_docker() -> str:
    docker = shutil.which("docker")
    assert docker is not None, "Docker CLI is required for Docker integration tests"
    available = subprocess.run(  # noqa: S603 - resolved Docker executable
        [docker, "info"],
        capture_output=True,
        check=False,
        timeout=10,
    )
    assert available.returncode == 0, "Docker daemon is required for Docker integration tests"
    return docker


def _inspect_container(docker: str, container_name: str) -> int:
    result = subprocess.run(  # noqa: S603 - fixed Docker command vector
        [docker, "inspect", container_name],
        capture_output=True,
        check=False,
        timeout=10,
    )
    return result.returncode
