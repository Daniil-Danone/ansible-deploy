import ipaddress
import socket
import urllib.error
import urllib.request
from pathlib import Path

from .models import EnvironmentConfig, GlobalConfig
from .runner import AnsibleRunner, RunnerError, ansible_vars


def write_inventory(repo: Path, config: EnvironmentConfig, *, bootstrap: bool) -> Path:
    state = repo / ".deploy-state"
    state.mkdir(mode=0o700, exist_ok=True)
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
) -> None:
    variables = ansible_vars(global_config, config)
    runner.build_image()
    runner.trust_host(
        config.server.host, config.server.ssh_port, config.server.host_key_fingerprints
    )
    bootstrap_inventory = write_inventory(repo, config, bootstrap=True)
    managed_inventory = write_inventory(repo, config, bootstrap=False)
    # A check run cannot safely predict creation of a user and reconnect as that user.
    if not dry_run:
        runner.playbook("bootstrap.yml", bootstrap_inventory, variables, config.server.ssh_key)
        runner.playbook(
            "verify_deploy_access.yml",
            managed_inventory,
            variables,
            config.server.ssh_key,
            exit_code=4,
        )
    runner.playbook(
        "site.yml",
        managed_inventory,
        variables,
        config.server.ssh_key,
        compose_file=config.application.compose,
        env_file=config.application.env_file,
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
        "update.yml",
        inventory,
        ansible_vars(global_config, config),
        config.server.ssh_key,
        check=dry_run,
    )


def status(config: EnvironmentConfig, *, timeout: float = 10.0) -> None:
    url = f"https://{config.domain}{config.health_path}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310
            if response.status != 200:
                raise RunnerError(f"Health check returned HTTP {response.status}", 7)
    except (OSError, urllib.error.URLError) as exc:
        raise RunnerError(f"Health check failed: {exc}", 7) from exc
