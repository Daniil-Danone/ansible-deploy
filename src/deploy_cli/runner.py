import base64
import hashlib
import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections import deque
from collections.abc import Sequence
from importlib import resources
from importlib.resources.abc import Traversable
from pathlib import Path, PurePosixPath

from .models import EnvironmentConfig, ExtraEnvFile, GlobalConfig, MonitoringConfig
from .redaction import Redactor
from .secret_store import SecretStoreError, validate_external_file_for_use

RUNTIME_IMAGE_REPOSITORY = "ansible-deploy"
RUNTIME_HASH_LABEL = "io.ansible-deploy.runtime-sha256"
HEARTBEAT_INTERVAL_SECONDS = 30.0
FAILURE_OUTPUT_LINES = 400
FAILURE_OUTPUT_LINE_BYTES = 16 * 1024


class RunnerError(RuntimeError):
    def __init__(self, message: str, exit_code: int) -> None:
        super().__init__(message)
        self.exit_code = exit_code


def extra_env_mount(target: str) -> str:
    """Container path of an extra env file; ``target`` is a validated plain file name."""
    return f"/run/secrets/app_extra_env/{target}"


def _before_subprocess_launch() -> None:
    """Deterministic test seam immediately before launch-boundary validation."""


class AnsibleRunner:
    def __init__(
        self,
        repo: Path,
        redactor: Redactor,
        *,
        environment: str = "stage",
        verbose: bool = False,
        external_secret_root: Path | None = None,
        external_trusted_base: Path | None = None,
        validate_external_trusted_base: bool = True,
    ) -> None:
        self.project_dir = repo.resolve()
        # Kept as a compatibility alias for callers/tests written before project-local mode.
        self.repo = self.project_dir
        self.environment = environment
        self.redactor = redactor
        self.state_dir = deployment_state_dir(self.project_dir, environment)
        self.verbose = verbose
        self.external_secret_root = external_secret_root
        self.external_trusted_base = external_trusted_base
        self.validate_external_trusted_base = validate_external_trusted_base
        self._pending_external_mounts: list[tuple[str, Path, bool]] = []
        self.runtime_image = runtime_image_reference()

    def build_image(self) -> None:
        runtime_hash = _resource_tree_hash(runtime_resources())
        self.runtime_image = _runtime_image_reference(runtime_hash)
        if self._image_has_runtime_hash(runtime_hash):
            print(f"[CACHE] runtime image {runtime_hash[:12]} is current", file=sys.stderr)
            return
        with tempfile.TemporaryDirectory(prefix="ansible-deploy-runtime-") as directory:
            context = Path(directory)
            _copy_resource_tree(runtime_resources(), context)
            self._run(
                [
                    "docker",
                    "build",
                    "--progress",
                    "plain",
                    "--label",
                    f"{RUNTIME_HASH_LABEL}={runtime_hash}",
                    "-t",
                    self.runtime_image,
                    str(context),
                ],
                exit_code=5,
            )

    def _image_has_runtime_hash(self, runtime_hash: str, image: str | None = None) -> bool:
        try:
            result = subprocess.run(  # noqa: S603 - fixed executable and argument vector
                [  # noqa: S607 - standard Docker executable
                    "docker",
                    "image",
                    "inspect",
                    "--format",
                    f'{{{{ index .Config.Labels "{RUNTIME_HASH_LABEL}" }}}}',
                    image or _runtime_image_reference(runtime_hash),
                ],
                cwd=self.repo,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return result.returncode == 0 and result.stdout.strip() == runtime_hash

    def trust_host(self, host: str, port: int, expected_fingerprints: list[str]) -> None:
        self.state_dir = prepare_state_directory(self.project_dir, self.environment)
        container_name = self._container_name()
        args = [
            "docker",
            "run",
            "--rm",
            "--name",
            container_name,
            "--entrypoint",
            "ssh-keyscan",
            self.runtime_image,
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
        extra_env_files: Sequence[ExtraEnvFile] = (),
        registry_auth_file: Path | None = None,
        observability_secret_file: Path | None = None,
        backup_credentials_file: Path | None = None,
        age_identity_file: Path | None = None,
        check: bool = False,
        exit_code: int = 5,
        bootstrap_password: str | None = None,
    ) -> None:
        self.state_dir = deployment_state_dir(self.project_dir, self.environment)
        if self.external_secret_root is not None:
            external_mounts = [
                ("SSH private key", ssh_key, True),
                ("application environment", env_file, True),
                *(
                    (f"application extra environment {extra.target}", extra.source, True)
                    for extra in extra_env_files
                ),
                ("registry authentication", registry_auth_file, True),
                ("observability secret", observability_secret_file, True),
                ("backup credentials", backup_credentials_file, True),
                ("age identity", age_identity_file, True),
            ]
            self._pending_external_mounts = [
                (field, path, secret)
                for field, path, secret in external_mounts
                if path is not None
            ]
            try:
                self._validate_external_mounts(exit_code)
            except BaseException:
                self._pending_external_mounts = []
                raise
        args = [
            "docker",
            "run",
            "--rm",
            "--tmpfs",
            "/run/ansible-deploy-secrets:rw,noexec,nosuid,nodev,size=1m,mode=0700",
            "-v",
            f"{self.state_dir}:/state",
            "-v",
            f"{inventory}:/run/config/inventory.yml:ro",
            "-v",
            f"{ssh_key}:/run/secrets-source/ssh_key:ro",
            "-e",
            "ANSIBLE_CONFIG=/opt/ansible-deploy/ansible/ansible.cfg",
            self.runtime_image,
            f"/opt/ansible-deploy/ansible/playbooks/{playbook}",
            "-i",
            "/run/config/inventory.yml",
            "--private-key",
            "/run/ansible-deploy-secrets/ssh_key",
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
        for extra in extra_env_files:
            args[3:3] = ["-v", f"{extra.source}:{extra_env_mount(extra.target)}:ro"]
        if registry_auth_file is not None:
            args[3:3] = ["-v", f"{registry_auth_file}:/run/secrets/registry_auth:ro"]
        if observability_secret_file is not None:
            args[3:3] = [
                "-v",
                f"{observability_secret_file}:/run/secrets/observability:ro",
            ]
        if backup_credentials_file is not None:
            args[3:3] = [
                "-v",
                f"{backup_credentials_file}:/run/secrets/backup_credentials:ro",
            ]
        if age_identity_file is not None:
            args[3:3] = [
                "-v",
                f"{age_identity_file}:/run/secrets/age_identity:ro",
            ]
        if check:
            args.extend(["--check", "--diff"])
        if self.verbose:
            args.append("-vv")
        self._run(args, exit_code=exit_code, stdin_text=password_stdin)

    def _validate_external_mounts(self, exit_code: int) -> None:
        if self.external_secret_root is None:
            return
        for field, path, secret in self._pending_external_mounts:
            try:
                validate_external_file_for_use(
                    self.project_dir,
                    self.external_secret_root,
                    path,
                    secret=secret,
                    trusted_base=self.external_trusted_base,
                    validate_trusted_base=self.validate_external_trusted_base,
                )
            except (SecretStoreError, OSError, ValueError):
                raise RunnerError(
                    f"Required {field} is unavailable for {self.environment}", exit_code
                ) from None

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
        started = time.monotonic()
        step = _command_step(command)
        print(f"[RUN] {step}", file=sys.stderr)
        buffered_output: deque[str] = deque(maxlen=FAILURE_OUTPUT_LINES)
        try:
            if container_name is not None and self._pending_external_mounts:
                _before_subprocess_launch()
                self._validate_external_mounts(exit_code)
            process = subprocess.Popen(  # noqa: S603 - argument vector, never a shell
                command,
                cwd=self.repo,
                stdin=subprocess.PIPE if stdin_text is not None else None,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=dict(
                    os.environ,
                    ANSIBLE_NOCOLOR="1",
                    BUILDKIT_PROGRESS="plain",
                    NO_COLOR="1",
                ),
            )
            if process.stdout is None:
                raise RunnerError("Runtime output pipe was not created", exit_code)
            stdout = process.stdout
            if stdin_text is not None:
                if process.stdin is None:
                    raise RunnerError("Runtime input pipe was not created", exit_code)
                process.stdin.write(stdin_text)
                process.stdin.close()
            output_queue: queue.Queue[str | BaseException | None] = queue.Queue(maxsize=1)

            def read_output() -> None:
                try:
                    for line in stdout:
                        output_queue.put(line)
                except BaseException as exc:  # pragma: no cover - defensive pipe boundary
                    output_queue.put(exc)
                    return
                output_queue.put(None)

            output_reader = threading.Thread(target=read_output, daemon=True)
            output_reader.start()
            next_heartbeat = time.monotonic() + HEARTBEAT_INTERVAL_SECONDS
            while True:
                timeout = max(0.0, next_heartbeat - time.monotonic())
                try:
                    item = output_queue.get(timeout=timeout)
                except queue.Empty:
                    elapsed = time.monotonic() - started
                    print(f"[WAIT] {step} is still running ({elapsed:.0f}s)", file=sys.stderr)
                    next_heartbeat = time.monotonic() + HEARTBEAT_INTERVAL_SECONDS
                    continue
                if item is None:
                    break
                if isinstance(item, BaseException):
                    raise item
                safe_line = _bounded_line(self.redactor(item))
                if self.verbose:
                    print(safe_line, end="", file=sys.stderr)
                else:
                    buffered_output.append(safe_line)
                if time.monotonic() >= next_heartbeat:
                    elapsed = time.monotonic() - started
                    print(f"[WAIT] {step} is still running ({elapsed:.0f}s)", file=sys.stderr)
                    next_heartbeat = time.monotonic() + HEARTBEAT_INTERVAL_SECONDS
            output_reader.join()
            result = process.wait()
            completed = True
            if result:
                print(
                    f"[FAIL] {step} ({time.monotonic() - started:.1f}s, code {result})",
                    file=sys.stderr,
                )
                if buffered_output:
                    print("[DETAIL] bounded failure output follows", file=sys.stderr)
                    print("".join(buffered_output), end="", file=sys.stderr)
                raise RunnerError(f"Ansible runtime failed with code {result}", exit_code)
            print(f"[DONE] {step} ({time.monotonic() - started:.1f}s)", file=sys.stderr)
        except RunnerError:
            raise
        except (OSError, subprocess.SubprocessError) as exc:
            raise RunnerError("Ansible runtime I/O failed", exit_code) from exc
        finally:
            self._pending_external_mounts = []
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


def _runtime_image_reference(runtime_hash: str) -> str:
    return f"{RUNTIME_IMAGE_REPOSITORY}:runtime-{runtime_hash}"


def runtime_image_reference() -> str:
    """Return the immutable image reference for the packaged runtime assets."""
    return _runtime_image_reference(_resource_tree_hash(runtime_resources()))


def _bounded_line(line: str) -> str:
    encoded = line.encode("utf-8", errors="replace")
    if len(encoded) <= FAILURE_OUTPUT_LINE_BYTES:
        return line
    suffix = b"... [line truncated]\n"
    prefix = encoded[: FAILURE_OUTPUT_LINE_BYTES - len(suffix)]
    return prefix.decode("utf-8", errors="ignore") + suffix.decode("ascii")


def runtime_resources() -> Traversable:
    """Return the runtime tree shipped in both wheels and editable installs."""
    return resources.files("deploy_cli").joinpath("runtime")


def _resource_tree_hash(source: Traversable) -> str:
    """Hash packaged runtime names and bytes in a platform-independent order."""
    if not source.is_dir():
        raise RunnerError("Packaged Ansible runtime assets are missing", 5)
    digest = hashlib.sha256()

    def visit(directory: Traversable, prefix: PurePosixPath) -> None:
        for item in sorted(directory.iterdir(), key=lambda entry: entry.name):
            relative = prefix / item.name
            if item.is_dir():
                visit(item, relative)
            elif item.is_file():
                digest.update(relative.as_posix().encode("utf-8"))
                digest.update(b"\0")
                with item.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(chunk)
                digest.update(b"\0")

    visit(source, PurePosixPath())
    return digest.hexdigest()


def _command_step(command: Sequence[str]) -> str:
    if command[:2] == ["docker", "build"]:
        return "build runtime image"
    if command[:2] == ["docker", "run"]:
        for argument in command:
            if argument.startswith("/opt/ansible-deploy/ansible/playbooks/"):
                return f"run {PurePosixPath(argument).name}"
        return "run deployment runtime"
    return "run deployment command"


def _is_reparse_path(path: Path) -> bool:
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    if is_junction is not None and is_junction():
        return True
    try:
        attributes = int(getattr(path.lstat(), "st_file_attributes", 0))
    except (FileNotFoundError, OSError):
        return False
    return bool(attributes & 0x400)  # FILE_ATTRIBUTE_REPARSE_POINT


def deployment_state_dir(project_dir: Path, environment: str) -> Path:
    project = project_dir.resolve()
    state_root = project / ".deploy-state"
    state_dir = state_root / environment
    for path in (state_root, state_dir):
        if _is_reparse_path(path):
            raise RunnerError("Deployment state path cannot be a symlink or reparse point", 2)
        try:
            path.resolve(strict=False).relative_to(project)
        except ValueError as exc:
            raise RunnerError("Deployment state path escapes the project directory", 2) from exc
    return state_dir


def prepare_state_directory(project_dir: Path, environment: str) -> Path:
    state_dir = deployment_state_dir(project_dir, environment)
    state_dir.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    deployment_state_dir(project_dir, environment)
    state_dir.parent.chmod(0o700)
    state_dir.mkdir(mode=0o700, exist_ok=True)
    state_dir = deployment_state_dir(project_dir, environment)
    state_dir.chmod(0o700)
    return state_dir


def _copy_resource_tree(source: Traversable, destination: Path) -> None:
    """Materialize only packaged runtime assets into an ephemeral Docker context."""
    if not source.is_dir():
        raise RunnerError("Packaged Ansible runtime assets are missing", 5)
    destination.mkdir(parents=True, exist_ok=True)
    for item in source.iterdir():
        target = destination / item.name
        if item.is_dir():
            _copy_resource_tree(item, target)
        elif item.is_file():
            with item.open("rb") as source_stream, target.open("wb") as target_stream:
                shutil.copyfileobj(source_stream, target_stream)


def ansible_vars(
    global_config: GlobalConfig,
    config: EnvironmentConfig | MonitoringConfig,
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
    variables: dict[str, object] = {
        "deploy_user": config.server.deploy_user,
        "deploy_ssh_port": config.server.ssh_port,
        "deploy_public_key": public_key,
        "app_environment": config.environment,
        "deploy_identity_dir": "/etc/ansible-deploy",
        "deploy_identity_file": "/etc/ansible-deploy/identity.json",
        "deploy_legacy_identity_file": "/etc/ansible-deploy/environment",
        "deployment_version": deployment_version,
        "deployment_checksum": deployment_checksum,
        "deployment_images": deployment_images or [],
        "app_domain": config.domain,
        "acme_email": config.acme_email,
        "reboot_time": reboot.time,
        "reboot_timezone": reboot.timezone,
        "reboot_enabled": reboot.enabled,
        "security_updates_enabled": global_config.global_.security_updates.enabled,
        "hardening_controls": controls.model_dump(),
    }
    if isinstance(config, MonitoringConfig):
        variables.update(
            {
                "monitoring_dir": config.monitoring.remote_dir,
                "monitoring_secret_file": "/run/secrets/observability",
                "monitoring_retention_days": config.monitoring.retention_days,
                "monitoring_grafana_port": config.monitoring.grafana_port,
                "monitoring_loki_port": config.monitoring.loki_port,
                "app_nginx_site": "monitoring",
                "app_dir": config.monitoring.remote_dir,
                "app_compose_project": "ansible_deploy_monitoring",
                "app_upstream_port": config.monitoring.grafana_port,
                "health_path": "/api/health",
            }
        )
    else:
        variables.update(
            {
                "app_compose_file": "/run/config/compose.yml",
                "app_env_file": "/run/secrets/app_env",
                "app_extra_env_files": [
                    {"src": extra_env_mount(extra.target), "dest": extra.target}
                    for extra in config.application.extra_env_files
                ],
                "app_registry_auth_file": (
                    "/run/secrets/registry_auth"
                    if config.application.registry_auth_file is not None
                    else ""
                ),
                "app_dir": config.application.remote_dir,
                "app_compose_project": (
                    PurePosixPath(config.application.remote_dir).name
                    if config.environment == "stage"
                    else "myapp_prod"
                ),
                "app_upstream_port": 8080,
                "health_path": config.health_path,
                "app_allowed_bind_paths": config.application.allowed_bind_paths,
                "app_nginx_site": f"application-{config.environment}",
            }
        )
        if config.collector is not None:
            variables.update(
                {
                    "collector_dir": config.collector.remote_dir,
                    "collector_push_url": config.collector.push_url,
                    "collector_username": config.collector.username,
                    "collector_password_file": "/run/secrets/observability",
                }
            )
    return variables
