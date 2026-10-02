from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from deploy_cli.config import ConfigurationError, load_configuration
from deploy_cli.models import EnvironmentConfig, GlobalConfig


def test_checked_in_configuration_has_supported_schema() -> None:
    repo = Path(__file__).parents[1]

    global_config, environment = load_configuration(repo, "stage")

    assert global_config.schema_version == 1
    assert environment.environment == "stage"
    assert environment.application.compose.is_absolute()


def test_unknown_environment_is_rejected() -> None:
    with pytest.raises(ConfigurationError, match="not implemented"):
        load_configuration(Path.cwd(), "prod")


@pytest.mark.parametrize(
    "path",
    ["/", "/srv", "/srv/..", "/srv/app/../other", "/srv//app", "/etc/app", "/srv/app name"],
)
def test_unsafe_remote_directory_is_rejected(path: str) -> None:
    raw = yaml.safe_load((Path(__file__).parents[1] / "environments/stage/config.yml").read_text())
    raw["application"]["remote_dir"] = path

    with pytest.raises(ValidationError, match="normalized"):
        EnvironmentConfig.model_validate(raw)


def test_unknown_timezone_is_rejected() -> None:
    raw = yaml.safe_load((Path(__file__).parents[1] / "config/global.yml").read_text())
    raw["global"]["security_updates"]["reboot"]["timezone"] = "Mars/Olympus"

    with pytest.raises(ValidationError, match="IANA"):
        GlobalConfig.model_validate(raw)


def test_unsafe_linux_user_is_rejected() -> None:
    raw = yaml.safe_load((Path(__file__).parents[1] / "environments/stage/config.yml").read_text())
    raw["server"]["deploy_user"] = "deploy;id"

    with pytest.raises(ValidationError, match="safe Linux"):
        EnvironmentConfig.model_validate(raw)
