from pathlib import Path

import yaml

from deploy_cli import cli
from deploy_cli.cli import run
from deploy_cli.runner import RunnerError


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

    assert run(["--repo", str(tmp_path), "stage"]) == 7
