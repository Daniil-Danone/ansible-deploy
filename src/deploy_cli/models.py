import ipaddress
import re
from pathlib import Path, PurePosixPath
from typing import Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


def _managed_remote_dir(value: str, *, field: str) -> str:
    path = PurePosixPath(value)
    normalized = str(path)
    if (
        not path.is_absolute()
        or value != normalized
        or ".." in path.parts
        or "." in path.parts
        or len(path.parts) < 3
        or path.parts[1] not in {"srv", "opt"}
        or any(not re.fullmatch(r"[A-Za-z0-9._-]+", part) for part in path.parts[2:])
    ):
        raise ValueError(
            f"{field} must be a normalized dedicated directory below /srv or /opt"
        )
    return normalized


def _paths_overlap(first: str, second: str) -> bool:
    first_path = PurePosixPath(first)
    second_path = PurePosixPath(second)
    return (
        first_path == second_path
        or first_path in second_path.parents
        or second_path in first_path.parents
    )


class RebootConfig(StrictModel):
    enabled: bool = True
    only_when_required: Literal[True] = True
    time: str = Field(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    timezone: str = "Europe/Moscow"

    @field_validator("timezone")
    @classmethod
    def known_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("timezone must be present in the IANA database") from exc
        return value


class SecurityUpdatesConfig(StrictModel):
    enabled: bool = True
    reboot: RebootConfig


class ControlsConfig(StrictModel):
    ssh: bool = True
    firewall: bool = True
    fail2ban: bool = True
    unattended_upgrades: bool = True


class HardeningConfig(StrictModel):
    profile: Literal["default"] = "default"
    controls: ControlsConfig


class GlobalBody(StrictModel):
    security_updates: SecurityUpdatesConfig
    hardening: HardeningConfig


class GlobalConfig(StrictModel):
    schema_version: Literal[1]
    global_: GlobalBody = Field(alias="global")


class ServerConfig(StrictModel):
    host: str
    ssh_port: int = Field(default=22, ge=1, le=65535)
    bootstrap_user: str = "root"
    deploy_user: str = "deploy"
    ssh_key: Path
    public_key: Path
    host_key_fingerprints: list[str] = Field(min_length=1)

    @field_validator("ssh_key", "public_key", mode="before")
    @classmethod
    def expand_path(cls, value: str) -> Path:
        # Symlink validation must see the path exactly as configured; resolving here
        # would erase the evidence before the key-safety boundary can reject it.
        return Path(value).expanduser()

    @field_validator("bootstrap_user", "deploy_user")
    @classmethod
    def safe_user(cls, value: str) -> str:
        if not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", value):
            raise ValueError("user must be a safe Linux account name")
        return value

    @field_validator("host")
    @classmethod
    def safe_host(cls, value: str) -> str:
        try:
            ipaddress.ip_address(value)
            return value
        except ValueError:
            return _validate_domain(value, field="host")

    @field_validator("host_key_fingerprints")
    @classmethod
    def valid_fingerprints(cls, values: list[str]) -> list[str]:
        if any(not re.fullmatch(r"SHA256:[A-Za-z0-9+/]{43}", value) for value in values):
            raise ValueError("host key fingerprints must use OpenSSH SHA256 format")
        return values


class ApplicationConfig(StrictModel):
    compose: Path
    env_file: Path
    registry_auth_file: Path | None = None
    remote_dir: str = "/srv/myapp"
    allowed_loopback_ports: list[int] = Field(default_factory=list)
    allowed_bind_paths: list[str] = Field(default_factory=list)
    required_env_vars: list[str] = Field(default_factory=lambda: ["APP_ENV"])

    @field_validator("remote_dir")
    @classmethod
    def absolute_remote_dir(cls, value: str) -> str:
        return _managed_remote_dir(value, field="application remote_dir")

    @field_validator("registry_auth_file", mode="before")
    @classmethod
    def expand_optional_path(cls, value: str | None) -> Path | None:
        return None if value is None else Path(value).expanduser()

    @field_validator("required_env_vars")
    @classmethod
    def safe_required_env_vars(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)) or any(
            not re.fullmatch(r"[A-Z_][A-Z0-9_]*", value) for value in values
        ):
            raise ValueError("required_env_vars must contain unique shell-style names")
        return values

    @field_validator("allowed_bind_paths")
    @classmethod
    def safe_bind_paths(cls, values: list[str]) -> list[str]:
        for value in values:
            path = PurePosixPath(value)
            if not path.is_absolute() or ".." in path.parts or value != str(path):
                raise ValueError("allowed_bind_paths must contain normalized absolute paths")
        return values


class CollectorConfig(StrictModel):
    push_url: str
    username: str = Field(pattern=r"^[A-Za-z0-9._-]{1,64}$")
    password_file: Path
    remote_dir: str = "/opt/ansible-deploy/alloy"

    @field_validator("password_file", mode="before")
    @classmethod
    def expand_password_path(cls, value: str) -> Path:
        return Path(value).expanduser()

    @field_validator("push_url")
    @classmethod
    def secure_push_url(cls, value: str) -> str:
        if not value or value != value.strip() or re.search(r"\s", value):
            raise ValueError("push_url cannot contain whitespace")
        try:
            parsed = urlsplit(value)
            port = parsed.port
        except ValueError as exc:
            raise ValueError("push_url must contain a valid HTTPS authority") from exc
        if (
            parsed.scheme != "https"
            or parsed.hostname is None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path != "/loki/api/v1/push"
            or parsed.query
            or parsed.fragment
            or "?" in value
            or "#" in value
            or not re.fullmatch(
                r"(?:\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9.-]+)(?::[0-9]{1,5})?",
                parsed.netloc,
            )
        ):
            raise ValueError(
                "push_url must be https://host[:port]/loki/api/v1/push without credentials, "
                "query or fragment"
            )
        if port is not None and not 1 <= port <= 65535:
            raise ValueError("push_url port must be between 1 and 65535")
        try:
            ipaddress.ip_address(parsed.hostname)
        except ValueError:
            _validate_domain(parsed.hostname, field="push_url host")
        return value

    @field_validator("remote_dir")
    @classmethod
    def safe_remote_dir(cls, value: str) -> str:
        return _managed_remote_dir(value, field="collector remote_dir")

class EnvironmentConfig(StrictModel):
    schema_version: Literal[1]
    environment: Literal["stage", "prod"]
    server: ServerConfig
    application: ApplicationConfig
    domain: str
    acme_email: str
    health_path: str = "/health"
    collector: CollectorConfig | None = None

    @model_validator(mode="after")
    def secure_health_path(self) -> "EnvironmentConfig":
        if not self.health_path.startswith("/"):
            raise ValueError("health_path must start with /")
        if self.collector is not None and _paths_overlap(
            self.application.remote_dir, self.collector.remote_dir
        ):
            raise ValueError(
                "collector remote_dir must not equal, contain or be contained by "
                "application remote_dir"
            )
        return self

    @field_validator("domain")
    @classmethod
    def safe_domain(cls, value: str) -> str:
        return _validate_domain(value, field="domain")

    @field_validator("acme_email")
    @classmethod
    def safe_email(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+", value):
            raise ValueError("acme_email must be a safe email address")
        _validate_domain(value.rsplit("@", 1)[1], field="acme_email domain")
        return value


class MonitoringStackConfig(StrictModel):
    remote_dir: str = "/opt/ansible-deploy/monitoring"
    secrets_file: Path
    retention_days: int = Field(default=30, ge=1, le=3650)
    grafana_port: int = Field(default=3000, ge=1, le=65535)
    loki_port: int = Field(default=3100, ge=1, le=65535)

    @field_validator("secrets_file", mode="before")
    @classmethod
    def expand_secret_path(cls, value: str) -> Path:
        return Path(value).expanduser()

    @field_validator("remote_dir")
    @classmethod
    def safe_remote_dir(cls, value: str) -> str:
        return _managed_remote_dir(value, field="monitoring remote_dir")


class MonitoringConfig(StrictModel):
    schema_version: Literal[1]
    environment: Literal["monitoring"]
    server: ServerConfig
    domain: str
    acme_email: str
    monitoring: MonitoringStackConfig

    @field_validator("domain")
    @classmethod
    def safe_domain(cls, value: str) -> str:
        return _validate_domain(value, field="domain")

    @field_validator("acme_email")
    @classmethod
    def safe_email(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+", value):
            raise ValueError("acme_email must be a safe email address")
        _validate_domain(value.rsplit("@", 1)[1], field="acme_email domain")
        return value


def _validate_domain(value: str, *, field: str) -> str:
    if len(value) > 253 or not re.fullmatch(
        r"(?=.{1,253}\Z)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+"
        r"[A-Za-z](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?",
        value,
    ):
        raise ValueError(f"{field} must be a valid DNS name")
    return value.lower()
