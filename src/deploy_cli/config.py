import ipaddress
import json
import re
import socket
from pathlib import Path
from typing import Any

import yaml
from dotenv import dotenv_values
from pydantic import BaseModel, ValidationError

from .models import EnvironmentConfig, GlobalConfig


class ConfigurationError(ValueError):
    """Configuration is absent or invalid."""


def _load_yaml[ModelT: BaseModel](path: Path, model: type[ModelT]) -> ModelT:
    try:
        raw: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
        return model.model_validate(raw)
    except (OSError, yaml.YAMLError, ValidationError) as exc:
        raise ConfigurationError(f"Invalid configuration {path}: {exc}") from exc


def load_configuration(repo: Path, environment: str) -> tuple[GlobalConfig, EnvironmentConfig]:
    if environment not in {"stage", "prod"}:
        raise ConfigurationError(f"Environment is not implemented yet: {environment}")
    global_config = _load_yaml(repo / "config/global.yml", GlobalConfig)
    env_path = repo / "environments" / environment / "config.yml"
    env_config = _load_yaml(env_path, EnvironmentConfig)
    if env_config.environment != environment:
        raise ConfigurationError(
            f"Configuration environment mismatch: requested {environment!r}, "
            f"file declares {env_config.environment!r}"
        )
    app = env_config.application
    app.compose = (repo / app.compose).resolve()
    app.env_file = (repo / app.env_file).resolve()
    if app.registry_auth_file is not None and not app.registry_auth_file.is_absolute():
        app.registry_auth_file = (repo / app.registry_auth_file).resolve()
    return global_config, env_config


def validate_local_inputs(
    config: EnvironmentConfig,
    *,
    require_ssh: bool = True,
    require_public_key: bool = True,
    require_application: bool = True,
) -> None:
    required: list[Path] = []
    if require_ssh:
        required.append(config.server.ssh_key)
    if require_public_key:
        required.append(config.server.public_key)
    if require_application:
        required.extend([config.application.compose, config.application.env_file])
        if config.application.registry_auth_file is not None:
            required.append(config.application.registry_auth_file)
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise ConfigurationError("Required local files are missing: " + ", ".join(missing))


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


def validate_environment_file(config: EnvironmentConfig) -> None:
    """Validate required keys without exposing any secret values."""
    try:
        values = dotenv_values(config.application.env_file)
    except (OSError, ValueError) as exc:
        raise ConfigurationError(f"Invalid environment file: {exc}") from exc
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


def validate_registry_auth(config: EnvironmentConfig) -> None:
    path = config.application.registry_auth_file
    if path is None:
        return
    try:
        document: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ConfigurationError("Registry authentication file is not valid JSON") from exc
    if not isinstance(document, dict) or not isinstance(document.get("auths"), dict):
        raise ConfigurationError("Registry authentication file must contain an auths mapping")


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
