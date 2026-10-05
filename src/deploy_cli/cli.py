import argparse
import base64
import getpass
import json
import re
import subprocess
import sys
import warnings
from pathlib import Path

from .config import (
    ConfigurationError,
    load_configuration,
    read_external_secret_text,
    validate_backup_inputs,
    validate_compose,
    validate_environment_file,
    validate_local_inputs,
    validate_observability_inputs,
    validate_production_isolation,
    validate_registry_auth,
    validate_restore_isolation,
)
from .images import publish_images
from .keys import ensure_deploy_key
from .models import EnvironmentConfig, GlobalConfig, MonitoringConfig, is_backup_identifier
from .project import ProjectError, ProjectSyncResult, sync_project
from .redaction import Redactor, secrets_from_env
from .runner import AnsibleRunner, RunnerError
from .secret_store import SecretStoreError, create_project_id, external_secret_root
from .workflow import (
    backup_operation,
    collector_status,
    deploy,
    deploy_collector,
    deploy_monitoring,
    dns_preflight,
    monitoring_status,
    restore_backup,
    rollback,
    status,
    update_monitoring,
    update_server,
)


def _json_string_values(value: object) -> set[str]:
    if isinstance(value, str):
        return {value}
    if isinstance(value, dict):
        return {secret for item in value.values() for secret in _json_string_values(item)}
    if isinstance(value, list):
        return {secret for item in value for secret in _json_string_values(item)}
    return set()


def _operation_secrets(config: EnvironmentConfig | MonitoringConfig) -> set[str]:
    """Read validated operation inputs once and return values that must never reach output."""
    files: list[tuple[Path, str, str]] = []
    if isinstance(config, MonitoringConfig):
        files.append((config.monitoring.secrets_file, "monitoring secrets", "env"))
    else:
        files.append((config.application.env_file, "application environment", "env"))
        if config.application.registry_auth_file is not None:
            files.append(
                (config.application.registry_auth_file, "registry authentication", "registry")
            )
        if config.collector is not None:
            files.append((config.collector.password_file, "collector password", "raw"))

    secrets = {str(config.server.ssh_key), *(str(path) for path, _, _ in files)}
    for path, field, kind in files:
        if not path.is_file():
            continue
        text = read_external_secret_text(config, path, field=field)
        secrets.add(text)
        if kind == "env":
            secrets.update(secrets_from_env(text))
        elif kind == "raw":
            secrets.add(text.strip())
        else:
            try:
                registry = json.loads(text)
            except (json.JSONDecodeError, UnicodeError):
                continue
            secrets.update(_json_string_values(registry))
            if isinstance(registry, dict):
                auths = registry.get("auths", {})
                if isinstance(auths, dict):
                    for entry in auths.values():
                        if not isinstance(entry, dict) or not isinstance(entry.get("auth"), str):
                            continue
                        try:
                            decoded = base64.b64decode(entry["auth"], validate=True).decode("utf-8")
                        except (ValueError, UnicodeError):
                            continue
                        secrets.update({decoded, *decoded.split(":", 1)})
    return {secret for secret in secrets if secret}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="deploy")
    project = parser.add_mutually_exclusive_group()
    project.add_argument(
        "--project-dir",
        type=Path,
        default=Path.cwd(),
        help="application project directory (default: current directory)",
    )
    project.add_argument("--repo", dest="project_dir", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)
    project_command = sub.add_parser("project", help="Initialize or update project files")
    project_sub = project_command.add_subparsers(dest="project_command", required=True)
    project_sub.add_parser("init", help="Create a commit-safe deployment scaffold")
    project_sync = project_sub.add_parser("sync", help="Update managed scaffold files")
    project_sync.add_argument(
        "--check", action="store_true", help="report pending updates without writing files"
    )
    for environment, label in (("stage", "Stage"), ("prod", "Production")):
        deploy_parser = sub.add_parser(environment, help=f"Provision and deploy {label}")
        deploy_parser.add_argument("--dry-run", action="store_true")
        deploy_parser.add_argument(
            "--ask-bootstrap-password",
            action="store_true",
            help="prompt securely for the initial SSH password",
        )
        deploy_parser.add_argument("--version", help="Deployment version (defaults to Git SHA)")
        if environment == "prod":
            deploy_parser.add_argument("--yes", action="store_true")
    status_parser = sub.add_parser("status", help="Check public environment health")
    status_parser.add_argument("environment", choices=["stage", "prod"])
    monitoring = sub.add_parser("monitoring", help="Manage Grafana and Loki")
    monitoring.add_argument("action", choices=["deploy", "status", "update"])
    monitoring.add_argument("--dry-run", action="store_true")
    monitoring.add_argument("--ask-bootstrap-password", action="store_true")
    collectors = sub.add_parser("collectors", help="Manage Stage/Production log collectors")
    collectors.add_argument("action", choices=["deploy", "status", "update"])
    collectors.add_argument("environment", choices=["stage", "prod", "all"])
    collectors.add_argument("--dry-run", action="store_true")
    collectors.add_argument("--yes", action="store_true")
    server = sub.add_parser("server")
    server_sub = server.add_subparsers(dest="server_command", required=True)
    update = server_sub.add_parser("update", help="Reapply managed server state")
    update.add_argument("environment", choices=["stage", "prod", "monitoring", "all"])
    update.add_argument("--dry-run", action="store_true")
    update.add_argument("--yes", action="store_true")
    rollback_parser = sub.add_parser("rollback", help="Roll back Production application/config")
    rollback_parser.add_argument("environment", choices=["prod"])
    rollback_parser.add_argument("--yes", action="store_true")
    images = sub.add_parser("images", help="Build and publish application images")
    images_sub = images.add_subparsers(dest="images_command", required=True)
    publish = images_sub.add_parser("publish", help="Publish images and pin Compose digests")
    publish.add_argument("environment", choices=["stage", "prod"])
    publish.add_argument("--registry", required=True, choices=["ghcr", "dockerhub"])
    publish.add_argument("--namespace", required=True)
    publish.add_argument("--username")
    publish.add_argument("--ask-token", action="store_true")
    publish.add_argument(
        "--pull-username", help="server pull username (defaults to publisher username)"
    )
    publish.add_argument(
        "--ask-pull-token",
        action="store_true",
        help="prompt for a separate read-only token used by the server",
    )
    publish.add_argument("--tag", help="image tag (defaults to application Git SHA)")
    secrets = sub.add_parser("secrets", help="Manage external project secrets")
    secrets_sub = secrets.add_subparsers(dest="secrets_command", required=True)
    secrets_path = secrets_sub.add_parser("path", help="Print this project's external secret root")
    secrets_path.add_argument(
        "--new-project-id",
        action="store_true",
        help="create or replace .deploy/project-id without copying secrets",
    )
    backup = sub.add_parser("backup", help="Manage encrypted Production backups")
    backup_sub = backup.add_subparsers(dest="backup_command", required=True)
    for action in ("setup", "run", "list"):
        operation = backup_sub.add_parser(action)
        operation.add_argument("environment", choices=["prod"])
    restore = backup_sub.add_parser("restore")
    restore.add_argument("environment", choices=["prod"])
    restore.add_argument("--target", required=True)
    restore.add_argument("--backup", required=True, dest="backup_id")
    restore.add_argument(
        "--ask-bootstrap-password",
        action="store_true",
        help="prompt securely when preparing a pristine Restore VPS",
    )
    restore.add_argument("--yes", action="store_true")
    return parser


def _print_project_result(result: ProjectSyncResult) -> None:
    for path in result.created:
        print(f"[CREATE] {path.as_posix()}")
    for path in result.updated:
        print(f"[UPDATE] {path.as_posix()}")
    for path, candidate in result.conflicts:
        print(
            f"[CONFLICT] {path.as_posix()} was modified; review {candidate.as_posix()}",
            file=sys.stderr,
        )
    if not result.changes_required:
        print("[OK] Project scaffold is up to date")


def _deployment_version(repo: Path, supplied: str | None) -> str:
    if supplied is not None:
        version = supplied
    else:
        try:
            result = subprocess.run(  # noqa: S603 - fixed Git command and argument vector
                ["git", "rev-parse", "HEAD"],  # noqa: S607 - fixed executable
                cwd=repo,
                capture_output=True,
                text=True,
                check=True,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ConfigurationError(
                "Unable to determine deployment Git SHA; use --version"
            ) from exc
        version = result.stdout.strip()
    if not re.fullmatch(r"[0-9a-f]{7,40}", version):
        raise ConfigurationError("Deployment version must be a 7-40 character lowercase Git SHA")
    return version


def _confirm_production(config_environment: str, host: str, domain: str, *, yes: bool) -> None:
    if yes:
        return
    if not sys.stdin.isatty():
        raise ConfigurationError("Production operation requires an interactive terminal or --yes")
    print(f"Production target: environment={config_environment}, host={host}, domain={domain}")
    if input("Type 'prod' to continue: ").strip() != "prod":
        raise ConfigurationError("Production operation was not confirmed")


def _prompt_bootstrap_password(user: str, host: str) -> str:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            password = getpass.getpass(f"Bootstrap SSH password for {user}@{host}: ")
    except getpass.GetPassWarning as exc:
        raise ConfigurationError(
            "Secure password input is unavailable; run from an interactive terminal"
        ) from exc
    if not password:
        raise ConfigurationError("Bootstrap SSH password cannot be empty")
    if any(character in password for character in ("\0", "\r", "\n")):
        raise ConfigurationError("Bootstrap SSH password cannot contain NUL, CR or LF")
    return password


def _string_values(value: object) -> set[str]:
    if isinstance(value, str):
        return {value} if value else set()
    if isinstance(value, dict):
        return set().union(*(_string_values(item) for item in value.values()))
    if isinstance(value, list):
        return set().union(*(_string_values(item) for item in value))
    return set()


def _secret_text_fragments(value: str) -> set[str]:
    fragments = {value.strip()} if value.strip() else set()
    for line in value.splitlines():
        stripped = line.strip()
        if stripped:
            fragments.add(stripped)
        if "=" in stripped:
            _, secret = stripped.split("=", 1)
            if secret:
                fragments.add(secret)
    return fragments


def _operation_secrets(config: EnvironmentConfig) -> set[str]:
    values = {str(config.server.ssh_key), str(config.application.env_file)}
    env_text = read_external_secret_text(
        config, config.application.env_file, field="application environment"
    )
    values.update(secrets_from_env(env_text))
    registry = config.application.registry_auth_file
    if registry is not None:
        values.add(str(registry))
        registry_text = read_external_secret_text(
            config, registry, field="registry authentication"
        )
        values.update(_secret_text_fragments(registry_text))
        try:
            values.update(_string_values(json.loads(registry_text)))
        except json.JSONDecodeError:
            pass
    return values


def _load_and_validate(
    repo: Path, environment: str, command: str, *, action: str | None = None
) -> tuple[GlobalConfig, EnvironmentConfig | MonitoringConfig]:
    global_config, config = load_configuration(repo, environment)
    if environment == "prod" and isinstance(config, EnvironmentConfig):
        validate_production_isolation(repo, config)
    if command == "monitoring":
        if not isinstance(config, MonitoringConfig):
            raise ConfigurationError("Monitoring command requires monitoring configuration")
        validate_local_inputs(
            config,
            require_ssh=action == "update",
            require_public_key=False,
            require_application=False,
        )
        if action != "status":
            validate_observability_inputs(config)
    elif command == "collectors":
        if not isinstance(config, EnvironmentConfig):
            raise ConfigurationError("Collectors require an application environment")
        validate_local_inputs(config, require_public_key=False, require_application=False)
        if action != "status":
            validate_observability_inputs(config)
    elif command == "status":
        if not isinstance(config, EnvironmentConfig):
            raise ConfigurationError("Application status requires stage or prod")
        validate_local_inputs(
            config, require_ssh=False, require_public_key=False, require_application=False
        )
    elif command in {"server", "rollback"}:
        validate_local_inputs(config, require_public_key=False, require_application=False)
        if isinstance(config, MonitoringConfig):
            validate_observability_inputs(config)
    else:
        if not isinstance(config, EnvironmentConfig):
            raise ConfigurationError("Application deploy requires stage or prod")
        validate_local_inputs(config, require_ssh=False, require_public_key=False)
        validate_compose(config)
        validate_environment_file(config)
        validate_registry_auth(config)
    return global_config, config


def run(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    project_dir = args.project_dir.resolve()
    try:
        if args.command == "project":
            result = sync_project(
                project_dir,
                check=args.project_command == "sync" and args.check,
            )
            _print_project_result(result)
            return 1 if result.conflicts or (result.check and result.changes_required) else 0
        if args.command == "secrets":
            if args.new_project_id:
                create_project_id(project_dir)
            print(external_secret_root(project_dir))
            return 0
        if args.command == "images":
            published = publish_images(
                project_dir,
                args.environment,
                registry=args.registry,
                namespace=args.namespace,
                username=args.username,
                ask_token=args.ask_token,
                pull_username=args.pull_username,
                ask_pull_token=args.ask_pull_token,
                tag=args.tag,
            )
            for image in published:
                print(f"[IMAGE] {image.service}: {image.immutable_reference}")
            print(f"[OK] Published {len(published)} images and updated {args.environment} Compose")
            return 0
        if args.command == "backup":
            try:
                if args.backup_command == "restore" and args.target != "restore":
                    raise ConfigurationError(
                        "Restore target must be the dedicated restore environment"
                    )
                global_config, source = load_configuration(project_dir, "prod")
                if not isinstance(source, EnvironmentConfig):
                    raise ConfigurationError("Production environment configuration is required")
                validate_backup_inputs(source, require_identity=args.backup_command == "restore")
                backup_config = source.backup
                if backup_config is None:
                    raise ConfigurationError("Production backup is not configured")
                if args.backup_command == "restore":
                    if not is_backup_identifier(args.backup_id):
                        raise ConfigurationError("Backup restore identifier is invalid")
                    _, target = load_configuration(project_dir, args.target)
                    if not isinstance(target, EnvironmentConfig) or target.environment != "restore":
                        raise ConfigurationError(
                            "Restore target must be a dedicated restore environment"
                        )
                    validate_restore_isolation(source, target)
                    if not args.yes:
                        if not sys.stdin.isatty():
                            raise ConfigurationError(
                                "Restore requires an interactive terminal or --yes"
                            )
                        print(
                            "Restore drill: "
                            f"source={source.environment}, target={target.environment}, "
                            f"host={target.server.host}, backup={args.backup_id}"
                        )
                        if input("Type 'restore' to continue: ").strip() != "restore":
                            raise ConfigurationError("Restore was not confirmed")
                    print(f"[KEY] {ensure_deploy_key(target)}")
                    validate_local_inputs(target)
                    validate_compose(target)
                    validate_environment_file(target)
                    validate_registry_auth(target)
                    dns_preflight(target)
                    active = target
                else:
                    validate_local_inputs(
                        source, require_public_key=False, require_application=False
                    )
                    active = source
                backup_bootstrap_password: str | None = None
                backup_secret_values: set[str] = (
                    _operation_secrets(active) if args.backup_command == "restore" else set()
                )
                if args.backup_command == "restore":
                    if args.ask_bootstrap_password:
                        backup_bootstrap_password = _prompt_bootstrap_password(
                            active.server.bootstrap_user, active.server.host
                        )
                        backup_secret_values.add(backup_bootstrap_password)
                for field, path in (
                    ("backup credentials", backup_config.credentials_file),
                    ("age identity", backup_config.age_identity_file),
                ):
                    backup_secret_values.add(str(path))
                    if field == "age identity" and args.backup_command != "restore":
                        continue
                    backup_secret_values.update(
                        _secret_text_fragments(
                            read_external_secret_text(source, path, field=field)
                        )
                    )
                external_context = active.external_secret_context
                runner = AnsibleRunner(
                    project_dir,
                    Redactor(
                        backup_secret_values
                        | {
                            str(backup_config.credentials_file),
                            str(backup_config.age_identity_file),
                            str(active.server.ssh_key),
                        }
                    ),
                    environment=active.environment,
                    verbose=args.verbose,
                    external_secret_root=(
                        external_context[1] if external_context is not None else None
                    ),
                    external_trusted_base=(
                        external_context[2] if external_context is not None else None
                    ),
                    validate_external_trusted_base=(
                        external_context[3] if external_context is not None else True
                    ),
                )
                if args.backup_command == "restore":
                    restore_backup(
                        project_dir,
                        global_config,
                        source,
                        active,
                        runner,
                        backup_id=args.backup_id,
                        bootstrap_password=backup_bootstrap_password,
                    )
                else:
                    backup_operation(
                        project_dir,
                        global_config,
                        source,
                        runner,
                        action=args.backup_command,
                    )
                print(f"[OK] backup {args.backup_command} completed")
                return 0
            except (ConfigurationError, SecretStoreError) as exc:
                raise RunnerError(str(exc), 8) from exc
        if (
            (args.command in {"stage", "prod"} or args.command == "monitoring")
            and args.ask_bootstrap_password
            and args.dry_run
        ):
            raise ConfigurationError(
                "--ask-bootstrap-password cannot be used with --dry-run; "
                "bootstrap is not performed in check mode"
            )
        if (
            args.command == "monitoring"
            and args.action != "deploy"
            and args.ask_bootstrap_password
        ):
            raise ConfigurationError(
                "--ask-bootstrap-password is only valid for monitoring deploy"
            )
        if args.command in {"stage", "prod"}:
            environment = args.command
        elif args.command == "monitoring":
            environment = "monitoring"
        else:
            environment = args.environment
        all_environments = (
            ["stage", "prod", "monitoring"]
            if args.command == "server"
            else ["stage", "prod"]
        )
        environments = all_environments if environment == "all" else [environment]
        loaded = [
            _load_and_validate(
                project_dir, name, args.command, action=getattr(args, "action", None)
            )
            for name in environments
        ]
        is_dry_run = getattr(args, "dry_run", False)
        if (
            any(name == "prod" for name in environments)
            and args.command != "status"
            and not is_dry_run
        ):
            prod_config = loaded[environments.index("prod")][1]
            _confirm_production(
                prod_config.environment,
                prod_config.server.host,
                prod_config.domain,
                yes=getattr(args, "yes", False),
            )

        if args.command in {"stage", "prod"} or (
            args.command == "monitoring" and args.action == "deploy"
        ):
            for _, config in loaded:
                if not args.dry_run:
                    print(f"[KEY] {ensure_deploy_key(config)}")
                validate_local_inputs(config, require_application=False)

        bootstrap_password: str | None = None
        if args.command in {"stage", "prod", "monitoring"} and getattr(
            args, "ask_bootstrap_password", False
        ):
            bootstrap_password = _prompt_bootstrap_password(
                loaded[0][1].server.bootstrap_user,
                loaded[0][1].server.host,
            )

        update_failures: list[tuple[str, RunnerError]] = []
        for current_environment, (global_config, config) in zip(environments, loaded, strict=True):
            secret_values = _operation_secrets(config)
            if bootstrap_password is not None:
                secret_values.add(bootstrap_password)
            redactor = Redactor(secret_values)
            external_context = config.external_secret_context
            runner = AnsibleRunner(
                project_dir,
                redactor,
                environment=current_environment,
                verbose=args.verbose,
                external_secret_root=(
                    external_context[1] if external_context is not None else None
                ),
                external_trusted_base=(
                    external_context[2] if external_context is not None else None
                ),
                validate_external_trusted_base=(
                    external_context[3] if external_context is not None else True
                ),
            )
            if args.command == "status":
                if not isinstance(config, EnvironmentConfig):
                    raise ConfigurationError("Application status requires stage or prod")
                status(config)
                print(f"[OK] https://{config.domain}{config.health_path} is healthy")
            elif args.command in {"stage", "prod"}:
                if not isinstance(config, EnvironmentConfig):
                    raise ConfigurationError("Application deploy requires stage or prod")
                dns_preflight(config)
                version = _deployment_version(project_dir, args.version)
                deploy(
                    project_dir,
                    global_config,
                    config,
                    runner,
                    dry_run=args.dry_run,
                    deployment_version=version,
                    bootstrap_password=bootstrap_password,
                )
                print(f"[OK] {current_environment} deployment {version} completed")
            elif args.command == "rollback":
                if not isinstance(config, EnvironmentConfig):
                    raise ConfigurationError("Rollback requires Production")
                rollback(project_dir, global_config, config, runner)
                print("[OK] prod rollback completed and verified")
            elif args.command == "monitoring":
                if not isinstance(config, MonitoringConfig):
                    raise ConfigurationError("Monitoring configuration is required")
                if args.action == "status":
                    monitoring_status(config)
                    print(f"[OK] https://{config.domain}/api/health is healthy")
                elif args.action == "deploy":
                    dns_preflight(config)
                    deploy_monitoring(
                        project_dir,
                        global_config,
                        config,
                        runner,
                        dry_run=args.dry_run,
                        bootstrap_password=bootstrap_password,
                    )
                    print("[OK] monitoring stack deployed and verified")
                else:
                    update_monitoring(
                        project_dir, global_config, config, runner, dry_run=args.dry_run
                    )
                    print("[OK] monitoring stack updated")
            elif args.command == "collectors":
                if not isinstance(config, EnvironmentConfig):
                    raise ConfigurationError("Collectors require stage or prod")
                try:
                    if args.action == "status":
                        collector_status(project_dir, global_config, config, runner)
                    else:
                        deploy_collector(
                            project_dir,
                            global_config,
                            config,
                            runner,
                            dry_run=args.dry_run,
                        )
                    print(f"[OK] {current_environment} collector {args.action} completed")
                except RunnerError as exc:
                    if environment != "all":
                        raise
                    update_failures.append((current_environment, exc))
                    print(f"[ERROR] {current_environment} collector failed: {exc}", file=sys.stderr)
            else:
                try:
                    if isinstance(config, MonitoringConfig):
                        update_monitoring(
                            project_dir, global_config, config, runner, dry_run=args.dry_run
                        )
                    else:
                        update_server(
                            project_dir, global_config, config, runner, dry_run=args.dry_run
                        )
                    print(f"[OK] {current_environment} server state updated")
                except RunnerError as exc:
                    if environment != "all":
                        raise
                    update_failures.append((current_environment, exc))
                    print(f"[ERROR] {current_environment} update failed: {exc}", file=sys.stderr)
        if update_failures:
            failed_names = ", ".join(name for name, _ in update_failures)
            operation = "collectors" if args.command == "collectors" else "server update all"
            raise RunnerError(
                f"{operation} failed for: {failed_names}",
                update_failures[0][1].exit_code,
            )
        return 0
    except (ConfigurationError, ProjectError, SecretStoreError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 2
    except RunnerError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return exc.exit_code
    except KeyboardInterrupt:
        print("[ERROR] Interrupted", file=sys.stderr)
        return 1


def main() -> None:
    raise SystemExit(run())


if __name__ == "__main__":
    main()
