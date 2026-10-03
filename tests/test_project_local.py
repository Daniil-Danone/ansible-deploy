import hashlib
import os
import shutil
import subprocess
import sys
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
from deploy_cli.keys import ensure_deploy_key
from deploy_cli.redaction import Redactor
from deploy_cli.runner import AnsibleRunner, RunnerError, runtime_resources


def _copy_demo(destination: Path) -> Path:
    source = Path(__file__).parents[1] / "examples/demo-app"
    project = destination / "application with spaces"
    shutil.copytree(source, project)
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
    assert config.application.env_file == (
        project / ".deploy/environments/stage/app.env"
    ).resolve()
    assert config.server.ssh_key == (project / ".deploy/keys/stage_ed25519").resolve()


def test_relative_project_path_cannot_escape_project(tmp_path: Path) -> None:
    project = _copy_demo(tmp_path)
    config_path = project / ".deploy/environments/stage/config.yml"
    text = config_path.read_text(encoding="utf-8").replace(
        "deploy/compose.stage.yml", "../outside.yml"
    )
    config_path.write_text(text, encoding="utf-8")

    with pytest.raises(ConfigurationError, match="escapes the project"):
        load_configuration(project, "stage")


def test_relative_key_keeps_symlink_evidence_for_key_guard(tmp_path: Path) -> None:
    project = _copy_demo(tmp_path)
    real_keys = project / "real-keys"
    real_keys.mkdir()
    key_parent = project / ".deploy/keys"
    try:
        key_parent.symlink_to(real_keys, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable")

    _, config = load_configuration(project, "stage")

    assert config.server.ssh_key == project / ".deploy/keys/stage_ed25519"
    with pytest.raises(ConfigurationError, match="symlink"):
        ensure_deploy_key(config)


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


def test_all_application_input_paths_are_canonicalized(tmp_path: Path) -> None:
    project = _copy_demo(tmp_path)
    secrets = project / "secrets"
    secrets.mkdir()
    env = secrets / "stage.env"
    registry = secrets / "registry.json"
    env.write_text("APP_ENV=stage\n", encoding="utf-8")
    registry.write_text('{"auths": {}}\n', encoding="utf-8")
    env_alias = project / "deploy/env-link"
    try:
        env_alias.symlink_to(env)
    except OSError:
        pytest.skip("file symlinks are unavailable")
    config_path = project / ".deploy/environments/stage/config.yml"
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    raw["application"]["compose"] = str(
        project / "deploy" / ".." / "deploy" / "compose.stage.yml"
    )
    raw["application"]["env_file"] = "deploy/env-link"
    raw["application"]["registry_auth_file"] = str(
        secrets / ".." / "secrets" / "registry.json"
    )
    config_path.write_text(yaml.safe_dump(raw), encoding="utf-8")

    _, config = load_configuration(project, "stage")

    assert config.application.compose == (project / "deploy/compose.stage.yml").resolve()
    assert config.application.env_file == env.resolve()
    assert config.application.registry_auth_file == registry.resolve()


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
        if local.is_file():
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


@pytest.mark.skipif(os.name == "nt" and not shutil.which("python"), reason="Python unavailable")
def test_wheel_installs_with_runtime_outside_source_checkout(tmp_path: Path) -> None:
    source = Path(__file__).parents[1]
    wheel_dir = tmp_path / "wheel"
    subprocess.run(  # noqa: S603 - fixed interpreter and argument vector
        [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            "--no-deps",
            "--wheel-dir",
            str(wheel_dir),
            str(source),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    )
    venv = tmp_path / "venv"
    subprocess.run(  # noqa: S603 - fixed interpreter and argument vector
        [sys.executable, "-m", "venv", "--system-site-packages", str(venv)],
        check=True,
        timeout=60,
    )
    python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    wheel = next(wheel_dir.glob("*.whl"))
    subprocess.run(  # noqa: S603 - isolated venv executable and local wheel
        [str(python), "-m", "pip", "install", "--no-deps", str(wheel)],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
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
