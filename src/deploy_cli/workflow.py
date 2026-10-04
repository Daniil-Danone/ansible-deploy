import hashlib
import ipaddress
import json
import socket
import urllib.error
import urllib.request
from pathlib import Path

import yaml

from .config import read_external_secret_bytes
from .models import EnvironmentConfig, GlobalConfig, MonitoringConfig
from .runner import AnsibleRunner, RunnerError, ansible_vars, prepare_state_directory


def deployment_manifest(config: EnvironmentConfig) -> tuple[str, list[str]]:
    env_bytes = read_external_secret_bytes(
        config, config.application.env_file, field="application environment"
    )
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
        ("env", env_bytes),
        ("images", json.dumps(images, separators=(",", ":")).encode()),
    ]
    registry = config.application.registry_auth_file
    if registry is not None:
        inputs.append(
            (
                "registry_auth",
                read_external_secret_bytes(
                    config, registry, field="registry authentication"
                ),
            )
        )
    digest = hashlib.sha256()
    for name, value in inputs:
        digest.update(name.encode() + b"\0" + len(value).to_bytes(8, "big") + value)
    return digest.hexdigest(), images


def write_inventory(
    repo: Path, config: EnvironmentConfig | MonitoringConfig, *, bootstrap: bool
) -> Path:
    state = prepare_state_directory(repo, config.environment)
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


def dns_preflight(config: EnvironmentConfig | MonitoringConfig) -> None:
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
    managed_error: RunnerError | None = None
    try:
        runner.playbook(
            "verify_deploy_access.yml",
            managed_inventory,
            variables,
            config.server.ssh_key,
            exit_code=4,
        )
        managed_access = True
    except RunnerError as error:
        managed_error = error
        managed_access = False
        if dry_run:
            raise RunnerError(
                f"Managed deployment access failed: {managed_error}. "
                "For a pristine server, run the first deployment without --dry-run "
                "using a pre-authorized bootstrap SSH key or --ask-bootstrap-password",
                managed_error.exit_code,
            ) from managed_error

    if managed_access:
        runner.playbook(
            "guard_environment.yml",
            managed_inventory,
            variables,
            config.server.ssh_key,
            check=dry_run,
            exit_code=3,
        )
    else:
        # Probe bootstrap access without mutation. Once managed access is established,
        # an identity-guard failure above is authoritative and never falls back to root.
        if managed_error is None:
            raise RuntimeError("managed access state is inconsistent")
        try:
            runner.playbook(
                "verify_deploy_access.yml",
                bootstrap_inventory,
                variables,
                config.server.ssh_key,
                exit_code=4,
                bootstrap_password=bootstrap_password,
            )
        except RunnerError as bootstrap_error:
            raise RunnerError(
                f"Managed deployment access failed: {managed_error}; "
                f"bootstrap SSH access also failed: {bootstrap_error}. "
                "Authorize the generated public key for the bootstrap user or rerun "
                "with --ask-bootstrap-password",
                bootstrap_error.exit_code,
            ) from bootstrap_error
        bootstrap_guard_variables = dict(variables, require_unclaimed_environment=True)
        runner.playbook(
            "guard_environment.yml",
            bootstrap_inventory,
            bootstrap_guard_variables,
            config.server.ssh_key,
            exit_code=3,
            bootstrap_password=bootstrap_password,
        )
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
    if not dry_run:
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


def deploy_monitoring(
    repo: Path,
    global_config: GlobalConfig,
    config: MonitoringConfig,
    runner: AnsibleRunner,
    *,
    dry_run: bool,
    bootstrap_password: str | None = None,
) -> None:
    """Provision monitoring without entering the application release transaction."""
    variables = ansible_vars(global_config, config)
    runner.build_image()
    runner.trust_host(
        config.server.host, config.server.ssh_port, config.server.host_key_fingerprints
    )
    managed = write_inventory(repo, config, bootstrap=False)
    bootstrap = write_inventory(repo, config, bootstrap=True)
    try:
        runner.playbook(
            "verify_deploy_access.yml", managed, variables, config.server.ssh_key, exit_code=4
        )
    except RunnerError as managed_error:
        if dry_run:
            raise RunnerError(
                f"Managed deployment access failed: {managed_error}. "
                "Bootstrap monitoring before using --dry-run",
                managed_error.exit_code,
            ) from managed_error
        runner.playbook(
            "verify_deploy_access.yml",
            bootstrap,
            variables,
            config.server.ssh_key,
            exit_code=4,
            bootstrap_password=bootstrap_password,
        )
        runner.playbook(
            "guard_environment.yml",
            bootstrap,
            dict(variables, require_unclaimed_environment=True),
            config.server.ssh_key,
            exit_code=3,
            bootstrap_password=bootstrap_password,
        )
        runner.playbook(
            "bootstrap.yml",
            bootstrap,
            variables,
            config.server.ssh_key,
            bootstrap_password=bootstrap_password,
        )
        runner.playbook(
            "verify_deploy_access.yml", managed, variables, config.server.ssh_key, exit_code=4
        )
    else:
        runner.playbook(
            "guard_environment.yml",
            managed,
            variables,
            config.server.ssh_key,
            check=dry_run,
            exit_code=3,
        )
    runner.playbook(
        "monitoring.yml",
        managed,
        variables,
        config.server.ssh_key,
        observability_secret_file=config.monitoring.secrets_file,
        check=dry_run,
        exit_code=6,
    )
    if not dry_run:
        runner.playbook(
            "monitoring_status.yml", managed, variables, config.server.ssh_key, exit_code=7
        )


def update_monitoring(
    repo: Path,
    global_config: GlobalConfig,
    config: MonitoringConfig,
    runner: AnsibleRunner,
    *,
    dry_run: bool,
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
        check=dry_run,
        exit_code=3,
    )
    runner.playbook(
        "monitoring_update.yml",
        inventory,
        variables,
        config.server.ssh_key,
        observability_secret_file=config.monitoring.secrets_file,
        check=dry_run,
        exit_code=6,
    )


def deploy_collector(
    repo: Path,
    global_config: GlobalConfig,
    config: EnvironmentConfig,
    runner: AnsibleRunner,
    *,
    dry_run: bool,
) -> None:
    if config.collector is None:
        raise RunnerError(f"Collector is not configured for {config.environment}", 2)
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
        check=dry_run,
        exit_code=3,
    )
    runner.playbook(
        "collector.yml",
        inventory,
        variables,
        config.server.ssh_key,
        observability_secret_file=config.collector.password_file,
        check=dry_run,
        exit_code=6,
    )
    if not dry_run:
        runner.playbook(
            "collector_status.yml", inventory, variables, config.server.ssh_key, exit_code=7
        )


def collector_status(
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
        "collector_status.yml",
        inventory,
        variables,
        config.server.ssh_key,
        exit_code=7,
    )


def monitoring_status(config: MonitoringConfig, *, timeout: float = 10.0) -> None:
    url = f"https://{config.domain}/api/health"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310
            if response.status != 200:
                raise RunnerError(f"Grafana health returned HTTP {response.status}", 7)
    except (OSError, urllib.error.URLError) as exc:
        raise RunnerError(f"Grafana health check failed: {exc}", 7) from exc


def _backup_variables(source: EnvironmentConfig, target: EnvironmentConfig) -> dict[str, object]:
    backup = source.backup
    if backup is None:
        raise RunnerError("Production backup is not configured", 8)
    return {
        "app_environment": target.environment,
        "backup_schedule": backup.schedule,
        "backup_id": "",
        "backup_runtime_config": {
            "age_identity": "/run/ansible-deploy-age-identity",
            "age_recipient": backup.age_recipient,
            "compose_file": f"{target.application.remote_dir}/current/compose.yml",
            "include": [item.model_dump(mode="json") for item in backup.include],
            "rclone_config": "/etc/ansible-deploy/backup/rclone.conf",
            "remote": backup.remote,
            "result_file": "/var/lib/ansible-deploy/backup/last-result.json",
            "retention": backup.retention.model_dump(),
        },
    }


def backup_operation(
    repo: Path,
    global_config: GlobalConfig,
    config: EnvironmentConfig,
    runner: AnsibleRunner,
    *,
    action: str,
) -> None:
    runner.build_image()
    runner.trust_host(
        config.server.host, config.server.ssh_port, config.server.host_key_fingerprints
    )
    inventory = write_inventory(repo, config, bootstrap=False)
    variables = ansible_vars(global_config, config)
    variables.update(_backup_variables(config, config))
    variables["backup_action"] = action
    runner.playbook(
        "guard_environment.yml",
        inventory,
        variables,
        config.server.ssh_key,
        exit_code=8,
    )
    runner.playbook(
        "backup.yml",
        inventory,
        variables,
        config.server.ssh_key,
        backup_credentials_file=(
            config.backup.credentials_file
            if config.backup is not None and action == "setup"
            else None
        ),
        exit_code=8,
    )


def restore_backup(
    repo: Path,
    global_config: GlobalConfig,
    source: EnvironmentConfig,
    target: EnvironmentConfig,
    runner: AnsibleRunner,
    *,
    backup_id: str,
) -> None:
    if target.environment != "restore":
        raise RunnerError("Backup restore target must be the dedicated restore environment", 8)
    if source.backup is None:
        raise RunnerError("Production backup is not configured", 8)
    runner.build_image()
    runner.trust_host(
        target.server.host, target.server.ssh_port, target.server.host_key_fingerprints
    )
    inventory = write_inventory(repo, target, bootstrap=False)
    variables = ansible_vars(global_config, target)
    variables.update(_backup_variables(source, target))
    variables.update({"backup_action": "restore", "backup_id": backup_id})
    runner.playbook(
        "guard_environment.yml",
        inventory,
        variables,
        target.server.ssh_key,
        exit_code=8,
    )
    runner.playbook(
        "backup_restore.yml",
        inventory,
        variables,
        target.server.ssh_key,
        backup_credentials_file=source.backup.credentials_file,
        age_identity_file=source.backup.age_identity_file,
        exit_code=8,
    )
