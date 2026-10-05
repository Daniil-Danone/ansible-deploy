import base64
import json
import os
import shutil
import subprocess
import threading
import time
import traceback
from pathlib import Path

import pytest
import yaml

from deploy_cli import cli, images
from deploy_cli.config import ConfigurationError
from deploy_cli.images import PublishedImage
from deploy_cli.runner import RunnerError
from deploy_cli.secret_file import secure_secret_permissions

DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64


@pytest.fixture(autouse=True)
def _external_secret_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    trusted_base = tmp_path / "trusted external base"
    trusted_base.mkdir()
    secure_secret_permissions(trusted_base)
    monkeypatch.setenv(
        "ANSIBLE_DEPLOY_SECRETS_DIR", str(trusted_base / "project secrets")
    )


def _demo(tmp_path: Path) -> Path:
    source = Path(__file__).parents[1] / "examples/demo-app"
    destination = tmp_path / "demo project"
    shutil.copytree(source, destination)
    for environment in ("stage", "prod"):
        config_path = destination / f".deploy/environments/{environment}/config.yml"
        config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        config["application"].pop("registry_auth_file", None)
        config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    return destination


def _configure_registry_auth(project: Path, environment: str = "stage") -> Path:
    config_path = project / f".deploy/environments/{environment}/config.yml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    relative = f"environments/{environment}/registry-auth.json"
    config["application"]["registry_auth_file"] = relative
    config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    _, deployment = images.load_configuration(project, environment)
    path = deployment.application.registry_auth_file
    assert path is not None
    context = deployment.external_secret_context
    assert context is not None
    context_project, root, trusted_base, validate_base = context
    images.ensure_external_parent_for_write(
        context_project,
        root,
        path,
        trusted_base=trusted_base,
        validate_trusted_base=validate_base,
    )
    return path


def _write_portable_auth(path: Path, username: str, credential: str) -> bytes:
    content = images._portable_registry_auth(username, credential, "ghcr.io")
    path.write_bytes(content)
    secure_secret_permissions(path)
    return content


def test_images_publish_command_parsing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = _demo(tmp_path)
    calls: list[tuple[Path, str, str, str, str, bool]] = []

    def publish(project_dir, environment, **kwargs):
        calls.append(
            (
                project_dir,
                environment,
                kwargs["registry"],
                kwargs["namespace"],
                kwargs["pull_username"],
                kwargs["ask_pull_token"],
            )
        )
        return [
            PublishedImage(
                service="backend",
                tagged_reference="ghcr.io/acme/backend:abcdef0",
                immutable_reference=f"ghcr.io/acme/backend@{DIGEST_A}",
            )
        ]

    monkeypatch.setattr(cli, "publish_images", publish)

    assert (
        cli.run(
            [
                "--project-dir",
                str(project),
                "images",
                "publish",
                "stage",
                "--registry",
                "ghcr",
                "--namespace",
                "Acme",
                "--pull-username",
                "reader",
                "--ask-pull-token",
                "--tag",
                "abcdef0",
            ]
        )
        == 0
    )
    assert calls == [(project.resolve(), "stage", "ghcr", "Acme", "reader", True)]


@pytest.mark.parametrize(
    ("registry", "expected"),
    [
        ("ghcr", "ghcr.io/acme/demo-backend"),
        ("dockerhub", "docker.io/acme/demo-backend"),
    ],
)
def test_registry_repository_names(registry: str, expected: str) -> None:
    assert images.registry_repository(registry, "Acme", "demo-backend") == expected


def test_docker_operational_error_suppresses_sensitive_exception_chain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = "DO-NOT-DISCLOSE-external-secret-path"
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError(marker)),
    )

    with pytest.raises(RunnerError) as raised:
        images._docker(["version"], tmp_path, images.Redactor([]))

    assert marker not in "".join(traceback.format_exception(raised.value))
    assert raised.value.__cause__ is None
    assert raised.value.__suppress_context__


def test_failed_docker_command_reports_redacted_diagnostics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token = "ghp-super-secret-token"  # noqa: S105 - synthetic regression value
    stderr = f"denied: permission_denied for {token}\nwrite:packages scope required"
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args=["docker"], returncode=1, stdout="", stderr=stderr
        ),
    )

    with pytest.raises(RunnerError) as raised:
        images._docker(
            ["login", "ghcr.io", "--username", "owner"],
            tmp_path,
            images.Redactor([token]),
        )

    message = str(raised.value)
    assert token not in message
    assert "[REDACTED]" in message
    assert "write:packages scope required" in message
    assert "docker login ghcr.io --username" in message
    assert raised.value.exit_code == 5


@pytest.mark.parametrize(
    ("registry", "environment", "expected_host", "token"),
    [
        (
            "ghcr",
            {"GHCR_TOKEN": "gh-secret", "GHCR_USERNAME": "octocat"},
            "ghcr.io",
            "gh-secret",
        ),
        (
            "dockerhub",
            {"DOCKERHUB_TOKEN": "hub-secret", "DOCKERHUB_USERNAME": "docker-user"},
            "docker.io",
            "hub-secret",
        ),
    ],
)
def test_publish_uses_login_stdin_and_updates_only_after_verified_digests(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    registry: str,
    environment: dict[str, str],
    expected_host: str,
    token: str,
) -> None:
    project = _demo(tmp_path)
    commands: list[tuple[list[str], str | None]] = []
    redactors: set[int] = set()
    def docker(arguments, project_dir, redactor, *, stdin_text=None):
        assert project_dir == project.resolve()
        assert isinstance(redactor, images.Redactor)
        redactors.add(id(redactor))
        commands.append((arguments, stdin_text))
        digest = DIGEST_A if "backend" in " ".join(arguments) else DIGEST_B
        if arguments[0] == "push":
            return subprocess.CompletedProcess(arguments, 0, f"digest: {digest}\n", "")
        if arguments[:3] == ["buildx", "imagetools", "inspect"]:
            return subprocess.CompletedProcess(arguments, 0, f'"{digest}"\n', "")
        return subprocess.CompletedProcess(arguments, 0, "", "")

    monkeypatch.setattr(images, "_docker", docker)
    result = images.publish_images(
        project,
        "stage",
        registry=registry,  # type: ignore[arg-type]
        namespace="Acme",
        username=None,
        ask_token=False,
        tag="abcdef0",
        environ=environment,
    )

    assert len(result) == 2
    login_args, login_stdin = commands[0]
    assert login_args[:2] == ["login", expected_host]
    assert login_args[-1] == "--password-stdin"
    assert login_stdin == token + "\n"
    assert all(token not in argument for command, _ in commands for argument in command)
    assert len(redactors) == 1
    assert [command[0][0] for command in commands].count("build") == 2
    assert [command[0][0] for command in commands].count("push") == 2
    compose = yaml.safe_load((project / "deploy/compose.stage.yml").read_text(encoding="utf-8"))
    expected_prefix = "ghcr.io/acme" if registry == "ghcr" else "docker.io/acme"
    assert compose["services"]["backend"]["image"] == f"{expected_prefix}/demo-backend@{DIGEST_A}"
    assert compose["services"]["frontend"]["image"] == f"{expected_prefix}/demo-frontend@{DIGEST_B}"


def test_existing_docker_login_needs_no_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _demo(tmp_path)
    commands: list[list[str]] = []

    def docker(arguments, project_dir, redactor, *, stdin_text=None):
        del project_dir, redactor, stdin_text
        commands.append(arguments)
        if arguments[0] == "push":
            return subprocess.CompletedProcess(arguments, 0, f"digest: {DIGEST_A}\n", "")
        if arguments[:3] == ["buildx", "imagetools", "inspect"]:
            return subprocess.CompletedProcess(arguments, 0, f'"{DIGEST_A}"\n', "")
        return subprocess.CompletedProcess(arguments, 0, "", "")

    monkeypatch.setattr(images, "_docker", docker)
    images.publish_images(
        project,
        "stage",
        registry="ghcr",
        namespace="acme",
        username=None,
        ask_token=False,
        tag="abcdef0",
        environ={},
    )

    assert not any(command[0] == "login" for command in commands)


def test_t15_default_demo_v2_publish_writes_external_auth_atomically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _demo(tmp_path)
    auth_path = _configure_registry_auth(project)
    write_token = "write-secret"  # noqa: S105 - synthetic regression-test value
    pull_token = "pull-secret"  # noqa: S105 - synthetic regression-test value

    def docker(arguments, project_dir, redactor, *, stdin_text=None):
        del project_dir, redactor, stdin_text
        if arguments[0] == "push":
            return subprocess.CompletedProcess(arguments, 0, f"digest: {DIGEST_A}\n", "")
        if arguments[:3] == ["buildx", "imagetools", "inspect"]:
            return subprocess.CompletedProcess(arguments, 0, f'"{DIGEST_A}"', "")
        return subprocess.CompletedProcess(arguments, 0, "", "")

    monkeypatch.setattr(images, "_docker", docker)
    images.publish_images(
        project,
        "stage",
        registry="ghcr",
        namespace="acme",
        username=None,
        ask_token=False,
        tag="abcdef0",
        environ={
            "GHCR_USERNAME": "octocat",
            "GHCR_TOKEN": write_token,
            "GHCR_PULL_USERNAME": "puller",
            "GHCR_PULL_TOKEN": pull_token,
        },
    )

    document = json.loads(auth_path.read_text(encoding="utf-8"))
    assert set(document) == {"auths"}
    assert set(document["auths"]) == {"ghcr.io"}
    encoded = document["auths"]["ghcr.io"]["auth"]
    assert base64.b64decode(encoded).decode() == f"puller:{pull_token}"
    assert write_token.encode() not in auth_path.read_bytes()
    assert base64.b64encode(f"octocat:{write_token}".encode()) not in auth_path.read_bytes()
    if os.name != "nt":
        assert auth_path.stat().st_mode & 0o777 == 0o600


def test_invalid_existing_server_auth_is_preserved_without_docker_actions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _demo(tmp_path)
    auth_path = _configure_registry_auth(project)
    original = b'{"auths":{"ghcr.io":{"auth":"preserve-me"}}}\n'
    auth_path.write_bytes(original)
    secure_secret_permissions(auth_path)
    called = False

    def docker(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("Docker must not run without portable credentials")

    monkeypatch.setattr(images, "_docker", docker)
    with pytest.raises(ConfigurationError, match="invalid inline"):
        images.publish_images(
            project,
            "stage",
            registry="ghcr",
            namespace="acme",
            username=None,
            ask_token=False,
            tag="abcdef0",
            environ={},
        )

    assert called is False
    assert auth_path.read_bytes() == original


def test_missing_pull_token_fails_before_docker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _demo(tmp_path)
    _configure_registry_auth(project)
    called = False

    def docker(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("Docker must not run before pull credential preflight")

    monkeypatch.setattr(images, "_docker", docker)
    with pytest.raises(ConfigurationError, match="separate --ask-pull-token"):
        images.publish_images(
            project,
            "stage",
            registry="ghcr",
            namespace="acme",
            username=None,
            ask_token=False,
            tag="abcdef0",
            environ={},
        )

    assert called is False


def test_publish_token_cannot_be_reused_as_server_pull_token(tmp_path: Path) -> None:
    project = _demo(tmp_path)
    _configure_registry_auth(project)

    with pytest.raises(ConfigurationError, match="must be different"):
        images.publish_images(
            project,
            "stage",
            registry="ghcr",
            namespace="acme",
            username=None,
            ask_token=False,
            tag="abcdef0",
            environ={
                "GHCR_USERNAME": "octocat",
                "GHCR_TOKEN": "same-token",
                "GHCR_PULL_TOKEN": "same-token",
            },
        )


def test_t15_compose_failure_rolls_back_new_external_registry_auth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _demo(tmp_path)
    auth_path = _configure_registry_auth(project)

    def docker(arguments, project_dir, redactor, *, stdin_text=None):
        del project_dir, redactor, stdin_text
        if arguments[0] == "push":
            return subprocess.CompletedProcess(arguments, 0, f"digest: {DIGEST_A}\n", "")
        if arguments[:3] == ["buildx", "imagetools", "inspect"]:
            return subprocess.CompletedProcess(arguments, 0, f'"{DIGEST_A}"', "")
        return subprocess.CompletedProcess(arguments, 0, "", "")

    monkeypatch.setattr(images, "_docker", docker)
    monkeypatch.setattr(
        images,
        "_atomic_compose_update",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            ConfigurationError("synthetic Compose failure")
        ),
    )
    with pytest.raises(ConfigurationError, match="synthetic Compose"):
        images.publish_images(
            project,
            "stage",
            registry="ghcr",
            namespace="acme",
            username=None,
            ask_token=False,
            tag="abcdef0",
            environ={
                "GHCR_USERNAME": "octocat",
                "GHCR_TOKEN": "write-secret",
                "GHCR_PULL_USERNAME": "reader",
                "GHCR_PULL_TOKEN": "pull-secret",
            },
        )

    assert not auth_path.exists()
    assert list(auth_path.parent.glob(".registry-auth.json.candidate.*")) == []


def test_t15_compose_failure_preserves_existing_external_auth_byte_for_byte(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _demo(tmp_path)
    auth_path = _configure_registry_auth(project)
    original = _write_portable_auth(auth_path, "reader", "existing-pull")  # noqa: S106

    def docker(arguments, project_dir, redactor, *, stdin_text=None):
        del project_dir, redactor, stdin_text
        if arguments[0] == "push":
            return subprocess.CompletedProcess(arguments, 0, f"digest: {DIGEST_A}\n", "")
        if arguments[:3] == ["buildx", "imagetools", "inspect"]:
            return subprocess.CompletedProcess(arguments, 0, f'"{DIGEST_A}"', "")
        return subprocess.CompletedProcess(arguments, 0, "", "")

    monkeypatch.setattr(images, "_docker", docker)
    monkeypatch.setattr(
        images,
        "_atomic_compose_update",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            ConfigurationError("synthetic Compose failure")
        ),
    )
    with pytest.raises(ConfigurationError, match="synthetic Compose"):
        images.publish_images(
            project,
            "stage",
            registry="ghcr",
            namespace="acme",
            username=None,
            ask_token=False,
            tag="abcdef0",
            environ={},
        )

    assert auth_path.read_bytes() == original


def test_external_existing_auth_edit_fails_cas_without_overwrite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _demo(tmp_path)
    auth_path = _configure_registry_auth(project)
    _write_portable_auth(auth_path, "reader", "existing")
    compose = project / "deploy/compose.stage.yml"
    compose_original = compose.read_bytes()
    external = b'{"auths":{"ghcr.io":{"auth":"external-edit"}}}\n'
    inspections = 0

    def docker(arguments, project_dir, redactor, *, stdin_text=None):
        nonlocal inspections
        del project_dir, redactor, stdin_text
        if arguments[0] == "push":
            return subprocess.CompletedProcess(arguments, 0, f"digest: {DIGEST_A}\n", "")
        if arguments[:3] == ["buildx", "imagetools", "inspect"]:
            inspections += 1
            if inspections == 2:
                auth_path.write_bytes(external)
            return subprocess.CompletedProcess(arguments, 0, f'"{DIGEST_A}"', "")
        return subprocess.CompletedProcess(arguments, 0, "", "")

    monkeypatch.setattr(images, "_docker", docker)
    with pytest.raises(ConfigurationError, match="changed during image publishing"):
        images.publish_images(
            project,
            "stage",
            registry="ghcr",
            namespace="acme",
            username=None,
            ask_token=False,
            tag="abcdef0",
            environ={},
        )

    assert auth_path.read_bytes() == external
    assert compose.read_bytes() == compose_original


def test_registry_auth_creation_race_never_overwrites_winner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "registry-auth.json"
    winner = b"unrelated winner"
    real_link = images.os.link

    def racing_link(source, destination, *, follow_symlinks):
        Path(destination).write_bytes(winner)
        raise FileExistsError

    monkeypatch.setattr(images.os, "link", racing_link)
    with pytest.raises(ConfigurationError, match="never overwritten"):
        images._create_registry_auth(path, b"candidate")
    monkeypatch.setattr(images.os, "link", real_link)

    assert path.read_bytes() == winner


def test_registry_auth_rollback_unlink_failure_is_reported_and_file_remains(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "registry-auth.json"
    content = images._portable_registry_auth("reader", "credential", "ghcr.io")
    original_unlink = Path.unlink

    def failing_unlink(target: Path, *args, **kwargs):
        if target == path:
            raise OSError("DO-NOT-DISCLOSE-unlink-path")
        return original_unlink(target, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", failing_unlink)
    with pytest.raises(ConfigurationError, match="rollback failed") as raised:
        with images._new_registry_auth_transaction(path, content):
            raise ConfigurationError("synthetic transaction failure")

    assert raised.value.__cause__ is None
    assert "DO-NOT-DISCLOSE" not in "".join(traceback.format_exception(raised.value))
    assert path.read_bytes() == content


def test_registry_auth_create_suppresses_sensitive_operational_chain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = "DO-NOT-DISCLOSE-registry-auth-path"
    monkeypatch.setattr(
        images,
        "_durable_temporary",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError(marker)),
    )

    with pytest.raises(ConfigurationError) as raised:
        images._create_registry_auth(tmp_path / "registry-auth.json", b"secret")

    assert raised.value.__cause__ is None
    assert marker not in "".join(traceback.format_exception(raised.value))


def test_external_registry_auth_target_is_independent_from_worktree_gitignore(
    tmp_path: Path,
) -> None:
    project = _demo(tmp_path)
    auth_path = _configure_registry_auth(project)
    (project / ".gitignore").write_text(".deploy-state/\n", encoding="utf-8")
    _, deployment = images.load_configuration(project, "stage")

    images._assert_registry_auth_target(project, deployment, auth_path)

    assert not auth_path.is_relative_to(project)
    assert not auth_path.exists()


def test_external_registry_auth_cannot_be_added_to_project_index(tmp_path: Path) -> None:
    project = _demo(tmp_path)
    auth_path = _configure_registry_auth(project)
    original = _write_portable_auth(auth_path, "reader", "existing")
    git = shutil.which("git")
    assert git is not None
    subprocess.run(  # noqa: S603, S607 - fixed test Git command
        [git, "init"], cwd=project, check=True, capture_output=True
    )
    tracked = subprocess.run(  # noqa: S603, S607 - fixed test Git command
        [git, "ls-files", "--error-unmatch", "--", str(auth_path)],
        cwd=project,
        check=False,
        capture_output=True,
    )

    assert tracked.returncode != 0
    assert auth_path.read_bytes() == original


def test_registry_auth_symlink_is_rejected(tmp_path: Path) -> None:
    project = _demo(tmp_path)
    auth_path = _configure_registry_auth(project)
    target = auth_path.parent / "actual-auth.json"
    _write_portable_auth(target, "reader", "existing")
    try:
        auth_path.symlink_to(target)
    except OSError:
        pytest.skip("Symbolic links are unavailable")

    with pytest.raises(ConfigurationError, match="Invalid schema v2"):
        images.publish_images(
            project,
            "stage",
            registry="ghcr",
            namespace="acme",
            username=None,
            ask_token=False,
            tag="abcdef0",
            environ={},
        )


def test_shared_auth_lock_preserves_success_after_parallel_failed_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _demo(tmp_path)
    shared = "environments/shared-registry-auth.json"
    for environment in ("stage", "prod"):
        config_path = project / f".deploy/environments/{environment}/config.yml"
        config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        config["application"]["registry_auth_file"] = shared
        config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    stage_compose = project / "deploy/compose.stage.yml"
    prod_compose = project / "deploy/compose.prod.yml"
    stage_original = stage_compose.read_bytes()
    stage_entered = threading.Event()
    release_stage = threading.Event()
    real_update = images._atomic_compose_update

    def docker(arguments, project_dir, redactor, *, stdin_text=None):
        del project_dir, redactor, stdin_text
        if arguments[0] == "push":
            return subprocess.CompletedProcess(arguments, 0, f"digest: {DIGEST_A}\n", "")
        if arguments[:3] == ["buildx", "imagetools", "inspect"]:
            return subprocess.CompletedProcess(arguments, 0, f'"{DIGEST_A}"', "")
        return subprocess.CompletedProcess(arguments, 0, "", "")

    def update(path, plan, references):
        if path == stage_compose:
            stage_entered.set()
            assert release_stage.wait(timeout=10)
            raise ConfigurationError("stage commit failed")
        return real_update(path, plan, references)

    monkeypatch.setattr(images, "_docker", docker)
    monkeypatch.setattr(images, "_atomic_compose_update", update)
    failures: list[Exception] = []

    def publish(environment: str) -> None:
        try:
            images.publish_images(
                project,
                environment,  # type: ignore[arg-type]
                registry="ghcr",
                namespace="acme",
                username=None,
                ask_token=False,
                tag="abcdef0",
                environ={
                    "GHCR_PULL_USERNAME": "reader",
                    "GHCR_PULL_TOKEN": "pull-secret",
                },
            )
        except Exception as exc:  # noqa: BLE001 - thread evidence is asserted below
            failures.append(exc)

    stage_thread = threading.Thread(target=publish, args=("stage",))
    prod_thread = threading.Thread(target=publish, args=("prod",))
    stage_thread.start()
    assert stage_entered.wait(timeout=10)
    prod_thread.start()
    time.sleep(0.1)
    release_stage.set()
    stage_thread.join(timeout=15)
    prod_thread.join(timeout=15)

    _, deployment = images.load_configuration(project, "prod")
    auth_path = deployment.application.registry_auth_file
    assert auth_path is not None
    assert len(failures) == 1
    assert "stage commit failed" in str(failures[0])
    assert stage_compose.read_bytes() == stage_original
    assert auth_path.is_file()
    assert b"pull-secret" not in auth_path.read_bytes()
    assert f"@{DIGEST_A}" in prod_compose.read_text(encoding="utf-8")


def test_ask_token_uses_secure_prompt_and_redacts_login_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _demo(tmp_path)
    token = "prompted-secret"  # noqa: S105 - synthetic regression-test value
    monkeypatch.setattr(images.getpass, "getpass", lambda prompt: token)

    calls: list[tuple[list[str], str | None]] = []
    real_run = subprocess.run

    def run(arguments, **kwargs):
        if "input" not in kwargs:
            return real_run(arguments, **kwargs)
        calls.append((arguments, kwargs["input"]))
        return subprocess.CompletedProcess(arguments, 1, "", f"login rejected {token}")

    monkeypatch.setattr(subprocess, "run", run)

    with pytest.raises(RunnerError) as raised:
        images.publish_images(
            project,
            "stage",
            registry="ghcr",
            namespace="acme",
            username="octocat",
            ask_token=True,
            tag="abcdef0",
            environ={},
        )

    assert token not in str(raised.value)
    assert "[REDACTED]" in str(raised.value)
    assert calls[0][1] == token + "\n"
    assert token not in calls[0][0]


def test_interactive_publish_prompts_for_write_then_separate_pull_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _demo(tmp_path)
    auth_path = _configure_registry_auth(project)
    write_token = "interactive-write"  # noqa: S105 - synthetic regression value
    pull_token = "interactive-pull"  # noqa: S105 - synthetic regression value
    prompts: list[str] = []
    supplied = iter([write_token, pull_token])

    def prompt(label: str) -> str:
        prompts.append(label)
        return next(supplied)

    def docker(arguments, project_dir, redactor, *, stdin_text=None):
        del project_dir, redactor, stdin_text
        if arguments[0] == "push":
            return subprocess.CompletedProcess(arguments, 0, f"digest: {DIGEST_A}\n", "")
        if arguments[:3] == ["buildx", "imagetools", "inspect"]:
            return subprocess.CompletedProcess(arguments, 0, f'"{DIGEST_A}"', "")
        return subprocess.CompletedProcess(arguments, 0, "", "")

    monkeypatch.setattr(images.getpass, "getpass", prompt)
    monkeypatch.setattr(images, "_docker", docker)
    images.publish_images(
        project,
        "stage",
        registry="ghcr",
        namespace="acme",
        username="octocat",
        ask_token=True,
        ask_pull_token=True,
        tag="abcdef0",
        environ={},
    )

    assert prompts == ["ghcr publish token: ", "ghcr server pull token: "]
    content = auth_path.read_bytes()
    assert base64.b64encode(f"octocat:{pull_token}".encode()) in content
    assert write_token.encode() not in content
    assert base64.b64encode(f"octocat:{write_token}".encode()) not in content


def test_publish_failure_does_not_partially_rewrite_compose(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _demo(tmp_path)
    compose = project / "deploy/compose.stage.yml"
    original = compose.read_bytes()
    pushes = 0

    def docker(arguments, project_dir, redactor, *, stdin_text=None):
        nonlocal pushes
        del project_dir, redactor, stdin_text
        if arguments[0] == "push":
            pushes += 1
            if pushes == 2:
                raise RunnerError("second push failed", 5)
        if arguments[:3] == ["buildx", "imagetools", "inspect"]:
            return subprocess.CompletedProcess(arguments, 0, f'"{DIGEST_A}"\n', "")
        if arguments[0] == "push":
            return subprocess.CompletedProcess(arguments, 0, f"digest: {DIGEST_A}\n", "")
        return subprocess.CompletedProcess(arguments, 0, "", "")

    monkeypatch.setattr(images, "_docker", docker)

    with pytest.raises(RunnerError, match="second push failed"):
        images.publish_images(
            project,
            "stage",
            registry="ghcr",
            namespace="acme",
            username=None,
            ask_token=False,
            tag="abcdef0",
            environ={},
        )

    assert compose.read_bytes() == original


def test_atomic_compose_update_restores_backup_after_post_replace_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _demo(tmp_path)
    compose = project / "deploy/compose.stage.yml"
    original = compose.read_bytes()
    plan = images._compose_plan(compose, {"backend"})
    real_plan = images._compose_plan
    calls = 0

    def fail_after_replace(path, services):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise ConfigurationError("post-replace validation failed")
        return real_plan(path, services)

    monkeypatch.setattr(images, "_compose_plan", fail_after_replace)

    with pytest.raises(ConfigurationError, match="post-replace"):
        images._atomic_compose_update(
            compose, plan, {"backend": f"ghcr.io/acme/backend@{DIGEST_A}"}
        )

    assert compose.read_bytes() == original
    assert list(compose.parent.glob(f".{compose.name}.*")) == []


def test_atomic_update_rechecks_cas_after_candidate_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _demo(tmp_path)
    compose = project / "deploy/compose.stage.yml"
    plan = images._compose_plan(compose, {"backend"})
    real_temporary = images._durable_temporary
    calls = 0
    external = b""

    def edit_during_candidate(path, *, prefix, content, mode):
        nonlocal calls, external
        temporary = real_temporary(path, prefix=prefix, content=content, mode=mode)
        calls += 1
        if calls == 1:
            external = compose.read_bytes() + b"\n# user edit during candidate creation\n"
            compose.write_bytes(external)
        return temporary

    monkeypatch.setattr(images, "_durable_temporary", edit_during_candidate)

    with pytest.raises(ConfigurationError, match="changed during image publishing"):
        images._atomic_compose_update(
            compose, plan, {"backend": f"ghcr.io/acme/backend@{DIGEST_A}"}
        )

    assert compose.read_bytes() == external
    assert b"# user edit during candidate creation" in compose.read_bytes()
    assert list(compose.parent.glob(f".{compose.name}.*")) == []


def test_invalid_registry_digest_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        images,
        "_docker",
        lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, '"sha256:short"', ""),
    )

    with pytest.raises(RunnerError, match="invalid image digest"):
        images._verify_immutable_digest(
            f"ghcr.io/acme/app@{DIGEST_A}", DIGEST_A, tmp_path, images.Redactor([])
        )


def test_build_context_escape_is_rejected(tmp_path: Path) -> None:
    project = _demo(tmp_path)
    config_path = project / ".deploy/images.yml"
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    raw["services"]["backend"]["context"] = "../outside"
    config_path.write_text(yaml.safe_dump(raw), encoding="utf-8")

    with pytest.raises(ConfigurationError, match="escapes the project"):
        images.load_images_configuration(project, "stage")


def test_duplicate_registry_image_is_rejected(tmp_path: Path) -> None:
    project = _demo(tmp_path)
    config_path = project / ".deploy/images.yml"
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    raw["services"]["frontend"]["image"] = raw["services"]["backend"]["image"]
    config_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")

    with pytest.raises(ConfigurationError, match="share registry image 'demo-backend'"):
        images.load_images_configuration(project, "stage")


def test_surgical_update_preserves_comments_and_yaml_scalars(tmp_path: Path) -> None:
    project = _demo(tmp_path)
    compose = project / "deploy/compose.stage.yml"
    original = compose.read_text(encoding="utf-8")
    original = "# deployment comment\nx-enabled: on\n" + original.replace(
        "    image: ghcr.io/OWNER/demo-backend", "    image: ghcr.io/OWNER/demo-backend"
        "  # keep backend comment"
    )
    compose.write_text(original, encoding="utf-8", newline="")
    plan = images._compose_plan(compose, {"backend", "frontend"})
    updated = images._surgical_compose_update(
        plan,
        {
            "backend": f"ghcr.io/acme/backend@{DIGEST_A}",
            "frontend": f"ghcr.io/acme/frontend@{DIGEST_B}",
        },
    ).decode()

    assert updated.startswith("# deployment comment\nx-enabled: on\n")
    assert "# keep backend comment" in updated
    original_lines = original.splitlines()
    updated_lines = updated.splitlines()
    changed = [
        index for index, pair in enumerate(zip(original_lines, updated_lines, strict=True))
        if pair[0] != pair[1]
    ]
    assert changed == sorted(plan.image_lines.values())


def test_compose_alias_is_rejected_before_any_docker_action(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _demo(tmp_path)
    compose = project / "deploy/compose.stage.yml"
    text = compose.read_text(encoding="utf-8")
    compose.write_text(
        "x-defaults: &defaults\n  restart: unless-stopped\n"
        + text.replace("    restart: unless-stopped", "    <<: *defaults", 1),
        encoding="utf-8",
    )
    called = False

    def docker(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("Docker must not run before Compose preflight")

    monkeypatch.setattr(images, "_docker", docker)

    with pytest.raises(ConfigurationError, match="anchors, aliases"):
        images.publish_images(
            project,
            "stage",
            registry="ghcr",
            namespace="acme",
            username=None,
            ask_token=False,
            tag="abcdef0",
            environ={},
        )
    assert called is False


def test_push_digest_is_bound_to_immutable_inspection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _demo(tmp_path)
    inspected: list[str] = []

    def docker(arguments, project_dir, redactor, *, stdin_text=None):
        del project_dir, redactor, stdin_text
        if arguments[0] == "push":
            return subprocess.CompletedProcess(arguments, 0, f"digest: {DIGEST_A}\n", "")
        if arguments[:3] == ["buildx", "imagetools", "inspect"]:
            inspected.append(arguments[3])
            # A mutable-tag lookup could now return B. Immutable A must be requested.
            digest = DIGEST_A if arguments[3].endswith("@" + DIGEST_A) else DIGEST_B
            return subprocess.CompletedProcess(arguments, 0, f'"{digest}"', "")
        return subprocess.CompletedProcess(arguments, 0, "", "")

    monkeypatch.setattr(images, "_docker", docker)
    published = images.publish_images(
        project,
        "stage",
        registry="ghcr",
        namespace="acme",
        username=None,
        ask_token=False,
        tag="abcdef0",
        environ={},
    )

    assert all(reference.endswith("@" + DIGEST_A) for reference in inspected)
    assert all(item.immutable_reference.endswith("@" + DIGEST_A) for item in published)
    assert len({item.tagged_reference.rsplit(":", 1)[1] for item in published}) == 1
    assert published[0].tagged_reference.rsplit(":", 1)[1] != "abcdef0"


@pytest.mark.parametrize(
    ("mode", "arguments"),
    [
        ("output", ["push", "example/image"]),
        ("output", ["buildx", "imagetools", "inspect", "example/image"]),
        ("helper", ["push", "example/image"]),
    ],
)
def test_docker_errors_redact_registry_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    arguments: list[str],
) -> None:
    token = "registry-super-secret"  # noqa: S105 - synthetic regression-test value
    if mode == "output":
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda *args, **kwargs: subprocess.CompletedProcess(args, 1, "", token),
        )
    else:
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda *args, **kwargs: (_ for _ in ()).throw(OSError(token)),
        )

    with pytest.raises(RunnerError) as raised:
        images._docker(arguments, tmp_path, images.Redactor([token]))

    assert token not in str(raised.value)
    if mode == "output":
        assert "[REDACTED]" in str(raised.value)
    else:
        assert "safely" in str(raised.value)


def test_external_compose_edit_fails_cas_without_overwrite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _demo(tmp_path)
    compose = project / "deploy/compose.stage.yml"
    external = b""
    inspections = 0

    def docker(arguments, project_dir, redactor, *, stdin_text=None):
        nonlocal external, inspections
        del project_dir, redactor, stdin_text
        if arguments[0] == "push":
            return subprocess.CompletedProcess(arguments, 0, f"digest: {DIGEST_A}\n", "")
        if arguments[:3] == ["buildx", "imagetools", "inspect"]:
            inspections += 1
            if inspections == 2:
                external = compose.read_bytes() + b"\n# concurrent editor\n"
                compose.write_bytes(external)
            return subprocess.CompletedProcess(arguments, 0, f'"{DIGEST_A}"', "")
        return subprocess.CompletedProcess(arguments, 0, "", "")

    monkeypatch.setattr(images, "_docker", docker)

    with pytest.raises(ConfigurationError, match="changed during image publishing"):
        images.publish_images(
            project,
            "stage",
            registry="ghcr",
            namespace="acme",
            username=None,
            ask_token=False,
            tag="abcdef0",
            environ={},
        )

    assert compose.read_bytes() == external


def test_compose_publishers_serialize_on_lock(tmp_path: Path) -> None:
    project = _demo(tmp_path)
    compose = project / "deploy/compose.stage.yml"
    first_entered = threading.Event()
    release_first = threading.Event()
    second_entered = threading.Event()

    def first() -> None:
        with images._compose_lock(compose):
            first_entered.set()
            assert release_first.wait(timeout=5)

    def second() -> None:
        assert first_entered.wait(timeout=5)
        with images._compose_lock(compose):
            second_entered.set()

    first_thread = threading.Thread(target=first)
    second_thread = threading.Thread(target=second)
    first_thread.start()
    second_thread.start()
    assert first_entered.wait(timeout=5)
    time.sleep(0.1)
    assert not second_entered.is_set()
    release_first.set()
    first_thread.join(timeout=5)
    second_thread.join(timeout=5)
    assert second_entered.is_set()
