import os
import shutil
from pathlib import Path

import pytest
import yaml

from deploy_cli import cli
from deploy_cli.runner import AnsibleRunner
from deploy_cli.secret_file import secure_secret_permissions, validate_secret_permissions


@pytest.fixture(autouse=True)
def _external_secret_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    trusted_base = tmp_path / "trusted external base"
    trusted_base.mkdir()
    secure_secret_permissions(trusted_base)
    monkeypatch.setenv("ANSIBLE_DEPLOY_SECRETS_DIR", str(trusted_base / "project secrets"))


def _store_root() -> Path:
    return Path(os.environ["ANSIBLE_DEPLOY_SECRETS_DIR"])


def _demo_project(tmp_path: Path) -> Path:
    source = Path(__file__).parents[1] / "examples/demo-app"
    project = tmp_path / "application"
    shutil.copytree(source, project, ignore=shutil.ignore_patterns(".deploy-state"))
    return project


def _initialized_project(tmp_path: Path) -> Path:
    project = tmp_path / "scaffold"
    project.mkdir()
    assert cli.run(["--project-dir", str(project), "project", "init"]) == 0
    return project


def test_secrets_init_creates_an_owner_only_layout_and_is_idempotent(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _demo_project(tmp_path)

    assert cli.run(["--project-dir", str(project), "secrets", "init"]) == 0

    root = _store_root()
    created = capsys.readouterr().out
    directories = [root / "keys", root / "backup", root / "environments/stage"]
    for directory in directories:
        assert directory.is_dir()
        validate_secret_permissions(directory)
    secret = root / "environments/stage/app.env"
    validate_secret_permissions(secret)
    assert secret.read_text(encoding="utf-8") == "APP_ENV=stage\n"
    assert "[CREATE] environments/stage/app.env" in created
    assert "[CREATE] keys/" in created

    assert cli.run(["--project-dir", str(project), "secrets", "init"]) == 0

    repeated = capsys.readouterr().out
    assert secret.read_text(encoding="utf-8") == "APP_ENV=stage\n"
    assert "[KEEP] environments/stage/app.env" in repeated
    assert "[CREATE]" not in repeated


def test_secrets_init_never_overwrites_an_existing_secret_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _demo_project(tmp_path)
    assert cli.run(["--project-dir", str(project), "secrets", "init", "stage"]) == 0
    secret = _store_root() / "environments/stage/app.env"
    secret.write_text("APP_ENV=stage\nDATABASE_PASSWORD=already-chosen\n", encoding="utf-8")

    assert cli.run(["--project-dir", str(project), "secrets", "init", "stage"]) == 0

    output = capsys.readouterr().out
    assert "already-chosen" not in output
    assert "[KEEP] environments/stage/app.env" in output
    assert "DATABASE_PASSWORD=already-chosen" in secret.read_text(encoding="utf-8")


def test_secrets_init_reports_values_the_operator_must_fill(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _initialized_project(tmp_path)
    path = project / ".deploy/environments/stage/config.yml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    raw["application"]["required_env_vars"] = ["APP_ENV", "DATABASE_URL"]
    path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")

    assert cli.run(["--project-dir", str(project), "secrets", "init"]) == 0

    output = capsys.readouterr().out
    assert "[FILL] environments/stage/app.env: DATABASE_URL" in output
    assert "[FILL] environments/monitoring/monitoring.env: GF_SECURITY_ADMIN_PASSWORD" in output
    assert str(_store_root()) not in output


def test_hash_password_rejects_a_mismatch_before_running_the_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    answers = iter(["first-password", "second-password"])
    monkeypatch.setattr("deploy_cli.secret_setup.getpass.getpass", lambda prompt: next(answers))
    monkeypatch.setattr(
        AnsibleRunner,
        "build_image",
        lambda self: pytest.fail("the runtime image was built for a rejected password"),
    )

    assert cli.run(["--project-dir", str(tmp_path), "secrets", "hash-password"]) == 2

    assert "do not match" in capsys.readouterr().err


def test_hash_password_prints_only_the_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    password = "synthetic-monitoring-password"  # noqa: S105 - synthetic test value
    digest = "$6$synthetic$hash"
    monkeypatch.setattr("deploy_cli.secret_setup.getpass.getpass", lambda prompt: password)
    monkeypatch.setattr(AnsibleRunner, "build_image", lambda self: None)
    monkeypatch.setattr(AnsibleRunner, "openssl_password_hash", lambda self, value: digest)

    assert cli.run(["--project-dir", str(tmp_path), "secrets", "hash-password"]) == 0

    captured = capsys.readouterr()
    assert captured.out.strip() == digest
    assert password not in captured.out + captured.err
    assert "LOKI_PUSH_PASSWORD_HASH" in captured.err
