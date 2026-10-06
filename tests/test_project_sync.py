import hashlib
import subprocess
from pathlib import Path

import pytest
import yaml

from deploy_cli import cli
from deploy_cli.config import load_configuration
from deploy_cli.models import EnvironmentConfig
from deploy_cli.project import CD_PATH, SCAFFOLD_VERSION, STATE_PATH, WORKFLOW_PATH, sync_project


def _generated_workflow(project: Path) -> dict:
    return yaml.load(
        (project / WORKFLOW_PATH).read_text(encoding="utf-8"),
        Loader=yaml.BaseLoader,  # noqa: S506 - strings and collections only
    )


def _assert_deployment_secret_contract(workflow: dict) -> None:
    expected = {
        name: "${{ secrets." + name + " }}"
        for name in ("CLI_REPOSITORY_TOKEN", "ANSIBLE_DEPLOY_SECRET_STORE_JSON")
    }
    for name in ("stage", "production"):
        assert workflow["jobs"][name]["secrets"] == expected


def test_init_generates_cd_with_default_branch_and_event_boundaries(tmp_path: Path) -> None:
    assert cli.run(["--project-dir", str(tmp_path), "project", "init"]) == 0
    workflow = _generated_workflow(tmp_path)
    jobs = workflow["jobs"]

    assert workflow["on"]["push"]["branches"] == ["develop"]
    assert jobs["stage"]["if"] == (
        "(github.event_name == 'push' && github.ref == 'refs/heads/develop')"
    )
    assert jobs["production"]["if"] == (
        "github.event_name == 'workflow_dispatch' && inputs.deploy_production && "
        "github.ref == 'refs/heads/main'"
    )
    assert jobs["production"]["needs"] == ["quality", "collect_images"]
    assert "pull_request" in workflow["on"]
    assert "github.event_name == 'push'" in jobs["image_plan"]["if"]
    assert "github.ref == 'refs/heads/main'" in jobs["image_plan"]["if"]
    assert "workflow_dispatch" in workflow["on"]
    _assert_deployment_secret_contract(workflow)


def test_custom_init_branches_and_immutable_pin_survive_workflow_upgrade(
    tmp_path: Path, monkeypatch
) -> None:
    from deploy_cli import project

    sha = "a" * 40
    assert cli.run([
        "--project-dir", str(tmp_path), "project", "init",
        "--stage-branch", "release/stage", "--production-branch", "release/prod",
        "--tool-sha", sha,
    ]) == 0
    config = (tmp_path / CD_PATH).read_bytes()
    templates = project._template_files()
    templates[WORKFLOW_PATH] += b"# new delivery feature\n"
    monkeypatch.setattr(project, "_template_files", lambda: templates)

    result = sync_project(tmp_path)
    workflow = _generated_workflow(tmp_path)

    assert not result.conflicts
    assert WORKFLOW_PATH in result.updated
    assert (tmp_path / CD_PATH).read_bytes() == config
    assert workflow["on"]["push"]["branches"] == ["release/stage"]
    assert "refs/heads/release/prod" in workflow["jobs"]["production"]["if"]
    for name in ("stage", "production"):
        assert workflow["jobs"][name]["uses"].endswith("@" + sha)
        assert workflow["jobs"][name]["with"]["tool_sha"] == sha
    assert not sync_project(tmp_path, check=True).changes_required
    _assert_deployment_secret_contract(workflow)


def test_sync_upgrades_previous_caller_without_secret_mapping(tmp_path: Path) -> None:
    sync_project(tmp_path, stage_branch="test/danone-servers", tool_sha="a" * 40)
    path = tmp_path / WORKFLOW_PATH
    previous = path.read_text(encoding="utf-8").replace(
        "    secrets:\n"
        "      CLI_REPOSITORY_TOKEN: ${{ secrets.CLI_REPOSITORY_TOKEN }}\n"
        "      ANSIBLE_DEPLOY_SECRET_STORE_JSON: ${{ secrets.ANSIBLE_DEPLOY_SECRET_STORE_JSON }}\n",
        "",
    )
    path.write_text(previous, encoding="utf-8")
    state_path = tmp_path / STATE_PATH
    state = yaml.safe_load(state_path.read_text(encoding="utf-8"))
    state["files"][WORKFLOW_PATH.as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    state_path.write_text(yaml.safe_dump(state), encoding="utf-8")

    assert WORKFLOW_PATH in sync_project(tmp_path, check=True).updated
    result = sync_project(tmp_path)

    assert not result.conflicts
    assert WORKFLOW_PATH in result.updated
    workflow = _generated_workflow(tmp_path)
    _assert_deployment_secret_contract(workflow)
    assert workflow["on"]["push"]["branches"] == ["test/danone-servers"]
    assert workflow["jobs"]["stage"]["with"]["tool_sha"] == "a" * 40
    assert not sync_project(tmp_path, check=True).changes_required


def test_sync_renders_edited_cd_settings_without_overwriting_them(tmp_path: Path) -> None:
    sync_project(tmp_path)
    path = tmp_path / CD_PATH
    settings = yaml.safe_load(path.read_text(encoding="utf-8"))
    settings["stage_branch"] = "staging"
    settings["production_branch"] = "production"
    settings["tool_sha"] = "b" * 40
    path.write_text("# Keep local comments\n" + yaml.safe_dump(settings), encoding="utf-8")
    before = path.read_bytes()

    assert WORKFLOW_PATH in sync_project(tmp_path, check=True).updated
    result = sync_project(tmp_path)

    assert not result.conflicts
    assert path.read_bytes() == before
    workflow = _generated_workflow(tmp_path)
    assert workflow["on"]["push"]["branches"] == ["staging"]
    assert "refs/heads/production" in workflow["jobs"]["production"]["if"]


@pytest.mark.parametrize("option", ["--stage-branch", "--production-branch"])
@pytest.mark.parametrize(
    "branch",
    [
        "feature/*", "release?", "release+", "!develop", "x[y]", "x]",
        "a b", "a..b", "@{bad", "x.lock",
    ],
)
def test_init_rejects_invalid_or_glob_branch_before_writing(
    tmp_path: Path, branch: str, option: str
) -> None:
    assert cli.run([
        "--project-dir", str(tmp_path), "project", "init", option, branch,
    ]) == 2
    assert not (tmp_path / ".deploy").exists()
    assert not (tmp_path / ".github").exists()


def test_generic_image_plan_does_not_require_application_python_dependency_files(
    tmp_path: Path,
) -> None:
    sync_project(tmp_path)
    workflow = _generated_workflow(tmp_path)
    setup_python = next(
        step for step in workflow["jobs"]["image_plan"]["steps"]
        if step.get("uses", "").startswith("actions/setup-python@")
    )

    assert not (tmp_path / "requirements.txt").exists()
    assert not (tmp_path / "pyproject.toml").exists()
    assert "cache" not in setup_python["with"]
    assert "cache-dependency-path" not in setup_python["with"]


def test_branch_quotes_cannot_change_workflow_structure(tmp_path: Path) -> None:
    branch = "feature/quote'\"name"
    sync_project(tmp_path, stage_branch=branch, production_branch=branch)
    workflow = _generated_workflow(tmp_path)

    assert workflow["on"]["push"]["branches"] == [branch]
    assert "refs/heads/feature/quote''\"name" in workflow["jobs"]["production"]["if"]


def test_init_rejects_mutable_cli_ref(tmp_path: Path) -> None:
    assert cli.run([
        "--project-dir", str(tmp_path), "project", "init", "--tool-sha", "main",
    ]) == 2
    assert not (tmp_path / ".deploy").exists()


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
    assert (application / WORKFLOW_PATH).is_file()
    assert (application / CD_PATH).is_file()
    _assert_deployment_secret_contract(_generated_workflow(application))
