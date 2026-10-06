import io
import os
import shutil
from pathlib import Path

import pytest

from deploy_cli import cli
from deploy_cli.runner import AnsibleRunner, RunnerError
from deploy_cli.secret_file import secure_secret_permissions, validate_secret_permissions

_SALT = "syntheticsalt"
_MONITORING_ENV = (
    "# monitoring secrets, keep the comments\n"
    "GF_SECURITY_ADMIN_USER=admin\n"
    "GF_SECURITY_ADMIN_PASSWORD=synthetic-grafana-password\n"
    "\n"
    "LOKI_PUSH_USERNAME=alloy\n"
    f"LOKI_PUSH_PASSWORD_HASH=$6${_SALT}$staleleftoverdigest\n"
    "# trailing comment\n"
)


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
    assert cli.run(["--project-dir", str(project), "secrets", "init"]) == 0
    monitoring = _store_root() / "environments/monitoring/monitoring.env"
    monitoring.write_text(_MONITORING_ENV, encoding="utf-8", newline="")
    for environment in ("stage", "prod"):
        key = _store_root() / f"keys/{environment}_ed25519"
        key.write_text("synthetic private key\n", encoding="utf-8")
        secure_secret_permissions(key)
    return project


def _fake_hashing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hash a password as ``$6$<salt>$<password>``: comparable without Docker or openssl."""
    monkeypatch.setattr(AnsibleRunner, "build_image", lambda self: None)
    monkeypatch.setattr(
        AnsibleRunner,
        "openssl_password_hash",
        lambda self, password: f"$6${_SALT}${password}",
    )
    monkeypatch.setattr(
        AnsibleRunner,
        "openssl_password_hash_with_salt",
        lambda self, password, salt: f"$6${salt}${password}",
    )


def _reject_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        AnsibleRunner,
        "build_image",
        lambda self: pytest.fail("the runtime image was built for rejected input"),
    )


def _collector_password(environment: str) -> str:
    path = _store_root() / f"environments/{environment}/collector.password"
    return path.read_text(encoding="utf-8")


def _write_matching_passwords(password: str) -> None:
    monitoring = _store_root() / "environments/monitoring/monitoring.env"
    monitoring.write_text(
        _MONITORING_ENV.replace("$staleleftoverdigest", f"${password}"),
        encoding="utf-8",
        newline="",
    )
    for environment in ("stage", "prod"):
        path = _store_root() / f"environments/{environment}/collector.password"
        path.write_text(password, encoding="utf-8", newline="")


def test_rotate_writes_both_environments_without_a_trailing_line_ending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _demo_project(tmp_path)
    _fake_hashing(monkeypatch)

    assert cli.run(["--project-dir", str(project), "secrets", "rotate-collector-password"]) == 0

    stage = _collector_password("stage")
    assert stage == _collector_password("prod")
    assert stage and not stage.endswith(("\n", "\r"))
    for environment in ("stage", "prod"):
        validate_secret_permissions(
            _store_root() / f"environments/{environment}/collector.password"
        )
    output = capsys.readouterr()
    assert "[UPDATE] environments/stage/collector.password" in output.out
    assert "[UPDATE] environments/prod/collector.password" in output.out
    assert "[UPDATE] environments/monitoring/monitoring.env" in output.out
    assert "[OK] collector push password rotated" in output.out
    assert "deploy monitoring update" in output.err


def test_rotate_keeps_the_rest_of_the_monitoring_file_byte_for_byte(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _demo_project(tmp_path)
    _fake_hashing(monkeypatch)
    monitoring = _store_root() / "environments/monitoring/monitoring.env"

    assert cli.run(["--project-dir", str(project), "secrets", "rotate-collector-password"]) == 0

    before = _MONITORING_ENV.splitlines(keepends=True)
    # Path.read_text(newline=...) needs Python 3.13; decode the bytes instead.
    after = monitoring.read_bytes().decode("utf-8").splitlines(keepends=True)
    assert len(after) == len(before)
    changed = [index for index, line in enumerate(after) if line != before[index]]
    assert len(changed) == 1
    rotated = _collector_password("stage")
    assert after[changed[0]] == f"LOKI_PUSH_PASSWORD_HASH=$6${_SALT}${rotated}\n"
    validate_secret_permissions(monitoring)


def test_rotate_only_touches_the_requested_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _demo_project(tmp_path)
    _fake_hashing(monkeypatch)
    untouched = "synthetic-prod-password"
    (_store_root() / "environments/prod/collector.password").write_text(
        untouched, encoding="utf-8", newline=""
    )

    assert (
        cli.run(
            [
                "--project-dir",
                str(project),
                "secrets",
                "rotate-collector-password",
                "--environment",
                "stage",
            ]
        )
        == 0
    )

    assert _collector_password("prod") == untouched
    assert _collector_password("stage") != untouched


def test_rotate_never_prints_the_generated_password(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _demo_project(tmp_path)
    _fake_hashing(monkeypatch)

    assert cli.run(["--project-dir", str(project), "secrets", "rotate-collector-password"]) == 0

    captured = capsys.readouterr()
    assert _collector_password("stage") not in captured.out + captured.err
    assert str(_store_root()) not in captured.out


def test_rotate_rejects_a_monitoring_file_without_the_hash_variable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _demo_project(tmp_path)
    _fake_hashing(monkeypatch)
    monitoring = _store_root() / "environments/monitoring/monitoring.env"
    monitoring.write_text("LOKI_PUSH_USERNAME=alloy\n", encoding="utf-8", newline="")
    (_store_root() / "environments/stage/collector.password").write_text(
        "synthetic-stage-password", encoding="utf-8", newline=""
    )

    assert cli.run(["--project-dir", str(project), "secrets", "rotate-collector-password"]) == 2

    assert "LOKI_PUSH_PASSWORD_HASH= line" in capsys.readouterr().err
    # Nothing is written when the replacement cannot be built.
    assert monitoring.read_text(encoding="utf-8") == "LOKI_PUSH_USERNAME=alloy\n"
    assert _collector_password("stage") == "synthetic-stage-password"


@pytest.mark.parametrize("supplied", ["", "\n", "bad\nvalue\n"])
def test_rotate_password_stdin_rejects_unusable_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, supplied: str
) -> None:
    project = _demo_project(tmp_path)
    _reject_runner(monkeypatch)
    monkeypatch.setattr("sys.stdin", io.StringIO(supplied))

    assert (
        cli.run(
            [
                "--project-dir",
                str(project),
                "secrets",
                "rotate-collector-password",
                "--password-stdin",
            ]
        )
        == 2
    )


def test_rotate_password_stdin_stores_the_supplied_password(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _demo_project(tmp_path)
    _fake_hashing(monkeypatch)
    supplied = "synthetic-stdin-password"
    monkeypatch.setattr("sys.stdin", io.StringIO(f"{supplied}\n"))

    assert (
        cli.run(
            [
                "--project-dir",
                str(project),
                "secrets",
                "rotate-collector-password",
                "--password-stdin",
            ]
        )
        == 0
    )

    assert _collector_password("stage") == supplied


def test_check_reports_success_for_matching_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _demo_project(tmp_path)
    _fake_hashing(monkeypatch)
    _write_matching_passwords("synthetic-matching-password")

    assert cli.run(["--project-dir", str(project), "secrets", "check-collector-password"]) == 0

    output = capsys.readouterr().out
    assert "[OK] stage collector password matches the monitoring hash" in output
    assert "[OK] prod collector password matches the monitoring hash" in output


def test_check_reports_a_mismatch_with_the_configuration_exit_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _demo_project(tmp_path)
    _fake_hashing(monkeypatch)
    _write_matching_passwords("synthetic-matching-password")
    (_store_root() / "environments/prod/collector.password").write_text(
        "synthetic-drifted-password", encoding="utf-8", newline=""
    )

    assert cli.run(["--project-dir", str(project), "secrets", "check-collector-password"]) == 2

    captured = capsys.readouterr()
    assert "[OK] stage collector password matches the monitoring hash" in captured.out
    assert "[ERROR] prod collector password does not match the monitoring hash" in captured.err
    assert "deploy secrets rotate-collector-password" in captured.err


def test_check_rejects_an_unusable_salt_before_reaching_docker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _demo_project(tmp_path)
    _reject_runner(monkeypatch)
    monitoring = _store_root() / "environments/monitoring/monitoring.env"
    monitoring.write_text(
        _MONITORING_ENV.replace(
            f"$6${_SALT}$staleleftoverdigest", "$6$--salt; rm -rf /$digest"
        ),
        encoding="utf-8",
        newline="",
    )
    (_store_root() / "environments/stage/collector.password").write_text(
        "synthetic-stage-password", encoding="utf-8", newline=""
    )

    assert (
        cli.run(["--project-dir", str(project), "secrets", "check-collector-password", "stage"])
        == 2
    )

    assert "not a valid crypt SHA-512 hash" in capsys.readouterr().err


def test_collectors_deploy_preflight_rejects_a_drifted_password(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _demo_project(tmp_path)
    _fake_hashing(monkeypatch)
    _write_matching_passwords("synthetic-matching-password")
    (_store_root() / "environments/stage/collector.password").write_text(
        "synthetic-drifted-password", encoding="utf-8", newline=""
    )
    monkeypatch.setattr(
        cli,
        "deploy_collector",
        lambda *args, **kwargs: pytest.fail("a drifted password reached the server"),
    )

    assert cli.run(["--project-dir", str(project), "collectors", "deploy", "stage"]) == 2

    error = capsys.readouterr().err
    assert "Collector password for stage does not match LOKI_PUSH_PASSWORD_HASH" in error
    assert "deploy secrets rotate-collector-password" in error


def test_collectors_deploy_preflight_accepts_matching_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _demo_project(tmp_path)
    _fake_hashing(monkeypatch)
    _write_matching_passwords("synthetic-matching-password")
    deployed: list[str] = []
    monkeypatch.setattr(
        cli,
        "deploy_collector",
        lambda repo, global_config, config, runner, **kwargs: deployed.append(config.environment),
    )

    assert cli.run(["--project-dir", str(project), "collectors", "deploy", "stage"]) == 0

    assert deployed == ["stage"]


def test_collectors_status_skips_the_password_preflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _demo_project(tmp_path)
    _write_matching_passwords("synthetic-matching-password")
    _reject_runner(monkeypatch)
    monkeypatch.setattr(cli, "collector_status", lambda *args, **kwargs: None)

    assert cli.run(["--project-dir", str(project), "collectors", "status", "stage"]) == 0


def test_collectors_deploy_skips_the_preflight_without_a_monitoring_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _demo_project(tmp_path)
    _write_matching_passwords("synthetic-matching-password")
    shutil.rmtree(project / ".deploy/environments/monitoring")
    _reject_runner(monkeypatch)
    deployed: list[str] = []
    monkeypatch.setattr(
        cli,
        "deploy_collector",
        lambda repo, global_config, config, runner, **kwargs: deployed.append(config.environment),
    )

    assert cli.run(["--project-dir", str(project), "collectors", "deploy", "stage"]) == 0

    assert deployed == ["stage"]


def test_collectors_deploy_warns_but_continues_when_hashing_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _demo_project(tmp_path)
    _write_matching_passwords("synthetic-matching-password")

    def unavailable(self: AnsibleRunner) -> None:
        raise RunnerError("docker daemon is unreachable", 5)

    monkeypatch.setattr(AnsibleRunner, "build_image", unavailable)
    deployed: list[str] = []
    monkeypatch.setattr(
        cli,
        "deploy_collector",
        lambda repo, global_config, config, runner, **kwargs: deployed.append(config.environment),
    )

    assert cli.run(["--project-dir", str(project), "collectors", "deploy", "stage"]) == 0

    assert deployed == ["stage"]
    assert (
        "[WARN] unable to verify the collector password against the monitoring hash"
        in capsys.readouterr().err
    )
