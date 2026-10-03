import getpass
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import uuid
import warnings
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError
from yaml.nodes import MappingNode, ScalarNode
from yaml.tokens import AliasToken, AnchorToken, ScalarToken

from .config import ConfigurationError, load_configuration
from .redaction import Redactor
from .runner import RunnerError

if sys.platform == "win32":
    import msvcrt

    def _lock_descriptor(descriptor: int) -> None:
        if os.fstat(descriptor).st_size == 0:
            os.write(descriptor, b"0")
        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_LOCK, 1)

    def _unlock_descriptor(descriptor: int) -> None:
        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _lock_descriptor(descriptor: int) -> None:
        fcntl.flock(descriptor, fcntl.LOCK_EX)

    def _unlock_descriptor(descriptor: int) -> None:
        fcntl.flock(descriptor, fcntl.LOCK_UN)


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ImageBuild(_StrictModel):
    context: Path
    dockerfile: Path = Path("Dockerfile")
    image: str


class ImageEnvironment(_StrictModel):
    compose: Path


class ImagesConfig(_StrictModel):
    schema_version: Literal[1]
    services: dict[str, ImageBuild]
    environments: dict[Literal["stage", "prod"], ImageEnvironment]


class PublishedImage(BaseModel):
    service: str
    tagged_reference: str
    immutable_reference: str


@dataclass(frozen=True)
class ComposePlan:
    original: bytes
    signature: tuple[int, int, int, int]
    image_lines: dict[str, int]


def _contained(project: Path, path: Path, *, label: str) -> Path:
    candidate = path if path.is_absolute() else project / path
    canonical = candidate.resolve(strict=False)
    try:
        canonical.relative_to(project)
    except ValueError as exc:
        raise ConfigurationError(f"{label} escapes the project directory") from exc
    return canonical


def load_images_configuration(
    project_dir: Path, environment: Literal["stage", "prod"]
) -> tuple[ImagesConfig, Path]:
    project = project_dir.resolve()
    path = project / ".deploy/images.yml"
    try:
        raw: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
        config = ImagesConfig.model_validate(raw)
    except (OSError, UnicodeError, yaml.YAMLError, ValidationError) as exc:
        raise ConfigurationError(f"Invalid image publishing configuration {path}: {exc}") from exc
    if not config.services:
        raise ConfigurationError("Image publishing configuration has no services")
    if environment not in config.environments:
        raise ConfigurationError(f"Image publishing has no {environment} environment")
    for service, build in config.services.items():
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", service):
            raise ConfigurationError(f"Invalid Compose service name in images.yml: {service!r}")
        if not re.fullmatch(r"[a-z0-9]+(?:[._-][a-z0-9]+)*", build.image):
            raise ConfigurationError(f"Invalid registry image name for service {service!r}")
        context = _contained(project, build.context, label=f"Build context for {service}")
        if not context.is_dir():
            raise ConfigurationError(f"Build context for {service!r} is not a directory: {context}")
        dockerfile = build.dockerfile
        dockerfile = dockerfile if dockerfile.is_absolute() else context / dockerfile
        dockerfile = _contained(project, dockerfile, label=f"Dockerfile for {service}")
        try:
            dockerfile.relative_to(context)
        except ValueError as exc:
            raise ConfigurationError(
                f"Dockerfile for {service!r} must be inside its build context"
            ) from exc
        if not dockerfile.is_file():
            raise ConfigurationError(f"Dockerfile for {service!r} is missing: {dockerfile}")
        build.context = context
        build.dockerfile = dockerfile
    compose = _contained(
        project,
        config.environments[environment].compose,
        label=f"{environment} deployment Compose",
    )
    if not compose.is_file():
        raise ConfigurationError(f"Deployment Compose is missing: {compose}")
    _, deployment = load_configuration(project, environment)
    if compose != deployment.application.compose:
        raise ConfigurationError(
            f"images.yml {environment} Compose must match the deployment environment config"
        )
    config.environments[environment].compose = compose
    return config, compose


def registry_repository(registry: str, namespace: str, image: str) -> str:
    normalized = namespace.strip().lower()
    if not re.fullmatch(r"[a-z0-9]+(?:[._-][a-z0-9]+)*", normalized):
        raise ConfigurationError("Registry namespace must be a Docker-compatible name")
    if registry == "ghcr":
        return f"ghcr.io/{normalized}/{image}"
    if registry == "dockerhub":
        return f"docker.io/{normalized}/{image}"
    raise ConfigurationError(f"Unsupported registry: {registry}")


def _git_tag(project: Path, supplied: str | None) -> str:
    if supplied is not None:
        tag = supplied
    else:
        try:
            result = subprocess.run(  # noqa: S603 - fixed Git command
                ["git", "rev-parse", "HEAD"],  # noqa: S607 - standard Git executable
                cwd=project,
                capture_output=True,
                text=True,
                check=True,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ConfigurationError("Unable to determine image tag; use --tag") from exc
        tag = result.stdout.strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", tag):
        raise ConfigurationError("Image tag is not Docker-compatible")
    return tag


def _prompt_token(registry: str) -> str:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            token = getpass.getpass(f"{registry} access token: ")
    except getpass.GetPassWarning as exc:
        raise ConfigurationError("Secure token input is unavailable") from exc
    return _validate_token(token)


def _validate_token(token: str) -> str:
    if not token:
        raise ConfigurationError("Registry token cannot be empty")
    if any(character in token for character in ("\0", "\r", "\n")):
        raise ConfigurationError("Registry token cannot contain NUL, CR or LF")
    return token


def _credentials(
    registry: str,
    username: str | None,
    *,
    ask_token: bool,
    environ: dict[str, str],
) -> tuple[str, str] | None:
    if registry == "ghcr":
        token = environ.get("GHCR_TOKEN") or environ.get("GITHUB_TOKEN")
        env_username = environ.get("GHCR_USERNAME") or environ.get("GITHUB_ACTOR")
    else:
        token = environ.get("DOCKERHUB_TOKEN")
        env_username = environ.get("DOCKERHUB_USERNAME")
    selected_username = username or env_username
    if ask_token:
        if not selected_username:
            raise ConfigurationError("Registry username is required with --ask-token")
        return selected_username, _prompt_token(registry)
    if token:
        if not selected_username:
            raise ConfigurationError("Registry token was provided without a username")
        return selected_username, _validate_token(token)
    if username:
        raise ConfigurationError("--username requires --ask-token or a registry token env var")
    return None


def _docker(
    arguments: list[str],
    project: Path,
    redactor: Redactor,
    *,
    stdin_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(  # noqa: S603 - fixed Docker executable, validated arguments
            ["docker", *arguments],  # noqa: S607 - standard Docker executable
            cwd=project,
            input=stdin_text,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RunnerError(f"Unable to run Docker: {redactor(str(exc))}", 5) from exc
    if result.returncode != 0:
        detail = redactor((result.stderr or result.stdout).strip())
        raise RunnerError(f"Docker command failed: {detail or 'no diagnostic output'}", 5)
    return result


def _push_digest(result: subprocess.CompletedProcess[str]) -> str:
    values = set(
        re.findall(r"\bdigest:\s*(sha256:[0-9a-f]{64})\b", result.stdout + result.stderr)
    )
    if len(values) != 1:
        raise RunnerError("Docker push did not report exactly one valid image digest", 5)
    return str(values.pop())


def _verify_immutable_digest(
    immutable_reference: str, expected: str, project: Path, redactor: Redactor
) -> None:
    result = _docker(
        [
            "buildx",
            "imagetools",
            "inspect",
            immutable_reference,
            "--format",
            "{{json .Manifest.Digest}}",
        ],
        project,
        redactor,
    )
    value = result.stdout.strip()
    try:
        digest: Any = json.loads(value)
    except json.JSONDecodeError as exc:
        raise RunnerError("Registry returned an invalid image digest", 5) from exc
    if not isinstance(digest, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise RunnerError("Registry returned an invalid image digest", 5)
    if digest != expected:
        raise RunnerError("Registry immutable digest verification did not match docker push", 5)


def _mapping_pairs(node: MappingNode, *, label: str) -> dict[str, tuple[ScalarNode, Any]]:
    pairs: dict[str, tuple[ScalarNode, Any]] = {}
    for key, value in node.value:
        if not isinstance(key, ScalarNode) or not isinstance(key.value, str):
            raise ConfigurationError(f"{label} must use explicit scalar keys")
        if key.value in pairs:
            raise ConfigurationError(f"{label} contains duplicate key {key.value!r}")
        pairs[key.value] = (key, value)
    return pairs


def _compose_plan(path: Path, configured_services: set[str]) -> ComposePlan:
    before = path.stat()
    original = path.read_bytes()
    after = path.stat()
    signature = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    if signature != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
        raise ConfigurationError("Deployment Compose changed while it was being read")
    try:
        text = original.decode("utf-8")
        for token in yaml.scan(text):
            if isinstance(token, (AnchorToken, AliasToken)) or (
                isinstance(token, ScalarToken) and token.value == "<<"
            ):
                raise ConfigurationError(
                    "Deployment Compose anchors, aliases and merge keys are unsupported"
                )
        document = yaml.compose(text)
    except (UnicodeError, yaml.YAMLError) as exc:
        raise ConfigurationError(f"Invalid deployment Compose: {exc}") from exc
    if not isinstance(document, MappingNode):
        raise ConfigurationError("Deployment Compose must contain a services mapping")
    top = _mapping_pairs(document, label="Deployment Compose")
    services_node = top.get("services", (None, None))[1]
    if not isinstance(services_node, MappingNode):
        raise ConfigurationError("Deployment Compose must contain a services mapping")
    services = _mapping_pairs(services_node, label="Compose services")
    image_lines: dict[str, int] = {}
    lines = text.splitlines(keepends=True)
    image_pattern = re.compile(
        r"^([ \t]*image:[ \t]*)(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|[^#\r\n]*?)"
        r"([ \t]*(?:#.*)?)(\r?\n)?$"
    )
    for service in configured_services:
        service_node = services.get(service, (None, None))[1]
        if not isinstance(service_node, MappingNode):
            raise ConfigurationError(f"Compose does not define service {service!r} as a mapping")
        service_fields = _mapping_pairs(service_node, label=f"Compose service {service!r}")
        image_node = service_fields.get("image", (None, None))[1]
        if not isinstance(image_node, ScalarNode):
            raise ConfigurationError(f"Compose service {service!r} needs one explicit image")
        line_number = image_node.start_mark.line
        if line_number >= len(lines) or image_pattern.fullmatch(lines[line_number]) is None:
            raise ConfigurationError(
                f"Compose service {service!r} image must be one explicit block-style line"
            )
        if line_number in image_lines.values():
            raise ConfigurationError("Compose image lines must be unique")
        image_lines[service] = line_number
    return ComposePlan(original=original, signature=signature, image_lines=image_lines)


def _surgical_compose_update(plan: ComposePlan, references: dict[str, str]) -> bytes:
    text = plan.original.decode("utf-8")
    lines = text.splitlines(keepends=True)
    pattern = re.compile(
        r"^([ \t]*image:[ \t]*)(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|[^#\r\n]*?)"
        r"([ \t]*(?:#.*)?)(\r?\n)?$"
    )
    for service, reference in references.items():
        line_number = plan.image_lines[service]
        match = pattern.fullmatch(lines[line_number])
        if match is None:
            raise ConfigurationError("Compose image line changed after preflight")
        lines[line_number] = (
            match.group(1) + reference + match.group(2) + (match.group(3) or "")
        )
    return "".join(lines).encode("utf-8")


def _durable_temporary(path: Path, *, prefix: str, content: bytes, mode: int) -> Path:
    descriptor, name = tempfile.mkstemp(prefix=prefix, dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return temporary


def _signature(path: Path) -> tuple[int, int, int, int]:
    value = path.stat()
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns)


@contextmanager
def _compose_lock(path: Path) -> Iterator[None]:
    lock = path.parent / f".{path.name}.ansible-deploy.lock"
    if lock.is_symlink():
        raise ConfigurationError("Compose publishing lock cannot be a symlink")
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor: int | None = None
    try:
        descriptor = os.open(lock, flags, 0o600)
        opened = os.fstat(descriptor)
        current = os.lstat(lock)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)
        ):
            raise ConfigurationError("Compose publishing lock must be one regular file")
        _lock_descriptor(descriptor)
    except OSError as exc:
        if descriptor is not None:
            os.close(descriptor)
        raise ConfigurationError(f"Unable to lock deployment Compose: {exc}") from exc
    except BaseException:
        if descriptor is not None:
            os.close(descriptor)
        raise
    try:
        yield
    finally:
        try:
            _unlock_descriptor(descriptor)
        finally:
            os.close(descriptor)


def _atomic_compose_update(path: Path, plan: ComposePlan, references: dict[str, str]) -> None:
    original = plan.original
    original_mode = path.stat().st_mode
    updated = _surgical_compose_update(plan, references)
    if _signature(path) != plan.signature or path.read_bytes() != original:
        raise ConfigurationError("Deployment Compose changed during image publishing")
    temporary = _durable_temporary(
        path,
        prefix=f".{path.name}.candidate.",
        content=updated,
        mode=original_mode,
    )
    try:
        backup = _durable_temporary(
            path,
            prefix=f".{path.name}.backup.",
            content=original,
            mode=original_mode,
        )
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    replaced = False
    committed_signature: tuple[int, int, int, int] | None = None
    try:
        # Reparse the exact candidate and prove only planned image lines are addressable.
        candidate_plan = _compose_plan(temporary, set(references))
        if candidate_plan.original != updated:
            raise ConfigurationError("Compose candidate verification failed")
        if _signature(path) != plan.signature or path.read_bytes() != original:
            raise ConfigurationError("Deployment Compose changed during image publishing")
        os.replace(temporary, path)
        replaced = True
        committed_signature = _signature(path)
        committed = _compose_plan(path, set(references))
        if committed.original != updated:
            raise ConfigurationError("Compose post-replace verification failed")
    except BaseException:
        if (
            replaced
            and committed_signature is not None
            and _signature(path) == committed_signature
            and hashlib.sha256(path.read_bytes()).digest() == hashlib.sha256(updated).digest()
        ):
            os.replace(backup, path)
        temporary.unlink(missing_ok=True)
        backup.unlink(missing_ok=True)
        raise
    backup.unlink()


def publish_images(
    project_dir: Path,
    environment: Literal["stage", "prod"],
    *,
    registry: Literal["ghcr", "dockerhub"],
    namespace: str,
    username: str | None,
    ask_token: bool,
    tag: str | None,
    environ: dict[str, str] | None = None,
) -> list[PublishedImage]:
    project = project_dir.resolve()
    config, compose = load_images_configuration(project, environment)
    selected_tag = _git_tag(project, tag)
    credential_environment = dict(os.environ if environ is None else environ)
    registry_host = "ghcr.io" if registry == "ghcr" else "docker.io"
    run_tag = f"{selected_tag[:111]}-{uuid.uuid4().hex[:12]}"
    with _compose_lock(compose):
        # Complete YAML/service preflight happens under the interprocess lock and
        # before login, build, push or registry inspection.
        plan = _compose_plan(compose, set(config.services))
        credentials = _credentials(
            registry, username, ask_token=ask_token, environ=credential_environment
        )
        secrets = {credentials[1]} if credentials is not None else set()
        redactor = Redactor(secrets)
        if credentials is not None:
            login_username, token = credentials
            _docker(
                ["login", registry_host, "--username", login_username, "--password-stdin"],
                project,
                redactor,
                stdin_text=token + "\n",
            )
        published: list[PublishedImage] = []
        immutable: dict[str, str] = {}
        for service, build in config.services.items():
            repository = registry_repository(registry, namespace, build.image)
            reference = f"{repository}:{run_tag}"
            _docker(
                [
                    "build",
                    "--file",
                    str(build.dockerfile),
                    "--tag",
                    reference,
                    str(build.context),
                ],
                project,
                redactor,
            )
            push = _docker(["push", reference], project, redactor)
            digest = _push_digest(push)
            immutable_reference = f"{repository}@{digest}"
            _verify_immutable_digest(immutable_reference, digest, project, redactor)
            immutable[service] = immutable_reference
            published.append(
                PublishedImage(
                    service=service,
                    tagged_reference=reference,
                    immutable_reference=immutable_reference,
                )
            )
        _atomic_compose_update(compose, plan, immutable)
    return published
