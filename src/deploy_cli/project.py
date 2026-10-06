from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from importlib import metadata
from importlib.resources import files
from pathlib import Path
from typing import Any

import yaml

from .secret_store import create_project_id, load_project_id

SCAFFOLD_VERSION = 3
STATE_PATH = Path(".deploy/template-state.yml")
CD_PATH = Path(".deploy/cd.yml")
WORKFLOW_PATH = Path(".github/workflows/deploy.yml")


class ProjectError(RuntimeError):
    """A project scaffold cannot be synchronized safely."""


@dataclass(frozen=True)
class ProjectSyncResult:
    created: tuple[Path, ...]
    updated: tuple[Path, ...]
    unchanged: tuple[Path, ...]
    conflicts: tuple[tuple[Path, Path], ...]
    check: bool

    @property
    def changes_required(self) -> bool:
        return bool(self.created or self.updated or self.conflicts)


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _template_files() -> dict[Path, bytes]:
    root = files("deploy_cli").joinpath("templates", "project")
    result: dict[Path, bytes] = {}

    def collect(current: Any, relative: Path) -> None:
        for child in current.iterdir():
            if child.name == "__pycache__" or child.name.endswith((".pyc", ".pyo")):
                continue
            child_relative = relative / child.name
            if child.is_dir():
                collect(child, child_relative)
            elif child.is_file():
                result[child_relative] = child.read_bytes()

    collect(root, Path())
    if not result:
        raise ProjectError("Packaged project scaffold is empty")
    return result


def _validate_branch(value: Any, field: str) -> str:
    # Literal Git branch names only: GitHub push filters must not become globs.
    if (
        not isinstance(value, str)
        or not value
        or value == "@"
        or value.startswith(("-", "/"))
        or value.endswith(("/", "."))
        or any(part.startswith(".") or part.endswith(".lock") for part in value.split("/"))
        or any(token in value for token in ("..", "//", "@{"))
        or re.search(r"[\x00-\x20\x7f~^:?*+!\[\]\\]", value)
    ):
        raise ProjectError(
            f"CD {field} must be a valid Git branch name without GitHub filter metacharacters"
        )
    return value


def _cd_templates(
    project_dir: Path,
    templates: dict[Path, bytes],
    *,
    stage_branch: str | None,
    production_branch: str | None,
    tool_sha: str | None,
) -> dict[Path, bytes]:
    path = project_dir / CD_PATH
    settings: Any = yaml.safe_load(templates[CD_PATH])
    if path.exists():
        if path.is_symlink() or not path.is_file():
            raise ProjectError(f"Refusing unsafe CD configuration: {CD_PATH.as_posix()}")
        try:
            settings = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, yaml.YAMLError) as exc:
            raise ProjectError("Unable to read .deploy/cd.yml") from exc
    if not isinstance(settings, dict) or settings.get("schema_version") != 1:
        raise ProjectError("Unsupported .deploy/cd.yml schema")
    for name, override in (
        ("stage_branch", stage_branch),
        ("production_branch", production_branch),
        ("tool_sha", tool_sha),
    ):
        if override is not None:
            if path.exists() and settings.get(name) != override:
                raise ProjectError(f"Edit {name} in .deploy/cd.yml and run project sync")
            settings[name] = override
    stage = _validate_branch(settings.get("stage_branch"), "stage_branch")
    production = _validate_branch(settings.get("production_branch"), "production_branch")
    sha = settings.get("tool_sha")
    if not isinstance(sha, str) or (
        sha != "TOOL_FULL_SHA" and re.fullmatch(r"[0-9a-f]{40}", sha) is None
    ):
        raise ProjectError("CD tool_sha must be a full lowercase Git SHA")
    if tool_sha == "TOOL_FULL_SHA":
        raise ProjectError("--tool-sha must be a full lowercase Git SHA")
    result = dict(templates)
    if path.exists():
        # CD settings are project-owned; only the rendered workflow is upgraded.
        result[CD_PATH] = path.read_bytes()
    else:
        result[CD_PATH] = yaml.safe_dump(settings, sort_keys=False).encode()
    workflow = result[WORKFLOW_PATH].decode("utf-8")
    replacements = {
        "__STAGE_BRANCH_YAML__": json.dumps(stage),
        "__STAGE_REF_EXPRESSION__": "'refs/heads/" + stage.replace("'", "''") + "'",
        "__PRODUCTION_REF_EXPRESSION__": "'refs/heads/" + production.replace("'", "''") + "'",
        "__TOOL_SHA__": sha,
    }
    for placeholder, value in replacements.items():
        workflow = workflow.replace(placeholder, value)
    result[WORKFLOW_PATH] = workflow.encode()
    return result


def _load_state(project_dir: Path) -> tuple[dict[str, str], int]:
    path = project_dir / STATE_PATH
    if not path.exists():
        return {}, 0
    if path.is_symlink() or not path.is_file():
        raise ProjectError(f"Refusing unsafe template state path: {STATE_PATH.as_posix()}")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ProjectError("Unable to read .deploy/template-state.yml") from exc
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise ProjectError("Unsupported .deploy/template-state.yml schema")
    version = raw.get("template_version")
    hashes = raw.get("files")
    if not isinstance(version, int) or not isinstance(hashes, dict):
        raise ProjectError("Invalid .deploy/template-state.yml")
    if any(not isinstance(key, str) or not isinstance(value, str) for key, value in hashes.items()):
        raise ProjectError("Invalid file hashes in .deploy/template-state.yml")
    return dict(hashes), version


def _state_content(hashes: dict[str, str]) -> bytes:
    try:
        cli_version = metadata.version("ansible-deploy")
    except metadata.PackageNotFoundError:
        cli_version = "source"
    body = {
        "schema_version": 1,
        "template_version": SCAFFOLD_VERSION,
        "cli_version": cli_version,
        "files": dict(sorted(hashes.items())),
    }
    return yaml.safe_dump(body, sort_keys=False, allow_unicode=True).encode()


def _write_new(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError as exc:
        raise ProjectError(f"File appeared during project sync: {path}") from exc
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(content)


def _replace(path: Path, content: bytes) -> None:
    temporary = path.with_name(f".{path.name}.deploy-tmp")
    if temporary.exists():
        raise ProjectError(f"Temporary project sync file already exists: {temporary}")
    _write_new(temporary, content)
    try:
        temporary.replace(path)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise


def sync_project(
    project_dir: Path,
    *,
    check: bool = False,
    stage_branch: str | None = None,
    production_branch: str | None = None,
    tool_sha: str | None = None,
) -> ProjectSyncResult:
    project_dir = project_dir.resolve()
    if not project_dir.is_dir():
        raise ProjectError(f"Project directory does not exist: {project_dir}")
    templates = _cd_templates(
        project_dir,
        _template_files(),
        stage_branch=stage_branch,
        production_branch=production_branch,
        tool_sha=tool_sha,
    )
    identity = project_dir / ".deploy/project-id"
    identity_missing = not identity.exists()
    if identity.exists():
        load_project_id(project_dir)
    elif not check:
        identity.parent.mkdir(parents=True, exist_ok=True)
        create_project_id(project_dir)
    previous_hashes, previous_version = _load_state(project_dir)
    if previous_version > SCAFFOLD_VERSION:
        raise ProjectError("Project scaffold is newer than this CLI; upgrade ansible-deploy")

    created: list[Path] = [Path(".deploy/project-id")] if identity_missing else []
    updated: list[Path] = []
    unchanged: list[Path] = []
    conflicts: list[tuple[Path, Path]] = []
    next_hashes = dict(previous_hashes)

    for relative, desired in sorted(templates.items(), key=lambda item: item[0].as_posix()):
        target = project_dir / relative
        desired_hash = _digest(desired)
        if not target.exists():
            created.append(relative)
            if not check:
                _write_new(target, desired)
                next_hashes[relative.as_posix()] = desired_hash
            continue
        if target.is_symlink() or not target.is_file():
            raise ProjectError(f"Refusing unsafe managed path: {relative.as_posix()}")
        actual = target.read_bytes()
        actual_hash = _digest(actual)
        previous_hash = previous_hashes.get(relative.as_posix())
        candidate = target.with_name(f"{target.name}.deploy-new")
        if actual_hash == desired_hash:
            unchanged.append(relative)
            next_hashes[relative.as_posix()] = desired_hash
        elif previous_hash == desired_hash and not candidate.exists():
            # User customization against the current template is normal. A future
            # template hash change will still produce a three-way conflict candidate.
            unchanged.append(relative)
        elif previous_hash is not None and actual_hash == previous_hash:
            updated.append(relative)
            if not check:
                _replace(target, desired)
                next_hashes[relative.as_posix()] = desired_hash
        else:
            conflicts.append((relative, candidate.relative_to(project_dir)))
            if candidate.exists():
                if candidate.is_symlink() or not candidate.is_file():
                    raise ProjectError(
                        f"Refusing unsafe conflict candidate: "
                        f"{candidate.relative_to(project_dir).as_posix()}"
                    )
                if not check and candidate.read_bytes() != desired:
                    _replace(candidate, desired)
            elif not check:
                _write_new(candidate, desired)
            next_hashes[relative.as_posix()] = desired_hash

    if not check:
        state = project_dir / STATE_PATH
        state.parent.mkdir(parents=True, exist_ok=True)
        content = _state_content(next_hashes)
        if state.exists():
            _replace(state, content)
        else:
            _write_new(state, content)

    return ProjectSyncResult(
        created=tuple(created),
        updated=tuple(updated),
        unchanged=tuple(unchanged),
        conflicts=tuple(conflicts),
        check=check,
    )
