import base64
import json
import shutil
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from deploy_cli.config import (
    ConfigurationError,
    load_configuration,
    validate_compose,
    validate_environment_file,
    validate_observability_inputs,
    validate_production_isolation,
    validate_registry_auth,
)
from deploy_cli.models import EnvironmentConfig, GlobalConfig, MonitoringConfig
from deploy_cli.secret_file import secure_secret_permissions


def test_checked_in_configuration_has_supported_schema() -> None:
    repo = Path(__file__).parents[1]

    global_config, environment = load_configuration(repo, "stage")

    assert global_config.schema_version == 1
    assert environment.environment == "stage"
    assert environment.application.compose.is_absolute()


def test_unknown_environment_is_rejected() -> None:
    with pytest.raises(ConfigurationError, match="not implemented"):
        load_configuration(Path.cwd(), "development")


def test_checked_in_monitoring_configuration_is_supported() -> None:
    repo = Path(__file__).parents[1]

    _, monitoring = load_configuration(repo, "monitoring")

    assert monitoring.environment == "monitoring"
    assert monitoring.monitoring.retention_days == 30


def test_monitoring_and_collector_secret_contracts_are_validated(tmp_path: Path) -> None:
    repo = Path(__file__).parents[1]
    _, monitoring = load_configuration(repo, "monitoring")
    monitoring_secret = tmp_path / "monitoring.env"
    monitoring_secret.write_text(
        "GF_SECURITY_ADMIN_USER=admin\n"
        "GF_SECURITY_ADMIN_PASSWORD=synthetic-admin-password\n"
        "LOKI_PUSH_USERNAME=alloy\n"
        "LOKI_PUSH_PASSWORD_HASH=$6$synthetic-hash\n",
        encoding="utf-8",
    )
    secure_secret_permissions(monitoring_secret)
    monitoring.monitoring.secrets_file = monitoring_secret

    _, stage = load_configuration(repo, "stage")
    collector_secret = tmp_path / "collector.password"
    collector_secret.write_text("synthetic-push-password\n", encoding="utf-8")
    secure_secret_permissions(collector_secret)
    assert stage.collector is not None
    stage.collector.password_file = collector_secret

    validate_observability_inputs(monitoring)
    validate_observability_inputs(stage)


def test_checked_in_production_configuration_is_isolated() -> None:
    repo = Path(__file__).parents[1]
    _, stage = load_configuration(repo, "stage")
    _, prod = load_configuration(repo, "prod")

    assert prod.environment == "prod"
    assert prod.application.compose != stage.application.compose
    assert prod.application.env_file != stage.application.env_file
    assert prod.application.remote_dir != stage.application.remote_dir
    validate_compose(prod)


def test_configuration_environment_mismatch_is_rejected(tmp_path: Path) -> None:
    source = Path(__file__).parents[1]
    (tmp_path / "config").mkdir()
    (tmp_path / "environments/prod").mkdir(parents=True)
    (tmp_path / "config/global.yml").write_text(
        (source / "config/global.yml").read_text(encoding="utf-8"), encoding="utf-8"
    )
    raw = yaml.safe_load((source / "environments/prod/config.yml").read_text())
    raw["environment"] = "stage"
    (tmp_path / "environments/prod/config.yml").write_text(yaml.safe_dump(raw), encoding="utf-8")

    with pytest.raises(ConfigurationError, match="mismatch"):
        load_configuration(tmp_path, "prod")


def test_env_file_must_match_selected_environment(tmp_path: Path) -> None:
    repo = Path(__file__).parents[1]
    _, prod = load_configuration(repo, "prod")
    env_file = tmp_path / "prod.env"
    env_file.write_text("APP_ENV=stage\nSECRET=not-printed\n", encoding="utf-8")
    prod.application.env_file = env_file

    with pytest.raises(ConfigurationError, match="does not match") as raised:
        validate_environment_file(prod)

    assert "not-printed" not in str(raised.value)


def test_production_cannot_reuse_stage_runtime() -> None:
    repo = Path(__file__).parents[1]
    _, stage = load_configuration(repo, "stage")
    _, prod = load_configuration(repo, "prod")
    prod.application.remote_dir = stage.application.remote_dir

    with pytest.raises(ConfigurationError, match="remote runtime"):
        validate_production_isolation(repo, prod)


def test_production_cannot_alias_stage_host(monkeypatch) -> None:
    repo = Path(__file__).parents[1]
    _, prod = load_configuration(repo, "prod")
    prod.server.host = "prod-vps.example.com"

    def resolve(host, port, *, type):
        del host, port, type
        return [(2, 1, 6, "", ("192.0.2.10", 0))]

    monkeypatch.setattr("deploy_cli.config.socket.getaddrinfo", resolve)

    with pytest.raises(ConfigurationError, match="same address"):
        validate_production_isolation(repo, prod)


@pytest.mark.parametrize(
    "path",
    ["/", "/srv", "/srv/..", "/srv/app/../other", "/srv//app", "/etc/app", "/srv/app name"],
)
def test_unsafe_remote_directory_is_rejected(path: str) -> None:
    raw = yaml.safe_load((Path(__file__).parents[1] / "environments/stage/config.yml").read_text())
    raw["application"]["remote_dir"] = path

    with pytest.raises(ValidationError, match="normalized"):
        EnvironmentConfig.model_validate(raw)


@pytest.mark.parametrize(
    "push_url",
    [
        "http://grafana.example.com/loki/api/v1/push",
        "https://user:secret@grafana.example.com/loki/api/v1/push",
        "https://grafana.example.com/loki/api/v1/push?tenant=one",
        "https://grafana.example.com/loki/api/v1/push?",
        "https://grafana.example.com/loki/api/v1/push#fragment",
        "https://grafana.example.com/loki/api/v1/push#",
        " https://grafana.example.com/loki/api/v1/push",
        "https://grafana.example.com/loki/api/v1/push ",
        "https://grafana.example.com/loki/api/v1/push/extra",
        "https://grafana.example.com:70000/loki/api/v1/push",
        "https://grafana.example.com:/loki/api/v1/push",
        "https:///loki/api/v1/push",
    ],
)
def test_collector_push_url_rejects_noncanonical_or_credentialed_urls(
    push_url: str,
) -> None:
    raw = yaml.safe_load(
        (Path(__file__).parents[1] / "environments/stage/config.yml").read_text()
    )
    raw["collector"]["push_url"] = push_url

    with pytest.raises(ValidationError, match="push_url") as raised:
        EnvironmentConfig.model_validate(raw)

    assert "user:secret" not in str(raised.value)


@pytest.mark.parametrize(
    "push_url",
    [
        "https://grafana.example.com/loki/api/v1/push",
        "https://grafana.example.com:8443/loki/api/v1/push",
        "https://192.0.2.30/loki/api/v1/push",
        "https://[2001:db8::30]:8443/loki/api/v1/push",
    ],
)
def test_collector_push_url_accepts_only_structured_https_authorities(push_url: str) -> None:
    raw = yaml.safe_load(
        (Path(__file__).parents[1] / "environments/stage/config.yml").read_text()
    )
    raw["collector"]["push_url"] = push_url

    assert EnvironmentConfig.model_validate(raw).collector.push_url == push_url  # type: ignore[union-attr]


@pytest.mark.parametrize(
    ("field", "remote_dir"),
    [
        ("application", "/"),
        ("application", "/etc/app"),
        ("collector", "/opt"),
        ("collector", "/var/lib/alloy"),
        ("collector", "/srv/../etc/alloy"),
    ],
)
def test_managed_remote_directories_reject_system_roots(
    field: str, remote_dir: str
) -> None:
    raw = yaml.safe_load(
        (Path(__file__).parents[1] / "environments/stage/config.yml").read_text()
    )
    raw[field]["remote_dir"] = remote_dir

    with pytest.raises(ValidationError, match="dedicated directory"):
        EnvironmentConfig.model_validate(raw)


@pytest.mark.parametrize(
    ("app_dir", "collector_dir"),
    [
        ("/srv/myapp", "/srv/myapp"),
        ("/srv/myapp", "/srv/myapp/alloy"),
        ("/srv/myapp/runtime", "/srv/myapp"),
    ],
)
def test_collector_remote_directory_cannot_overlap_application(
    app_dir: str, collector_dir: str
) -> None:
    raw = yaml.safe_load(
        (Path(__file__).parents[1] / "environments/stage/config.yml").read_text()
    )
    raw["application"]["remote_dir"] = app_dir
    raw["collector"]["remote_dir"] = collector_dir

    with pytest.raises(ValidationError, match="must not equal, contain or be contained"):
        EnvironmentConfig.model_validate(raw)


@pytest.mark.parametrize("remote_dir", ["/", "/opt", "/etc/monitoring", "/srv/../etc"])
def test_monitoring_remote_directory_requires_dedicated_opt_or_srv_leaf(
    remote_dir: str,
) -> None:
    raw = yaml.safe_load(
        (Path(__file__).parents[1] / "environments/monitoring/config.yml").read_text()
    )
    raw["monitoring"]["remote_dir"] = remote_dir

    with pytest.raises(ValidationError, match="dedicated directory"):
        MonitoringConfig.model_validate(raw)


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


def test_portable_registry_auth_is_accepted_for_compose_registry(tmp_path: Path) -> None:
    repo = Path(__file__).parents[1]
    _, config = load_configuration(repo, "stage")
    compose = tmp_path / "compose.yml"
    compose.write_text(
        "services:\n  app:\n    image: ghcr.io/acme/app@sha256:" + "a" * 64 + "\n",
        encoding="utf-8",
    )
    auth = tmp_path / "registry-auth.json"
    encoded = base64.b64encode(b"octocat:token").decode()
    auth.write_text(json.dumps({"auths": {"ghcr.io": {"auth": encoded}}}), encoding="utf-8")
    secure_secret_permissions(auth)
    config.application.compose = compose
    config.application.registry_auth_file = auth

    validate_registry_auth(config)


@pytest.mark.parametrize(
    ("document", "message"),
    [
        ({"auths": {}, "credsStore": "desktop"}, "credential helpers"),
        ({"auths": {}}, "cannot be empty"),
        ({"auths": {"ghcr.io": {}}}, "inline auth"),
        ({"auths": {"ghcr.io": {"auth": "not base64"}}}, "invalid inline"),
        (
            {"auths": {"ghcr.io": {"auth": base64.b64encode(b"user:").decode()}}},
            "incomplete inline",
        ),
    ],
)
def test_nonportable_registry_auth_is_rejected_without_secret_disclosure(
    tmp_path: Path, document: dict[str, object], message: str
) -> None:
    repo = Path(__file__).parents[1]
    _, config = load_configuration(repo, "stage")
    auth = tmp_path / "registry-auth.json"
    auth.write_text(json.dumps(document), encoding="utf-8")
    config.application.registry_auth_file = auth

    with pytest.raises(ConfigurationError, match=message) as raised:
        validate_registry_auth(config)

    assert "not base64" not in str(raised.value)


def test_registry_auth_for_unrelated_compose_host_is_rejected(tmp_path: Path) -> None:
    repo = Path(__file__).parents[1]
    _, config = load_configuration(repo, "stage")
    auth = tmp_path / "registry-auth.json"
    encoded = base64.b64encode(b"octocat:token").decode()
    auth.write_text(json.dumps({"auths": {"registry.example.com": {"auth": encoded}}}))
    config.application.registry_auth_file = auth

    with pytest.raises(ConfigurationError, match="host not used"):
        validate_registry_auth(config)


@pytest.mark.parametrize(
    "collision",
    [
        "deploy/compose.stage.yml",
        "environments/stage/app.env",
        "keys/stage_ed25519",
        "keys/stage_ed25519.pub",
        ".deploy/images.yml",
        ".deploy/environments/stage/config.yml",
        "README.md",
    ],
)
def test_registry_auth_cannot_collide_with_project_inputs(
    tmp_path: Path, collision: str
) -> None:
    source = Path(__file__).parents[1] / "examples/demo-app"
    project = tmp_path / "demo"
    shutil.copytree(source, project)
    path = project / ".deploy/environments/stage/config.yml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    raw["schema_version"] = 1
    raw["application"]["registry_auth_file"] = collision
    path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")

    with pytest.raises(ConfigurationError, match="collides with protected"):
        load_configuration(project, "stage")


@pytest.mark.parametrize("absolute", [False, True])
def test_registry_auth_must_stay_inside_project(tmp_path: Path, absolute: bool) -> None:
    source = Path(__file__).parents[1] / "examples/demo-app"
    project = tmp_path / "demo"
    shutil.copytree(source, project)
    path = project / ".deploy/environments/stage/config.yml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    raw["schema_version"] = 1
    raw["application"]["registry_auth_file"] = (
        str((tmp_path.parent / "outside-secret.json").resolve())
        if absolute
        else "../secret.json"
    )
    path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")

    with pytest.raises(ConfigurationError, match="inside the project"):
        load_configuration(project, "stage")
