import json
import os
import subprocess
import time
import uuid
from pathlib import Path

import pytest
from test_runner_cleanup import _require_docker

RUNTIME_IMAGE = "ansible-deploy:local"
ROOT = Path(__file__).parents[1]
ROLES_PATH = "/workspace/src/deploy_cli/runtime/ansible/roles"
TARGET_VERSIONS_VARIABLE = "ANSIBLE_DEPLOY_TARGET_UBUNTU"
# Provisioning a systemd target installs the full package stack, so CI selects
# one Ubuntu release per matrix job instead of running every release here.
TARGET_VERSIONS = [
    version.strip()
    for version in os.environ.get(TARGET_VERSIONS_VARIABLE, "").split(",")
    if version.strip()
]
# ubuntu-minimal brings the release defaults a cloud VPS has (sudo-rs on 26.04,
# python3, tzdata); util-linux-extra provides hwclock for the timezone module.
TARGET_DOCKERFILE = """
ARG UBUNTU_VERSION
FROM ubuntu:${UBUNTU_VERSION}
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \\
 && apt-get install -y --no-install-recommends \\
      ubuntu-minimal systemd-sysv openssh-server util-linux-extra \\
 && rm -rf /var/lib/apt/lists/* \\
 && systemctl enable ssh \\
 && install -d -m 0700 /root/.ssh
STOPSIGNAL SIGRTMIN+3
CMD ["/sbin/init"]
"""


@pytest.mark.skipif(
    not TARGET_VERSIONS, reason=f"set {TARGET_VERSIONS_VARIABLE}=24.04,26.04 to provision"
)
@pytest.mark.parametrize("ubuntu_version", TARGET_VERSIONS or ["unselected"])
def test_production_roles_provision_supported_ubuntu_target(
    tmp_path: Path, ubuntu_version: str
) -> None:
    docker = _require_docker()
    suffix = uuid.uuid4().hex
    network = f"ansible-deploy-os-{suffix}"
    target = f"ansible-deploy-os-target-{suffix}"
    target_image = f"ansible-deploy-os-target:{ubuntu_version}"
    key_directory = tmp_path / "key"
    key_directory.mkdir()
    try:
        _run(
            docker,
            ["build", "--build-arg", f"UBUNTU_VERSION={ubuntu_version}", "-t", target_image, "-"],
            stdin=TARGET_DOCKERFILE,
            timeout=900,
        )
        _run(
            docker,
            [
                "run",
                "--rm",
                "--entrypoint",
                "ssh-keygen",
                "-v",
                f"{key_directory}:/keys",
                RUNTIME_IMAGE,
                "-q",
                "-t",
                "ed25519",
                "-N",
                "",
                "-f",
                "/keys/id",
            ],
        )
        _run(docker, ["network", "create", network])
        _run(
            docker,
            [
                "run",
                "--detach",
                "--privileged",
                "--cgroupns=host",
                "--volume",
                "/sys/fs/cgroup:/sys/fs/cgroup:rw",
                "--volume",
                "/var/lib/docker",
                "--name",
                target,
                "--hostname",
                "target",
                "--network",
                network,
                target_image,
            ],
        )
        _run(docker, ["cp", str(key_directory / "id.pub"), f"{target}:/root/.ssh/authorized_keys"])
        _run(docker, ["exec", target, "chown", "root:root", "/root/.ssh/authorized_keys"])
        _wait_for_ssh(docker, target)
        public_key = (key_directory / "id.pub").read_text(encoding="utf-8").strip()

        result = subprocess.run(  # noqa: S603 - fixed Docker command vector
            [
                docker,
                "run",
                "--rm",
                "--network",
                network,
                "--env",
                f"ANSIBLE_ROLES_PATH={ROLES_PATH}",
                "--volume",
                f"{key_directory / 'id'}:/run/secrets-source/ssh_key:ro",
                "--volume",
                f"{ROOT}:/workspace:ro",
                "--workdir",
                "/workspace",
                RUNTIME_IMAGE,
                "tests/integration/target_os_provisioning.yml",
                "-i",
                f"{target},",
                "-e",
                f"ansible_host={target}",
                "-e",
                "ansible_ssh_private_key_file=/run/ansible-deploy-secrets/ssh_key",
                "-e",
                "ansible_ssh_common_args=-oStrictHostKeyChecking=no",
                "-e",
                f"expected_ubuntu_version={ubuntu_version}",
                "-e",
                json.dumps({"deploy_public_key": public_key}),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=1800,
            check=False,
        )
        print(result.stdout)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "Require provisioned services and firewall to be active" in result.stdout
    finally:
        subprocess.run(  # noqa: S603 - fixed cleanup command vector
            [docker, "rm", "--force", "--volumes", target], capture_output=True, check=False
        )
        subprocess.run(  # noqa: S603 - fixed cleanup command vector
            [docker, "network", "rm", network], capture_output=True, check=False
        )


def _run(
    docker: str, arguments: list[str], *, stdin: str | None = None, timeout: int = 120
) -> None:
    result = subprocess.run(  # noqa: S603 - resolved Docker executable
        [docker, *arguments],
        input=stdin,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def _wait_for_ssh(docker: str, target: str) -> None:
    deadline = time.monotonic() + 120
    state = ""
    while time.monotonic() < deadline:
        active = subprocess.run(  # noqa: S603 - fixed Docker command vector
            [docker, "exec", target, "systemctl", "is-active", "ssh.socket", "ssh.service"],
            capture_output=True,
            text=True,
            check=False,
        )
        state = active.stdout
        if "active" in state.split():
            return
        time.sleep(1)
    raise AssertionError(f"SSH target did not start: {state}")
