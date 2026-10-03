import subprocess
import time
import uuid
from pathlib import Path

from test_runner_cleanup import _require_docker

RUNTIME_IMAGE = "ansible-deploy:local"
ROOT = Path(__file__).parents[1]


def test_docker_group_membership_is_refreshed_before_first_compose_run(
    tmp_path: Path,
) -> None:
    docker = _require_docker()
    suffix = uuid.uuid4().hex
    network = f"ansible-deploy-group-{suffix}"
    target = f"ansible-deploy-group-target-{suffix}"
    key_directory = tmp_path / "key"
    key_directory.mkdir()
    private_key = key_directory / "id"
    try:
        _run(docker, ["network", "create", network])
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
        setup = """
set -eu
apt-get update >/dev/null
apt-get install -y --no-install-recommends openssh-server sudo >/dev/null
groupadd docker
useradd --create-home --shell /bin/sh deploytest
printf '%s\n' '#!/bin/sh' 'id -nG | grep -qw docker' >/usr/local/bin/docker
chmod 0755 /usr/local/bin/docker
install -d -m 0700 -o deploytest -g deploytest /home/deploytest/.ssh
install -m 0600 -o deploytest -g deploytest /keys/id.pub /home/deploytest/.ssh/authorized_keys
printf '%s\n' 'deploytest ALL=(ALL) NOPASSWD:ALL' >/etc/sudoers.d/deploytest
mkdir -p /run/sshd
exec /usr/sbin/sshd -D -e
""".strip()
        _run(
            docker,
            [
                "run",
                "--detach",
                "--name",
                target,
                "--network",
                network,
                "--volume",
                f"{key_directory}:/keys:ro",
                "--entrypoint",
                "sh",
                RUNTIME_IMAGE,
                "-c",
                setup,
            ],
        )
        _wait_for_sshd(docker, target)

        result = subprocess.run(  # noqa: S603 - fixed Docker command vector
            [
                docker,
                "run",
                "--rm",
                "--network",
                network,
                "--volume",
                f"{private_key}:/run/secrets-source/ssh_key:ro",
                "--volume",
                f"{ROOT}:/workspace:ro",
                "--workdir",
                "/workspace",
                RUNTIME_IMAGE,
                "tests/integration/docker_group_refresh.yml",
                "-i",
                f"{target},",
                "-e",
                f"ansible_host={target}",
                "-e",
                "ansible_user=deploytest",
                "-e",
                "ansible_ssh_private_key_file=/run/ansible-deploy-secrets/ssh_key",
                "-e",
                "ansible_ssh_common_args=-oStrictHostKeyChecking=no",
                "-e",
                "ansible_python_interpreter=/usr/local/bin/python",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
            check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "Require the refreshed login session to have Docker membership" in result.stdout
    finally:
        subprocess.run(  # noqa: S603 - fixed cleanup command vector
            [docker, "rm", "--force", target], capture_output=True, check=False
        )
        subprocess.run(  # noqa: S603 - fixed cleanup command vector
            [docker, "network", "rm", network], capture_output=True, check=False
        )


def _run(docker: str, arguments: list[str]) -> None:
    result = subprocess.run(  # noqa: S603 - resolved Docker executable
        [docker, *arguments], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr


def _wait_for_sshd(docker: str, target: str) -> None:
    deadline = time.monotonic() + 120
    last_logs = ""
    while time.monotonic() < deadline:
        running = subprocess.run(  # noqa: S603 - fixed Docker command vector
            [docker, "exec", target, "test", "-e", "/run/sshd.pid"],
            capture_output=True,
            check=False,
        )
        if running.returncode == 0:
            return
        logs = subprocess.run(  # noqa: S603 - fixed Docker command vector
            [docker, "logs", target], capture_output=True, text=True, check=False
        )
        last_logs = logs.stdout + logs.stderr
        inspected = subprocess.run(  # noqa: S603 - fixed Docker command vector
            [docker, "inspect", "--format", "{{.State.Running}}", target],
            capture_output=True,
            text=True,
            check=False,
        )
        if inspected.stdout.strip() != "true":
            break
        time.sleep(1)
    raise AssertionError(f"SSH target did not start:\n{last_logs}")
