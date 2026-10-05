import base64
import binascii
import ipaddress
import json
import re
import socket
from io import StringIO
from pathlib import Path
from typing import Any, Literal, overload

import yaml
from dotenv import dotenv_values
from pydantic import BaseModel, ValidationError

from .models import EnvironmentConfig, GlobalConfig, MonitoringConfig
from .secret_file import SecretFileError, validate_secret_permissions
from .secret_store import (
    SecretStoreError,
    external_secret_location,
    resolve_external_file,
    validate_external_file_for_use,
)


class ConfigurationError(ValueError):
    """Configuration is absent or invalid."""


def _load_yaml[ModelT: BaseModel](path: Path, model: type[ModelT]) -> ModelT:
    try:
        raw: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
        if model in {EnvironmentConfig, MonitoringConfig} and (
            not isinstance(raw, dict) or raw.get("schema_version") != 2
        ):
            raise ConfigurationError(
                f"Unsupported deployment environment schema in {path}; "
                "only schema_version: 2 is supported"
            )
        return model.model_validate(raw)
    except ConfigurationError:
        raise
    except (OSError, yaml.YAMLError, ValidationError) as exc:
        raise ConfigurationError(f"Invalid configuration {path}: {exc}") from exc


def _configuration_root(project_dir: Path) -> Path:
    project_deploy = project_dir / ".deploy"
    if project_deploy.is_symlink():
        raise ConfigurationError("Project .deploy directory cannot be a symbolic link")
    return project_deploy


def _project_application_path(project_dir: Path, configured: Path, *, field: str) -> Path:
    relative = not configured.is_absolute()
    candidate = project_dir / configured if relative else configured
    resolved = candidate.resolve(strict=False)
    if not relative:
        return resolved
    try:
        resolved.relative_to(project_dir)
    except ValueError as exc:
        raise ConfigurationError(f"Relative {field} path escapes the project directory") from exc
    return resolved


def _external_file_path(
    project_dir: Path, configured: Path, *, field: str, secret: bool = True
) -> Path:
    try:
        return resolve_external_file(project_dir, configured, secret=secret)
    except (SecretStoreError, OSError, ValueError):
        raise ConfigurationError(f"Invalid schema v2 {field}") from None


def _set_external_context(
    project_dir: Path, config: EnvironmentConfig | MonitoringConfig
) -> None:
    try:
        location = external_secret_location(project_dir)
        config.set_external_secret_context(
            project_dir,
            location.root,
            location.trusted_base,
            location.validate_trusted_base,
        )
    except SecretStoreError:
        raise ConfigurationError(
            f"Invalid schema v2 secret store for {config.environment}"
        ) from None


def validate_external_input_for_use(
    config: EnvironmentConfig | MonitoringConfig,
    path: Path,
    *,
    field: str,
    secret: bool = True,
) -> None:
    context = config.external_secret_context
    if context is None:
        raise ConfigurationError(
            f"Schema v2 secret context is unavailable for {config.environment}"
        )
    project_dir, root, trusted_base, validate_trusted_base = context
    try:
        validate_external_file_for_use(
            project_dir,
            root,
            path,
            secret=secret,
            trusted_base=trusted_base,
            validate_trusted_base=validate_trusted_base,
        )
    except (SecretStoreError, OSError, ValueError):
        # Configured names and absolute external paths are sensitive metadata too.
        raise ConfigurationError(
            f"Required {field} is unavailable for {config.environment}"
        ) from None


def read_external_secret_bytes(
    config: EnvironmentConfig | MonitoringConfig, path: Path, *, field: str
) -> bytes:
    validate_external_input_for_use(config, path, field=field)
    try:
        return path.read_bytes()
    except OSError:
        raise ConfigurationError(
            f"Unable to read {field} for {config.environment}"
        ) from None


def read_external_secret_text(
    config: EnvironmentConfig | MonitoringConfig, path: Path, *, field: str
) -> str:
    try:
        return read_external_secret_bytes(config, path, field=field).decode("utf-8")
    except UnicodeError:
        raise ConfigurationError(
            f"Invalid {field} encoding for {config.environment}"
        ) from None


def _reject_registry_auth_collisions(
    project_dir: Path,
    config_root: Path,
    environment_config: Path,
    config: EnvironmentConfig,
) -> None:
    auth = config.application.registry_auth_file
    if auth is None:
        return
    auth_canonical = auth.resolve(strict=False)
    protected = {
        "Compose": config.application.compose.resolve(strict=False),
        "environment file": config.application.env_file.resolve(strict=False),
        "SSH private key": config.server.ssh_key.resolve(strict=False),
        "SSH public key": config.server.public_key.resolve(strict=False),
        "global config": (config_root / "config/global.yml").resolve(strict=False),
        "environment config": environment_config.resolve(strict=False),
        "image publishing config": (project_dir / ".deploy/images.yml").resolve(
            strict=False
        ),
        "README": (project_dir / "README.md").resolve(strict=False),
        "GUIDE": (project_dir / "GUIDE.md").resolve(strict=False),
    }
    collisions = [label for label, path in protected.items() if path == auth_canonical]
    if collisions:
        raise ConfigurationError(
            "Registry authentication file collides with protected " + ", ".join(collisions)
        )


def _reject_secret_collisions(path: Path, protected: dict[str, Path], *, field: str) -> None:
    canonical = path.resolve(strict=False)
    collisions = [
        label
        for label, candidate in protected.items()
        if canonical == candidate.resolve(strict=False)
    ]
    if collisions:
        raise ConfigurationError(f"{field} file collides with protected " + ", ".join(collisions))


@overload
def load_configuration(
    project_dir: Path, environment: Literal["stage", "prod", "restore"]
) -> tuple[GlobalConfig, EnvironmentConfig]: ...


@overload
def load_configuration(
    project_dir: Path, environment: Literal["monitoring"]
) -> tuple[GlobalConfig, MonitoringConfig]: ...


@overload
def load_configuration(
    project_dir: Path, environment: str
) -> tuple[GlobalConfig, EnvironmentConfig | MonitoringConfig]: ...


def load_configuration(
    project_dir: Path, environment: str
) -> tuple[GlobalConfig, EnvironmentConfig | MonitoringConfig]:
    if environment not in {"stage", "prod", "restore", "monitoring"}:
        raise ConfigurationError(f"Environment is not implemented yet: {environment}")
    project_dir = project_dir.resolve()
    config_root = _configuration_root(project_dir)
    global_config = _load_yaml(config_root / "config/global.yml", GlobalConfig)
    env_path = config_root / "environments" / environment / "config.yml"
    env_config: EnvironmentConfig | MonitoringConfig
    if environment == "monitoring":
        env_config = _load_yaml(env_path, MonitoringConfig)
    else:
        env_config = _load_yaml(env_path, EnvironmentConfig)
    if env_config.environment != environment:
        raise ConfigurationError(
            f"Configuration environment mismatch: requested {environment!r}, "
            f"file declares {env_config.environment!r}"
        )
    if isinstance(env_config, MonitoringConfig):
        env_config.monitoring.secrets_file = _external_file_path(
            project_dir, env_config.monitoring.secrets_file, field="monitoring secrets"
        )
        env_config.server.ssh_key = _external_file_path(
            project_dir, env_config.server.ssh_key, field="SSH private key"
        )
        env_config.server.public_key = _external_file_path(
            project_dir,
            env_config.server.public_key,
            field="SSH public key",
            secret=False,
        )
        _reject_secret_collisions(
            env_config.monitoring.secrets_file,
            {
                "SSH private key": env_config.server.ssh_key,
                "SSH public key": env_config.server.public_key,
                "global config": config_root / "config/global.yml",
                "environment config": env_path,
                "README": project_dir / "README.md",
            },
            field="Monitoring secret",
        )
        _set_external_context(project_dir, env_config)
        return global_config, env_config
    app = env_config.application
    app.compose = _project_application_path(project_dir, app.compose, field="Compose")
    app.env_file = _external_file_path(
        project_dir, app.env_file, field="application environment"
    )
    if app.registry_auth_file is not None:
        app.registry_auth_file = _external_file_path(
            project_dir, app.registry_auth_file, field="registry authentication"
        )
    env_config.server.ssh_key = _external_file_path(
        project_dir, env_config.server.ssh_key, field="SSH private key"
    )
    env_config.server.public_key = _external_file_path(
        project_dir,
        env_config.server.public_key,
        field="SSH public key",
        secret=False,
    )
    if env_config.backup is not None:
        env_config.backup.credentials_file = _external_file_path(
            project_dir,
            env_config.backup.credentials_file,
            field="backup credentials",
        )
        env_config.backup.age_identity_file = _external_file_path(
            project_dir,
            env_config.backup.age_identity_file,
            field="age identity",
        )
    if env_config.collector is not None:
        env_config.collector.password_file = _external_file_path(
            project_dir,
            env_config.collector.password_file,
            field="collector password",
        )
        _reject_secret_collisions(
            env_config.collector.password_file,
            {
                "Compose": app.compose,
                "environment file": app.env_file,
                "registry authentication": app.registry_auth_file or Path("/__absent__"),
                "SSH private key": env_config.server.ssh_key,
                "SSH public key": env_config.server.public_key,
                "global config": config_root / "config/global.yml",
                "environment config": env_path,
                "README": project_dir / "README.md",
            },
            field="Collector password",
        )
    if env_config.backup is not None:
        protected = {
            "Compose": app.compose,
            "environment file": app.env_file,
            "registry authentication": app.registry_auth_file or Path("/__absent__"),
            "collector password": (
                env_config.collector.password_file
                if env_config.collector is not None
                else Path("/__absent__")
            ),
            "SSH private key": env_config.server.ssh_key,
            "SSH public key": env_config.server.public_key,
            "global config": config_root / "config/global.yml",
            "environment config": env_path,
            "README": project_dir / "README.md",
        }
        _reject_secret_collisions(
            env_config.backup.credentials_file,
            {**protected, "age identity": env_config.backup.age_identity_file},
            field="Backup credentials",
        )
        _reject_secret_collisions(
            env_config.backup.age_identity_file,
            {**protected, "backup credentials": env_config.backup.credentials_file},
            field="Age identity",
        )
    _reject_registry_auth_collisions(project_dir, config_root, env_path, env_config)
    _set_external_context(project_dir, env_config)
    return global_config, env_config


def validate_observability_inputs(config: EnvironmentConfig | MonitoringConfig) -> None:
    secret = (
        config.monitoring.secrets_file
        if isinstance(config, MonitoringConfig)
        else config.collector.password_file if config.collector is not None else None
    )
    if secret is None:
        raise ConfigurationError(f"{config.environment} collector is not configured")
    try:
        validate_external_input_for_use(config, secret, field="observability secret")
        if not secret.is_file():
            raise ConfigurationError(
                f"Required observability secret is unavailable for {config.environment}"
            )
        validate_secret_permissions(secret)
    except ConfigurationError:
        raise
    except (SecretStoreError, OSError, SecretFileError, ValueError):
        raise ConfigurationError(
            f"Unable to validate observability secret for {config.environment}"
        ) from None
    try:
        if isinstance(config, MonitoringConfig):
            values = dotenv_values(
                stream=StringIO(
                    read_external_secret_text(
                        config, secret, field="observability secret"
                    )
                )
            )
            required = {
                "GF_SECURITY_ADMIN_USER",
                "GF_SECURITY_ADMIN_PASSWORD",
                "LOKI_PUSH_USERNAME",
                "LOKI_PUSH_PASSWORD_HASH",
            }
            missing = sorted(name for name in required if not values.get(name))
            if missing:
                raise ConfigurationError(
                    "Monitoring secret file is missing variables: " + ", ".join(missing)
                )
            password_hash = values["LOKI_PUSH_PASSWORD_HASH"]
            if not isinstance(password_hash, str) or not password_hash.startswith("$6$"):
                raise ConfigurationError("LOKI_PUSH_PASSWORD_HASH must be a crypt SHA-512 hash")
        else:
            password = read_external_secret_text(
                config, secret, field="observability secret"
            ).rstrip("\r\n")
            if not password or "\n" in password or "\r" in password or "\0" in password:
                raise ConfigurationError("Collector password file must contain one non-empty line")
    except ConfigurationError:
        raise
    except (SecretStoreError, OSError, UnicodeError, ValueError):
        raise ConfigurationError(
            f"Observability secret is invalid for {config.environment}"
        ) from None


def validate_backup_inputs(config: EnvironmentConfig, *, require_identity: bool = False) -> None:
    if config.environment != "prod" or config.backup is None or not config.backup.enabled:
        raise ConfigurationError("Production backup is not enabled")
    required = [("backup credentials", config.backup.credentials_file)]
    if require_identity:
        required.append(("age identity", config.backup.age_identity_file))
    for field, path in required:
        try:
            validate_external_input_for_use(config, path, field=field, secret=True)
            if not path.is_file():
                raise ConfigurationError(f"Required {field} is unavailable")
            validate_secret_permissions(path)
        except ConfigurationError:
            raise
        except (SecretStoreError, OSError, SecretFileError, ValueError):
            raise ConfigurationError(f"Unable to validate {field}") from None


def validate_restore_isolation(source: EnvironmentConfig, target: EnvironmentConfig) -> None:
    if source.environment != "prod" or target.environment != "restore":
        raise ConfigurationError("Restore requires Production source and Restore target")
    reused = []
    if source.domain == target.domain:
        reused.append("domain")
    if source.application.remote_dir == target.application.remote_dir:
        reused.append("remote runtime")
    if source.application.compose == target.application.compose:
        reused.append("Compose")
    if reused:
        raise ConfigurationError("Restore must not reuse Production " + ", ".join(reused))
    if _host_addresses(source.server.host, source.server.ssh_port).intersection(
        _host_addresses(target.server.host, target.server.ssh_port)
    ):
        raise ConfigurationError("Restore and Production server hosts resolve to the same address")


def validate_local_inputs(
    config: EnvironmentConfig | MonitoringConfig,
    *,
    require_ssh: bool = True,
    require_public_key: bool = True,
    require_application: bool = True,
) -> None:
    required: list[tuple[str, Path, bool]] = []
    if require_ssh:
        required.append(("SSH private key", config.server.ssh_key, True))
    if require_public_key:
        required.append(("SSH public key", config.server.public_key, False))
    if require_application and isinstance(config, EnvironmentConfig):
        required.extend(
            [
                ("Compose file", config.application.compose, False),
                ("application environment", config.application.env_file, True),
            ]
        )
        if config.application.registry_auth_file is not None:
            required.append(
                ("registry authentication", config.application.registry_auth_file, True)
            )
    for field, path, secret in required:
        if field != "Compose file":
            validate_external_input_for_use(config, path, field=field, secret=secret)
    missing = [field for field, path, _ in required if not path.is_file()]
    if missing:
        raise ConfigurationError(
            f"Required local inputs are missing for {config.environment}: "
            + ", ".join(missing)
        )


def validate_compose(config: EnvironmentConfig) -> None:
    try:
        document: Any = yaml.safe_load(config.application.compose.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigurationError(f"Invalid Compose file: {exc}") from exc
    if not isinstance(document, dict) or not isinstance(document.get("services"), dict):
        raise ConfigurationError("Compose file must contain a services mapping")
    if "include" in document:
        raise ConfigurationError("Compose include is forbidden; provide one self-contained file")
    networks = _validate_network_definitions(document.get("networks", {}))
    volumes = _validate_volume_definitions(document.get("volumes", {}))
    allowed = set(config.application.allowed_loopback_ports)
    for service_name, raw_service in document["services"].items():
        if not isinstance(raw_service, dict):
            raise ConfigurationError(f"Compose service {service_name!r} must be a mapping")
        if "extends" in raw_service:
            raise ConfigurationError(f"Compose service {service_name!r} uses forbidden extends")
        if "network_mode" in raw_service:
            raise ConfigurationError(
                f"Compose service {service_name!r} uses forbidden network_mode/container sharing"
            )
        _validate_inline_environment(service_name, raw_service.get("environment"))
        _validate_service_networks(service_name, raw_service.get("networks", []), networks)
        _validate_service_volumes(
            service_name,
            raw_service.get("volumes", []),
            set(config.application.allowed_bind_paths),
            volumes,
        )
        if config.environment == "prod":
            image = raw_service.get("image")
            if not isinstance(image, str) or not re.fullmatch(
                r"[^\s@]+@sha256:[0-9a-f]{64}", image
            ):
                raise ConfigurationError(
                    f"Production Compose service {service_name!r} must use a digest-pinned image"
                )
            if "build" in raw_service:
                raise ConfigurationError(
                    f"Production Compose service {service_name!r} cannot build on the server"
                )
        for published in raw_service.get("ports", []):
            if _contains_interpolation(published):
                raise ConfigurationError(
                    f"Compose service {service_name!r} interpolates security-sensitive ports"
                )
            host_ip, host_port = _published_binding(published)
            if host_ip != "127.0.0.1" or host_port not in allowed:
                raise ConfigurationError(
                    f"Compose service {service_name!r} publishes forbidden binding "
                    f"{host_ip}:{host_port}; only configured loopback ports are allowed"
                )


_SECRET_ENV_NAME = re.compile(
    r"(?:^|_)(?:PASSWORD|PASSWD|SECRET|TOKEN|API_?KEY|PRIVATE_KEY|CREDENTIALS?|AUTH|"
    r"ACCESS_KEY|BEARER|PAT)(?:_|$)",
    re.IGNORECASE,
)
_ENVIRONMENT_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SECRET_ENV_VALUE = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----|"
    r"[a-z][a-z0-9+.-]*://[^\s/:@]+:[^\s/@]+@|"
    r"\$\{[^}]*?(?:PASSWORD|PASSWD|SECRET|TOKEN|API[_-]?KEY|PRIVATE[_-]?KEY|"
    r"CREDENTIALS?|AUTH|ACCESS[_-]?KEY|BEARER|PAT)[^}]*\}",
    re.IGNORECASE,
)


def _validate_inline_environment(service_name: object, environment: object) -> None:
    """Keep credentials in the external env_file rather than committed Compose YAML."""
    if environment is None:
        return
    entries: list[tuple[str, object]] = []
    if isinstance(environment, dict):
        for name, value in environment.items():
            if not isinstance(name, str):
                raise ConfigurationError(
                    f"Compose service {service_name!r} environment keys must be strings"
                )
            entries.append((name, value))
    elif isinstance(environment, list):
        for item in environment:
            if not isinstance(item, str):
                raise ConfigurationError(
                    f"Compose service {service_name!r} environment list must contain strings"
                )
            name, separator, list_value = item.partition("=")
            entries.append((name, list_value if separator else None))
    else:
        raise ConfigurationError(
            f"Compose service {service_name!r} environment must be a mapping or list"
        )
    for name, value in entries:
        if not _ENVIRONMENT_NAME.fullmatch(name):
            raise ConfigurationError(
                f"Compose service {service_name!r} has invalid environment name {name!r}"
            )
        if _SECRET_ENV_NAME.search(name):
            raise ConfigurationError(
                f"Compose service {service_name!r} declares secret-like environment key "
                f"{name!r}; keep secrets in the external application env_file"
            )
        if isinstance(value, str) and _SECRET_ENV_VALUE.search(value):
            raise ConfigurationError(
                f"Compose service {service_name!r} contains a secret-like inline "
                "environment value; keep secrets in the external application env_file"
            )


def validate_environment_file(config: EnvironmentConfig) -> None:
    """Validate required keys without exposing any secret values."""
    try:
        content = read_external_secret_text(
            config, config.application.env_file, field="application environment"
        )
        values = dotenv_values(stream=StringIO(content))
    except ConfigurationError:
        raise
    except ValueError:
        raise ConfigurationError(
            f"Invalid application environment for {config.environment}"
        ) from None
    missing = [name for name in config.application.required_env_vars if not values.get(name)]
    if missing:
        raise ConfigurationError(
            "Environment file is missing required variables: " + ", ".join(missing)
        )
    declared_environment = values.get("APP_ENV")
    if declared_environment != config.environment:
        raise ConfigurationError(
            "Environment file APP_ENV does not match selected environment "
            f"{config.environment!r}"
        )


def validate_registry_auth(
    config: EnvironmentConfig, *, expected_registry_host: str | None = None
) -> None:
    path = config.application.registry_auth_file
    if path is None:
        return
    try:
        document: Any = json.loads(
            read_external_secret_text(config, path, field="registry authentication")
        )
    except ConfigurationError:
        raise
    except (SecretStoreError, OSError, UnicodeError, ValueError, json.JSONDecodeError):
        raise ConfigurationError(
            f"Registry authentication is invalid for {config.environment}"
        ) from None
    if not isinstance(document, dict) or not isinstance(document.get("auths"), dict):
        raise ConfigurationError("Registry authentication file must contain an auths mapping")
    if "credsStore" in document or "credHelpers" in document:
        raise ConfigurationError(
            "Registry authentication must be portable and cannot use credential helpers"
        )
    auths = document["auths"]
    if not auths:
        raise ConfigurationError("Registry authentication auths mapping cannot be empty")
    normalized_hosts: set[str] = set()
    for host, entry in auths.items():
        if not isinstance(host, str) or not host or not isinstance(entry, dict):
            raise ConfigurationError("Registry authentication contains an invalid entry")
        encoded = entry.get("auth")
        if not isinstance(encoded, str) or not encoded:
            raise ConfigurationError(
                "Registry authentication entries require inline auth credentials"
            )
        try:
            decoded = base64.b64decode(encoded, validate=True).decode("utf-8")
        except (binascii.Error, UnicodeError):
            raise ConfigurationError(
                "Registry authentication contains invalid inline credentials"
            ) from None
        username, separator, token = decoded.partition(":")
        if not separator or not username or not token:
            raise ConfigurationError(
                "Registry authentication contains incomplete inline credentials"
            )
        normalized_hosts.add(
            "docker.io" if host == "https://index.docker.io/v1/" else host
        )
    try:
        compose: Any = yaml.safe_load(
            config.application.compose.read_text(encoding="utf-8")
        )
        services = compose["services"]
    except (OSError, UnicodeError, yaml.YAMLError, KeyError, TypeError, ValueError):
        raise ConfigurationError(
            "Unable to match registry authentication to Compose images"
        ) from None
    compose_hosts: set[str] = set()
    for service in services.values():
        if not isinstance(service, dict) or not isinstance(service.get("image"), str):
            continue
        first = service["image"].split("/", 1)[0]
        compose_hosts.add(
            first if "." in first or ":" in first or first == "localhost" else "docker.io"
        )
    if not normalized_hosts.issubset(compose_hosts):
        raise ConfigurationError(
            "Registry authentication contains a host not used by Compose images"
        )
    if expected_registry_host is not None and expected_registry_host not in normalized_hosts:
        raise ConfigurationError(
            "Registry authentication does not contain inline credentials for the selected registry"
        )
    try:
        validate_secret_permissions(path)
    except (SecretStoreError, OSError, SecretFileError, ValueError):
        raise ConfigurationError(
            f"Registry authentication permissions are invalid for {config.environment}"
        ) from None


def validate_production_isolation(repo: Path, prod: EnvironmentConfig) -> None:
    _, stage = load_configuration(repo, "stage")
    comparisons = {
        "server host": (prod.server.host, stage.server.host),
        "domain": (prod.domain, stage.domain),
        "remote runtime": (prod.application.remote_dir, stage.application.remote_dir),
        "Compose": (prod.application.compose, stage.application.compose),
        "env file": (prod.application.env_file, stage.application.env_file),
    }
    reused = [
        label
        for label, (prod_value, stage_value) in comparisons.items()
        if prod_value == stage_value
    ]
    if reused:
        raise ConfigurationError(
            "Production must not reuse Stage " + ", ".join(reused)
        )
    if _host_addresses(prod.server.host, prod.server.ssh_port).intersection(
        _host_addresses(stage.server.host, stage.server.ssh_port)
    ):
        raise ConfigurationError("Production and Stage server hosts resolve to the same address")


def _host_addresses(host: str, port: int) -> set[str]:
    try:
        return {str(ipaddress.ip_address(host))}
    except ValueError:
        pass
    try:
        return {
            str(ipaddress.ip_address(str(info[4][0]).split("%", 1)[0]))
            for info in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        }
    except socket.gaierror as exc:
        raise ConfigurationError(f"Unable to resolve configured server host {host}") from exc


def _published_binding(binding: object) -> tuple[str, int]:
    if isinstance(binding, str):
        parts = binding.split(":")
        if len(parts) != 3 or not parts[1].isdigit():
            raise ConfigurationError(
                f"Compose port {binding!r} must explicitly use host_ip:host_port:container_port"
            )
        return parts[0], int(parts[1])
    if isinstance(binding, dict):
        host_ip = binding.get("host_ip")
        published = binding.get("published")
        if host_ip is None or not str(published).isdigit() or binding.get("mode", "host") != "host":
            raise ConfigurationError(f"Unsupported Compose port definition: {binding!r}")
        return str(host_ip), int(str(published))
    raise ConfigurationError(f"Unsupported Compose port definition: {binding!r}")


def _validate_service_volumes(
    service_name: object,
    raw_volumes: object,
    allowed_bind_paths: set[str],
    declared_volumes: set[str],
) -> None:
    if not isinstance(raw_volumes, list):
        raise ConfigurationError(f"Compose service {service_name!r} volumes must be a list")
    for volume in raw_volumes:
        if _contains_interpolation(volume):
            raise ConfigurationError(
                f"Compose service {service_name!r} interpolates a volume definition"
            )
        if isinstance(volume, str):
            source = volume.split(":", 1)[0]
            if source.startswith((".", "/")):
                if not source.startswith("/") or source not in allowed_bind_paths:
                    raise ConfigurationError(
                        f"Compose service {service_name!r} uses an unapproved bind mount"
                    )
            elif ":" in volume and source not in declared_volumes:
                raise ConfigurationError(
                    f"Compose service {service_name!r} references an undeclared named volume"
                )
        elif isinstance(volume, dict):
            volume_type = volume.get("type", "volume")
            if volume_type == "bind":
                bind_source = volume.get("source")
                if not isinstance(bind_source, str) or bind_source not in allowed_bind_paths:
                    raise ConfigurationError(
                        f"Compose service {service_name!r} uses an unapproved bind mount"
                    )
            elif volume_type != "volume":
                raise ConfigurationError(
                    f"Compose service {service_name!r} uses unsupported volume type"
                )
            else:
                named_source = volume.get("source")
                if named_source is not None and named_source not in declared_volumes:
                    raise ConfigurationError(
                        f"Compose service {service_name!r} references an undeclared named volume"
                    )
        else:
            raise ConfigurationError(
                f"Compose service {service_name!r} has an invalid volume definition"
            )


def _validate_volume_definitions(raw_volumes: object) -> set[str]:
    if not isinstance(raw_volumes, dict):
        raise ConfigurationError("Compose volumes must be a mapping")
    volumes: set[str] = set()
    for volume_name, definition in raw_volumes.items():
        if not isinstance(volume_name, str) or "$" in volume_name:
            raise ConfigurationError("Compose volume names cannot use interpolation")
        if definition not in (None, {}):
            if _contains_interpolation(definition):
                raise ConfigurationError(
                    f"Compose volume {volume_name!r} contains forbidden interpolation"
                )
            raise ConfigurationError(
                f"Compose volume {volume_name!r} must be a managed named volume"
            )
        volumes.add(volume_name)
    return volumes


def _contains_interpolation(value: object) -> bool:
    if isinstance(value, str):
        return "$" in value
    if isinstance(value, dict):
        return any(_contains_interpolation(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_interpolation(item) for item in value)
    return False


def _validate_network_definitions(raw_networks: object) -> set[str]:
    if not isinstance(raw_networks, dict):
        raise ConfigurationError("Compose networks must be a mapping")
    networks = {"default"}
    for network_name, raw_network in raw_networks.items():
        if not isinstance(network_name, str) or "$" in network_name:
            raise ConfigurationError("Compose network names cannot use interpolation")
        if raw_network is None:
            raw_network = {}
        if not isinstance(raw_network, dict):
            raise ConfigurationError(f"Compose network {network_name!r} must be a mapping")
        if _contains_interpolation(raw_network):
            raise ConfigurationError(
                f"Compose network {network_name!r} contains forbidden interpolation"
            )
        unsupported = set(raw_network) - {"driver", "internal"}
        if unsupported:
            raise ConfigurationError(
                f"Compose network {network_name!r} uses unsupported options: "
                + ", ".join(sorted(unsupported))
            )
        if raw_network.get("driver", "bridge") != "bridge":
            raise ConfigurationError(
                f"Compose network {network_name!r} must use the managed bridge driver"
            )
        if "internal" in raw_network and not isinstance(raw_network["internal"], bool):
            raise ConfigurationError(f"Compose network {network_name!r} internal must be boolean")
        networks.add(network_name)
    return networks


def _validate_service_networks(
    service_name: object, raw_networks: object, declared_networks: set[str]
) -> None:
    if isinstance(raw_networks, list):
        referenced = raw_networks
    elif isinstance(raw_networks, dict):
        if any(value not in (None, {}) for value in raw_networks.values()):
            raise ConfigurationError(
                f"Compose service {service_name!r} uses unsupported network attachment options"
            )
        referenced = list(raw_networks)
    else:
        raise ConfigurationError(
            f"Compose service {service_name!r} networks must be a list or mapping"
        )
    for network_name in referenced:
        if (
            not isinstance(network_name, str)
            or "$" in network_name
            or network_name not in declared_networks
        ):
            raise ConfigurationError(
                f"Compose service {service_name!r} references an unmanaged network"
            )
