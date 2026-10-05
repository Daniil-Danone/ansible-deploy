import ipaddress
import re
import unicodedata
from datetime import datetime
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


def is_backup_identifier(value: str) -> bool:
    match = re.fullmatch(
        r"(?P<stamp>\d{4}-\d{2}-\d{2}T\d{6}Z)(?:-[0-9a-f]{16})?", value
    )
    if match is None:
        return False
    try:
        return datetime.strptime(match.group("stamp"), "%Y-%m-%dT%H%M%SZ").strftime(
            "%Y-%m-%dT%H%M%SZ"
        ) == match.group("stamp")
    except ValueError:
        return False


def _validate_v2_portable_names(raw: Any, paths: tuple[tuple[str, str], ...]) -> Any:
    if not isinstance(raw, dict) or raw.get("schema_version") != 2:
        return raw
    for section, field in paths:
        body = raw.get(section)
        value = body.get(field) if isinstance(body, dict) else None
        if section in {"collector", "backup"} and body is None:
            continue
        if value is None and section == "application" and field == "registry_auth_file":
            continue
        _require_portable_name(value)
    return raw


def _require_portable_name(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("schema v2 external file names must be strings")
    posix = PurePosixPath(value)
    windows = PureWindowsPath(value)
    reserved = {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{number}" for number in range(1, 10)),
        *(f"LPT{number}" for number in range(1, 10)),
    }
    if (
        not value
        or unicodedata.normalize("NFC", value) != value
        or "\\" in value
        or ":" in value
        or posix.is_absolute()
        or windows.is_absolute()
        or bool(windows.drive)
        or value != str(posix)
        or any(part in {"", ".", ".."} for part in posix.parts)
        or any(part.endswith((".", " ")) for part in posix.parts)
        or any(part.split(".", 1)[0].upper() in reserved for part in posix.parts)
        or any(unicodedata.category(character).startswith("C") for character in value)
    ):
        raise ValueError("schema v2 external file names must be normalized relative paths")
    return value


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


class ExtraEnvFile(StrictModel):
    """Additional secret env file delivered next to Compose under a declared name."""

    source: Path
    # Plain file name beside compose.yml: no directories, never the primary ``.env``.
    target: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}\.env$")

    @field_validator("source", mode="before")
    @classmethod
    def portable_source(cls, value: object) -> object:
        return _require_portable_name(value)


class ApplicationConfig(StrictModel):
    compose: Path
    env_file: Path
    extra_env_files: list[ExtraEnvFile] = Field(default_factory=list)
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

    @field_validator("extra_env_files")
    @classmethod
    def unique_extra_env_files(cls, values: list[ExtraEnvFile]) -> list[ExtraEnvFile]:
        targets = [item.target for item in values]
        sources = [item.source.as_posix() for item in values]
        if len(targets) != len(set(targets)):
            raise ValueError("extra_env_files targets must be unique")
        if len(sources) != len(set(sources)):
            raise ValueError("extra_env_files sources must be unique")
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


class BackupPostgresSource(StrictModel):
    type: Literal["postgres"]
    service: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
    database: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_$-]{0,62}$")
    user: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_$-]{0,62}$")


class BackupPathSource(StrictModel):
    type: Literal["file", "directory", "glob"]
    path: str
    restore_destination: str

    @field_validator("path")
    @classmethod
    def safe_path(cls, value: str) -> str:
        path = PurePosixPath(value)
        if not path.is_absolute() or value != str(path) or ".." in path.parts:
            raise ValueError("backup paths must be normalized absolute POSIX paths")
        return value

    @field_validator("restore_destination")
    @classmethod
    def safe_restore_destination(cls, value: str) -> str:
        path = PurePosixPath(value)
        if (
            path.is_absolute()
            or value != str(path)
            or value in {"", "."}
            or any(part in {"", ".", ".."} for part in path.parts)
        ):
            raise ValueError(
                "backup restore_destination must be a normalized relative POSIX path"
            )
        return value


class BackupRetention(StrictModel):
    daily: int = Field(default=7, ge=0, le=366)
    weekly: int = Field(default=4, ge=0, le=260)
    monthly: int = Field(default=6, ge=0, le=120)

    @model_validator(mode="after")
    def keeps_at_least_one_backup(self) -> "BackupRetention":
        if self.daily == self.weekly == self.monthly == 0:
            raise ValueError("backup retention must keep at least one backup")
        return self


class BackupConfig(StrictModel):
    enabled: bool = True
    schedule: str = "03:15"
    remote: str = Field(pattern=r"^[A-Za-z0-9_-]+:[A-Za-z0-9_./-]+$")
    credentials_file: Path
    age_identity_file: Path
    age_recipient: str = Field(pattern=r"^age1[0-9a-z]{58}$")
    include: list[BackupPostgresSource | BackupPathSource] = Field(min_length=1)
    retention: BackupRetention = Field(default_factory=BackupRetention)

    @field_validator("credentials_file", "age_identity_file", mode="before")
    @classmethod
    def expand_secret_path(cls, value: str) -> Path:
        return Path(value).expanduser()

    @field_validator("schedule")
    @classmethod
    def valid_schedule(cls, value: str) -> str:
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
            raise ValueError("backup schedule must use HH:MM")
        return value

    @model_validator(mode="after")
    def non_overlapping_restore_destinations(self) -> "BackupConfig":
        destinations = [
            item.restore_destination
            for item in self.include
            if isinstance(item, BackupPathSource)
        ]
        for index, first in enumerate(destinations):
            for second in destinations[index + 1 :]:
                if _paths_overlap(first, second):
                    raise ValueError("backup restore destinations must not overlap")
        return self


class EnvironmentConfig(StrictModel):
    _project_dir: Path | None = PrivateAttr(default=None)
    _external_secret_root: Path | None = PrivateAttr(default=None)
    _external_trusted_base: Path | None = PrivateAttr(default=None)
    _validate_external_trusted_base: bool = PrivateAttr(default=True)

    schema_version: Literal[2]
    environment: Literal["stage", "prod", "restore"]
    source_environment: Literal["prod"] | None = None
    server: ServerConfig
    application: ApplicationConfig
    domain: str
    acme_email: str
    health_path: str = "/health"
    collector: CollectorConfig | None = None
    backup: BackupConfig | None = None

    def set_external_secret_context(
        self, project_dir: Path, root: Path, trusted_base: Path, validate_trusted_base: bool
    ) -> None:
        self._project_dir = project_dir
        self._external_secret_root = root
        self._external_trusted_base = trusted_base
        self._validate_external_trusted_base = validate_trusted_base

    @property
    def external_secret_context(self) -> tuple[Path, Path, Path, bool] | None:
        if (
            self._project_dir is None
            or self._external_secret_root is None
            or self._external_trusted_base is None
        ):
            return None
        return (
            self._project_dir,
            self._external_secret_root,
            self._external_trusted_base,
            self._validate_external_trusted_base,
        )

    @model_validator(mode="before")
    @classmethod
    def portable_schema_v2_names(cls, raw: Any) -> Any:
        return _validate_v2_portable_names(
            raw,
            (
                ("server", "ssh_key"),
                ("server", "public_key"),
                ("application", "env_file"),
                ("application", "registry_auth_file"),
                ("collector", "password_file"),
                ("backup", "credentials_file"),
                ("backup", "age_identity_file"),
            ),
        )

    @model_validator(mode="after")
    def secure_health_path(self) -> "EnvironmentConfig":
        if self.environment == "restore" and self.source_environment != "prod":
            raise ValueError("restore environment requires source_environment: prod")
        if self.environment != "restore" and self.source_environment is not None:
            raise ValueError("source_environment is only valid for restore environment")
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
    _project_dir: Path | None = PrivateAttr(default=None)
    _external_secret_root: Path | None = PrivateAttr(default=None)
    _external_trusted_base: Path | None = PrivateAttr(default=None)
    _validate_external_trusted_base: bool = PrivateAttr(default=True)

    schema_version: Literal[2]
    environment: Literal["monitoring"]
    server: ServerConfig
    domain: str
    acme_email: str
    monitoring: MonitoringStackConfig

    def set_external_secret_context(
        self, project_dir: Path, root: Path, trusted_base: Path, validate_trusted_base: bool
    ) -> None:
        self._project_dir = project_dir
        self._external_secret_root = root
        self._external_trusted_base = trusted_base
        self._validate_external_trusted_base = validate_trusted_base

    @property
    def external_secret_context(self) -> tuple[Path, Path, Path, bool] | None:
        if (
            self._project_dir is None
            or self._external_secret_root is None
            or self._external_trusted_base is None
        ):
            return None
        return (
            self._project_dir,
            self._external_secret_root,
            self._external_trusted_base,
            self._validate_external_trusted_base,
        )

    @model_validator(mode="before")
    @classmethod
    def portable_schema_v2_names(cls, raw: Any) -> Any:
        return _validate_v2_portable_names(
            raw,
            (
                ("server", "ssh_key"),
                ("server", "public_key"),
                ("monitoring", "secrets_file"),
            ),
        )

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
