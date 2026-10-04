import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from deploy_cli import secret_store
from deploy_cli.cli import run
from deploy_cli.secret_store import (
    SecretStoreError,
    create_project_id,
    external_secret_root,
    load_project_id,
)

PROJECT_ID = "12345678-1234-4abc-8def-1234567890ab"


def _project(parent: Path, name: str = "project") -> Path:
    project = parent / name
    (project / ".deploy").mkdir(parents=True)
    (project / ".deploy/project-id").write_text(f"{PROJECT_ID}\n", encoding="utf-8")
    return project


def test_t1_committed_project_id_keeps_namespace_after_move_and_clone(tmp_path: Path) -> None:
    original = _project(tmp_path, "original")
    moved = tmp_path / "moved project"
    clone = tmp_path / "clone project"
    original.rename(moved)
    shutil.copytree(moved, clone)
    data_home = tmp_path / "user data"

    moved_root = external_secret_root(
        moved, environ={"XDG_DATA_HOME": str(data_home)}, platform="linux"
    )
    clone_root = external_secret_root(
        clone, environ={"XDG_DATA_HOME": str(data_home)}, platform="linux"
    )

    expected = (data_home / f"ansible-deploy/projects/{PROJECT_ID}").resolve()
    assert moved_root == expected
    assert clone_root == expected


def test_t2_new_project_id_changes_namespace_without_copying_secrets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = _project(tmp_path)
    secret_base = tmp_path / "external store"
    monkeypatch.setenv("HOME", str(secret_base))
    monkeypatch.setenv("XDG_DATA_HOME", str(secret_base))
    monkeypatch.setenv("LOCALAPPDATA", str(secret_base))
    namespace_base = (
        secret_base / "Library/Application Support"
        if sys.platform == "darwin"
        else secret_base
    )
    old_root = external_secret_root(project)
    expected_old_root = namespace_base / f"ansible-deploy/projects/{PROJECT_ID}"
    assert old_root == expected_old_root.resolve()
    old_root.mkdir(parents=True)
    (old_root / "sentinel.txt").write_text("do not copy\n", encoding="utf-8")

    assert run(
        [
            "--project-dir",
            str(project),
            "secrets",
            "path",
            "--new-project-id",
        ]
    ) == 0
    output = capsys.readouterr()
    new_id = load_project_id(project)
    new_root = namespace_base / f"ansible-deploy/projects/{new_id}"

    assert str(new_id) != PROJECT_ID
    assert output.out == f"{new_root.resolve()}\n"
    assert not (new_root / "sentinel.txt").exists()
    assert (old_root / "sentinel.txt").read_text(encoding="utf-8") == "do not copy\n"


@pytest.mark.parametrize(
    ("content", "message"),
    [
        (None, "missing"),
        ("not-a-uuid\n", "canonical UUID"),
        ("12345678-1234-4ABC-8def-1234567890ab\n", "canonical UUID"),
        (f" {PROJECT_ID}\n", "canonical UUID"),
        (f"{PROJECT_ID}{'0' * 64}\n", "canonical UUID"),
    ],
)
def test_t3_missing_or_invalid_project_id_is_rejected(
    tmp_path: Path, content: str | None, message: str
) -> None:
    project = tmp_path / "project"
    (project / ".deploy").mkdir(parents=True)
    if content is not None:
        (project / ".deploy/project-id").write_text(content, encoding="utf-8")

    with pytest.raises(SecretStoreError, match=message):
        load_project_id(project)


def test_t3_symlinked_project_id_is_rejected(tmp_path: Path) -> None:
    project = tmp_path / "project"
    (project / ".deploy").mkdir(parents=True)
    target = tmp_path / "outside-id"
    target.write_text(f"{PROJECT_ID}\n", encoding="utf-8")
    try:
        (project / ".deploy/project-id").symlink_to(target)
    except OSError:
        pytest.skip("file symlinks are unavailable")

    with pytest.raises(SecretStoreError, match="symlink or reparse"):
        load_project_id(project)


def test_create_rejects_symlinked_project_id(tmp_path: Path) -> None:
    project = tmp_path / "project"
    (project / ".deploy").mkdir(parents=True)
    target = tmp_path / "outside-id"
    target.write_text(f"{PROJECT_ID}\n", encoding="utf-8")
    try:
        (project / ".deploy/project-id").symlink_to(target)
    except OSError:
        pytest.skip("file symlinks are unavailable")

    with pytest.raises(SecretStoreError):
        create_project_id(project)
    assert target.read_text(encoding="utf-8").strip() == PROJECT_ID


@pytest.mark.skipif(sys.platform != "win32", reason="Windows junction regression")
def test_t3_junctioned_project_identity_directory_is_rejected(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    target = tmp_path / "outside metadata"
    target.mkdir()
    (target / "project-id").write_text(f"{PROJECT_ID}\n", encoding="utf-8")
    result = subprocess.run(  # noqa: S603 - fixed Windows junction command
        ["cmd", "/c", "mklink", "/J", str(project / ".deploy"), str(target)],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.skip("Windows junction creation is unavailable")

    with pytest.raises(SecretStoreError, match="symlink or reparse"):
        load_project_id(project)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows junction regression")
def test_create_rejects_junction_at_project_id_path(tmp_path: Path) -> None:
    project = tmp_path / "project"
    (project / ".deploy").mkdir(parents=True)
    target = tmp_path / "outside identity directory"
    target.mkdir()
    result = subprocess.run(  # noqa: S603 - fixed Windows junction command
        ["cmd", "/c", "mklink", "/J", str(project / ".deploy/project-id"), str(target)],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.skip("Windows junction creation is unavailable")

    with pytest.raises(SecretStoreError):
        create_project_id(project)
    assert list(target.iterdir()) == []


def _replace_deploy_with_outside(project: Path, outside: Path, held: Path) -> bool:
    try:
        (project / ".deploy").rename(held)
    except OSError:
        return False
    if sys.platform == "win32":
        result = subprocess.run(  # noqa: S603 - fixed Windows junction command
            ["cmd", "/c", "mklink", "/J", str(project / ".deploy"), str(outside)],  # noqa: S607
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0
    else:
        (project / ".deploy").symlink_to(outside, target_is_directory=True)
    return True


def test_load_uses_held_directory_when_deploy_is_swapped_after_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project(tmp_path)
    outside = tmp_path / "outside metadata"
    outside.mkdir()
    outside_id = "87654321-4321-4abc-8def-ba0987654321"
    (outside / "project-id").write_bytes(f"{outside_id}\n".encode())
    held = project / ".deploy-held"
    swapped: list[bool] = []

    def attack(path: Path) -> None:
        assert path == project / ".deploy"
        swapped.append(_replace_deploy_with_outside(project, outside, held))

    monkeypatch.setattr(secret_store, "_after_directory_open", attack)

    assert str(load_project_id(project)) == PROJECT_ID
    assert swapped == ([False] if sys.platform == "win32" else [True])
    assert (outside / "project-id").read_text(encoding="utf-8").strip() == outside_id


def test_create_uses_held_directory_when_deploy_is_swapped_after_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project(tmp_path)
    outside = tmp_path / "outside metadata"
    outside.mkdir()
    outside_id = "87654321-4321-4abc-8def-ba0987654321"
    (outside / "project-id").write_bytes(f"{outside_id}\n".encode())
    held = project / ".deploy-held"
    swapped: list[bool] = []

    def attack(path: Path) -> None:
        assert path == project / ".deploy"
        swapped.append(_replace_deploy_with_outside(project, outside, held))

    monkeypatch.setattr(secret_store, "_after_directory_open", attack)

    new_id = create_project_id(project)
    actual_directory = project / ".deploy" if sys.platform == "win32" else held
    written = (actual_directory / "project-id").read_text(encoding="utf-8").strip()
    assert written == str(new_id)
    assert swapped == ([False] if sys.platform == "win32" else [True])
    assert (outside / "project-id").read_text(encoding="utf-8").strip() == outside_id


@pytest.mark.parametrize(
    ("platform", "environment", "relative"),
    [
        ("win32", {"LOCALAPPDATA": "{base}"}, "ansible-deploy/projects"),
        ("linux", {"XDG_DATA_HOME": "{base}"}, "ansible-deploy/projects"),
        ("darwin", {}, "Library/Application Support/ansible-deploy/projects"),
    ],
)
def test_t4_platform_roots_preserve_unicode_and_spaces(
    tmp_path: Path, platform: str, environment: dict[str, str], relative: str
) -> None:
    project = _project(tmp_path)
    base = tmp_path / "Данные с пробелами"
    values = {key: value.format(base=base) for key, value in environment.items()}
    home = base if platform == "darwin" else tmp_path / "unused home"

    actual = external_secret_root(project, environ=values, platform=platform, home=home)

    assert actual == (base / relative / PROJECT_ID).resolve()


def test_t4_override_is_exact_root_and_cli_prints_only_that_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _project(tmp_path)
    override = (tmp_path / "CI секреты с пробелами").resolve()
    monkeypatch.setenv("ANSIBLE_DEPLOY_SECRETS_DIR", str(override))

    assert run(["--project-dir", str(project), "secrets", "path"]) == 0

    captured = capsys.readouterr()
    assert captured.out == f"{override}\n"
    assert captured.err == ""
    assert PROJECT_ID not in captured.out
    assert "secret" not in captured.out.lower()


def test_project_id_is_uuid_v4_after_explicit_rotation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _project(tmp_path)
    monkeypatch.setenv("ANSIBLE_DEPLOY_SECRETS_DIR", str(tmp_path / "external"))

    assert run(["--project-dir", str(project), "secrets", "path", "--new-project-id"]) == 0

    assert load_project_id(project).version == 4
    assert uuid.UUID((project / ".deploy/project-id").read_text(encoding="utf-8").strip())
