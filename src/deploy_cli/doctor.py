"""Workstation readiness checks for ``ansible-deploy doctor``."""

import json
import shutil
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

from rich.console import Console
from rich.table import Table
from rich.text import Text

from .secret_store import (
    EXTERNAL_STORE_ENV,
    ExternalSecretLocation,
    SecretStoreError,
    SecretStoreSettings,
    load_project_id,
    secret_store_settings,
    validate_external_root_ancestry,
)

Status = Literal["OK", "WARN", "FAIL"]
DOCKER_INFO_TIMEOUT = 15
TOOL_TIMEOUT = 10
_STYLES: dict[Status, str] = {"OK": "green", "WARN": "yellow", "FAIL": "bold red"}


@dataclass(frozen=True)
class Check:
    name: str
    status: Status
    detail: str
    hint: str = ""


def _find_tool(name: str) -> str | None:
    """Test seam: tool discovery without patching the process-wide ``shutil.which``."""
    return shutil.which(name)


def _run_tool(argv: Sequence[str], timeout: int) -> subprocess.CompletedProcess[str]:
    """Test seam: run a fixed diagnostic command and capture its output."""
    return subprocess.run(  # noqa: S603 - executable resolved via PATH lookup, fixed arguments
        list(argv), capture_output=True, text=True, timeout=timeout, check=False
    )


def _platform_hint(*, windows: str, macos: str, linux: str) -> str:
    if sys.platform == "win32":
        return windows
    if sys.platform == "darwin":
        return macos
    return linux


def _python_check() -> Check:
    # The package requires Python 3.12+, so reaching this code already proves compatibility.
    version = ".".join(str(part) for part in sys.version_info[:3])
    return Check("Python", "OK", f"{version} ({sys.executable})")


def _first_line(text: str) -> str:
    lines = text.strip().splitlines()
    return lines[0].strip() if lines else ""


def _version_detail(executable: str, argv: Sequence[str]) -> str:
    try:
        result = _run_tool([executable, *argv], TOOL_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return executable
    version = _first_line(result.stdout or result.stderr) if result.returncode == 0 else ""
    return f"{version} ({executable})" if version else executable


def _docker_checks() -> list[Check]:
    install_hint = _platform_hint(
        windows="Install Docker Desktop: https://docs.docker.com/desktop/setup/install/windows-install/",
        macos="Install Docker Desktop: https://docs.docker.com/desktop/setup/install/mac-install/",
        linux="Install Docker Engine: https://docs.docker.com/engine/install/",
    )
    docker = _find_tool("docker")
    if docker is None:
        return [Check("Docker CLI", "FAIL", "docker not found on PATH", install_hint)]
    checks = [Check("Docker CLI", "OK", _version_detail(docker, ["--version"]))]
    start_hint = _platform_hint(
        windows="Start Docker Desktop and wait until the engine is running",
        macos="Start Docker Desktop and wait until the engine is running",
        linux="Start the daemon: sudo systemctl start docker",
    )
    try:
        result = _run_tool([docker, "info", "--format", "{{.ServerVersion}}"], DOCKER_INFO_TIMEOUT)
    except subprocess.TimeoutExpired:
        checks.append(
            Check(
                "Docker daemon",
                "FAIL",
                f"docker info did not answer within {DOCKER_INFO_TIMEOUT}s",
                start_hint,
            )
        )
        return checks
    except (OSError, subprocess.SubprocessError) as exc:
        checks.append(Check("Docker daemon", "FAIL", f"docker info failed: {exc}", start_hint))
        return checks
    if result.returncode == 0:
        version = _first_line(result.stdout)
        checks.append(Check("Docker daemon", "OK", f"server {version}" if version else "reachable"))
        return checks
    error = _first_line(result.stderr) or f"docker info exited with {result.returncode}"
    hint = start_hint
    if sys.platform not in {"win32", "darwin"} and "permission denied" in error.lower():
        hint = "Add your user to the docker group: sudo usermod -aG docker $USER, then log in again"
    checks.append(Check("Docker daemon", "FAIL", error, hint))
    return checks


def _required_tool_checks() -> list[Check]:
    checks: list[Check] = []
    git = _find_tool("git")
    if git is None:
        hint = _platform_hint(
            windows="Install Git for Windows: https://git-scm.com/download/win",
            macos="Install Git: xcode-select --install",
            linux="Install Git: sudo apt install git (or your distribution's package)",
        )
        checks.append(Check("git", "FAIL", "git not found on PATH", hint))
    else:
        checks.append(Check("git", "OK", _version_detail(git, ["--version"])))
    ssh_keygen = _find_tool("ssh-keygen")
    if ssh_keygen is None:
        hint = _platform_hint(
            windows="Enable OpenSSH Client: Settings > System > Optional features",
            macos="Install OpenSSH (ships with macOS; check PATH)",
            linux="Install OpenSSH client: sudo apt install openssh-client",
        )
        checks.append(Check("ssh-keygen", "FAIL", "ssh-keygen not found on PATH", hint))
    else:
        checks.append(Check("ssh-keygen", "OK", ssh_keygen))
    return checks


def _optional_tool_checks() -> list[Check]:
    checks: list[Check] = []
    for name, hint in (
        ("age", "Optional, used for backups: https://github.com/FiloSottile/age#installation"),
        ("rclone", "Optional, used for backups: https://rclone.org/install/"),
    ):
        executable = _find_tool(name)
        if executable is None:
            checks.append(Check(name, "WARN", f"{name} not found on PATH", hint))
        else:
            checks.append(Check(name, "OK", executable))
    return checks


def _permission_hint(path: Path) -> str:
    return _platform_hint(
        windows=(
            f'Keep only your account in the ACL: icacls "{path}" /inheritance:r '
            '/grant:r "%USERNAME%:(OI)(CI)F" (and remove other entries), '
            "or run ansible-deploy setup with a new empty directory"
        ),
        macos=f'Restrict access: chmod 700 "{path}"',
        linux=f'Restrict access: chmod 700 "{path}"',
    )


def describe_source(settings: SecretStoreSettings) -> str:
    if settings.source == "env":
        return f"env {EXTERNAL_STORE_ENV} (exact per-project root)"
    if settings.source == "config":
        return "user config secrets_dir"
    return "OS default"


def _directory_check(
    name: str, location: ExternalSecretLocation, detail: str, missing_hint: str
) -> Check:
    if not location.root.exists():
        return Check(name, "WARN", f"{detail}: not created yet", missing_hint)
    try:
        validate_external_root_ancestry(location, require_root=True)
    except SecretStoreError as exc:
        return Check(name, "FAIL", f"{detail}: {exc}", _permission_hint(location.root))
    return Check(name, "OK", f"{detail}: owner-only")


def _secret_store_checks(project_dir: Path | None) -> list[Check]:
    try:
        settings = secret_store_settings()
    except SecretStoreError as exc:
        return [
            Check(
                "Secrets store",
                "FAIL",
                str(exc),
                "Fix the value, then check with: ansible-deploy config show",
            )
        ]
    checks = [
        _directory_check(
            "Secrets store",
            settings.location,
            f"{settings.path} ({describe_source(settings)})",
            "Run: ansible-deploy setup",
        )
    ]
    if project_dir is None:
        return checks
    try:
        project_id = load_project_id(project_dir)
    except SecretStoreError as exc:
        checks.append(Check("Project id", "FAIL", str(exc), "Run: ansible-deploy project init"))
        return checks
    checks.append(Check("Project id", "OK", str(project_id)))
    if settings.source != "env":
        location = settings.project_location(project_id)
        checks.append(
            _directory_check(
                "Project secrets",
                location,
                str(location.root),
                "Created on first key or registry write; print it: ansible-deploy secrets path",
            )
        )
    return checks


def _gitignore_check(project_dir: Path) -> Check:
    name = "Project .gitignore"
    git = _find_tool("git")
    if git is not None:
        try:
            result = _run_tool(
                [git, "-C", str(project_dir), "check-ignore", "-q", ".deploy-state/"],
                TOOL_TIMEOUT,
            )
        except (OSError, subprocess.SubprocessError):
            result = None
        if result is not None and result.returncode in {0, 1}:
            if result.returncode == 0:
                return Check(name, "OK", ".deploy-state/ is ignored")
            return Check(
                name,
                "WARN",
                ".deploy-state/ is not ignored by Git",
                "Add '.deploy-state/' to .gitignore",
            )
    # Not a Git repository (or Git unavailable): fall back to the project's own .gitignore.
    try:
        lines = (project_dir / ".gitignore").read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        lines = []
    patterns = {".deploy-state", ".deploy-state/", "/.deploy-state", "/.deploy-state/"}
    if any(line.strip() in patterns for line in lines):
        return Check(name, "OK", ".deploy-state/ is listed in .gitignore")
    return Check(
        name,
        "WARN",
        ".deploy-state/ is not listed in .gitignore",
        "Add '.deploy-state/' to .gitignore",
    )


def find_project(project_dir: Path) -> Path | None:
    """Return the project directory when it contains deployment configuration."""
    return project_dir if (project_dir / ".deploy").is_dir() else None


def run_checks(project_dir: Path) -> list[Check]:
    project = find_project(project_dir)
    checks = [
        _python_check(),
        *_docker_checks(),
        *_required_tool_checks(),
        *_secret_store_checks(project),
        *_optional_tool_checks(),
    ]
    if project is not None:
        checks.append(_gitignore_check(project))
    return checks


def exit_code(checks: Sequence[Check]) -> int:
    return 1 if any(check.status == "FAIL" for check in checks) else 0


def render_checks(checks: Sequence[Check], console: Console) -> None:
    table = Table(title="ansible-deploy doctor", show_lines=False)
    table.add_column("Check", no_wrap=True)
    table.add_column("Status", no_wrap=True)
    table.add_column("Details", overflow="fold")
    table.add_column("Hint", overflow="fold")
    for check in checks:
        style = _STYLES[check.status]
        # Text() keeps paths and tool output from being parsed as rich markup.
        table.add_row(
            check.name, Text(check.status, style=style), Text(check.detail), Text(check.hint)
        )
    console.print(table)
    failures = sum(check.status == "FAIL" for check in checks)
    warnings = sum(check.status == "WARN" for check in checks)
    if failures:
        console.print(f"[bold red]{failures} check(s) failed[/], {warnings} warning(s)")
    elif warnings:
        console.print(f"[green]No failures[/], [yellow]{warnings} warning(s)[/]")
    else:
        console.print("[green]All checks passed[/]")


def run_doctor(project_dir: Path, *, as_json: bool = False) -> int:
    checks = run_checks(project_dir)
    if as_json:
        print(json.dumps([asdict(check) for check in checks], indent=2))
    else:
        render_checks(checks, Console(highlight=False))
    return exit_code(checks)
