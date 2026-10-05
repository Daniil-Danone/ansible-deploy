from pathlib import Path

import pytest

from deploy_cli.config import ConfigurationError, load_configuration, validate_compose


def _config(tmp_path: Path, compose: str):
    repo = Path(__file__).parents[1] / "examples/demo-app"
    _, config = load_configuration(repo, "stage")
    compose_path = tmp_path / "compose.yml"
    compose_path.write_text(compose, encoding="utf-8")
    config.application.compose = compose_path
    return config


def _prod_config(tmp_path: Path, compose: str):
    repo = Path(__file__).parents[1] / "examples/demo-app"
    _, config = load_configuration(repo, "prod")
    compose_path = tmp_path / "compose.yml"
    compose_path.write_text(compose, encoding="utf-8")
    config.application.compose = compose_path
    return config


def test_loopback_binding_from_allowlist_is_accepted(tmp_path: Path) -> None:
    config = _config(
        tmp_path,
        "services:\n  app:\n    image: example/app\n    ports: ['127.0.0.1:8080:80']\n",
    )

    validate_compose(config)


@pytest.mark.parametrize(
    "service",
    [
        "network_mode: host",
        "network_mode: ${MODE:-host}",
        "ports: ['8080:80']",
        "ports: ['0.0.0.0:8080:80']",
        "ports: ['127.0.0.1:5432:5432']",
        "ports: ['${BIND:-127.0.0.1}:8080:80']",
        "extends: {file: base.yml, service: app}",
    ],
)
def test_unsafe_networking_is_rejected(tmp_path: Path, service: str) -> None:
    config = _config(tmp_path, f"services:\n  app:\n    image: example/app\n    {service}\n")

    with pytest.raises(ConfigurationError, match="forbidden|explicitly|interpolates"):
        validate_compose(config)


def test_compose_include_is_rejected(tmp_path: Path) -> None:
    config = _config(
        tmp_path,
        "include: [base.yml]\nservices:\n  app:\n    image: example/app\n",
    )

    with pytest.raises(ConfigurationError, match="include is forbidden"):
        validate_compose(config)


def test_external_network_named_host_is_rejected(tmp_path: Path) -> None:
    config = _config(
        tmp_path,
        """services:
  app:
    image: example/app
    networks: [escape]
networks:
  escape:
    external: true
    name: host
""",
    )

    with pytest.raises(ConfigurationError, match="unsupported options"):
        validate_compose(config)


@pytest.mark.parametrize(
    "network",
    [
        "driver: host",
        "driver: ${NETWORK_DRIVER:-bridge}",
        "name: custom-name",
    ],
)
def test_only_managed_bridge_networks_are_accepted(tmp_path: Path, network: str) -> None:
    config = _config(
        tmp_path,
        f"services:\n  app:\n    image: example/app\n    networks: [app]\n"
        f"networks:\n  app:\n    {network}\n",
    )

    with pytest.raises(ConfigurationError, match="bridge|interpolation|unsupported"):
        validate_compose(config)


@pytest.mark.parametrize(
    "volume",
    ["./data:/data", "/srv/other:/data", {"type": "bind", "source": "./data", "target": "/data"}],
)
def test_unapproved_bind_mounts_are_rejected(tmp_path: Path, volume: object) -> None:
    config = _config(tmp_path, "services:\n  app:\n    image: example/app\n")
    config.application.compose.write_text(
        "services:\n  app:\n    image: example/app\n    volumes:\n      - "
        + (
            repr(volume)
            if isinstance(volume, str)
            else "{type: bind, source: ./data, target: /data}"
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError, match="unapproved bind mount"):
        validate_compose(config)


@pytest.mark.parametrize("image", ["example/app", "example/app:latest", "example/app:1.2.3"])
def test_production_requires_digest_pinned_images(tmp_path: Path, image: str) -> None:
    config = _prod_config(tmp_path, f"services:\n  app:\n    image: {image}\n")

    with pytest.raises(ConfigurationError, match="digest-pinned"):
        validate_compose(config)


@pytest.mark.parametrize(
    "compose",
    [
        "services:\n  app:\n    image: example/app\n    volumes: ['${DATA_DIR}:/data']\n",
        (
            "services:\n  app:\n    image: example/app\n    volumes:\n"
            "      - {type: bind, source: '${DATA_DIR}', target: /data}\n"
        ),
        (
            "services:\n  app:\n    image: example/app\n    volumes: [data:/data]\n"
            "volumes:\n  data:\n    driver_opts:\n      device: '${DATA_DIR}'\n"
        ),
    ],
)
def test_volume_interpolation_is_rejected_everywhere(tmp_path: Path, compose: str) -> None:
    config = _config(tmp_path, compose)

    with pytest.raises(ConfigurationError, match="interpolat"):
        validate_compose(config)


def test_declared_named_volume_is_accepted(tmp_path: Path) -> None:
    config = _config(
        tmp_path,
        "services:\n  app:\n    image: example/app\n    volumes: [data:/data]\n"
        "volumes:\n  data: {}\n",
    )

    validate_compose(config)
