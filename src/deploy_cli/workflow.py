import hashlib
import ipaddress
import json
import socket
import urllib.error
import urllib.request
from pathlib import Path

import yaml

from .models import EnvironmentConfig, GlobalConfig
from .runner import AnsibleRunner, RunnerError, ansible_vars


def deployment_manifest(config: EnvironmentConfig) -> tuple[str, list[str]]:
    compose_bytes = config.application.compose.read_bytes()
    compose = yaml.safe_load(compose_bytes)
    images = sorted(
        str(service["image"])
        for service in compose["services"].values()
        if isinstance(service, dict) and "image" in service
    )
    inputs: list[tuple[str, bytes]] = [
        (
            "config",
            json.dumps(
                {
                    "allowed_bind_paths": config.application.allowed_bind_paths,
                    "allowed_loopback_ports": config.application.allowed_loopback_ports,
                    "domain": config.domain,
                    "environment": config.environment,
                    "health_path": config.health_path,
                    "remote_dir": config.application.remote_dir,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode(),
        ),
        ("compose", compose_bytes),
        ("env", config.application.env_file.read_bytes()),
        ("images", json.dumps(images, separators=(",", ":")).encode()),
    ]
    registry = config.application.registry_auth_file
    if registry is not None:
        inputs.append(("registry_auth", registry.read_bytes()))
    digest = hashlib.sha256()
    for name, value in inputs:
        digest.update(name.encode() + b"\0" + len(value).to_bytes(8, "big") + value)
    return digest.hexdigest(), images


def write_inventory(repo: Path, config: EnvironmentConfig, *, bootstrap: bool) -> Path:
    state = repo / ".deploy-state" / config.environment
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = state / ("bootstrap.yml" if bootstrap else "managed.yml")
    user = config.server.bootstrap_user if bootstrap else config.server.deploy_user
    content = (
        "---\nall:\n  hosts:\n    target:\n"
        f"      ansible_host: {config.server.host!r}\n"
        f"      ansible_port: {config.server.ssh_port}\n"
        f"      ansible_user: {user!r}\n"
    )
    path.write_text(content, encoding="utf-8")
    return path


def dns_preflight(config: EnvironmentConfig) -> None:
    domain_addresses = _resolved_addresses(config.domain, 443)
    server_addresses = _resolved_addresses(config.server.host, config.server.ssh_port)
    if not domain_addresses.intersection(server_addresses):
        raise RunnerError(
            f"DNS {config.domain} and server {config.server.host} "
            "do not resolve to a common address",
            2,
        )


def _resolved_addresses(host: str, port: int) -> set[str]:
    try:
        literal = ipaddress.ip_address(host)
        return {_normalized_address(literal)}
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise RunnerError(f"DNS lookup failed for {host}: {exc}", 2) from exc
    addresses = {_normalized_address(str(info[4][0])) for info in infos}
    if not addresses:
        raise RunnerError(f"DNS lookup returned no addresses for {host}", 2)
    return addresses


def _normalized_address(value: str | ipaddress.IPv4Address | ipaddress.IPv6Address) -> str:
    if isinstance(value, (ipaddress.IPv4Address, ipaddress.IPv6Address)):
        address = value
    else:
        address = ipaddress.ip_address(value.split("%", 1)[0])
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return str(address.ipv4_mapped)
    return str(address)


def deploy(
    repo: Path,
    global_config: GlobalConfig,
    config: EnvironmentConfig,
    runner: AnsibleRunner,
    *,
    dry_run: bool,
    deployment_version: str = "unmanaged",
    bootstrap_password: str | None = None,
) -> None:
    checksum, images = deployment_manifest(config)
    variables = ansible_vars(
        global_config,
        config,
        deployment_version=deployment_version,
        deployment_checksum=checksum,
        deployment_images=images,
    )
    runner.build_image()
    runner.trust_host(
        config.server.host, config.server.ssh_port, config.server.host_key_fingerprints
    )
    bootstrap_inventory = write_inventory(repo, config, bootstrap=True)
    managed_inventory = write_inventory(repo, config, bootstrap=False)
    runner.playbook(
        "guard_environment.yml",
        bootstrap_inventory,
        variables,
        config.server.ssh_key,
        check=dry_run,
        exit_code=3,
        bootstrap_password=bootstrap_password,
    )
    # A check run cannot safely predict creation of a user and reconnect as that user.
    if not dry_run:
        runner.playbook(
            "bootstrap.yml",
            bootstrap_inventory,
            variables,
            config.server.ssh_key,
            bootstrap_password=bootstrap_password,
        )
        runner.playbook(
            "verify_deploy_access.yml",
            managed_inventory,
            variables,
            config.server.ssh_key,
            exit_code=4,
        )
        runner.playbook(
            "abort_release.yml",
            managed_inventory,
            variables,
            config.server.ssh_key,
            exit_code=6,
        )
    try:
        runner.playbook(
            "site.yml",
            managed_inventory,
            variables,
            config.server.ssh_key,
            compose_file=config.application.compose,
            env_file=config.application.env_file,
            registry_auth_file=config.application.registry_auth_file,
            check=dry_run,
            exit_code=6,
        )
        if not dry_run:
            runner.playbook(
                "health.yml",
                managed_inventory,
                variables,
                config.server.ssh_key,
                exit_code=7,
            )
            runner.playbook(
                "finalize_release.yml",
                managed_inventory,
                variables,
                config.server.ssh_key,
                exit_code=6,
            )
    except RunnerError as original:
        if dry_run:
            raise
        try:
            runner.playbook(
                "abort_release.yml",
                managed_inventory,
                variables,
                config.server.ssh_key,
                exit_code=6,
            )
        except RunnerError as recovery:
            raise RunnerError(
                f"{original}; recovery also failed: {recovery}", original.exit_code
            ) from original
        raise


def update_server(
    repo: Path,
    global_config: GlobalConfig,
    config: EnvironmentConfig,
    runner: AnsibleRunner,
    *,
    dry_run: bool,
) -> None:
    runner.build_image()
    runner.trust_host(
        config.server.host, config.server.ssh_port, config.server.host_key_fingerprints
    )
    inventory = write_inventory(repo, config, bootstrap=False)
    runner.playbook(
        "guard_environment.yml",
        inventory,
        ansible_vars(global_config, config),
        config.server.ssh_key,
        check=dry_run,
        exit_code=3,
    )
    runner.playbook(
        "update.yml",
        inventory,
        ansible_vars(global_config, config),
        config.server.ssh_key,
        check=dry_run,
    )


def rollback(
    repo: Path,
    global_config: GlobalConfig,
    config: EnvironmentConfig,
    runner: AnsibleRunner,
) -> None:
    runner.build_image()
    runner.trust_host(
        config.server.host, config.server.ssh_port, config.server.host_key_fingerprints
    )
    inventory = write_inventory(repo, config, bootstrap=False)
    variables = ansible_vars(global_config, config)
    runner.playbook(
        "guard_environment.yml",
        inventory,
        variables,
        config.server.ssh_key,
        exit_code=3,
    )
    runner.playbook(
        "abort_release.yml", inventory, variables, config.server.ssh_key, exit_code=9
    )
    try:
        runner.playbook(
            "rollback.yml", inventory, variables, config.server.ssh_key, exit_code=9
        )
        runner.playbook(
            "finalize_release.yml", inventory, variables, config.server.ssh_key, exit_code=9
        )
    except RunnerError as original:
        try:
            runner.playbook(
                "abort_release.yml", inventory, variables, config.server.ssh_key, exit_code=9
            )
        except RunnerError as recovery:
            raise RunnerError(
                f"{original}; recovery also failed: {recovery}", original.exit_code
            ) from original
        raise


def status(config: EnvironmentConfig, *, timeout: float = 10.0) -> None:
    url = f"https://{config.domain}{config.health_path}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310
            if response.status != 200:
                raise RunnerError(f"Health check returned HTTP {response.status}", 7)
    except (OSError, urllib.error.URLError) as exc:
        raise RunnerError(f"Health check failed: {exc}", 7) from exc
