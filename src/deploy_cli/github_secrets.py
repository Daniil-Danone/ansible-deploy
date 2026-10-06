"""Upload the configured deployment inputs through GitHub CLI's stdin contract."""

import base64
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from .config import (
    ConfigurationError,
    load_configuration,
    read_external_secret_bytes,
    validate_external_input_for_use,
)
from .models import EnvironmentConfig, MonitoringConfig

GITHUB_ENVIRONMENTS = {"stage": "stage", "prod": "production", "monitoring": "monitoring"}
GITHUB_HOST = "github.com"
SECRET_NAME = "ANSIBLE_DEPLOY_SECRET_STORE_JSON"  # noqa: S105 - secret name, not its value
SECRET_SIZE_LIMIT = 49152


def validate_repository(value: str) -> str:
    """Require an explicit GitHub owner/repository, never a URL or inferred remote."""
    if not re.fullmatch(
        r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?/"
        r"[A-Za-z0-9_.-]{1,100}",
        value,
    ) or value.rsplit("/", 1)[-1] in {".", ".."}:
        raise ConfigurationError("GitHub repository must use OWNER/REPO format")
    return value


def _payload(config: EnvironmentConfig | MonitoringConfig) -> tuple[bytes, int]:
    context = config.external_secret_context
    if context is None:
        raise ConfigurationError(f"Secret store context is unavailable for {config.environment}")
    root = context[1]
    files = [
        (config.server.ssh_key, "SSH private key", True),
        (config.server.public_key, "SSH public key", False),
    ]
    if isinstance(config, MonitoringConfig):
        files.append((config.monitoring.secrets_file, "monitoring secrets", True))
    else:
        files.append((config.application.env_file, "application environment", True))
        files.extend(
            (item.source, "additional application environment", True)
            for item in config.application.extra_env_files
        )
        if config.application.registry_auth_file is not None:
            files.append((config.application.registry_auth_file, "registry authentication", True))
        if config.collector is not None:
            files.append((config.collector.password_file, "collector password", True))

    encoded: dict[str, str] = {}
    for path, field, secret in files:
        # Revalidate even repeated refs: a file used as both public and secret input
        # must satisfy the stronger permission boundary too.
        if secret:
            content = read_external_secret_bytes(config, path, field=field)
        else:
            validate_external_input_for_use(config, path, field=field, secret=False)
            try:
                content = path.read_bytes()
            except OSError:
                raise ConfigurationError(
                    f"Unable to read {field} for {config.environment}"
                ) from None
        if not content:
            raise ConfigurationError(f"Required {field} is empty for {config.environment}")
        try:
            relative = path.relative_to(root).as_posix()
        except ValueError:
            raise ConfigurationError(
                f"Invalid secret store reference for {config.environment}"
            ) from None
        encoded[relative] = base64.b64encode(content).decode("ascii")

    payload = json.dumps(encoded, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(payload) > SECRET_SIZE_LIMIT:
        raise ConfigurationError(f"GitHub secret exceeds the 48 KiB limit for {config.environment}")
    return payload, len(encoded)


def _gh(
    executable: str, arguments: list[str], *, failure: str, payload: bytes | None = None
) -> None:
    try:
        result = subprocess.run(  # noqa: S603 - located gh executable, fixed subcommands
            [executable, *arguments],
            input=payload,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env={**os.environ, "GH_HOST": GITHUB_HOST},
            check=False,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        # gh can echo credentials or submitted data; never propagate its diagnostics.
        raise ConfigurationError(failure) from None
    if result.returncode != 0:
        raise ConfigurationError(failure)


def upload_github_secrets(
    project_dir: Path,
    *,
    repository: str,
    environments: list[str] | None = None,
    check: bool = False,
) -> int:
    repository = validate_repository(repository)
    github_repository = f"{GITHUB_HOST}/{repository}"
    selected = list(dict.fromkeys(environments or GITHUB_ENVIRONMENTS))
    if any(environment not in GITHUB_ENVIRONMENTS for environment in selected):
        raise ConfigurationError("GitHub upload supports stage, prod and monitoring")
    executable = shutil.which("gh")
    if executable is None:
        raise ConfigurationError(
            "GitHub CLI (gh) is required; install it and run `gh auth login --hostname github.com`"
        )

    # Validate every local input before the first remote write, avoiding a partial
    # upload when another selected environment has missing files or an oversized JSON.
    prepared: list[tuple[str, bytes, int]] = []
    for environment in selected:
        _, config = load_configuration(project_dir, environment)
        payload, count = _payload(config)
        prepared.append((GITHUB_ENVIRONMENTS[environment], payload, count))

    _gh(
        executable,
        ["auth", "status", "--active", "--hostname", GITHUB_HOST],
        failure="GitHub authentication failed; run `gh auth login --hostname github.com`",
    )
    _gh(
        executable,
        ["repo", "view", github_repository],
        failure=f"Unable to access GitHub repository {repository}",
    )
    for environment, payload, count in prepared:
        if not check:
            _gh(
                executable,
                ["secret", "set", SECRET_NAME, "--repo", github_repository, "--env", environment],
                payload=payload,
                failure=(
                    f"GitHub secret upload failed for {environment}; verify the Environment "
                    "exists and your account can manage its secrets"
                ),
            )
        action = "checked" if check else "uploaded"
        print(f"[OK] {environment}: {count} files {action}")
    return 0
