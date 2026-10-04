import base64
import json
import warnings
from pathlib import Path

import pytest
import yaml

from deploy_cli import cli
from deploy_cli.cli import run
from deploy_cli.runner import RunnerError


def _loaded(environment: str):
    source = Path(__file__).parents[1]
    return cli.load_configuration(source, environment)


@pytest.mark.parametrize("operation", ["application", "monitoring", "collector"])
def test_operation_redactor_hides_synthetic_failure_secrets(
    tmp_path: Path, operation: str
) -> None:
    secret = f"synthetic-{operation}-failure-secret"
    if operation == "monitoring":
        _, config = _loaded("monitoring")
        config.monitoring.secrets_file = tmp_path / "monitoring.env"
        config.monitoring.secrets_file.write_text(
            f"GF_SECURITY_ADMIN_PASSWORD={secret}\n", encoding="utf-8"
        )
    else:
        _, config = _loaded("stage")
        config.application.env_file = tmp_path / "stage.env"
        config.application.env_file.write_text("APP_ENV=stage\n", encoding="utf-8")
        assert config.collector is not None
        config.collector.password_file = tmp_path / "collector.password"
        config.collector.password_file.write_text(
            secret if operation == "collector" else "collector-secret", encoding="utf-8"
        )
        if operation == "application":
            config.application.env_file.write_text(
                f"APP_ENV=stage\nDATABASE_PASSWORD={secret}\n", encoding="utf-8"
            )
            registry_password = f"{secret}-registry"
            auth = base64.b64encode(f"robot:{registry_password}".encode()).decode()
            config.application.registry_auth_file = tmp_path / "registry.json"
            config.application.registry_auth_file.write_text(
                json.dumps({"auths": {"registry.example.com": {"auth": auth}}}),
                encoding="utf-8",
            )

    redactor = cli.Redactor(cli._operation_secrets(config))
    failure = redactor(f"operation failed: {secret} {secret}-registry")

    assert secret not in failure
    assert "[REDACTED]" in failure


def test_missing_local_files_return_configuration_exit_code(tmp_path: Path) -> None:
    (tmp_path / "config").mkdir()
    (tmp_path / "environments/stage").mkdir(parents=True)
    source = Path(__file__).parents[1]
    (tmp_path / "config/global.yml").write_text(
        (source / "config/global.yml").read_text(encoding="utf-8"), encoding="utf-8"
    )
    (tmp_path / "environments/stage/config.yml").write_text(
        (source / "environments/stage/config.yml").read_text(encoding="utf-8"), encoding="utf-8"
    )

    assert run(["--repo", str(tmp_path), "stage"]) == 2


def test_public_health_failure_keeps_health_exit_code(
    tmp_path: Path, monkeypatch,
) -> None:
    source = Path(__file__).parents[1]
    (tmp_path / "config").mkdir()
    (tmp_path / "environments/stage").mkdir(parents=True)
    (tmp_path / "config/global.yml").write_text(
        (source / "config/global.yml").read_text(encoding="utf-8"), encoding="utf-8"
    )
    raw = yaml.safe_load((source / "environments/stage/config.yml").read_text(encoding="utf-8"))
    for name in ("key", "key.pub", "stage.env"):
        (tmp_path / name).write_text("placeholder", encoding="utf-8")
    (tmp_path / "stage.env").write_text("APP_ENV=stage\n", encoding="utf-8")
    (tmp_path / "compose.yml").write_text(
        "services:\n  app:\n    image: example/app\n", encoding="utf-8"
    )
    raw["server"]["ssh_key"] = str(tmp_path / "key")
    raw["server"]["public_key"] = str(tmp_path / "key.pub")
    raw["application"]["env_file"] = "stage.env"
    raw["application"]["compose"] = "compose.yml"
    (tmp_path / "environments/stage/config.yml").write_text(
        yaml.safe_dump(raw), encoding="utf-8"
    )
    monkeypatch.setattr(cli, "dns_preflight", lambda config: None)
    monkeypatch.setattr(cli, "ensure_deploy_key", lambda config: "using test key")
    monkeypatch.setattr(
        cli,
        "deploy",
        lambda *args, **kwargs: (_ for _ in ()).throw(RunnerError("health failed", 7)),
    )

    assert run(["--repo", str(tmp_path), "stage", "--version", "abcdef0"]) == 7


def test_prod_requires_explicit_confirmation(monkeypatch) -> None:
    monkeypatch.setattr(
        cli, "_load_and_validate", lambda repo, env, command, **kwargs: _loaded(env)
    )

    assert run(["prod", "--version", "abcdef0"]) == 2


def test_prod_yes_dispatches_versioned_deploy(monkeypatch) -> None:
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        cli, "_load_and_validate", lambda repo, env, command, **kwargs: _loaded(env)
    )
    monkeypatch.setattr(cli, "dns_preflight", lambda config: None)
    monkeypatch.setattr(
        cli,
        "deploy",
        lambda repo, global_config, config, runner, **kwargs: calls.append(
            (config.environment, kwargs["deployment_version"])
        ),
    )

    assert run(["prod", "--yes", "--version", "abcdef0"]) == 0
    assert calls == [("prod", "abcdef0")]


def test_prod_dry_run_does_not_require_confirmation(monkeypatch) -> None:
    monkeypatch.setattr(
        cli, "_load_and_validate", lambda repo, env, command, **kwargs: _loaded(env)
    )
    monkeypatch.setattr(cli, "dns_preflight", lambda config: None)
    monkeypatch.setattr(cli, "deploy", lambda *args, **kwargs: None)

    assert run(["prod", "--dry-run", "--version", "abcdef0"]) == 0


def test_server_update_all_is_ordered_and_reports_failure(monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        cli, "_load_and_validate", lambda repo, env, command, **kwargs: _loaded(env)
    )

    def update(repo, global_config, config, runner, **kwargs):
        calls.append(config.environment)
        if config.environment == "prod":
            raise RunnerError("prod failed", 5)

    monkeypatch.setattr(cli, "update_server", update)
    monkeypatch.setattr(cli, "update_monitoring", update)

    assert run(["server", "update", "all", "--yes"]) == 5
    assert calls == ["stage", "prod", "monitoring"]


def test_server_update_all_attempts_prod_after_stage_failure(monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        cli, "_load_and_validate", lambda repo, env, command, **kwargs: _loaded(env)
    )

    def update(repo, global_config, config, runner, **kwargs):
        calls.append(config.environment)
        if config.environment == "stage":
            raise RunnerError("stage failed", 5)

    monkeypatch.setattr(cli, "update_server", update)
    monkeypatch.setattr(cli, "update_monitoring", update)

    assert run(["server", "update", "all", "--yes"]) == 5
    assert calls == ["stage", "prod", "monitoring"]


def test_collectors_all_is_ordered_and_aggregates_after_errors(monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        cli, "_load_and_validate", lambda repo, env, command, **kwargs: _loaded(env)
    )

    def reconcile(repo, global_config, config, runner, **kwargs):
        calls.append(config.environment)
        if config.environment == "stage":
            raise RunnerError("stage collector failed", 6)

    monkeypatch.setattr(cli, "deploy_collector", reconcile)

    assert run(["collectors", "update", "all", "--yes"]) == 6
    assert calls == ["stage", "prod"]


def test_monitoring_status_dispatches_without_application_release(monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        cli, "_load_and_validate", lambda repo, env, command, **kwargs: _loaded(env)
    )
    monkeypatch.setattr(
        cli, "monitoring_status", lambda config: calls.append(config.environment)
    )
    monkeypatch.setattr(
        cli, "deploy", lambda *args, **kwargs: pytest.fail("application release was called")
    )

    assert run(["monitoring", "status"]) == 0
    assert calls == ["monitoring"]


def test_rollback_failure_keeps_rollback_exit_code(monkeypatch) -> None:
    monkeypatch.setattr(
        cli, "_load_and_validate", lambda repo, env, command, **kwargs: _loaded(env)
    )
    monkeypatch.setattr(
        cli,
        "rollback",
        lambda *args, **kwargs: (_ for _ in ()).throw(RunnerError("rollback failed", 9)),
    )

    assert run(["rollback", "prod", "--yes"]) == 9


def test_password_bootstrap_prompts_after_production_confirmation(monkeypatch) -> None:
    events: list[str] = []
    dispatched: list[str | None] = []
    monkeypatch.setattr(
        cli, "_load_and_validate", lambda repo, env, command, **kwargs: _loaded(env)
    )
    monkeypatch.setattr(cli, "dns_preflight", lambda config: None)
    monkeypatch.setattr(
        cli,
        "_confirm_production",
        lambda *args, **kwargs: events.append("confirmation"),
    )
    monkeypatch.setattr(
        cli.getpass,
        "getpass",
        lambda prompt: events.append("password") or "root-password",
    )
    monkeypatch.setattr(
        cli,
        "deploy",
        lambda *args, **kwargs: dispatched.append(kwargs["bootstrap_password"]),
    )

    assert run(["prod", "--ask-bootstrap-password", "--version", "abcdef0"]) == 0
    assert events == ["confirmation", "password"]
    assert dispatched == ["root-password"]


def test_empty_bootstrap_password_is_rejected(monkeypatch) -> None:
    monkeypatch.setattr(
        cli, "_load_and_validate", lambda repo, env, command, **kwargs: _loaded(env)
    )
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt: "")

    assert run(["stage", "--ask-bootstrap-password", "--version", "abcdef0"]) == 2


def test_password_bootstrap_is_rejected_in_dry_run_without_prompt(monkeypatch) -> None:
    prompted = False

    def prompt(message):
        nonlocal prompted
        prompted = True
        return "should-not-be-read"

    monkeypatch.setattr(cli.getpass, "getpass", prompt)

    assert (
        run(
            [
                "stage",
                "--ask-bootstrap-password",
                "--dry-run",
                "--version",
                "abcdef0",
            ]
        )
        == 2
    )
    assert prompted is False


@pytest.mark.parametrize("password", ["bad\0value", "bad\rvalue", "bad\nvalue"])
def test_bootstrap_password_rejects_protocol_delimiters(monkeypatch, password: str) -> None:
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt: password)

    with pytest.raises(cli.ConfigurationError, match="NUL, CR or LF"):
        cli._prompt_bootstrap_password("root", "server")


def test_bootstrap_password_preserves_spaces_and_tabs(monkeypatch) -> None:
    expected = "  spaced\tpassword  "
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt: expected)

    assert cli._prompt_bootstrap_password("root", "server") == expected


def test_getpass_insecure_fallback_is_rejected(monkeypatch) -> None:
    def insecure_prompt(prompt):
        warnings.warn("fallback", cli.getpass.GetPassWarning, stacklevel=2)
        return "must-not-be-used"

    monkeypatch.setattr(cli.getpass, "getpass", insecure_prompt)

    with pytest.raises(cli.ConfigurationError, match="Secure password input is unavailable"):
        cli._prompt_bootstrap_password("root", "server")
