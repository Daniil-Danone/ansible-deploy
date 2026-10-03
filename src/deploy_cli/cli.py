import argparse
import getpass
import re
import subprocess
import sys
import warnings
from pathlib import Path

from .config import (
    ConfigurationError,
    load_configuration,
    validate_compose,
    validate_environment_file,
    validate_local_inputs,
    validate_production_isolation,
    validate_registry_auth,
)
from .keys import ensure_deploy_key
from .models import EnvironmentConfig, GlobalConfig
from .redaction import Redactor, secrets_from_env
from .runner import AnsibleRunner, RunnerError
from .workflow import deploy, dns_preflight, rollback, status, update_server


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="deploy")
    parser.add_argument("--repo", type=Path, default=Path.cwd(), help=argparse.SUPPRESS)
    parser.add_argument("--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)
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
    server = sub.add_parser("server")
    server_sub = server.add_subparsers(dest="server_command", required=True)
    update = server_sub.add_parser("update", help="Reapply managed server state")
    update.add_argument("environment", choices=["stage", "prod", "all"])
    update.add_argument("--dry-run", action="store_true")
    update.add_argument("--yes", action="store_true")
    rollback_parser = sub.add_parser("rollback", help="Roll back Production application/config")
    rollback_parser.add_argument("environment", choices=["prod"])
    rollback_parser.add_argument("--yes", action="store_true")
    return parser


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


def _load_and_validate(
    repo: Path, environment: str, command: str
) -> tuple[GlobalConfig, EnvironmentConfig]:
    global_config, config = load_configuration(repo, environment)
    if environment == "prod":
        validate_production_isolation(repo, config)
    if command == "status":
        validate_local_inputs(
            config, require_ssh=False, require_public_key=False, require_application=False
        )
    elif command in {"server", "rollback"}:
        validate_local_inputs(config, require_public_key=False, require_application=False)
    else:
        validate_local_inputs(config, require_ssh=False, require_public_key=False)
        validate_compose(config)
        validate_environment_file(config)
        validate_registry_auth(config)
    return global_config, config


def run(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    repo = args.repo.resolve()
    try:
        if (
            args.command in {"stage", "prod"}
            and args.ask_bootstrap_password
            and args.dry_run
        ):
            raise ConfigurationError(
                "--ask-bootstrap-password cannot be used with --dry-run; "
                "bootstrap is not performed in check mode"
            )
        environment = args.command if args.command in {"stage", "prod"} else args.environment
        environments = ["stage", "prod"] if environment == "all" else [environment]
        loaded = [_load_and_validate(repo, name, args.command) for name in environments]
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
                yes=args.yes,
            )

        if args.command in {"stage", "prod"}:
            for _, config in loaded:
                if not args.dry_run:
                    print(f"[KEY] {ensure_deploy_key(config)}")
                validate_local_inputs(config, require_application=False)

        bootstrap_password: str | None = None
        if args.command in {"stage", "prod"} and args.ask_bootstrap_password:
            bootstrap_password = _prompt_bootstrap_password(
                loaded[0][1].server.bootstrap_user,
                loaded[0][1].server.host,
            )

        update_failures: list[tuple[str, RunnerError]] = []
        for current_environment, (global_config, config) in zip(environments, loaded, strict=True):
            secret_values: set[str] = set()
            if config.application.env_file.is_file():
                env_text = config.application.env_file.read_text(encoding="utf-8")
                secret_values = secrets_from_env(env_text)
            if bootstrap_password is not None:
                secret_values.add(bootstrap_password)
            redactor = Redactor(secret_values | {str(config.server.ssh_key)})
            runner = AnsibleRunner(
                repo, redactor, environment=current_environment, verbose=args.verbose
            )
            if args.command == "status":
                status(config)
                print(f"[OK] https://{config.domain}{config.health_path} is healthy")
            elif args.command in {"stage", "prod"}:
                dns_preflight(config)
                version = _deployment_version(repo, args.version)
                deploy(
                    repo,
                    global_config,
                    config,
                    runner,
                    dry_run=args.dry_run,
                    deployment_version=version,
                    bootstrap_password=bootstrap_password,
                )
                print(f"[OK] {current_environment} deployment {version} completed")
            elif args.command == "rollback":
                rollback(repo, global_config, config, runner)
                print("[OK] prod rollback completed and verified")
            else:
                try:
                    update_server(repo, global_config, config, runner, dry_run=args.dry_run)
                    print(f"[OK] {current_environment} server state updated")
                except RunnerError as exc:
                    if environment != "all":
                        raise
                    update_failures.append((current_environment, exc))
                    print(f"[ERROR] {current_environment} update failed: {exc}", file=sys.stderr)
        if update_failures:
            failed_names = ", ".join(name for name, _ in update_failures)
            raise RunnerError(
                f"server update all failed for: {failed_names}",
                update_failures[0][1].exit_code,
            )
        return 0
    except ConfigurationError as exc:
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
