import argparse
import sys
from pathlib import Path

from .config import (
    ConfigurationError,
    load_configuration,
    validate_compose,
    validate_local_inputs,
)
from .redaction import Redactor, secrets_from_env
from .runner import AnsibleRunner, RunnerError
from .workflow import deploy, dns_preflight, status, update_server


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="deploy")
    parser.add_argument("--repo", type=Path, default=Path.cwd(), help=argparse.SUPPRESS)
    parser.add_argument("--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)
    stage = sub.add_parser("stage", help="Provision and deploy Stage")
    stage.add_argument("--dry-run", action="store_true")
    status_parser = sub.add_parser("status", help="Check public environment health")
    status_parser.add_argument("environment", choices=["stage"])
    server = sub.add_parser("server")
    server_sub = server.add_subparsers(dest="server_command", required=True)
    update = server_sub.add_parser("update", help="Reapply managed server state")
    update.add_argument("environment", choices=["stage"])
    update.add_argument("--dry-run", action="store_true")
    return parser


def run(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    repo = args.repo.resolve()
    environment = getattr(args, "environment", "stage")
    try:
        global_config, config = load_configuration(repo, environment)
        if args.command == "status":
            validate_local_inputs(
                config,
                require_ssh=False,
                require_public_key=False,
                require_application=False,
            )
        elif args.command == "server":
            validate_local_inputs(config, require_public_key=False, require_application=False)
        else:
            validate_local_inputs(config)
            validate_compose(config)
        secret_values: set[str] = set()
        if config.application.env_file.is_file():
            env_text = config.application.env_file.read_text(encoding="utf-8")
            secret_values = secrets_from_env(env_text)
        redactor = Redactor(secret_values | {str(config.server.ssh_key)})
        runner = AnsibleRunner(repo, redactor, verbose=args.verbose)
        if args.command == "status":
            status(config)
            print(f"[OK] https://{config.domain}{config.health_path} is healthy")
        elif args.command == "stage":
            dns_preflight(config)
            deploy(repo, global_config, config, runner, dry_run=args.dry_run)
            print("[OK] Stage deployment completed")
        else:
            update_server(repo, global_config, config, runner, dry_run=args.dry_run)
            print("[OK] Stage server state updated")
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
