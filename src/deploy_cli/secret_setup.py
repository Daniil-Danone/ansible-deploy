"""``ansible-deploy secrets`` commands that write or verify store files.

Every command here only ever prints variable names and store-relative file names: an
absolute external path is sensitive metadata, and a secret value never leaves the
store, the hidden prompt or stdin.
"""

import getpass
import hmac
import os
import secrets as secrets_module
import sys
import warnings
from collections.abc import Iterable, Sequence
from io import StringIO
from pathlib import Path

from dotenv import dotenv_values

from .config import ConfigurationError, load_configuration, read_external_secret_text
from .models import EnvironmentConfig, MonitoringConfig
from .redaction import Redactor
from .runner import CRYPT_SALT_PATTERN, AnsibleRunner
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
COLLECTOR_ENVIRONMENTS = ("stage", "prod")
ROTATE_HINT = "deploy secrets rotate-collector-password"
_SHARED_DIRECTORIES = ("keys", "backup")
_PUSH_HASH_VARIABLE = "LOKI_PUSH_PASSWORD_HASH"
_MONITORING_REQUIRED = (
    "GF_SECURITY_ADMIN_USER",
    "GF_SECURITY_ADMIN_PASSWORD",
    "LOKI_PUSH_USERNAME",
    _PUSH_HASH_VARIABLE,
)
_NO_TEMPLATE = "no template; add the service variables"
_GENERATED_PASSWORD_BYTES = 32
_INVALID_PUSH_HASH = f"{_PUSH_HASH_VARIABLE} is not a valid crypt SHA-512 hash"


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


def _validated_password(password: str) -> str:
    if not password:
        raise ConfigurationError("Password cannot be empty")
    if any(character in password for character in ("\0", "\r", "\n")):
        raise ConfigurationError("Password cannot contain NUL, CR or LF")
    return password


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
    password = _validated_password(_prompt_hidden("Password to hash: "))
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


def _password_from_stdin() -> str:
    """Read the new password from stdin, tolerating exactly one trailing line ending."""
    try:
        raw = sys.stdin.read()
    except (OSError, UnicodeError):
        raise ConfigurationError("Unable to read the password from stdin") from None
    for ending in ("\r\n", "\n"):
        if raw.endswith(ending):
            raw = raw[: -len(ending)]
            break
    return _validated_password(raw)


def _collector_targets(environment: str) -> tuple[str, ...]:
    if environment == "all":
        return COLLECTOR_ENVIRONMENTS
    if environment not in COLLECTOR_ENVIRONMENTS:
        raise ConfigurationError(f"Environment is not implemented yet: {environment}")
    return (environment,)


def _external_context(
    config: EnvironmentConfig | MonitoringConfig,
) -> tuple[Path, Path, Path, bool]:
    context = config.external_secret_context
    if context is None:
        raise ConfigurationError(
            f"Schema v2 secret context is unavailable for {config.environment}"
        )
    return context


def _store_relative_name(config: EnvironmentConfig | MonitoringConfig, path: Path) -> str:
    _, root, trusted_base, validate_trusted_base = _external_context(config)
    return _relative_name(ExternalSecretLocation(root, trusted_base, validate_trusted_base), path)


def _replace_external_secret(
    config: EnvironmentConfig | MonitoringConfig, target: Path, content: bytes
) -> None:
    """Overwrite one external secret file atomically, keeping it owner-only.

    Unlike ``_ensure_template`` this replaces an existing file, so the content goes to a
    sibling temporary file first: an interrupted rotation must never leave half a secret.
    """
    project_dir, root, trusted_base, validate_trusted_base = _external_context(config)
    ensure_external_parent_for_write(
        project_dir,
        root,
        target,
        trusted_base=trusted_base,
        validate_trusted_base=validate_trusted_base,
    )
    temporary = target.with_name(f".{target.name}.new")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        secure_secret_permissions(temporary)
        os.replace(temporary, target)
        secure_secret_permissions(target)
    except (OSError, SecretFileError):
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            raise SecretStoreError(
                "Unable to write an external secret file and rollback failed"
            ) from None
        raise SecretStoreError("Unable to write an external secret file") from None


def _collector_configuration(project_dir: Path, environment: str) -> EnvironmentConfig:
    _, config = load_configuration(project_dir, environment)
    if not isinstance(config, EnvironmentConfig) or config.collector is None:
        raise ConfigurationError(f"{environment} collector is not configured")
    return config


def _collector_password(config: EnvironmentConfig) -> str:
    if config.collector is None:
        raise ConfigurationError(f"{config.environment} collector is not configured")
    password = read_external_secret_text(
        config, config.collector.password_file, field="collector password"
    ).rstrip("\r\n")
    if not password or any(character in password for character in ("\0", "\r", "\n")):
        raise ConfigurationError(
            f"Collector password file for {config.environment} must contain one non-empty line"
        )
    return password


def _monitoring_push_hash(config: MonitoringConfig) -> str:
    values = dotenv_values(
        stream=StringIO(
            read_external_secret_text(
                config, config.monitoring.secrets_file, field="monitoring secrets"
            )
        )
    )
    password_hash = values.get(_PUSH_HASH_VARIABLE)
    if not password_hash:
        raise ConfigurationError(
            f"Monitoring secrets file does not define {_PUSH_HASH_VARIABLE}"
        )
    return password_hash


def _crypt_salt(password_hash: str) -> str:
    """Salt of a crypt SHA-512 hash, validated before it can reach the openssl argv."""
    fields = password_hash.split("$")
    if len(fields) < 4 or fields[0] != "" or fields[1] != "6":
        raise ConfigurationError(_INVALID_PUSH_HASH)
    if fields[2].startswith("rounds="):
        if len(fields) < 5:
            raise ConfigurationError(_INVALID_PUSH_HASH)
        salt = f"{fields[2]}${fields[3]}"
    else:
        salt = fields[2]
    if not CRYPT_SALT_PATTERN.fullmatch(salt):
        raise ConfigurationError(_INVALID_PUSH_HASH)
    return salt


def _hash_runner(project_dir: Path, secrets: Iterable[str], *, verbose: bool) -> AnsibleRunner:
    runner = AnsibleRunner(
        project_dir,
        Redactor(secrets),
        environment="monitoring",
        verbose=verbose,
    )
    runner.build_image()
    return runner


def collector_password_mismatches(
    project_dir: Path,
    environments: Sequence[str],
    *,
    verbose: bool = False,
    report: bool = False,
) -> list[str]:
    """Environments whose ``collector.password`` does not match the monitoring push hash.

    The salt is validated before the runtime image is built, so a tampered hash never
    reaches Docker. ``report`` prints one line per environment for the standalone command.
    """
    monitoring = load_configuration(project_dir, "monitoring")[1]
    password_hash = _monitoring_push_hash(monitoring)
    salt = _crypt_salt(password_hash)
    passwords = [
        (name, _collector_password(_collector_configuration(project_dir, name)))
        for name in environments
    ]
    runner = _hash_runner(project_dir, [password for _, password in passwords], verbose=verbose)
    mismatched: list[str] = []
    for name, password in passwords:
        if hmac.compare_digest(
            runner.openssl_password_hash_with_salt(password, salt), password_hash
        ):
            if report:
                print(f"[OK] {name} collector password matches the monitoring hash")
            continue
        mismatched.append(name)
        if report:
            print(
                f"[ERROR] {name} collector password does not match the monitoring hash",
                file=sys.stderr,
            )
    return mismatched


def check_collector_password(
    project_dir: Path, environment: str = "all", *, verbose: bool = False
) -> int:
    """Verify the collector plaintext against the monitoring hash, changing nothing."""
    mismatched = collector_password_mismatches(
        project_dir, _collector_targets(environment), verbose=verbose, report=True
    )
    if mismatched:
        raise ConfigurationError(
            f"Collector password does not match {_PUSH_HASH_VARIABLE} for "
            f"{', '.join(mismatched)}; run: {ROTATE_HINT}"
        )
    return 0


def _replace_push_hash_line(text: str, digest: str) -> str:
    """Swap only the hash value, keeping every other byte and the file's line endings."""
    lines = text.splitlines(keepends=True)
    matches = [
        index
        for index, line in enumerate(lines)
        if line.startswith(f"{_PUSH_HASH_VARIABLE}=")
    ]
    if not matches:
        raise ConfigurationError(
            f"Monitoring secrets file has no {_PUSH_HASH_VARIABLE}= line; "
            "run 'deploy secrets init monitoring' and fill the template"
        )
    if len(matches) > 1:
        raise ConfigurationError(
            f"Monitoring secrets file declares {_PUSH_HASH_VARIABLE} more than once"
        )
    index = matches[0]
    ending = ""
    for candidate in ("\r\n", "\n", "\r"):
        if lines[index].endswith(candidate):
            ending = candidate
            break
    lines[index] = f"{_PUSH_HASH_VARIABLE}={digest}{ending}"
    return "".join(lines)


def rotate_collector_password(
    project_dir: Path,
    environment: str = "all",
    *,
    password_stdin: bool = False,
    verbose: bool = False,
) -> int:
    """Write one new push password as plaintext and as the monitoring crypt SHA-512 hash.

    The two representations drifting apart is silent: nginx answers every push with 401
    and Loki stays empty. Both are therefore written by one command from one value.
    """
    targets = _collector_targets(environment)
    password = (
        _password_from_stdin()
        if password_stdin
        else secrets_module.token_urlsafe(_GENERATED_PASSWORD_BYTES)
    )
    monitoring = load_configuration(project_dir, "monitoring")[1]
    collectors = [
        (config, config.collector.password_file)
        for config in (_collector_configuration(project_dir, name) for name in targets)
        if config.collector is not None
    ]
    runner = _hash_runner(project_dir, [password], verbose=verbose)
    digest = runner.openssl_password_hash(password)
    secrets_file = monitoring.monitoring.secrets_file
    # Rejected input must not leave a rotated plaintext behind, so the replacement text
    # is built (and the hash line validated) before anything is written.
    updated = _replace_push_hash_line(
        read_external_secret_text(monitoring, secrets_file, field="monitoring secrets"), digest
    )
    for config, password_file in collectors:
        _replace_external_secret(config, password_file, password.encode("utf-8"))
        print(f"[UPDATE] {_store_relative_name(config, password_file)}")
    _replace_external_secret(monitoring, secrets_file, updated.encode("utf-8"))
    print(f"[UPDATE] {_store_relative_name(monitoring, secrets_file)}")
    print("[OK] collector push password rotated")
    print(
        "[HINT] apply it: deploy monitoring update && deploy collectors deploy all --yes",
        file=sys.stderr,
    )
    return 0
