import hashlib
import os
import shutil
import subprocess
import threading
from io import StringIO
from pathlib import Path

import pytest
import yaml

from deploy_cli import cli
from deploy_cli.config import (
    ConfigurationError,
    load_configuration,
    validate_production_isolation,
)
from deploy_cli.redaction import Redactor
from deploy_cli.runner import AnsibleRunner, RunnerError, runtime_resources
from deploy_cli.secret_file import secure_secret_permissions


@pytest.fixture(autouse=True)
def _external_secret_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    trusted_base = tmp_path / "trusted external base"
    trusted_base.mkdir()
    secure_secret_permissions(trusted_base)
    monkeypatch.setenv(
        "ANSIBLE_DEPLOY_SECRETS_DIR", str(trusted_base / "project secrets")
    )


def _copy_demo(destination: Path) -> Path:
    source = Path(__file__).parents[1] / "examples/demo-app"
    project = destination / "application with spaces"
    shutil.copytree(source, project, ignore=shutil.ignore_patterns(".deploy-state"))
    return project


def test_project_local_config_resolves_paths_from_project_not_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _copy_demo(tmp_path)
    arbitrary_cwd = tmp_path / "unrelated cwd"
    arbitrary_cwd.mkdir()
    monkeypatch.chdir(arbitrary_cwd)

    _, config = load_configuration(project, "stage")

    assert config.application.compose == (project / "deploy/compose.stage.yml").resolve()
    external_root = Path(os.environ["ANSIBLE_DEPLOY_SECRETS_DIR"])
    assert config.application.env_file == external_root / "environments/stage/app.env"
    assert config.server.ssh_key == external_root / "keys/stage_ed25519"


def test_relative_project_path_cannot_escape_project(tmp_path: Path) -> None:
    project = _copy_demo(tmp_path)
    config_path = project / ".deploy/environments/stage/config.yml"
    text = config_path.read_text(encoding="utf-8").replace(
        "deploy/compose.stage.yml", "../outside.yml"
    )
    config_path.write_text(text, encoding="utf-8")

    with pytest.raises(ConfigurationError, match="escapes the project"):
        load_configuration(project, "stage")


def test_external_key_parent_symlink_is_rejected(tmp_path: Path) -> None:
    project = _copy_demo(tmp_path)
    external_root = Path(os.environ["ANSIBLE_DEPLOY_SECRETS_DIR"])
    external_root.mkdir()
    secure_secret_permissions(external_root)
    real_keys = tmp_path / "real-keys"
    real_keys.mkdir()
    key_parent = external_root / "keys"
    try:
        key_parent.symlink_to(real_keys, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable")

    with pytest.raises(ConfigurationError, match="Invalid schema v2 SSH private key"):
        load_configuration(project, "stage")


def test_absolute_application_alias_is_canonicalized_for_isolation(tmp_path: Path) -> None:
    project = _copy_demo(tmp_path)
    stage_compose = (project / "deploy/compose.stage.yml").resolve()
    prod_config = project / ".deploy/environments/prod/config.yml"
    raw = yaml.safe_load(prod_config.read_text(encoding="utf-8"))
    raw["application"]["compose"] = str(stage_compose.parent / ".." / "deploy" / stage_compose.name)
    prod_config.write_text(yaml.safe_dump(raw), encoding="utf-8")

    _, prod = load_configuration(project, "prod")

    assert prod.application.compose == stage_compose
    with pytest.raises(ConfigurationError, match="Production must not reuse Stage Compose"):
        validate_production_isolation(project, prod)


def test_schema_v2_application_input_paths_are_resolved(tmp_path: Path) -> None:
    project = _copy_demo(tmp_path)
    external_root = Path(os.environ["ANSIBLE_DEPLOY_SECRETS_DIR"])
    config_path = project / ".deploy/environments/stage/config.yml"
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    raw["application"]["compose"] = str(
        project / "deploy" / ".." / "deploy" / "compose.stage.yml"
    )
    raw["application"]["env_file"] = "environments/stage/app.env"
    raw["application"]["registry_auth_file"] = "environments/stage/registry.json"
    config_path.write_text(yaml.safe_dump(raw), encoding="utf-8")

    _, config = load_configuration(project, "stage")

    assert config.application.compose == (project / "deploy/compose.stage.yml").resolve()
    assert config.application.env_file == external_root / "environments/stage/app.env"
    assert (
        config.application.registry_auth_file
        == external_root / "environments/stage/registry.json"
    )


def test_application_symlink_alias_is_canonicalized_for_isolation(tmp_path: Path) -> None:
    project = _copy_demo(tmp_path)
    stage_compose = (project / "deploy/compose.stage.yml").resolve()
    alias = project / "deploy/prod-alias.yml"
    try:
        alias.symlink_to(stage_compose)
    except OSError:
        pytest.skip("file symlinks are unavailable")
    prod_config = project / ".deploy/environments/prod/config.yml"
    raw = yaml.safe_load(prod_config.read_text(encoding="utf-8"))
    raw["application"]["compose"] = str(alias)
    prod_config.write_text(yaml.safe_dump(raw), encoding="utf-8")

    _, prod = load_configuration(project, "prod")

    assert prod.application.compose == stage_compose
    with pytest.raises(ConfigurationError, match="Production must not reuse Stage Compose"):
        validate_production_isolation(project, prod)


def test_project_dir_option_works_from_arbitrary_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _copy_demo(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    seen: list[Path] = []
    monkeypatch.setattr(cli, "status", lambda config: seen.append(config.application.compose))

    assert cli.run(["--project-dir", str(project), "status", "stage"]) == 0
    assert seen == [(project / "deploy/compose.stage.yml").resolve()]


def test_state_is_local_to_each_application(tmp_path: Path) -> None:
    first = AnsibleRunner(tmp_path / "first", Redactor([]), environment="stage")
    second = AnsibleRunner(tmp_path / "second", Redactor([]), environment="stage")

    assert first.state_dir == tmp_path / "first/.deploy-state/stage"
    assert second.state_dir == tmp_path / "second/.deploy-state/stage"
    assert first.state_dir != second.state_dir


def test_symlinked_state_root_is_rejected(tmp_path: Path) -> None:
    project = tmp_path / "project"
    target = tmp_path / "outside"
    project.mkdir()
    target.mkdir()
    try:
        (project / ".deploy-state").symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable")

    with pytest.raises(RunnerError, match="symlink or reparse"):
        AnsibleRunner(project, Redactor([]))


@pytest.mark.skipif(os.name != "nt", reason="Windows junction regression")
def test_windows_junction_state_root_is_rejected(tmp_path: Path) -> None:
    project = tmp_path / "project"
    target = tmp_path / "outside"
    project.mkdir()
    target.mkdir()
    result = subprocess.run(  # noqa: S603 - fixed Windows junction command
        ["cmd", "/c", "mklink", "/J", str(project / ".deploy-state"), str(target)],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.skip("Windows junction creation is unavailable")

    with pytest.raises(RunnerError, match="symlink or reparse"):
        AnsibleRunner(project, Redactor([]))


def test_packaged_runtime_is_complete() -> None:
    runtime = runtime_resources()

    for relative in (
        "Dockerfile",
        "docker-entrypoint.py",
        "password-once.sh",
        "ansible/ansible.cfg",
        "ansible/requirements.yml",
        "ansible/playbooks/site.yml",
        "ansible/roles/application/tasks/main.yml",
    ):
        current = runtime
        for part in relative.split("/"):
            current = current.joinpath(part)
        assert current.is_file(), relative


def test_packaged_runtime_matches_contributor_sources() -> None:
    source = Path(__file__).parents[1]
    runtime = runtime_resources()
    pairs = [
        (source / "Dockerfile", runtime.joinpath("Dockerfile")),
        (source / "docker-entrypoint.py", runtime.joinpath("docker-entrypoint.py")),
        (source / "password-once.sh", runtime.joinpath("password-once.sh")),
    ]
    for local in (source / "ansible").rglob("*"):
        if local.is_file() and "__pycache__" not in local.parts and local.suffix != ".pyc":
            packaged = runtime.joinpath("ansible")
            for part in local.relative_to(source / "ansible").parts:
                packaged = packaged.joinpath(part)
            pairs.append((local, packaged))

    for local, packaged in pairs:
        assert hashlib.sha256(local.read_bytes()).digest() == hashlib.sha256(
            packaged.read_bytes()
        ).digest(), local


def test_runtime_build_uses_ephemeral_minimal_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _copy_demo(tmp_path)
    runner = AnsibleRunner(project, Redactor([]))
    observed: dict[str, object] = {}
    monkeypatch.setattr(runner, "_image_has_runtime_hash", lambda _digest: False)

    def inspect(args, *, exit_code, stdin_text=None):
        del exit_code, stdin_text
        context = Path(args[-1])
        observed["path"] = context
        observed["files"] = {
            path.relative_to(context).as_posix() for path in context.rglob("*") if path.is_file()
        }
        assert context.is_dir()

    monkeypatch.setattr(runner, "_run", inspect)
    runner.build_image()

    context = observed["path"]
    assert isinstance(context, Path)
    assert not context.exists()
    files = observed["files"]
    assert isinstance(files, set)
    assert "Dockerfile" in files
    assert "ansible/playbooks/site.yml" in files
    assert not any(name.startswith(".deploy/") for name in files)
    assert not any(name.startswith("backend/") for name in files)


def test_runtime_build_reuses_image_with_matching_content_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _copy_demo(tmp_path)
    runner = AnsibleRunner(project, Redactor([]))
    observed_hashes: list[str] = []
    commands: list[list[str]] = []
    monkeypatch.setattr(
        runner,
        "_image_has_runtime_hash",
        lambda digest: observed_hashes.append(digest) or True,
    )
    monkeypatch.setattr(
        runner,
        "_run",
        lambda args, **_kwargs: commands.append(list(args)),
    )

    runner.build_image()

    assert len(observed_hashes) == 1
    assert len(observed_hashes[0]) == 64
    assert commands == []


def test_runtime_build_label_changes_when_packaged_asset_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _copy_demo(tmp_path)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    runner = AnsibleRunner(project, Redactor([]))
    commands: list[list[str]] = []
    monkeypatch.setattr("deploy_cli.runner.runtime_resources", lambda: runtime)
    monkeypatch.setattr(runner, "_image_has_runtime_hash", lambda _digest: False)
    monkeypatch.setattr(
        runner,
        "_run",
        lambda args, **_kwargs: commands.append(list(args)),
    )

    runner.build_image()
    first_label = commands[-1][commands[-1].index("--label") + 1]
    (runtime / "Dockerfile").write_text("FROM busybox\n", encoding="utf-8")
    runner.build_image()
    second_label = commands[-1][commands[-1].index("--label") + 1]

    assert first_label != second_label


def test_runtime_build_and_runner_use_exact_content_addressed_reference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _copy_demo(tmp_path)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    runner = AnsibleRunner(project, Redactor([]))
    commands: list[list[str]] = []
    monkeypatch.setattr("deploy_cli.runner.runtime_resources", lambda: runtime)
    monkeypatch.setattr(runner, "_image_has_runtime_hash", lambda _digest: False)
    monkeypatch.setattr(runner, "_run", lambda args, **_kwargs: commands.append(list(args)))

    runner.build_image()
    first_reference = runner.runtime_image
    (runtime / "Dockerfile").write_text("FROM busybox\n", encoding="utf-8")
    runner.build_image()
    second_reference = runner.runtime_image

    assert first_reference.startswith("ansible-deploy:runtime-")
    assert second_reference.startswith("ansible-deploy:runtime-")
    assert first_reference != second_reference
    assert commands[0][commands[0].index("-t") + 1] == first_reference
    assert commands[1][commands[1].index("-t") + 1] == second_reference
    assert "ansible-deploy:local" not in commands[0] + commands[1]


def test_concurrent_runtime_versions_never_retag_a_shared_mutable_reference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtimes: dict[str, Path] = {}
    for name, base in (("version-a", "scratch"), ("version-b", "busybox")):
        runtime = tmp_path / name
        runtime.mkdir()
        (runtime / "Dockerfile").write_text(f"FROM {base}\n", encoding="utf-8")
        runtimes[name] = runtime
    runners = {
        name: AnsibleRunner(tmp_path / f"project-{name}", Redactor([])) for name in runtimes
    }
    commands: list[list[str]] = []
    lock = threading.Lock()

    monkeypatch.setattr(
        "deploy_cli.runner.runtime_resources",
        lambda: runtimes[threading.current_thread().name],
    )
    for runner in runners.values():
        monkeypatch.setattr(runner, "_image_has_runtime_hash", lambda _digest: False)

        def capture(args, **_kwargs):
            with lock:
                commands.append(list(args))

        monkeypatch.setattr(runner, "_run", capture)

    threads = [
        threading.Thread(target=runner.build_image, name=name)
        for name, runner in runners.items()
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    references = {command[command.index("-t") + 1] for command in commands}
    assert len(references) == 2
    assert references == {runner.runtime_image for runner in runners.values()}
    assert all(reference.startswith("ansible-deploy:runtime-") for reference in references)


def test_playbook_mounts_only_addressed_project_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _copy_demo(tmp_path)
    inventory = project / ".deploy-state/stage/managed.yml"
    inventory.parent.mkdir(parents=True)
    inventory.write_text("all: {}\n", encoding="utf-8")
    key = project / "key"
    key.write_text("private\n", encoding="utf-8")
    commands: list[list[str]] = []

    class Process:
        stdout = StringIO()
        stdin = None

        @staticmethod
        def poll() -> int:
            return 0

        @staticmethod
        def wait(timeout=None) -> int:
            del timeout
            return 0

    def popen(args, **kwargs):
        del kwargs
        commands.append(args)
        return Process()

    monkeypatch.setattr(subprocess, "Popen", popen)
    runner = AnsibleRunner(project, Redactor([]))
    runner.playbook("update.yml", inventory, {}, key)

    command = commands[0]
    assert f"{inventory}:/run/config/inventory.yml:ro" in command
    assert f"{project}:/workspace:ro" not in command
    assert "/opt/ansible-deploy/ansible/playbooks/update.yml" in command


def test_wheel_installs_with_runtime_outside_source_checkout(
    tmp_path: Path, installed_wheel_python: Path
) -> None:
    source = Path(__file__).parents[1]
    python = installed_wheel_python
    outside = tmp_path / "outside"
    outside.mkdir()
    result = subprocess.run(  # noqa: S603 - isolated venv executable
        [
            str(python),
            "-c",
            "from deploy_cli.runner import runtime_resources; "
            "p=runtime_resources(); assert p.joinpath('Dockerfile').is_file(); "
            "import deploy_cli; print(deploy_cli.__file__)",
        ],
        cwd=outside,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert str(source.resolve()) not in result.stdout
    help_result = subprocess.run(  # noqa: S603 - isolated venv executable
        [str(python), "-m", "deploy_cli.cli", "--help"],
        cwd=outside,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert "--project-dir" in help_result.stdout
