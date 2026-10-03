import json
import subprocess
import uuid
from pathlib import Path

import pytest
from test_docker_group_refresh import _wait_for_sshd
from test_runner_cleanup import _require_docker

from deploy_cli.redaction import Redactor
from deploy_cli.runner import AnsibleRunner, RunnerError, prepare_state_directory

RUNTIME_IMAGE = "ansible-deploy:local"


def test_password_bootstrap_then_key_only_managed_access(tmp_path: Path) -> None:
    docker = _require_docker()
    suffix = uuid.uuid4().hex
    target = f"ansible-deploy-lifecycle-target-{suffix}"
    password = f"bootstrap-{suffix}"  # noqa: S105 - disposable integration credential
    key = tmp_path / "id_ed25519"
    wrong_key = tmp_path / "wrong_ed25519"
    try:
        _run(
            docker,
            [
                "run",
                "--rm",
                "--entrypoint",
                "ssh-keygen",
                "-v",
                f"{tmp_path}:/keys",
                RUNTIME_IMAGE,
                "-q",
                "-t",
                "ed25519",
                "-N",
                "",
                "-f",
                "/keys/id_ed25519",
            ],
        )
        _run(
            docker,
            [
                "run",
                "--rm",
                "--entrypoint",
                "ssh-keygen",
                "-v",
                f"{tmp_path}:/keys",
                RUNTIME_IMAGE,
                "-q",
                "-t",
                "ed25519",
                "-N",
                "",
                "-f",
                "/keys/wrong_ed25519",
            ],
        )
        setup = f"""
set -eu
apt-get update >/dev/null
apt-get install -y --no-install-recommends openssh-server sudo >/dev/null
echo 'root:{password}' | chpasswd
printf '%s\n' 'PermitRootLogin yes' 'PasswordAuthentication yes' \
  >/etc/ssh/sshd_config.d/bootstrap.conf
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
                "--entrypoint",
                "sh",
                RUNTIME_IMAGE,
                "-c",
                setup,
            ],
        )
        _wait_for_sshd(docker, target)
        address = _output(
            docker,
            [
                "inspect",
                "--format",
                "{{range.NetworkSettings.Networks}}{{.IPAddress}}{{end}}",
                target,
            ],
        ).strip()
        known_hosts = _output(
            docker,
            [
                "run",
                "--rm",
                "--entrypoint",
                "ssh-keyscan",
                RUNTIME_IMAGE,
                "-T",
                "30",
                address,
            ],
        )
        state = prepare_state_directory(tmp_path, "stage")
        (state / "known_hosts").write_text(known_hosts, encoding="utf-8")
        managed_inventory = _inventory(tmp_path / "managed.yml", address, "deploy")
        bootstrap_inventory = _inventory(tmp_path / "bootstrap.yml", address, "root")
        variables: dict[str, object] = {
            "deploy_user": "deploy",
            "deploy_public_key": key.with_suffix(".pub").read_text(encoding="utf-8").strip(),
            "deploy_identity_dir": "/etc/ansible-deploy",
            "deploy_identity_file": "/etc/ansible-deploy/identity.json",
            "deploy_legacy_identity_file": "/etc/ansible-deploy/environment",
            "app_compose_project": "demo",
            "app_environment": "stage",
            "app_nginx_site": "application-stage",
            "app_domain": "stage.example.test",
            "health_path": "/health",
            "app_upstream_port": 8080,
            "app_dir": "/srv/demo",
        }
        runner = AnsibleRunner(tmp_path, Redactor([password]), environment="stage")

        with pytest.raises(RunnerError):
            runner.playbook("guard_environment.yml", managed_inventory, variables, key)
        runner.playbook(
            "guard_environment.yml",
            bootstrap_inventory,
            variables,
            key,
            bootstrap_password=password,
        )
        runner.playbook(
            "bootstrap.yml",
            bootstrap_inventory,
            variables,
            key,
            bootstrap_password=password,
        )
        runner.playbook("verify_deploy_access.yml", managed_inventory, variables, key)

        identity = {
            "compose_project": "demo",
            "environment": "stage",
            "nginx_site": "application-stage",
            "routing": {
                "domain": "stage.example.test",
                "health_path": "/health",
                "upstream_port": "8080",
            },
            "runtime_dir": "/srv/demo",
        }
        _run(
            docker,
            [
                "exec",
                target,
                "sh",
                "-c",
                "mkdir -p /etc/ansible-deploy && printf '%s' \"$1\" > "
                "/etc/ansible-deploy/identity.json",
                "sh",
                json.dumps(identity, separators=(",", ":")),
            ],
        )
        claimed_bootstrap_variables = dict(
            variables, require_unclaimed_environment=True
        )
        with pytest.raises(RunnerError):
            runner.playbook(
                "guard_environment.yml",
                bootstrap_inventory,
                claimed_bootstrap_variables,
                key,
                bootstrap_password=password,
            )
        _run(
            docker,
            [
                "exec",
                target,
                "sh",
                "-c",
                "printf '%s\n' 'PermitRootLogin no' 'PasswordAuthentication no' "
                ">/etc/ssh/sshd_config.d/bootstrap.conf && kill -HUP $(cat /run/sshd.pid)",
            ],
        )

        runner.playbook("guard_environment.yml", managed_inventory, variables, key, check=True)
        with pytest.raises(RunnerError):
            runner.playbook(
                "guard_environment.yml",
                bootstrap_inventory,
                variables,
                key,
                bootstrap_password=password,
            )
        with pytest.raises(RunnerError):
            runner.playbook("guard_environment.yml", managed_inventory, variables, wrong_key)
        wrong_identity = dict(variables, app_domain="wrong.example.test")
        with pytest.raises(RunnerError):
            runner.playbook("guard_environment.yml", managed_inventory, wrong_identity, key)
        persisted = _output(
            docker, ["exec", target, "cat", "/etc/ansible-deploy/identity.json"]
        )
        assert json.loads(persisted) == identity
    finally:
        subprocess.run(  # noqa: S603 - fixed cleanup command vector
            [docker, "rm", "--force", target], capture_output=True, check=False
        )


def _inventory(path: Path, host: str, user: str) -> Path:
    path.write_text(
        "---\nall:\n  hosts:\n    target:\n"
        f"      ansible_host: {host!r}\n"
        f"      ansible_user: {user!r}\n"
        "      ansible_python_interpreter: /usr/local/bin/python\n",
        encoding="utf-8",
    )
    return path


def _run(docker: str, arguments: list[str]) -> None:
    result = subprocess.run(  # noqa: S603 - resolved Docker executable
        [docker, *arguments], capture_output=True, text=True, check=False, timeout=180
    )
    assert result.returncode == 0, result.stdout + result.stderr


def _output(docker: str, arguments: list[str]) -> str:
    result = subprocess.run(  # noqa: S603 - resolved Docker executable
        [docker, *arguments], capture_output=True, text=True, check=False, timeout=60
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout
