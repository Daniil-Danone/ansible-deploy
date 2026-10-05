"""Per-user CLI settings stored outside any project (``config.toml``)."""

import os
import sys
import tempfile
import tomllib
from collections.abc import Mapping
from pathlib import Path

import tomli_w
from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

CONFIG_ENV = "ANSIBLE_DEPLOY_CONFIG"
CONFIG_FILE_NAME = "config.toml"
_APP_DIR = "ansible-deploy"


class UserConfigError(ValueError):
    """Raised when the per-user CLI configuration is missing required context or invalid."""


def absolute_path_setting(value: object, *, name: str) -> Path:
    """Validate a user-supplied directory setting the same way for env and config sources."""
    if not isinstance(value, str | Path):
        raise UserConfigError(f"{name} must be a path string")
    text = str(value)
    if not text:
        raise UserConfigError(f"{name} must not be empty")
    path = Path(text).expanduser()
    if not path.is_absolute():
        raise UserConfigError(f"{name} must be an absolute path")
    # Keep the lexical path: handle-based validation must still observe links at the path itself.
    return Path(os.path.abspath(path))


def secrets_dir_setting(value: object, *, name: str) -> Path:
    path = absolute_path_setting(value, name=name)
    if path.parent == path:
        # The store directory itself must be restricted; a drive/filesystem root cannot be.
        raise UserConfigError(f"{name} must not be a filesystem root")
    return path


class UserConfig(BaseModel):
    """Machine-level settings; every key is optional so a missing file means defaults."""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    secrets_dir: Path | None = None

    @field_validator("secrets_dir", mode="before")
    @classmethod
    def _absolute_secrets_dir(cls, value: object) -> Path | None:
        if value is None:
            return None
        try:
            return secrets_dir_setting(value, name="secrets_dir")
        except UserConfigError as exc:
            raise ValueError(str(exc)) from None


def user_config_path(
    *,
    environ: Mapping[str, str] | None = None,
    platform: str | None = None,
    home: Path | None = None,
) -> Path:
    environment = os.environ if environ is None else environ
    override = environment.get(CONFIG_ENV)
    if override is not None:
        return absolute_path_setting(override, name=CONFIG_ENV)
    current_platform = sys.platform if platform is None else platform
    user_home = Path.home() if home is None else home
    if current_platform == "win32":
        app_data = environment.get("APPDATA")
        base = Path(app_data) if app_data else user_home / "AppData/Roaming"
    elif current_platform == "darwin":
        base = user_home / "Library/Application Support"
    else:
        xdg_config_home = environment.get("XDG_CONFIG_HOME")
        base = Path(xdg_config_home) if xdg_config_home else user_home / ".config"
    if not base.is_absolute():
        raise UserConfigError("User configuration base directory must be an absolute path")
    return Path(os.path.abspath(base / _APP_DIR / CONFIG_FILE_NAME))


def load_user_config(path: Path) -> UserConfig:
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return UserConfig()
    except OSError as exc:
        raise UserConfigError(f"Unable to read user configuration {path}") from exc
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise UserConfigError(f"User configuration {path} is not valid TOML") from exc
    try:
        return UserConfig.model_validate(data)
    except ValidationError as exc:
        details = "; ".join(
            f"{'.'.join(str(part) for part in error['loc']) or 'config'}: {error['msg']}"
            for error in exc.errors()
        )
        raise UserConfigError(f"Invalid user configuration {path}: {details}") from None


def save_user_config(config: UserConfig, path: Path) -> None:
    """Write the whole file atomically so a crash never leaves a truncated config."""
    data = {
        key: str(value) if isinstance(value, Path) else value
        for key, value in config.model_dump(exclude_none=True).items()
    }
    content = tomli_w.dumps(data).encode("utf-8")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
    except OSError as exc:
        raise UserConfigError(f"Unable to write user configuration {path}") from exc
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise UserConfigError(f"Unable to write user configuration {path}") from exc
