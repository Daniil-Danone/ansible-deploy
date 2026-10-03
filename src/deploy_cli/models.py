import ipaddress
import re
from pathlib import Path, PurePosixPath
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


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
            raise ValueError("remote_dir must be normalized and located below /srv or /opt")
        return normalized

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


class EnvironmentConfig(StrictModel):
    schema_version: Literal[1]
    environment: Literal["stage", "prod"]
    server: ServerConfig
    application: ApplicationConfig
    domain: str
    acme_email: str
    health_path: str = "/health"

    @model_validator(mode="after")
    def secure_health_path(self) -> "EnvironmentConfig":
        if not self.health_path.startswith("/"):
            raise ValueError("health_path must start with /")
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


def _validate_domain(value: str, *, field: str) -> str:
    if len(value) > 253 or not re.fullmatch(
        r"(?=.{1,253}\Z)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+"
        r"[A-Za-z](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?",
        value,
    ):
        raise ValueError(f"{field} must be a valid DNS name")
    return value.lower()
