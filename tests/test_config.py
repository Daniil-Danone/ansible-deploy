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

ROOT = Path(__file__).parents[1]
DEMO = ROOT / "examples/demo-app"
FIXTURE = DEMO / ".deploy"
PROJECT_TEMPLATE = ROOT / "src/deploy_cli/templates/project/.deploy"


def _use_test_secret(config: EnvironmentConfig | MonitoringConfig, path: Path) -> None:
    config.set_external_secret_context(DEMO, path.parent, path.parent, False)


def test_checked_in_configuration_has_supported_schema() -> None:
    repo = DEMO

    global_config, environment = load_configuration(repo, "stage")

    assert global_config.schema_version == 1
    assert environment.environment == "stage"
    assert environment.application.compose.is_absolute()


def test_unknown_environment_is_rejected() -> None:
    with pytest.raises(ConfigurationError, match="not implemented"):
        load_configuration(Path.cwd(), "development")


def test_checked_in_monitoring_configuration_is_supported() -> None:
    repo = DEMO

    _, monitoring = load_configuration(repo, "monitoring")

    assert monitoring.environment == "monitoring"
    assert monitoring.monitoring.retention_days == 30


def test_monitoring_and_collector_secret_contracts_are_validated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("deploy_cli.config.validate_external_input_for_use", lambda *a, **k: None)
    repo = DEMO
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
    _use_test_secret(monitoring, monitoring_secret)

    _, stage = load_configuration(repo, "stage")
    collector_secret = tmp_path / "collector.password"
    collector_secret.write_text("synthetic-push-password\n", encoding="utf-8")
    secure_secret_permissions(collector_secret)
    assert stage.collector is not None
    stage.collector.password_file = collector_secret
    _use_test_secret(stage, collector_secret)

    validate_observability_inputs(monitoring)
    validate_observability_inputs(stage)


def test_checked_in_production_configuration_is_isolated() -> None:
    repo = DEMO
    _, stage = load_configuration(repo, "stage")
    _, prod = load_configuration(repo, "prod")

    assert prod.environment == "prod"
    assert prod.application.compose != stage.application.compose
    assert prod.application.env_file != stage.application.env_file
    assert prod.application.remote_dir != stage.application.remote_dir
    validate_compose(prod)


def test_configuration_environment_mismatch_is_rejected(tmp_path: Path) -> None:
    source = FIXTURE
    (tmp_path / ".deploy/config").mkdir(parents=True)
    (tmp_path / ".deploy/environments/prod").mkdir(parents=True)
    (tmp_path / ".deploy/project-id").write_text("11111111-1111-4111-8111-111111111111\n")
    (tmp_path / ".deploy/config/global.yml").write_text(
        (source / "config/global.yml").read_text(encoding="utf-8"), encoding="utf-8"
    )
    raw = yaml.safe_load((source / "environments/prod/config.yml").read_text())
    raw["environment"] = "stage"
    (tmp_path / ".deploy/environments/prod/config.yml").write_text(
        yaml.safe_dump(raw), encoding="utf-8"
    )

    with pytest.raises(ConfigurationError, match="mismatch"):
        load_configuration(tmp_path, "prod")


def test_env_file_must_match_selected_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("deploy_cli.config.validate_external_input_for_use", lambda *a, **k: None)
    repo = DEMO
    _, prod = load_configuration(repo, "prod")
    env_file = tmp_path / "prod.env"
    env_file.write_text("APP_ENV=stage\nSECRET=not-printed\n", encoding="utf-8")
    prod.application.env_file = env_file
    _use_test_secret(prod, env_file)

    with pytest.raises(ConfigurationError, match="does not match") as raised:
        validate_environment_file(prod)

    assert "not-printed" not in str(raised.value)


def test_production_cannot_reuse_stage_runtime() -> None:
    repo = DEMO
    _, stage = load_configuration(repo, "stage")
    _, prod = load_configuration(repo, "prod")
    prod.application.remote_dir = stage.application.remote_dir

    with pytest.raises(ConfigurationError, match="remote runtime"):
        validate_production_isolation(repo, prod)


def test_production_cannot_alias_stage_host(monkeypatch) -> None:
    repo = DEMO
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
    raw = yaml.safe_load((FIXTURE / "environments/stage/config.yml").read_text())
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
        (FIXTURE / "environments/stage/config.yml").read_text()
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
        (FIXTURE / "environments/stage/config.yml").read_text()
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
        (FIXTURE / "environments/stage/config.yml").read_text()
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
        (FIXTURE / "environments/stage/config.yml").read_text()
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
        (FIXTURE / "environments/monitoring/config.yml").read_text()
    )
    raw["monitoring"]["remote_dir"] = remote_dir

    with pytest.raises(ValidationError, match="dedicated directory"):
        MonitoringConfig.model_validate(raw)


def test_unknown_timezone_is_rejected() -> None:
    raw = yaml.safe_load((FIXTURE / "config/global.yml").read_text())
    raw["global"]["security_updates"]["reboot"]["timezone"] = "Mars/Olympus"

    with pytest.raises(ValidationError, match="IANA"):
        GlobalConfig.model_validate(raw)


def test_unsafe_linux_user_is_rejected() -> None:
    raw = yaml.safe_load((FIXTURE / "environments/stage/config.yml").read_text())
    raw["server"]["deploy_user"] = "deploy;id"

    with pytest.raises(ValidationError, match="safe Linux"):
        EnvironmentConfig.model_validate(raw)


def test_portable_registry_auth_is_accepted_for_compose_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("deploy_cli.config.validate_external_input_for_use", lambda *a, **k: None)
    repo = DEMO
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
    _use_test_secret(config, auth)

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
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    document: dict[str, object],
    message: str,
) -> None:
    monkeypatch.setattr("deploy_cli.config.validate_external_input_for_use", lambda *a, **k: None)
    repo = DEMO
    _, config = load_configuration(repo, "stage")
    auth = tmp_path / "registry-auth.json"
    auth.write_text(json.dumps(document), encoding="utf-8")
    config.application.registry_auth_file = auth
    _use_test_secret(config, auth)

    with pytest.raises(ConfigurationError, match=message) as raised:
        validate_registry_auth(config)

    assert "not base64" not in str(raised.value)


def test_registry_auth_for_unrelated_compose_host_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("deploy_cli.config.validate_external_input_for_use", lambda *a, **k: None)
    repo = DEMO
    _, config = load_configuration(repo, "stage")
    auth = tmp_path / "registry-auth.json"
    encoded = base64.b64encode(b"octocat:token").decode()
    auth.write_text(json.dumps({"auths": {"registry.example.com": {"auth": encoded}}}))
    config.application.registry_auth_file = auth
    _use_test_secret(config, auth)

    with pytest.raises(ConfigurationError, match="host not used"):
        validate_registry_auth(config)


@pytest.mark.parametrize("environment", ["stage", "prod", "monitoring", "restore"])
def test_schema_v1_environment_is_rejected_before_external_store_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, environment: str
) -> None:
    source = DEMO
    project = tmp_path / "demo"
    shutil.copytree(source, project)
    path = project / f".deploy/environments/{environment}/config.yml"
    if environment == "restore":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((PROJECT_TEMPLATE / "environments/restore/config.yml").read_bytes())
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    raw["schema_version"] = 1
    path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    touched = False

    def external_store(*args, **kwargs):
        nonlocal touched
        del args, kwargs
        touched = True
        raise AssertionError("external store must not be accessed")

    monkeypatch.setattr("deploy_cli.config.external_secret_location", external_store)

    with pytest.raises(ConfigurationError, match="only schema_version: 2 is supported"):
        load_configuration(project, environment)
    assert not touched


def test_legacy_root_layout_is_not_used_when_dot_deploy_is_missing(tmp_path: Path) -> None:
    project = tmp_path / "legacy-project"
    shutil.copytree(FIXTURE / "config", project / "config")
    shutil.copytree(FIXTURE / "environments", project / "environments")
    stage = project / "environments/stage/config.yml"
    raw = yaml.safe_load(stage.read_text(encoding="utf-8"))
    raw["schema_version"] = 1
    stage.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")

    with pytest.raises(ConfigurationError, match=r"Invalid configuration .*\.deploy"):
        load_configuration(project, "stage")


@pytest.mark.parametrize(
    "environment",
    [
        {"POSTGRES_PASSWORD": "inline"},
        ["API_TOKEN=inline"],
        {"DATABASE_URL": "postgresql://user:password@db/app"},
        {"UPSTREAM": "${SERVICE_SECRET}"},
        {"UPSTREAM": "${AWS_ACCESS_KEY_ID}"},
        {"UPSTREAM": "${AUTH_BEARER}"},
        {"UPSTREAM": "${GITHUB_PAT}"},
    ],
)
def test_compose_rejects_inline_secret_like_environment(
    tmp_path: Path, environment: object
) -> None:
    _, config = load_configuration(DEMO, "stage")
    compose = tmp_path / "compose.yml"
    compose.write_text(
        yaml.safe_dump({"services": {"app": {"environment": environment}}}),
        encoding="utf-8",
    )
    config.application.compose = compose

    with pytest.raises(ConfigurationError, match="secret-like"):
        validate_compose(config)


@pytest.mark.parametrize(
    "environment",
    [
        {1: "value"},
        ["=value"],
        ["BAD-NAME=value"],
    ],
)
def test_compose_rejects_invalid_environment_names(
    tmp_path: Path, environment: object
) -> None:
    _, config = load_configuration(DEMO, "stage")
    compose = tmp_path / "compose.yml"
    compose.write_text(
        yaml.safe_dump({"services": {"app": {"environment": environment}}}),
        encoding="utf-8",
    )
    config.application.compose = compose

    with pytest.raises(ConfigurationError, match="environment keys|invalid environment name"):
        validate_compose(config)


@pytest.mark.parametrize(
    "name",
    ["APIKEY", "AWS_ACCESS_KEY_ID", "AUTH_BEARER", "GITHUB_PAT"],
)
def test_compose_rejects_additional_secret_environment_names(
    tmp_path: Path, name: str
) -> None:
    _, config = load_configuration(DEMO, "stage")
    compose = tmp_path / "compose.yml"
    compose.write_text(
        yaml.safe_dump({"services": {"app": {"environment": {name: "inline"}}}}),
        encoding="utf-8",
    )
    config.application.compose = compose

    with pytest.raises(ConfigurationError, match="secret-like"):
        validate_compose(config)


def test_compose_accepts_non_secret_inline_environment(tmp_path: Path) -> None:
    _, config = load_configuration(DEMO, "stage")
    compose = tmp_path / "compose.yml"
    compose.write_text(
        yaml.safe_dump(
            {
                "services": {
                    "app": {
                        "image": "example/app",
                        "environment": {"APP_ENV": "stage", "LOG_LEVEL": "info"},
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    config.application.compose = compose

    validate_compose(config)


def test_reverse_proxy_defaults_reproduce_nginx_builtins() -> None:
    raw = yaml.safe_load((FIXTURE / "environments/stage/config.yml").read_text())
    assert "reverse_proxy" not in raw

    config = EnvironmentConfig.model_validate(raw)

    assert config.reverse_proxy.client_max_body_size == "1m"
    assert config.reverse_proxy.proxy_read_timeout == 60


@pytest.mark.parametrize(
    ("size", "expected"),
    [
        ("12m", "12m"),
        ("12M", "12m"),
        ("512k", "512k"),
        ("1g", "1g"),
        ("1048576", "1048576"),
        (1048576, "1048576"),
    ],
)
@pytest.mark.parametrize("timeout", [1, 120, 3600])
def test_reverse_proxy_accepts_nginx_sizes_and_bounded_timeouts(
    size: object, expected: str, timeout: int
) -> None:
    raw = yaml.safe_load((FIXTURE / "environments/stage/config.yml").read_text())
    raw["reverse_proxy"] = {"client_max_body_size": size, "proxy_read_timeout": timeout}

    config = EnvironmentConfig.model_validate(raw)

    assert config.reverse_proxy.client_max_body_size == expected
    assert config.reverse_proxy.proxy_read_timeout == timeout


@pytest.mark.parametrize(
    "size",
    [
        "0",
        0,
        "0m",
        "012m",
        "12mb",
        "12 m",
        " 12m",
        "1.5m",
        "-1m",
        "12t",
        "",
        "off",
        "m",
        True,
        "12m;",
        "1234567890m",
        12.5,
    ],
)
def test_reverse_proxy_rejects_invalid_body_size(size: object) -> None:
    raw = yaml.safe_load((FIXTURE / "environments/stage/config.yml").read_text())
    raw["reverse_proxy"] = {"client_max_body_size": size}

    with pytest.raises(ValidationError, match="positive Nginx size"):
        EnvironmentConfig.model_validate(raw)


@pytest.mark.parametrize("timeout", [0, -1, 3601, "120", 120.0, True, "120s"])
def test_reverse_proxy_rejects_invalid_read_timeout(timeout: object) -> None:
    raw = yaml.safe_load((FIXTURE / "environments/stage/config.yml").read_text())
    raw["reverse_proxy"] = {"proxy_read_timeout": timeout}

    with pytest.raises(ValidationError, match="proxy_read_timeout"):
        EnvironmentConfig.model_validate(raw)


def test_reverse_proxy_rejects_unknown_settings() -> None:
    raw = yaml.safe_load((FIXTURE / "environments/stage/config.yml").read_text())
    raw["reverse_proxy"] = {"proxy_send_timeout": 120}

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        EnvironmentConfig.model_validate(raw)


def test_monitoring_does_not_accept_application_reverse_proxy_settings() -> None:
    raw = yaml.safe_load((FIXTURE / "environments/monitoring/config.yml").read_text())
    raw["reverse_proxy"] = {"client_max_body_size": "12m"}

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        MonitoringConfig.model_validate(raw)
