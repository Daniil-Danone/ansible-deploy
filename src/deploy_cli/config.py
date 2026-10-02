from pathlib import Path
from typing import Any

import yaml
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
    if environment != "stage":
        raise ConfigurationError(f"Environment is not implemented yet: {environment}")
    global_config = _load_yaml(repo / "config/global.yml", GlobalConfig)
    env_path = repo / "environments" / environment / "config.yml"
    env_config = _load_yaml(env_path, EnvironmentConfig)
    app = env_config.application
    app.compose = (repo / app.compose).resolve()
    app.env_file = (repo / app.env_file).resolve()
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
