import hashlib
import subprocess
from pathlib import Path

import yaml

from deploy_cli import cli
from deploy_cli.config import load_configuration
from deploy_cli.models import EnvironmentConfig
from deploy_cli.project import SCAFFOLD_VERSION, STATE_PATH, sync_project


def test_init_is_idempotent_and_creates_all_environment_skeletons(tmp_path: Path) -> None:
    assert cli.run(["--project-dir", str(tmp_path), "project", "init"]) == 0
    identity = (tmp_path / ".deploy/project-id").read_text(encoding="utf-8")
    first_state = (tmp_path / STATE_PATH).read_bytes()

    assert cli.run(["--project-dir", str(tmp_path), "project", "init"]) == 0

    assert (tmp_path / ".deploy/project-id").read_text(encoding="utf-8") == identity
    assert (tmp_path / STATE_PATH).read_bytes() == first_state
    for environment in ("stage", "prod", "monitoring", "restore"):
        assert (tmp_path / f".deploy/environments/{environment}/config.yml").is_file()
    assert (tmp_path / ".deploy/ci_image_contract.py").is_file()


def test_generated_scaffold_loads_every_environment_and_has_complete_disabled_backup(
    tmp_path: Path,
) -> None:
    assert cli.run(["--project-dir", str(tmp_path), "project", "init"]) == 0

    loaded = {
        environment: load_configuration(tmp_path, environment)[1]
        for environment in ("stage", "prod", "monitoring", "restore")
    }

    prod = loaded["prod"]
    restore = loaded["restore"]
    assert isinstance(prod, EnvironmentConfig)
    assert prod.backup is not None
    assert prod.backup.enabled is False
    assert isinstance(restore, EnvironmentConfig)
    assert restore.source_environment == "prod"


def test_sync_adds_missing_environments_to_stage_only_project(tmp_path: Path) -> None:
    stage = tmp_path / ".deploy/environments/stage/config.yml"
    stage.parent.mkdir(parents=True)
    stage.write_text("user-owned: true\n", encoding="utf-8")

    result = sync_project(tmp_path)

    assert stage.read_text(encoding="utf-8") == "user-owned: true\n"
    assert (stage.with_name("config.yml.deploy-new")).is_file()
    assert (tmp_path / ".deploy/environments/prod/config.yml").is_file()
    assert (tmp_path / ".deploy/environments/monitoring/config.yml").is_file()
    assert (tmp_path / ".deploy/environments/restore/config.yml").is_file()
    assert result.conflicts


def test_modified_managed_file_is_never_overwritten_on_template_upgrade(
    tmp_path: Path, monkeypatch
) -> None:
    from deploy_cli import project

    sync_project(tmp_path)
    compose = tmp_path / "deploy/compose.prod.yml"
    compose.write_text("services: {custom: {}}\n", encoding="utf-8")
    templates = project._template_files()
    templates[Path("deploy/compose.prod.yml")] += b"# upgraded\n"
    monkeypatch.setattr(project, "_template_files", lambda: templates)

    result = sync_project(tmp_path)

    assert compose.read_text(encoding="utf-8") == "services: {custom: {}}\n"
    assert (tmp_path / "deploy/compose.prod.yml.deploy-new").is_file()
    assert result.conflicts == (
        (Path("deploy/compose.prod.yml"), Path("deploy/compose.prod.yml.deploy-new")),
    )


def test_stale_conflict_candidate_is_atomically_refreshed_on_second_upgrade(
    tmp_path: Path, monkeypatch
) -> None:
    from deploy_cli import project

    sync_project(tmp_path)
    compose = tmp_path / "deploy/compose.prod.yml"
    original = b"services: {custom: {}}\n"
    compose.write_bytes(original)
    templates = project._template_files()
    relative = Path("deploy/compose.prod.yml")

    templates[relative] += b"# version two\n"
    monkeypatch.setattr(project, "_template_files", lambda: templates)
    sync_project(tmp_path)
    candidate = compose.with_name("compose.prod.yml.deploy-new")
    version_two = candidate.read_bytes()

    templates[relative] += b"# version three\n"
    result = sync_project(tmp_path)

    assert compose.read_bytes() == original
    assert candidate.read_bytes() == templates[relative]
    assert candidate.read_bytes() != version_two
    assert result.conflicts == ((relative, Path("deploy/compose.prod.yml.deploy-new")),)
    assert not candidate.with_name(f".{candidate.name}.deploy-tmp").exists()


def test_customized_current_template_is_an_idempotent_noop(tmp_path: Path) -> None:
    sync_project(tmp_path)
    config = tmp_path / ".deploy/environments/stage/config.yml"
    config.write_text("project-specific: true\n", encoding="utf-8")

    result = sync_project(tmp_path)

    assert not result.changes_required
    assert config.read_text(encoding="utf-8") == "project-specific: true\n"


def test_untouched_managed_file_updates_when_template_changes(
    tmp_path: Path, monkeypatch
) -> None:
    from deploy_cli import project

    sync_project(tmp_path)
    target = tmp_path / "deploy/compose.stage.yml"
    old = target.read_bytes()
    templates = project._template_files()
    replacement = old + b"# upgraded\n"
    templates[Path("deploy/compose.stage.yml")] = replacement
    monkeypatch.setattr(project, "_template_files", lambda: templates)

    result = sync_project(tmp_path)

    assert target.read_bytes() == replacement
    assert Path("deploy/compose.stage.yml") in result.updated


def test_check_reports_changes_without_writing(tmp_path: Path) -> None:
    result = sync_project(tmp_path, check=True)

    assert result.changes_required
    assert not (tmp_path / ".deploy").exists()
    assert Path(".deploy/project-id") in result.created
    assert cli.run(["--project-dir", str(tmp_path), "project", "sync", "--check"]) == 1


def test_scaffold_contains_no_runtime_secret_files_or_values(tmp_path: Path) -> None:
    sync_project(tmp_path)

    files = {
        path.relative_to(tmp_path).as_posix()
        for path in tmp_path.rglob("*")
        if path.is_file()
    }
    assert not any(
        name.endswith(("app.env", "registry-auth.json", ".key", "rclone.conf"))
        for name in files
    )
    assert not any(
        "SECRET=" in path.read_text(encoding="utf-8") for path in tmp_path.rglob("*.yml")
    )


def test_state_records_version_and_installed_hashes(tmp_path: Path) -> None:
    sync_project(tmp_path)
    state = yaml.safe_load((tmp_path / STATE_PATH).read_text(encoding="utf-8"))

    assert state["schema_version"] == 1
    assert state["template_version"] == SCAFFOLD_VERSION
    for relative, digest in state["files"].items():
        assert hashlib.sha256((tmp_path / relative).read_bytes()).hexdigest() == digest


def test_wheel_contains_scaffold_and_init_works_outside_checkout(
    tmp_path: Path, installed_wheel_python: Path
) -> None:
    python = installed_wheel_python
    application = tmp_path / "application"
    application.mkdir()

    result = subprocess.run(  # noqa: S603 - isolated venv executable
        [str(python), "-m", "deploy_cli.cli", "--project-dir", str(application), "project", "init"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert (application / ".deploy/environments/prod/config.yml").is_file()
