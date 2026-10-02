import base64
import hashlib
import json
import os
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

from .models import EnvironmentConfig, GlobalConfig
from .redaction import Redactor


class RunnerError(RuntimeError):
    def __init__(self, message: str, exit_code: int) -> None:
        super().__init__(message)
        self.exit_code = exit_code


class AnsibleRunner:
    def __init__(self, repo: Path, redactor: Redactor, *, verbose: bool = False) -> None:
        self.repo = repo
        self.redactor = redactor
        self.verbose = verbose

    def build_image(self) -> None:
        self._run(["docker", "build", "-t", "ansible-deploy:local", "."], exit_code=5)

    def trust_host(self, host: str, port: int, expected_fingerprints: list[str]) -> None:
        args = [
            "docker",
            "run",
            "--rm",
            "--entrypoint",
            "ssh-keyscan",
            "ansible-deploy:local",
            "-T",
            "10",
            "-p",
            str(port),
            host,
        ]
        try:
            result = subprocess.run(  # noqa: S603 - fixed executable and argument vector
                args,
                cwd=self.repo,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise RunnerError(f"Unable to scan SSH host key: {exc}", 4) from exc
        scanned = {
            line.strip()
            for line in result.stdout.splitlines()
            if line.strip() and not line.startswith("#")
        }
        if result.returncode != 0 or not scanned:
            detail = self.redactor(result.stderr.strip())
            raise RunnerError(f"SSH host key scan failed: {detail or 'no key returned'}", 4)
        trusted = {line for line in scanned if _fingerprint(line) in expected_fingerprints}
        if not trusted:
            raise RunnerError("SSH host key does not match a configured SHA256 fingerprint", 3)
        state_dir = self.repo / ".deploy-state"
        state_dir.mkdir(mode=0o700, exist_ok=True)
        known_hosts = state_dir / "known_hosts"
        if known_hosts.exists():
            existing = {
                line.strip()
                for line in known_hosts.read_text(encoding="utf-8").splitlines()
                if line.strip()
            }
            if existing != trusted:
                raise RunnerError("SSH host key changed since the previous trusted connection", 3)
        else:
            known_hosts.write_text(
                "\n".join(sorted(trusted)) + "\n", encoding="utf-8", newline="\n"
            )

    def playbook(
        self,
        playbook: str,
        inventory: Path,
        variables: dict[str, object],
        ssh_key: Path,
        *,
        compose_file: Path | None = None,
        env_file: Path | None = None,
        check: bool = False,
        exit_code: int = 5,
    ) -> None:
        args = [
            "docker",
            "run",
            "--rm",
            "-v",
            f"{self.repo / '.deploy-state'}:/state",
            "-v",
            f"{self.repo}:/workspace:ro",
            "-v",
            f"{ssh_key}:/run/secrets/ssh_key:ro",
            "-e",
            "ANSIBLE_CONFIG=/workspace/ansible/ansible.cfg",
            "ansible-deploy:local",
            f"ansible/playbooks/{playbook}",
            "-i",
            self._container_path(inventory),
            "--private-key",
            "/run/secrets/ssh_key",
            "--ssh-common-args",
            "-o UserKnownHostsFile=/state/known_hosts -o StrictHostKeyChecking=yes",
            "--extra-vars",
            json.dumps(variables),
        ]
        if compose_file is not None:
            args[3:3] = ["-v", f"{compose_file}:/run/config/compose.yml:ro"]
        if env_file is not None:
            args[3:3] = ["-v", f"{env_file}:/run/secrets/app_env:ro"]
        if check:
            args.extend(["--check", "--diff"])
        if self.verbose:
            args.append("-vv")
        self._run(args, exit_code=exit_code)

    def _container_path(self, path: Path) -> str:
        return "/workspace/" + path.resolve().relative_to(self.repo).as_posix()

    def _run(self, args: Sequence[str], *, exit_code: int) -> None:
        try:
            process = subprocess.Popen(  # noqa: S603 - argument vector, never a shell
                args,
                cwd=self.repo,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=os.environ.copy(),
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise RunnerError(f"Unable to start runtime: {exc}", exit_code) from exc
        if process.stdout is None:
            raise RunnerError("Runtime output pipe was not created", exit_code)
        for line in process.stdout:
            print(self.redactor(line), end="", file=sys.stderr)
        result = process.wait()
        if result:
            raise RunnerError(f"Ansible runtime failed with code {result}", exit_code)


def _fingerprint(known_host_line: str) -> str:
    try:
        key = known_host_line.split()[2]
        raw = base64.b64decode(key.encode("ascii"), validate=True)
    except (IndexError, ValueError) as exc:
        raise RunnerError("ssh-keyscan returned an invalid public key", 3) from exc
    digest = base64.b64encode(hashlib.sha256(raw).digest()).decode("ascii").rstrip("=")
    return f"SHA256:{digest}"


def ansible_vars(global_config: GlobalConfig, config: EnvironmentConfig) -> dict[str, object]:
    reboot = global_config.global_.security_updates.reboot
    controls = global_config.global_.hardening.controls
    public_key = ""
    if config.server.public_key.is_file():
        public_key = config.server.public_key.read_text(encoding="utf-8").strip()
    return {
        "deploy_user": config.server.deploy_user,
        "deploy_ssh_port": config.server.ssh_port,
        "deploy_public_key": public_key,
        "app_compose_file": "/run/config/compose.yml",
        "app_env_file": "/run/secrets/app_env",
        "app_fixture_config": "/workspace/environments/stage/fixture-nginx.conf",
        "app_dir": config.application.remote_dir,
        "app_domain": config.domain,
        "acme_email": config.acme_email,
        "health_path": config.health_path,
        "reboot_time": reboot.time,
        "reboot_timezone": reboot.timezone,
        "reboot_enabled": reboot.enabled,
        "security_updates_enabled": global_config.global_.security_updates.enabled,
        "hardening_controls": controls.model_dump(),
    }
