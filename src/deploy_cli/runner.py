import base64
import hashlib
import json
import os
import subprocess
import sys
import uuid
from collections.abc import Sequence
from pathlib import Path, PurePosixPath

from .models import EnvironmentConfig, GlobalConfig
from .redaction import Redactor


class RunnerError(RuntimeError):
    def __init__(self, message: str, exit_code: int) -> None:
        super().__init__(message)
        self.exit_code = exit_code


class AnsibleRunner:
    def __init__(
        self, repo: Path, redactor: Redactor, *, environment: str = "stage", verbose: bool = False
    ) -> None:
        self.repo = repo
        self.environment = environment
        self.redactor = redactor
        self.state_dir = repo / ".deploy-state" / environment
        self.verbose = verbose

    def build_image(self) -> None:
        self._run(["docker", "build", "-t", "ansible-deploy:local", "."], exit_code=5)

    def trust_host(self, host: str, port: int, expected_fingerprints: list[str]) -> None:
        container_name = self._container_name()
        args = [
            "docker",
            "run",
            "--rm",
            "--name",
            container_name,
            "--entrypoint",
            "ssh-keyscan",
            "ansible-deploy:local",
            "-T",
            "10",
            "-p",
            str(port),
            host,
        ]
        completed = False
        try:
            result = subprocess.run(  # noqa: S603 - fixed executable and argument vector
                args,
                cwd=self.repo,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                timeout=20,
            )
            completed = True
        except KeyboardInterrupt:
            raise
        except (OSError, subprocess.SubprocessError) as exc:
            raise RunnerError(f"Unable to scan SSH host key: {exc}", 4) from exc
        finally:
            if not completed:
                self._cleanup_container(container_name)
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
        self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        known_hosts = self.state_dir / "known_hosts"
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
        registry_auth_file: Path | None = None,
        check: bool = False,
        exit_code: int = 5,
        bootstrap_password: str | None = None,
    ) -> None:
        args = [
            "docker",
            "run",
            "--rm",
            "-v",
            f"{self.state_dir}:/state",
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
        password_stdin: str | None = None
        if bootstrap_password is not None:
            args[3:3] = ["-i", "-e", "ANSIBLE_BOOTSTRAP_PASSWORD_STDIN=1"]
            password_stdin = bootstrap_password
        if compose_file is not None:
            args[3:3] = ["-v", f"{compose_file}:/run/config/compose.yml:ro"]
        if env_file is not None:
            args[3:3] = ["-v", f"{env_file}:/run/secrets/app_env:ro"]
        if registry_auth_file is not None:
            args[3:3] = ["-v", f"{registry_auth_file}:/run/secrets/registry_auth:ro"]
        if check:
            args.extend(["--check", "--diff"])
        if self.verbose:
            args.append("-vv")
        self._run(args, exit_code=exit_code, stdin_text=password_stdin)

    def _container_path(self, path: Path) -> str:
        return "/workspace/" + path.resolve().relative_to(self.repo).as_posix()

    def _run(
        self,
        args: Sequence[str],
        *,
        exit_code: int,
        stdin_text: str | None = None,
    ) -> None:
        command = list(args)
        container_name: str | None = None
        if command[:2] == ["docker", "run"]:
            container_name = self._container_name()
            command[2:2] = ["--name", container_name]
        process: subprocess.Popen[str] | None = None
        completed = False
        try:
            process = subprocess.Popen(  # noqa: S603 - argument vector, never a shell
                command,
                cwd=self.repo,
                stdin=subprocess.PIPE if stdin_text is not None else None,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=os.environ.copy(),
            )
            if process.stdout is None:
                raise RunnerError("Runtime output pipe was not created", exit_code)
            if stdin_text is not None:
                if process.stdin is None:
                    raise RunnerError("Runtime input pipe was not created", exit_code)
                process.stdin.write(stdin_text)
                process.stdin.close()
            for line in process.stdout:
                print(self.redactor(line), end="", file=sys.stderr)
            result = process.wait()
            completed = True
            if result:
                raise RunnerError(f"Ansible runtime failed with code {result}", exit_code)
        except RunnerError:
            raise
        except (OSError, subprocess.SubprocessError) as exc:
            raise RunnerError("Ansible runtime I/O failed", exit_code) from exc
        finally:
            if container_name is not None and not completed:
                self._cleanup_container(container_name)
            running = False
            if process is not None:
                try:
                    running = process.poll() is None
                except OSError:
                    running = True
            if process is not None and running:
                try:
                    process.terminate()
                except OSError:
                    pass
                try:
                    process.wait(timeout=2)
                except (OSError, subprocess.TimeoutExpired):
                    try:
                        process.kill()
                    finally:
                        process.wait(timeout=2)

    def _container_name(self) -> str:
        return f"ansible-deploy-{self.environment}-{uuid.uuid4().hex}"

    def _cleanup_container(self, container_name: str) -> None:
        stop = self._docker_cleanup(["stop", "--time", "1", container_name])
        if stop != 0:
            self._docker_cleanup(["kill", container_name])
        self._docker_cleanup(["rm", "-f", container_name])

    def _docker_cleanup(self, arguments: list[str]) -> int:
        try:
            result = subprocess.run(  # noqa: S603 - fixed Docker command vector
                ["docker", *arguments],  # noqa: S607 - standard Docker executable
                cwd=self.repo,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=2,
            )
            return result.returncode
        except (OSError, subprocess.SubprocessError, KeyboardInterrupt):
            return -1


def _fingerprint(known_host_line: str) -> str:
    try:
        key = known_host_line.split()[2]
        raw = base64.b64decode(key.encode("ascii"), validate=True)
    except (IndexError, ValueError) as exc:
        raise RunnerError("ssh-keyscan returned an invalid public key", 3) from exc
    digest = base64.b64encode(hashlib.sha256(raw).digest()).decode("ascii").rstrip("=")
    return f"SHA256:{digest}"


def ansible_vars(
    global_config: GlobalConfig,
    config: EnvironmentConfig,
    *,
    deployment_version: str = "unmanaged",
    deployment_checksum: str = "unmanaged",
    deployment_images: list[str] | None = None,
) -> dict[str, object]:
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
        "app_registry_auth_file": (
            "/run/secrets/registry_auth"
            if config.application.registry_auth_file is not None
            else ""
        ),
        "app_dir": config.application.remote_dir,
        "app_environment": config.environment,
        "app_compose_project": (
            PurePosixPath(config.application.remote_dir).name
            if config.environment == "stage"
            else "myapp_prod"
        ),
        "app_upstream_port": 8080,
        "app_allowed_bind_paths": config.application.allowed_bind_paths,
        "app_nginx_site": f"application-{config.environment}",
        "deploy_identity_dir": "/etc/ansible-deploy",
        "deploy_identity_file": "/etc/ansible-deploy/identity.json",
        "deploy_legacy_identity_file": "/etc/ansible-deploy/environment",
        "deployment_version": deployment_version,
        "deployment_checksum": deployment_checksum,
        "deployment_images": deployment_images or [],
        "app_domain": config.domain,
        "acme_email": config.acme_email,
        "health_path": config.health_path,
        "reboot_time": reboot.time,
        "reboot_timezone": reboot.timezone,
        "reboot_enabled": reboot.enabled,
        "security_updates_enabled": global_config.global_.security_updates.enabled,
        "hardening_controls": controls.model_dump(),
    }
