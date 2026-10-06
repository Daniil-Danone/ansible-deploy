import base64
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from deploy_cli import cli, github_secrets, secret_file, secret_store
from deploy_cli.config import load_configuration
from deploy_cli.models import EnvironmentConfig
from deploy_cli.secret_file import secure_secret_permissions


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    # ACL verification has its own platform tests; retain native ACL creation and
    # all path/ancestry guards without repeatedly spawning whoami/icacls here.
    monkeypatch.setattr(secret_file, "validate_secret_permissions", lambda path: None)
    monkeypatch.setattr(secret_store, "validate_secret_permissions", lambda path: None)
    if os.name == "nt":
        sid = secret_file.windows_current_sid()
        monkeypatch.setattr(secret_file, "windows_current_sid", lambda: sid)
        monkeypatch.setattr(secret_store, "windows_current_sid", lambda: sid)
    project = tmp_path / "application"
    project.mkdir()
    base = tmp_path / "trusted"
    base.mkdir()
    secure_secret_permissions(base)
    root = base / "secret-store"
    monkeypatch.setenv("ANSIBLE_DEPLOY_SECRETS_DIR", str(root))
    assert cli.run(["--project-dir", str(project), "project", "init"]) == 0
    for environment in ("stage", "prod"):
        path = project / f".deploy/environments/{environment}/config.yml"
        raw = yaml.safe_load(path.read_text())
        raw["collector"] = {
            "push_url": "https://monitoring.example.com/loki/api/v1/push",
            "username": "alloy",
            "password_file": f"environments/{environment}/collector.password",
        }
        path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    assert cli.run(["--project-dir", str(project), "secrets", "init"]) == 0
    for environment in ("stage", "prod", "monitoring"):
        _, config = load_configuration(project, environment)
        paths = [config.server.ssh_key, config.server.public_key]
        if isinstance(config, EnvironmentConfig):
            paths.append(config.application.env_file)
            if config.application.registry_auth_file is not None:
                paths.append(config.application.registry_auth_file)
            if config.collector is not None:
                paths.append(config.collector.password_file)
        else:
            paths.append(config.monitoring.secrets_file)
        for path in paths:
            path.write_bytes(b"synthetic-private-value\r\n\xff")
            secure_secret_permissions(path)
    return project, root


@pytest.fixture
def gh(monkeypatch: pytest.MonkeyPatch) -> list[tuple[list[str], dict[str, Any]]]:
    calls: list[tuple[list[str], dict[str, Any]]] = []
    monkeypatch.setattr(github_secrets.shutil, "which", lambda name: "gh-test")

    def run(arguments: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        calls.append((arguments, kwargs))
        return subprocess.CompletedProcess(arguments, 0)

    monkeypatch.setattr(
        github_secrets,
        "subprocess",
        SimpleNamespace(
            run=run,
            DEVNULL=subprocess.DEVNULL,
            SubprocessError=subprocess.SubprocessError,
        ),
    )
    return calls


def _arguments(project: Path, *arguments: str) -> list[str]:
    return [
        "--project-dir",
        str(project),
        "secrets",
        "github",
        "upload",
        "--repo",
        "owner/application",
        *arguments,
    ]


def test_default_upload_dispatch_and_stdin_only(
    project: tuple[Path, Path], gh: list, capsys: pytest.CaptureFixture[str]
) -> None:
    directory, root = project
    capsys.readouterr()
    assert cli.run(_arguments(directory)) == 0
    assert [call[0][1:3] for call in gh[:2]] == [["auth", "status"], ["repo", "view"]]
    uploads = gh[2:]
    assert [call[0][-1] for call in uploads] == ["stage", "production", "monitoring"]
    output = capsys.readouterr()
    assert "production:" in output.out
    assert str(root) not in output.out + output.err
    assert "synthetic-private-value" not in output.out + output.err
    for arguments, kwargs in gh:
        assert kwargs["stdout"] == subprocess.DEVNULL
        assert kwargs["stderr"] == subprocess.DEVNULL
        assert "synthetic-private-value" not in " ".join(arguments)
        if arguments[1] == "secret":
            assert arguments == [
                "gh-test",
                "secret",
                "set",
                "ANSIBLE_DEPLOY_SECRET_STORE_JSON",
                "--repo",
                "github.com/owner/application",
                "--env",
                arguments[-1],
            ]
            values = json.loads(kwargs["input"])
            assert list(values) == sorted(values)
            for value in values.values():
                assert base64.b64decode(value) == b"synthetic-private-value\r\n\xff"
        else:
            assert kwargs["input"] is None


def test_check_subset_deduplicates_and_never_uploads(project: tuple[Path, Path], gh: list) -> None:
    directory, _ = project
    assert (
        cli.run(_arguments(directory, "--environment", "prod", "--environment", "prod", "--check"))
        == 0
    )
    assert len(gh) == 2


@pytest.mark.parametrize("check", [True, False])
def test_github_host_is_pinned_despite_environment_override(
    project: tuple[Path, Path], gh: list, monkeypatch: pytest.MonkeyPatch, check: bool
) -> None:
    directory, _ = project
    monkeypatch.setenv("GH_HOST", "github.enterprise.example.com")
    monkeypatch.setenv("GH_REPO", "github.enterprise.example.com/other/repo")
    arguments = _arguments(directory, "--environment", "stage")
    if check:
        arguments.append("--check")
    assert cli.run(arguments) == 0
    assert gh[0][0] == ["gh-test", "auth", "status", "--active", "--hostname", "github.com"]
    assert gh[1][0] == ["gh-test", "repo", "view", "github.com/owner/application"]
    if not check:
        assert gh[2][0][4:6] == ["--repo", "github.com/owner/application"]
    for _, kwargs in gh:
        assert kwargs["env"]["GH_HOST"] == "github.com"
    assert os.environ["GH_HOST"] == "github.enterprise.example.com"


def test_payload_uses_extra_env_optional_refs_and_excludes_backup(
    project: tuple[Path, Path], gh: list
) -> None:
    directory, root = project
    path = directory / ".deploy/environments/prod/config.yml"
    raw = yaml.safe_load(path.read_text())
    raw["application"]["extra_env_files"] = [
        {"source": "environments/prod/backend.env", "target": "backend.env"},
    ]
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    extra = root / "environments/prod/backend.env"
    extra.write_bytes(b"BACKEND_SECRET=extra")
    secure_secret_permissions(extra)
    assert cli.run(_arguments(directory, "--environment", "prod")) == 0
    payload = json.loads(gh[-1][1]["input"])
    assert set(payload) == {
        "keys/prod_ed25519",
        "keys/prod_ed25519.pub",
        "environments/prod/app.env",
        "environments/prod/registry-auth.json",
        "environments/prod/collector.password",
        "environments/prod/backend.env",
    }
    assert base64.b64decode(payload["environments/prod/backend.env"]) == b"BACKEND_SECRET=extra"
    assert not any("backup" in key or "restore" in key for key in payload)


def test_optional_refs_can_be_omitted(project: tuple[Path, Path], gh: list) -> None:
    directory, _ = project
    path = directory / ".deploy/environments/stage/config.yml"
    raw = yaml.safe_load(path.read_text())
    raw["application"].pop("registry_auth_file")
    raw.pop("collector")
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    assert cli.run(_arguments(directory, "--environment", "stage")) == 0
    assert len(json.loads(gh[-1][1]["input"])) == 3


def test_duplicate_file_references_have_one_entry(project: tuple[Path, Path]) -> None:
    directory, _ = project
    _, config = load_configuration(directory, "stage")
    config.server.public_key = config.server.ssh_key
    payload, count = github_secrets._payload(config)
    assert count == 4
    assert len(json.loads(payload)) == count


@pytest.mark.parametrize("damage", ["missing", "empty", "oversized", "directory"])
def test_invalid_files_prevent_all_uploads(
    project: tuple[Path, Path], gh: list, damage: str, capsys: pytest.CaptureFixture[str]
) -> None:
    directory, root = project
    secret = root / "environments/monitoring/monitoring.env"
    if damage == "missing":
        secret.unlink()
    elif damage == "empty":
        secret.write_bytes(b"")
    elif damage == "oversized":
        secret.write_bytes(b"X" * 49152)
    else:
        secret.unlink()
        secret.mkdir()
    capsys.readouterr()
    assert cli.run(_arguments(directory)) == 2
    assert not gh
    output = capsys.readouterr()
    assert str(root) not in output.out + output.err
    assert "synthetic-private-value" not in output.out + output.err


def test_unsafe_file_is_revalidated_before_read(
    project: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    directory, _ = project
    _, config = load_configuration(directory, "stage")
    config.application.env_file = directory / "outside-store.env"
    with pytest.raises(ValueError, match="application environment"):
        github_secrets._payload(config)


@pytest.mark.parametrize("failure", ["auth", "repo", "secret"])
@pytest.mark.parametrize("mode", ["returncode", "exception"])
def test_gh_failures_are_sanitized(
    project: tuple[Path, Path],
    gh: list,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failure: str,
    mode: str,
) -> None:
    directory, root = project

    def failing(arguments: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        if arguments[1] == failure:
            if mode == "exception":
                raise subprocess.CalledProcessError(
                    1, arguments, output=b"synthetic-leaked-payload", stderr=b"synthetic-token"
                )
            return subprocess.CompletedProcess(
                arguments, 1, stdout=b"synthetic-leaked-payload", stderr=b"synthetic-token"
            )
        return subprocess.CompletedProcess(arguments, 0)

    monkeypatch.setattr(github_secrets.subprocess, "run", failing)
    capsys.readouterr()
    assert cli.run(_arguments(directory, "--environment", "stage")) == 2
    output = capsys.readouterr()
    assert "[ERROR]" in output.err
    assert "synthetic" not in output.out + output.err
    assert str(root) not in output.out + output.err


def test_missing_gh_reports_prerequisite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(github_secrets.shutil, "which", lambda name: None)
    assert cli.run(_arguments(tmp_path)) == 2
    assert "gh auth login" in capsys.readouterr().err


@pytest.mark.parametrize(
    "repository", ["", "https://github.com/a/b", "a", "a/b/c", "a/..", "a/b b"]
)
def test_invalid_repository_is_rejected(repository: str) -> None:
    with pytest.raises(SystemExit) as error:
        cli._parser().parse_args(["secrets", "github", "upload", "--repo", repository])
    assert error.value.code == 2


def test_repository_argument_is_required() -> None:
    with pytest.raises(SystemExit):
        cli._parser().parse_args(["secrets", "github", "upload"])


def test_legacy_global_repo_alias_does_not_supply_github_repository(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        cli._parser().parse_args(["--repo", str(tmp_path), "secrets", "github", "upload"])
