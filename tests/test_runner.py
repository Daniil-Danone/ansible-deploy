import base64
import hashlib
import shutil
import subprocess
import time
import uuid
from io import StringIO
from pathlib import Path

import pytest
import yaml

from deploy_cli.config import load_configuration
from deploy_cli.redaction import Redactor
from deploy_cli.runner import AnsibleRunner, RunnerError, _fingerprint


def _host_key(raw: bytes) -> tuple[str, str]:
    encoded = base64.b64encode(raw).decode("ascii")
    line = f"example.com ssh-ed25519 {encoded}"
    digest = base64.b64encode(hashlib.sha256(raw).digest()).decode("ascii").rstrip("=")
    return line, f"SHA256:{digest}"


def test_openssh_fingerprint_is_calculated_from_key_blob() -> None:
    line, expected = _host_key(b"test-public-key-blob")

    assert _fingerprint(line) == expected


def test_changed_host_key_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    old_line, _ = _host_key(b"old-key")
    new_line, new_fingerprint = _host_key(b"new-key")
    state = tmp_path / ".deploy-state/stage"
    state.mkdir(parents=True)
    (state / "known_hosts").write_text(old_line + "\n", encoding="utf-8")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, new_line + "\n", ""),
    )
    runner = AnsibleRunner(tmp_path, Redactor([]))

    with pytest.raises(RunnerError, match="changed") as raised:
        runner.trust_host("example.com", 22, [new_fingerprint])

    assert raised.value.exit_code == 3


def test_host_trust_is_namespaced_by_environment(tmp_path: Path) -> None:
    stage = AnsibleRunner(tmp_path, Redactor([]), environment="stage")
    prod = AnsibleRunner(tmp_path, Redactor([]), environment="prod")

    assert stage.state_dir != prod.state_dir
    assert stage.state_dir == tmp_path / ".deploy-state/stage"
    assert prod.state_dir == tmp_path / ".deploy-state/prod"


def test_unreachable_ssh_scan_uses_ssh_exit_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 1, "", "timeout"),
    )
    runner = AnsibleRunner(tmp_path, Redactor([]))

    with pytest.raises(RunnerError, match="scan failed") as raised:
        runner.trust_host("example.com", 22, ["SHA256:" + "A" * 43])

    assert raised.value.exit_code == 4


def test_bootstrap_password_is_stdin_only_and_redacted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    password = "unique-root-password"  # noqa: S105 - synthetic regression-test value
    inventory = tmp_path / "inventory.yml"
    key = tmp_path / "key"
    inventory.write_text("all: {}\n", encoding="utf-8")
    key.write_text("private\n", encoding="utf-8")
    captured: dict[str, object] = {}

    class Input:
        value = ""

        def write(self, value: str) -> None:
            self.value += value

        def close(self) -> None:
            return None

    runtime_input = Input()

    class Process:
        stdout = StringIO(f"connection failed with {password}\n")
        stdin = runtime_input

        @staticmethod
        def wait() -> int:
            return 0

        @staticmethod
        def poll() -> int:
            return 0

    def popen(args, **kwargs):
        captured["args"] = args
        captured["env"] = kwargs["env"]
        return Process()

    monkeypatch.setattr(subprocess, "Popen", popen)
    runner = AnsibleRunner(tmp_path, Redactor([password]))
    runner.playbook("bootstrap.yml", inventory, {}, key, bootstrap_password=password)

    command = captured["args"]
    assert isinstance(command, list)
    assert password not in command
    assert "ANSIBLE_BOOTSTRAP_PASSWORD_STDIN=1" in command
    environment = captured["env"]
    assert isinstance(environment, dict)
    assert password not in environment.values()
    assert runtime_input.value == password
    output = capsys.readouterr().err
    assert password not in output
    assert "[REDACTED]" in output


def test_interrupted_runtime_is_terminated_and_reaped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []

    class Output:
        def __iter__(self):
            raise KeyboardInterrupt

    class Process:
        stdout = Output()
        stdin = None
        running = True

        def poll(self):
            return None if self.running else -15

        def terminate(self):
            events.append("terminate")
            self.running = False

        def wait(self, timeout=None):
            events.append(f"wait:{timeout}")
            return -15

        def kill(self):
            events.append("kill")

    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: Process())
    runner = AnsibleRunner(tmp_path, Redactor([]))
    monkeypatch.setattr(
        runner, "_cleanup_container", lambda name: events.append(f"cleanup:{name}")
    )

    with pytest.raises(KeyboardInterrupt):
        runner._run(["docker", "run"], exit_code=5)

    assert events[0].startswith("cleanup:ansible-deploy-stage-")
    assert events[1:] == ["terminate", "wait:2"]


def test_password_write_failure_terminates_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []

    class Input:
        def write(self, value):
            raise OSError("closed pipe")

        def close(self):
            return None

    class Process:
        stdout = StringIO()
        stdin = Input()
        running = True

        def poll(self):
            return None if self.running else -15

        def terminate(self):
            events.append("terminate")
            self.running = False

        def wait(self, timeout=None):
            events.append(f"wait:{timeout}")
            return -15

        def kill(self):
            events.append("kill")

    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: Process())
    runner = AnsibleRunner(tmp_path, Redactor([]))
    monkeypatch.setattr(
        runner, "_cleanup_container", lambda name: events.append(f"cleanup:{name}")
    )

    with pytest.raises(RunnerError, match="I/O failed"):
        runner._run(["docker", "run"], exit_code=5, stdin_text="secret")

    assert events[0].startswith("cleanup:ansible-deploy-stage-")
    assert events[1:] == ["terminate", "wait:2"]


def test_runtime_container_names_are_unique(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    commands: list[list[str]] = []

    class Process:
        stdout = StringIO()
        stdin = None

        def poll(self):
            return 0

        def wait(self, timeout=None):
            return 0

    def popen(args, **kwargs):
        commands.append(args)
        return Process()

    monkeypatch.setattr(subprocess, "Popen", popen)
    runner = AnsibleRunner(tmp_path, Redactor([]), environment="prod")
    runner._run(["docker", "run", "--rm", "image"], exit_code=5)
    runner._run(["docker", "run", "--rm", "image"], exit_code=5)

    names = [command[command.index("--name") + 1] for command in commands]
    assert names[0] != names[1]
    assert all(name.startswith("ansible-deploy-prod-") for name in names)


def test_relative_yaml_key_paths_are_absolute_for_nonbootstrap_runner_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = Path(__file__).parents[1]
    (tmp_path / "config").mkdir()
    environment_dir = tmp_path / "environments/stage"
    environment_dir.mkdir(parents=True)
    (tmp_path / "config/global.yml").write_text(
        (source / "config/global.yml").read_text(encoding="utf-8"), encoding="utf-8"
    )
    raw = yaml.safe_load((source / "environments/stage/config.yml").read_text(encoding="utf-8"))
    raw["server"]["ssh_key"] = "relative/deploy-key"
    raw["server"]["public_key"] = "relative/deploy-key.pub"
    (environment_dir / "config.yml").write_text(yaml.safe_dump(raw), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    _, config = load_configuration(tmp_path, "stage")
    expected_key = tmp_path / "relative/deploy-key"
    assert config.server.ssh_key == expected_key
    assert config.server.public_key == expected_key.with_suffix(".pub")
    assert config.server.ssh_key.is_absolute()

    inventory = tmp_path / "inventory.yml"
    inventory.write_text("all: {}\n", encoding="utf-8")
    commands: list[list[str]] = []

    class Process:
        stdout = StringIO()
        stdin = None

        def poll(self):
            return 0

        def wait(self, timeout=None):
            return 0

    def popen(args, **kwargs):
        commands.append(args)
        return Process()

    monkeypatch.setattr(subprocess, "Popen", popen)
    runner = AnsibleRunner(tmp_path, Redactor([]))
    runner.playbook("site.yml", inventory, {}, config.server.ssh_key, check=True)
    runner.playbook("update.yml", inventory, {}, config.server.ssh_key)
    runner.playbook("rollback.yml", inventory, {}, config.server.ssh_key)

    expected_mount = f"{expected_key}:/run/secrets/ssh_key:ro"
    assert ["--check" in command for command in commands] == [True, False, False]
    assert all(expected_mount in command for command in commands)


def test_real_interrupted_runtime_leaves_no_container(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("Docker CLI is unavailable")
    available = subprocess.run(  # noqa: S603 - resolved Docker executable
        [docker, "info"],
        capture_output=True,
        check=False,
        timeout=10,
    )
    if available.returncode != 0:
        pytest.skip("Docker daemon is unavailable")

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
                "ansible-deploy:local",
                "-c",
                "echo READY; sleep 60",
            ],
            exit_code=5,
        )

    assert time.monotonic() - started < 12
    assert _inspect_container(docker, container_name) != 0


def _inspect_container(docker: str, container_name: str) -> int:
    result = subprocess.run(  # noqa: S603 - fixed Docker command vector
        [docker, "inspect", container_name],
        capture_output=True,
        check=False,
        timeout=10,
    )
    return result.returncode
