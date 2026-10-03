from pathlib import Path

import yaml

from deploy_cli import cli
from deploy_cli.cli import run
from deploy_cli.runner import RunnerError


def _loaded(environment: str):
    source = Path(__file__).parents[1]
    return cli.load_configuration(source, environment)


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
    monkeypatch.setattr(
        cli,
        "deploy",
        lambda *args, **kwargs: (_ for _ in ()).throw(RunnerError("health failed", 7)),
    )

    assert run(["--repo", str(tmp_path), "stage", "--version", "abcdef0"]) == 7


def test_prod_requires_explicit_confirmation(monkeypatch) -> None:
    monkeypatch.setattr(cli, "_load_and_validate", lambda repo, env, command: _loaded(env))

    assert run(["prod", "--version", "abcdef0"]) == 2


def test_prod_yes_dispatches_versioned_deploy(monkeypatch) -> None:
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(cli, "_load_and_validate", lambda repo, env, command: _loaded(env))
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
    monkeypatch.setattr(cli, "_load_and_validate", lambda repo, env, command: _loaded(env))
    monkeypatch.setattr(cli, "dns_preflight", lambda config: None)
    monkeypatch.setattr(cli, "deploy", lambda *args, **kwargs: None)

    assert run(["prod", "--dry-run", "--version", "abcdef0"]) == 0


def test_server_update_all_is_ordered_and_reports_failure(monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(cli, "_load_and_validate", lambda repo, env, command: _loaded(env))

    def update(repo, global_config, config, runner, **kwargs):
        calls.append(config.environment)
        if config.environment == "prod":
            raise RunnerError("prod failed", 5)

    monkeypatch.setattr(cli, "update_server", update)

    assert run(["server", "update", "all", "--yes"]) == 5
    assert calls == ["stage", "prod"]


def test_server_update_all_attempts_prod_after_stage_failure(monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(cli, "_load_and_validate", lambda repo, env, command: _loaded(env))

    def update(repo, global_config, config, runner, **kwargs):
        calls.append(config.environment)
        if config.environment == "stage":
            raise RunnerError("stage failed", 5)

    monkeypatch.setattr(cli, "update_server", update)

    assert run(["server", "update", "all", "--yes"]) == 5
    assert calls == ["stage", "prod"]


def test_rollback_failure_keeps_rollback_exit_code(monkeypatch) -> None:
    monkeypatch.setattr(cli, "_load_and_validate", lambda repo, env, command: _loaded(env))
    monkeypatch.setattr(
        cli,
        "rollback",
        lambda *args, **kwargs: (_ for _ in ()).throw(RunnerError("rollback failed", 9)),
    )

    assert run(["rollback", "prod", "--yes"]) == 9
