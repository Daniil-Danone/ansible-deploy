"""``ansible-deploy secrets init`` and ``secrets hash-password``.

Both commands only ever print variable names and store-relative file names: an
absolute external path is sensitive metadata, and a secret value never leaves the
store or the hidden prompt.
"""

import getpass
import os
import sys
import warnings
from io import StringIO
from pathlib import Path

from dotenv import dotenv_values

from .config import ConfigurationError, load_configuration
from .models import EnvironmentConfig, MonitoringConfig
from .redaction import Redactor
from .runner import AnsibleRunner
from .secret_file import SecretFileError, secure_secret_permissions
from .secret_store import (
    ExternalSecretLocation,
    SecretStoreError,
    ensure_external_parent_for_write,
    ensure_secret_store,
    external_secret_location,
    secret_store_settings,
)

SECRET_ENVIRONMENTS = ("stage", "prod", "monitoring", "restore")
_SHARED_DIRECTORIES = ("keys", "backup")
_MONITORING_REQUIRED = (
    "GF_SECURITY_ADMIN_USER",
    "GF_SECURITY_ADMIN_PASSWORD",
    "LOKI_PUSH_USERNAME",
    "LOKI_PUSH_PASSWORD_HASH",
)
_NO_TEMPLATE = "no template; add the service variables"


def _relative_name(location: ExternalSecretLocation, path: Path) -> str:
    try:
        return path.relative_to(location.root).as_posix()
    except ValueError:
        raise SecretStoreError("External file is outside its configured root") from None


def _ensure_directory(project_dir: Path, location: ExternalSecretLocation, relative: str) -> bool:
    """Create one owner-only store directory; the helper creates a target's parent."""
    directory = location.root.joinpath(*relative.split("/"))
    existed = directory.is_dir()
    ensure_external_parent_for_write(
        project_dir,
        location.root,
        directory / "_",
        trusted_base=location.trusted_base,
        validate_trusted_base=location.validate_trusted_base,
    )
    return existed


def _declared_secret_files(
    config: EnvironmentConfig | MonitoringConfig,
) -> list[Path]:
    """Secret files the configuration names but no ``.env.example`` describes."""
    if not isinstance(config, EnvironmentConfig):
        return []
    declared = [item.source for item in config.application.extra_env_files]
    if config.collector is not None:
        declared.append(config.collector.password_file)
    return declared


def _ensure_template(
    project_dir: Path,
    location: ExternalSecretLocation,
    target: Path,
    content: bytes,
) -> tuple[bool, bytes]:
    """Create the secret file once, never touching an existing one."""
    ensure_external_parent_for_write(
        project_dir,
        location.root,
        target,
        trusted_base=location.trusted_base,
        validate_trusted_base=location.validate_trusted_base,
    )
    if target.exists():
        try:
            return True, target.read_bytes()
        except OSError:
            raise SecretStoreError("Unable to read an existing external secret file") from None
    try:
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        # Another process created the same file after the check; keep its content.
        return True, target.read_bytes()
    except OSError:
        raise SecretStoreError("Unable to create an external secret file") from None
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
        secure_secret_permissions(target)
    except (OSError, SecretFileError):
        raise SecretStoreError("Unable to protect a new external secret file") from None
    return False, content


def _pending_variables(content: bytes, required: tuple[str, ...]) -> list[str]:
    """Names the operator still has to fill: empty in the template or absent from it."""
    try:
        values = dotenv_values(stream=StringIO(content.decode("utf-8")))
    except UnicodeError:
        raise SecretStoreError("External secret template is not valid UTF-8") from None
    empty = [name for name, value in values.items() if not value]
    missing = [name for name in required if name not in values]
    return sorted(set(empty) | set(missing))


def initialize_secret_store(project_dir: Path, environment: str = "all") -> int:
    """Create the external store layout and the secret file templates for an environment."""
    if environment != "all" and environment not in SECRET_ENVIRONMENTS:
        raise ConfigurationError(f"Environment is not implemented yet: {environment}")
    requested = SECRET_ENVIRONMENTS if environment == "all" else (environment,)
    settings = secret_store_settings()
    ensure_secret_store(settings)
    location = external_secret_location(project_dir)
    created: list[str] = []
    existing: list[str] = []
    pending: list[tuple[str, list[str]]] = []
    prepared: list[str] = []
    for name in _SHARED_DIRECTORIES:
        (existing if _ensure_directory(project_dir, location, name) else created).append(
            f"{name}/"
        )
    for name in requested:
        config_path = project_dir / f".deploy/environments/{name}/config.yml"
        if not config_path.is_file():
            if environment != "all":
                raise ConfigurationError(f"Environment configuration is missing: {name}")
            continue
        _, config = load_configuration(project_dir, name)
        prepared.append(name)
        directory = f"environments/{name}"
        (existing if _ensure_directory(project_dir, location, directory) else created).append(
            f"{directory}/"
        )
        # Files the configuration declares but no template describes: per-service
        # env files and the collector password. Created empty and reported, so the
        # deployment does not fail later on a file nobody knew to create.
        for source in _declared_secret_files(config):
            relative = _relative_name(location, source)
            already, _ = _ensure_template(project_dir, location, source, b"")
            (existing if already else created).append(relative)
            if not already:
                pending.append((relative, [_NO_TEMPLATE]))
        example = project_dir / f".deploy/environments/{name}/.env.example"
        if not example.is_file():
            continue
        target: Path
        required: tuple[str, ...]
        if isinstance(config, MonitoringConfig):
            target = config.monitoring.secrets_file
            required = _MONITORING_REQUIRED
        elif isinstance(config, EnvironmentConfig):
            target = config.application.env_file
            required = tuple(config.application.required_env_vars)
        else:  # pragma: no cover - defensive: only two configuration kinds exist
            continue
        relative = _relative_name(location, target)
        already, content = _ensure_template(
            project_dir, location, target, example.read_bytes()
        )
        (existing if already else created).append(relative)
        if not already:
            variables = _pending_variables(content, required)
            if variables:
                pending.append((relative, variables))
    for relative in created:
        print(f"[CREATE] {relative}")
    for relative in existing:
        print(f"[KEEP] {relative}")
    for relative, variables in pending:
        print(f"[FILL] {relative}: " + ", ".join(variables))
    print("[OK] external secret store prepared for " + ", ".join(prepared))
    return 0


def _prompt_hidden(prompt: str) -> str:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            value = getpass.getpass(prompt)
    except getpass.GetPassWarning as exc:
        raise ConfigurationError(
            "Secure password input is unavailable; run from an interactive terminal"
        ) from exc
    return value


def hash_password(project_dir: Path, *, verbose: bool = False) -> int:
    """Hash a password with crypt SHA-512 inside the runtime container."""
    password = _prompt_hidden("Password to hash: ")
    if not password:
        raise ConfigurationError("Password cannot be empty")
    if any(character in password for character in ("\0", "\r", "\n")):
        raise ConfigurationError("Password cannot contain NUL, CR or LF")
    if _prompt_hidden("Repeat password: ") != password:
        raise ConfigurationError("Passwords do not match")
    runner = AnsibleRunner(
        project_dir,
        Redactor([password]),
        environment="monitoring",
        verbose=verbose,
    )
    runner.build_image()
    digest = runner.openssl_password_hash(password)
    print(digest)
    print(
        "[HINT] store this hash as LOKI_PUSH_PASSWORD_HASH in the monitoring secrets file",
        file=sys.stderr,
    )
    return 0
