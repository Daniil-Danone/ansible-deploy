import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest
import yaml

from deploy_cli import cli, images
from deploy_cli.config import ConfigurationError
from deploy_cli.images import PublishedImage
from deploy_cli.runner import RunnerError

DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64


def _demo(tmp_path: Path) -> Path:
    source = Path(__file__).parents[1] / "examples/demo-app"
    destination = tmp_path / "demo project"
    shutil.copytree(source, destination)
    return destination


def test_images_publish_command_parsing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = _demo(tmp_path)
    calls: list[tuple[Path, str, str, str]] = []

    def publish(project_dir, environment, **kwargs):
        calls.append((project_dir, environment, kwargs["registry"], kwargs["namespace"]))
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
                "--tag",
                "abcdef0",
            ]
        )
        == 0
    )
    assert calls == [(project.resolve(), "stage", "ghcr", "Acme")]


@pytest.mark.parametrize(
    ("registry", "expected"),
    [
        ("ghcr", "ghcr.io/acme/demo-backend"),
        ("dockerhub", "docker.io/acme/demo-backend"),
    ],
)
def test_registry_repository_names(registry: str, expected: str) -> None:
    assert images.registry_repository(registry, "Acme", "demo-backend") == expected


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


def test_ask_token_uses_secure_prompt_and_redacts_login_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _demo(tmp_path)
    token = "prompted-secret"  # noqa: S105 - synthetic regression-test value
    monkeypatch.setattr(images.getpass, "getpass", lambda prompt: token)

    calls: list[tuple[list[str], str | None]] = []

    def run(arguments, **kwargs):
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
    assert "[REDACTED]" in str(raised.value)


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
