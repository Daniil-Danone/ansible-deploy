"""Machine-level commands: ``setup``, ``config`` and ``doctor``."""

import argparse
import platform as platform_module
import sys
from pathlib import Path

import questionary
from rich.console import Console
from rich.markup import escape
from rich.table import Table
from rich.text import Text

from .doctor import describe_source, run_doctor
from .secret_store import (
    EXTERNAL_STORE_ENV,
    SecretStoreError,
    SecretStoreSettings,
    configured_secret_store_settings,
    default_secret_store_settings,
    ensure_secret_store,
    secret_store_settings,
)
from .user_config import (
    UserConfig,
    UserConfigError,
    load_user_config,
    save_user_config,
    secrets_dir_setting,
    user_config_path,
)

COMMANDS = frozenset({"setup", "config", "doctor"})
_NON_INTERACTIVE_FLAGS = "--secrets-dir PATH or --default-secrets-dir"


class SetupError(ValueError):
    """Raised when setup cannot proceed without user input or was cancelled."""


def register(sub: "argparse._SubParsersAction[argparse.ArgumentParser]") -> None:
    setup = sub.add_parser("setup", help="Prepare this machine (secret store, config, checks)")
    location = setup.add_mutually_exclusive_group()
    location.add_argument(
        "--secrets-dir", type=str, help="store project secrets in this absolute directory"
    )
    location.add_argument(
        "--default-secrets-dir",
        action="store_true",
        help="use the OS default secret store location",
    )
    setup.add_argument(
        "--non-interactive",
        "-y",
        action="store_true",
        help="never prompt; requires --secrets-dir or --default-secrets-dir",
    )
    config = sub.add_parser("config", help="Show per-user CLI configuration")
    config_sub = config.add_subparsers(dest="config_command", required=True)
    config_sub.add_parser("show", help="Show effective settings and their sources")
    config_sub.add_parser("path", help="Print the user configuration file path")
    doctor = sub.add_parser("doctor", help="Check workstation readiness")
    doctor.add_argument("--json", action="store_true", help="print checks as JSON")


def run_command(args: argparse.Namespace, project_dir: Path) -> int:
    try:
        if args.command == "doctor":
            return run_doctor(project_dir, as_json=args.json)
        if args.command == "config":
            if args.config_command == "path":
                print(user_config_path())
                return 0
            return _config_show()
        return _setup(
            project_dir,
            secrets_dir=args.secrets_dir,
            use_default=args.default_secrets_dir,
            non_interactive=args.non_interactive,
        )
    except (UserConfigError, SecretStoreError, SetupError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 2


def _console() -> Console:
    return Console(highlight=False)


def _os_name() -> str:
    if sys.platform == "win32":
        return f"Windows {platform_module.release()}"
    if sys.platform == "darwin":
        return f"macOS {platform_module.mac_ver()[0]}".strip()
    return f"Linux {platform_module.release()}"


def _config_show() -> int:
    path = user_config_path()
    config = load_user_config(path)
    settings = secret_store_settings()
    table = Table(title="ansible-deploy config")
    table.add_column("Setting", no_wrap=True)
    table.add_column("Value", overflow="fold")
    table.add_column("Source")
    table.add_column("Notes", overflow="fold")
    rows = (
        ("config file", str(path), "", "exists" if path.is_file() else "not created yet"),
        (
            "secrets_dir",
            str(settings.path),
            describe_source(settings),
            _secrets_note(settings, config),
        ),
    )
    for row in rows:
        # Text() keeps paths with brackets from being parsed as rich markup.
        table.add_row(*(Text(cell) for cell in row))
    _console().print(table)
    return 0


def _secrets_note(settings: SecretStoreSettings, config: UserConfig) -> str:
    if settings.source == "env" and config.secrets_dir is not None:
        return f"overrides config value {config.secrets_dir}"
    if settings.source == "env":
        return ""
    return "one <project-id> directory per project"


def interactive_terminal() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def git_worktree_containing(path: Path) -> Path | None:
    """Best-effort: the nearest ancestor that looks like a Git working tree."""
    for current in (path, *path.parents):
        if (current / ".git").exists():
            return current
    return None


def _ask(question: questionary.Question) -> object:
    answer = question.ask()
    if answer is None:
        raise SetupError("Setup was cancelled")
    return answer


def _validate_prompted_path(value: str) -> bool | str:
    try:
        secrets_dir_setting(value.strip(), name="Path")
    except UserConfigError as exc:
        return str(exc)
    return True


def _prompt_secrets_dir(
    console: Console, current: SecretStoreSettings, default: SecretStoreSettings
) -> Path | None:
    console.print(f"Detected OS: [bold]{escape(_os_name())}[/]")
    console.print(f"User config: {escape(str(user_config_path()))}")
    console.print(
        f"Current secrets store: {escape(str(current.path))} ({describe_source(current)})"
    )
    default_choice = f"Use default location ({default.path})"
    custom_choice = "Custom path"
    choice = _ask(
        questionary.select(
            "Where should project secrets be stored?",
            choices=[default_choice, custom_choice],
            default=custom_choice if current.source == "config" else default_choice,
        )
    )
    if choice == default_choice:
        return None
    while True:
        raw = _ask(
            questionary.path(
                "Absolute directory for project secrets:",
                default=str(current.path) if current.source == "config" else "",
                only_directories=True,
                validate=_validate_prompted_path,
            )
        )
        candidate = secrets_dir_setting(str(raw).strip(), name="Path")
        if not _warn_if_inside_worktree(console, candidate):
            return candidate
        if _ask(questionary.confirm("Use this path anyway?", default=False)):
            return candidate


def _warn_if_inside_worktree(console: Console, path: Path) -> bool:
    worktree = git_worktree_containing(path)
    if worktree is None:
        return False
    console.print(
        f"[yellow]Warning:[/] {escape(str(path))} is inside the Git working tree "
        f"{escape(str(worktree))}; secrets must never be committed."
    )
    return True


def _setup(
    project_dir: Path,
    *,
    secrets_dir: str | None,
    use_default: bool,
    non_interactive: bool,
) -> int:
    console = _console()
    config_path = user_config_path()
    config = load_user_config(config_path)
    default = default_secret_store_settings()
    chosen: Path | None
    if secrets_dir is not None:
        chosen = secrets_dir_setting(secrets_dir, name="--secrets-dir")
        _warn_if_inside_worktree(console, chosen)
    elif use_default:
        chosen = None
    elif non_interactive or not interactive_terminal():
        raise SetupError(
            "setup needs a terminal to ask questions; "
            f"in non-interactive mode pass {_NON_INTERACTIVE_FLAGS}"
        )
    else:
        chosen = _prompt_secrets_dir(console, secret_store_settings(), default)
        target = default.path if chosen is None else chosen
        if not _ask(
            questionary.confirm(
                f"Create {target} (owner-only) and save {config_path}?", default=True
            )
        ):
            raise SetupError("Setup was cancelled")

    settings = default if chosen is None else configured_secret_store_settings(chosen)
    try:
        ensure_secret_store(settings)
    except SecretStoreError as exc:
        raise SetupError(
            f"Secrets store {settings.path} is not usable: {exc}. "
            "Choose a new empty directory or restrict the existing one to your user"
        ) from None
    updated = config.model_copy(update={"secrets_dir": chosen})
    if updated != config or not config_path.exists():
        save_user_config(updated, config_path)
        console.print(f"[green]Saved[/] {escape(str(config_path))}")
    else:
        console.print(f"[green]Unchanged[/] {escape(str(config_path))}")
    console.print(f"[green]Secrets store ready:[/] {escape(str(settings.path))} (owner-only)")
    if secret_store_settings().source == "env":
        console.print(
            f"[yellow]Note:[/] {EXTERNAL_STORE_ENV} is set in this environment and "
            "overrides the configured location."
        )
    console.print()
    return run_doctor(project_dir)
