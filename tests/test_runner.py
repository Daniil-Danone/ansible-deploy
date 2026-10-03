import base64
import hashlib
import os
import runpy
import stat
import subprocess
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

    expected_mount = f"{expected_key}:/run/secrets-source/ssh_key:ro"
    assert ["--check" in command for command in commands] == [True, False, False]
    assert all(expected_mount in command for command in commands)
    assert all(
        f"{expected_key}:/run/secrets/ssh_key:ro" not in command for command in commands
    )
    assert all(
        "/run/ansible-deploy-secrets:rw,noexec,nosuid,nodev,size=1m,mode=0700"
        in command
        for command in commands
    )
    assert all(
        command[command.index("--private-key") + 1]
        == "/run/ansible-deploy-secrets/ssh_key"
        for command in commands
    )


def test_runtime_copies_permissive_bind_mount_to_private_container_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entrypoint = runpy.run_path(
        str(
            Path(__file__).parents[1]
            / "src/deploy_cli/runtime/docker-entrypoint.py"
        )
    )
    source = tmp_path / "mounted-key"
    destination = tmp_path / "container" / "ssh_key"
    source.write_bytes(b"private-key-material\n")
    source.chmod(0o777)
    original_source_mode = stat.S_IMODE(source.stat().st_mode)
    requested_open_modes: list[int] = []
    requested_chmod_modes: list[int] = []
    requested_fchmod_modes: list[int] = []
    original_open = os.open
    original_chmod = os.chmod
    original_fchmod = getattr(os, "fchmod", None)

    def tracked_open(path, flags, mode=0o777):
        if Path(path) == destination:
            requested_open_modes.append(mode)
        return original_open(path, flags, mode)

    def tracked_chmod(path, mode, *args, **kwargs):
        if Path(path) == destination:
            requested_chmod_modes.append(mode)
        return original_chmod(path, mode, *args, **kwargs)

    def tracked_fchmod(descriptor, mode):
        requested_fchmod_modes.append(mode)
        assert original_fchmod is not None
        return original_fchmod(descriptor, mode)

    monkeypatch.setattr(os, "open", tracked_open)
    monkeypatch.setattr(os, "chmod", tracked_chmod)
    if original_fchmod is not None:
        monkeypatch.setattr(os, "fchmod", tracked_fchmod)
    monkeypatch.setitem(entrypoint["prepare_ssh_key"].__globals__, "SSH_KEY_SOURCE", str(source))
    monkeypatch.setitem(
        entrypoint["prepare_ssh_key"].__globals__,
        "SSH_KEY_DESTINATION",
        str(destination),
    )

    entrypoint["prepare_ssh_key"]()

    assert source.read_bytes() == b"private-key-material\n"
    assert stat.S_IMODE(source.stat().st_mode) == original_source_mode
    assert destination.read_bytes() == source.read_bytes()
    assert requested_open_modes == [0o600]
    assert requested_fchmod_modes or requested_chmod_modes == [0o600]
    if os.name != "nt":
        assert stat.S_IMODE(destination.stat().st_mode) == 0o600


@pytest.mark.parametrize(
    "key_bytes",
    [b"", b"x" * (64 * 1024 + 1)],
    ids=["empty", "oversized"],
)
def test_runtime_rejects_invalid_key_size_without_creating_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, key_bytes: bytes
) -> None:
    entrypoint = runpy.run_path(
        str(Path(__file__).parents[1] / "src/deploy_cli/runtime/docker-entrypoint.py")
    )
    source = tmp_path / "mounted-key"
    destination = tmp_path / "container" / "ssh_key"
    source.write_bytes(key_bytes)
    monkeypatch.setitem(entrypoint["prepare_ssh_key"].__globals__, "SSH_KEY_SOURCE", str(source))
    monkeypatch.setitem(
        entrypoint["prepare_ssh_key"].__globals__,
        "SSH_KEY_DESTINATION",
        str(destination),
    )

    with pytest.raises(SystemExit, match="invalid size"):
        entrypoint["prepare_ssh_key"]()

    assert source.read_bytes() == key_bytes
    assert not destination.exists()


def test_runtime_removes_partial_key_copy_after_interrupt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entrypoint = runpy.run_path(
        str(Path(__file__).parents[1] / "src/deploy_cli/runtime/docker-entrypoint.py")
    )
    source = tmp_path / "mounted-key"
    destination = tmp_path / "container" / "ssh_key"
    source.write_bytes(b"private-key-material\n")
    globals_ = entrypoint["prepare_ssh_key"].__globals__
    monkeypatch.setitem(globals_, "SSH_KEY_SOURCE", str(source))
    monkeypatch.setitem(globals_, "SSH_KEY_DESTINATION", str(destination))
    monkeypatch.setattr(
        globals_["shutil"],
        "copyfileobj",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(KeyboardInterrupt()),
    )

    with pytest.raises(KeyboardInterrupt):
        entrypoint["prepare_ssh_key"]()

    assert source.read_bytes() == b"private-key-material\n"
    assert not destination.exists()


def test_runtime_does_not_overwrite_or_remove_existing_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entrypoint = runpy.run_path(
        str(Path(__file__).parents[1] / "src/deploy_cli/runtime/docker-entrypoint.py")
    )
    source = tmp_path / "mounted-key"
    destination = tmp_path / "container" / "ssh_key"
    source.write_bytes(b"private-key-material\n")
    destination.parent.mkdir()
    destination.write_bytes(b"existing\n")
    monkeypatch.setitem(entrypoint["prepare_ssh_key"].__globals__, "SSH_KEY_SOURCE", str(source))
    monkeypatch.setitem(
        entrypoint["prepare_ssh_key"].__globals__,
        "SSH_KEY_DESTINATION",
        str(destination),
    )

    with pytest.raises(FileExistsError):
        entrypoint["prepare_ssh_key"]()

    assert source.read_bytes() == b"private-key-material\n"
    assert destination.read_bytes() == b"existing\n"


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFOs are unavailable")
def test_runtime_rejects_fifo_source_without_blocking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entrypoint = runpy.run_path(
        str(Path(__file__).parents[1] / "src/deploy_cli/runtime/docker-entrypoint.py")
    )
    source = tmp_path / "mounted-key"
    destination = tmp_path / "container" / "ssh_key"
    os.mkfifo(source)
    monkeypatch.setitem(entrypoint["prepare_ssh_key"].__globals__, "SSH_KEY_SOURCE", str(source))
    monkeypatch.setitem(
        entrypoint["prepare_ssh_key"].__globals__,
        "SSH_KEY_DESTINATION",
        str(destination),
    )

    with pytest.raises(SystemExit, match="not a regular file"):
        entrypoint["prepare_ssh_key"]()

    assert source.exists()
    assert not destination.exists()
