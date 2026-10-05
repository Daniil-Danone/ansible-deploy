import json
import os
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

import pytest

from deploy_cli import doctor, workstation
from deploy_cli.cli import run
from deploy_cli.secret_store import (
    SecretStoreError,
    external_secret_root,
    secret_store_settings,
)
from deploy_cli.user_config import (
    UserConfig,
    UserConfigError,
    load_user_config,
    save_user_config,
    user_config_path,
)

PROJECT_ID = "1a7cf3a5-2d36-4c7f-9b28-6dd8f5f7a001"


@pytest.fixture(autouse=True)
def _wide_console(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COLUMNS", "250")
    monkeypatch.delenv("ANSIBLE_DEPLOY_SECRETS_DIR", raising=False)


def _config_file() -> Path:
    return Path(os.environ["ANSIBLE_DEPLOY_CONFIG"])


def _project(parent: Path) -> Path:
    project = parent / "project"
    (project / ".deploy").mkdir(parents=True)
    (project / ".deploy/project-id").write_text(f"{PROJECT_ID}\n", encoding="utf-8")
    return project


def _default_store_env(monkeypatch: pytest.MonkeyPatch, base: Path) -> None:
    base.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        # XDG_DATA_HOME's parent is a validated trust anchor; don't depend on the umask.
        base.chmod(0o700)
    monkeypatch.setenv("LOCALAPPDATA", str(base))
    monkeypatch.setenv("XDG_DATA_HOME", str(base / "data"))
    monkeypatch.setenv("HOME", str(base))


# --- user config ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("platform", "environment", "relative"),
    [
        ("win32", {"APPDATA": "{base}"}, "ansible-deploy/config.toml"),
        ("win32", {}, "home/AppData/Roaming/ansible-deploy/config.toml"),
        ("darwin", {}, "home/Library/Application Support/ansible-deploy/config.toml"),
        ("linux", {"XDG_CONFIG_HOME": "{base}"}, "ansible-deploy/config.toml"),
        ("linux", {}, "home/.config/ansible-deploy/config.toml"),
    ],
)
def test_user_config_path_per_platform(
    tmp_path: Path, platform: str, environment: dict[str, str], relative: str
) -> None:
    values = {key: value.format(base=tmp_path) for key, value in environment.items()}

    actual = user_config_path(environ=values, platform=platform, home=tmp_path / "home")

    assert actual == Path(os.path.abspath(tmp_path / relative))


def test_user_config_path_env_override_must_be_absolute(tmp_path: Path) -> None:
    override = tmp_path / "custom.toml"
    assert user_config_path(environ={"ANSIBLE_DEPLOY_CONFIG": str(override)}) == override
    with pytest.raises(UserConfigError, match="absolute"):
        user_config_path(environ={"ANSIBLE_DEPLOY_CONFIG": "relative.toml"})


def test_missing_config_file_means_defaults(tmp_path: Path) -> None:
    assert load_user_config(tmp_path / "absent.toml") == UserConfig()


def test_config_roundtrip_is_atomic_and_preserves_unicode(tmp_path: Path) -> None:
    path = tmp_path / "nested dir" / "config.toml"
    secrets = tmp_path / "Секреты с пробелами"

    save_user_config(UserConfig(secrets_dir=secrets), path)

    assert load_user_config(path).secrets_dir == secrets
    assert [item.name for item in path.parent.iterdir()] == ["config.toml"]
    save_user_config(UserConfig(), path)
    assert path.read_text(encoding="utf-8") == ""
    assert load_user_config(path) == UserConfig()


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ('secrets_dir = "relative/dir"\n', "absolute"),
        ('secrets_dir = ""\n', "must not be empty"),
        (f"secrets_dir = '{os.path.abspath(os.sep)}'\n", "filesystem root"),
        ("secrets_dir = 5\n", "path string"),
        ('unknown = "x"\n', "unknown"),
        ("secrets_dir = [\n", "not valid TOML"),
    ],
)
def test_invalid_config_is_rejected(tmp_path: Path, content: str, message: str) -> None:
    path = tmp_path / "config.toml"
    path.write_text(content, encoding="utf-8")

    with pytest.raises(UserConfigError, match=message):
        load_user_config(path)


# --- precedence ----------------------------------------------------------------------------


def test_secret_store_precedence_env_over_config_over_default(tmp_path: Path) -> None:
    project = _project(tmp_path)
    config = tmp_path / "config.toml"
    configured = tmp_path / "configured store"
    save_user_config(UserConfig(secrets_dir=configured), config)
    base = tmp_path / "local"
    environ = {"ANSIBLE_DEPLOY_CONFIG": str(config), "LOCALAPPDATA": str(base)}

    settings = secret_store_settings(environ=environ, platform="win32")
    assert (settings.source, settings.path) == ("config", configured)
    assert external_secret_root(project, environ=environ, platform="win32") == (
        configured / PROJECT_ID
    )

    override = tmp_path / "exact root"
    with_env = {**environ, "ANSIBLE_DEPLOY_SECRETS_DIR": str(override)}
    assert secret_store_settings(environ=with_env, platform="win32").source == "env"
    assert external_secret_root(project, environ=with_env, platform="win32") == override

    save_user_config(UserConfig(), config)
    settings = secret_store_settings(environ=environ, platform="win32")
    assert settings.source == "default"
    assert external_secret_root(project, environ=environ, platform="win32") == (
        base / "ansible-deploy/projects" / PROJECT_ID
    )


def test_env_override_wins_even_over_broken_config(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text("not toml [", encoding="utf-8")
    environ = {
        "ANSIBLE_DEPLOY_CONFIG": str(config),
        "ANSIBLE_DEPLOY_SECRETS_DIR": str(tmp_path / "root"),
    }

    assert secret_store_settings(environ=environ).source == "env"
    with pytest.raises(SecretStoreError, match="not valid TOML"):
        secret_store_settings(environ={"ANSIBLE_DEPLOY_CONFIG": str(config)})


# --- setup ---------------------------------------------------------------------------------


@pytest.fixture
def no_doctor(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    calls: list[Path] = []

    def fake(project_dir: Path, *, as_json: bool = False) -> int:
        calls.append(project_dir)
        return 0

    monkeypatch.setattr(workstation, "run_doctor", fake)
    return calls


def test_setup_non_interactive_custom_dir_creates_store_and_config(
    tmp_path: Path, no_doctor: list[Path]
) -> None:
    store = tmp_path / "machine secrets"

    assert run(["setup", "--non-interactive", "--secrets-dir", str(store)]) == 0

    assert load_user_config(_config_file()).secrets_dir == store
    settings = secret_store_settings()
    assert settings.source == "config"
    doctor_store = doctor._secret_store_checks(None)[0]
    assert doctor_store.status == "OK", doctor_store
    assert len(no_doctor) == 1

    # Idempotent re-run keeps the same config and validated directory.
    assert run(["setup", "--non-interactive", "--secrets-dir", str(store)]) == 0
    assert load_user_config(_config_file()).secrets_dir == store


def test_setup_default_dir_removes_custom_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_doctor: list[Path]
) -> None:
    _default_store_env(monkeypatch, tmp_path / "base")
    save_user_config(UserConfig(secrets_dir=tmp_path / "old"), _config_file())

    assert run(["setup", "-y", "--default-secrets-dir"]) == 0

    assert load_user_config(_config_file()) == UserConfig()
    settings = secret_store_settings()
    assert settings.source == "default"
    assert settings.path.is_dir()


def test_setup_rejects_relative_secrets_dir(
    no_doctor: list[Path], capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(["setup", "--secrets-dir", "relative/dir"]) == 2
    assert "--secrets-dir must be an absolute path" in capsys.readouterr().err
    assert not _config_file().exists()


@pytest.mark.parametrize("arguments", [["setup"], ["setup", "--non-interactive"]])
def test_setup_without_terminal_requires_flags(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    no_doctor: list[Path],
    arguments: list[str],
) -> None:
    monkeypatch.setattr(workstation, "interactive_terminal", lambda: False)

    assert run(arguments) == 2

    error = capsys.readouterr().err
    assert "--secrets-dir PATH" in error
    assert "--default-secrets-dir" in error
    assert not _config_file().exists()


def test_setup_refuses_existing_unprotected_directory(
    tmp_path: Path, no_doctor: list[Path], capsys: pytest.CaptureFixture[str]
) -> None:
    store = tmp_path / "shared"
    store.mkdir()
    if os.name != "nt":
        store.chmod(0o755)

    assert run(["setup", "--secrets-dir", str(store)]) == 2

    assert "is not usable" in capsys.readouterr().err
    assert not _config_file().exists()


class _Answer:
    def __init__(self, value: object) -> None:
        self.value = value

    def ask(self) -> object:
        return self.value


def _interactive(monkeypatch: pytest.MonkeyPatch, **answers: Sequence[object]) -> list[str]:
    asked: list[str] = []
    queues = {name: list(values) for name, values in answers.items()}

    def factory(name: str):  # type: ignore[no-untyped-def]
        def prompt(message: str, *args: object, **kwargs: object) -> _Answer:
            asked.append(message)
            if name == "select":
                choices = kwargs["choices"]
                assert isinstance(choices, list)
                index = queues[name].pop(0)
                assert isinstance(index, int)
                return _Answer(choices[index])
            if name == "path":
                validate = kwargs["validate"]
                assert callable(validate)
                assert validate("relative") != True  # noqa: E712
            return _Answer(queues[name].pop(0))

        return prompt

    monkeypatch.setattr(workstation, "interactive_terminal", lambda: True)
    for name in ("select", "path", "confirm"):
        monkeypatch.setattr(workstation.questionary, name, factory(name))
    return asked


def test_interactive_setup_custom_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_doctor: list[Path]
) -> None:
    store = tmp_path / "interactive store"
    asked = _interactive(monkeypatch, select=[1], path=[str(store)], confirm=[True])

    assert run(["setup"]) == 0

    assert load_user_config(_config_file()).secrets_dir == store
    assert any("Where should project secrets be stored" in message for message in asked)
    assert no_doctor


def test_interactive_setup_warns_about_git_worktree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    no_doctor: list[Path],
) -> None:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    inside = repo / "secrets"
    outside = tmp_path / "outside"
    _interactive(monkeypatch, select=[1], path=[str(inside), str(outside)], confirm=[False, True])

    assert run(["setup"]) == 0

    assert "inside the Git working tree" in capsys.readouterr().out
    assert load_user_config(_config_file()).secrets_dir == outside


def test_interactive_setup_default_choice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_doctor: list[Path]
) -> None:
    _default_store_env(monkeypatch, tmp_path / "base")
    _interactive(monkeypatch, select=[0], confirm=[True])

    assert run(["setup"]) == 0

    assert load_user_config(_config_file()) == UserConfig()
    assert secret_store_settings().path.is_dir()


def test_interactive_setup_cancel_writes_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    no_doctor: list[Path],
) -> None:
    _interactive(monkeypatch, select=[1], path=[None])

    assert run(["setup"]) == 2

    assert "cancelled" in capsys.readouterr().err
    assert not _config_file().exists()
    assert not no_doctor


# --- config show / path -------------------------------------------------------------------


def test_config_path_prints_override(capsys: pytest.CaptureFixture[str]) -> None:
    assert run(["config", "path"]) == 0
    assert capsys.readouterr().out.strip() == str(_config_file())


def test_config_show_reports_value_and_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    store = tmp_path / "store"
    save_user_config(UserConfig(secrets_dir=store), _config_file())

    assert run(["config", "show"]) == 0
    output = capsys.readouterr().out
    assert str(store) in output
    assert "user config secrets_dir" in output

    monkeypatch.setenv("ANSIBLE_DEPLOY_SECRETS_DIR", str(tmp_path / "env root"))
    assert run(["config", "show"]) == 0
    output = capsys.readouterr().out
    assert "env ANSIBLE_DEPLOY_SECRETS_DIR" in output
    assert "overrides config value" in output


def test_config_show_reports_invalid_config(capsys: pytest.CaptureFixture[str]) -> None:
    _config_file().write_text("secrets_dir = 'relative'\n", encoding="utf-8")

    assert run(["config", "show"]) == 2
    assert "absolute" in capsys.readouterr().err


# --- doctor --------------------------------------------------------------------------------


def _tools(
    monkeypatch: pytest.MonkeyPatch,
    *,
    missing: frozenset[str] = frozenset(),
    docker_info: subprocess.CompletedProcess[str] | Exception | None = None,
    ignored: bool = True,
) -> list[list[str]]:
    calls: list[list[str]] = []

    def find(name: str) -> str | None:
        return None if name in missing else f"/usr/bin/{name}"

    def run_tool(argv: Sequence[str], timeout: int) -> subprocess.CompletedProcess[str]:
        calls.append(list(argv))
        if argv[1:2] == ["info"]:
            if isinstance(docker_info, Exception):
                raise docker_info
            if docker_info is not None:
                return docker_info
            return subprocess.CompletedProcess(list(argv), 0, "28.4.0\n", "")
        if "check-ignore" in argv:
            return subprocess.CompletedProcess(list(argv), 0 if ignored else 1, "", "")
        return subprocess.CompletedProcess(list(argv), 0, f"{argv[0]} version 1.0\n", "")

    monkeypatch.setattr(doctor, "_find_tool", find)
    monkeypatch.setattr(doctor, "_run_tool", run_tool)
    return calls


def _doctor_json(
    project_dir: Path, capsys: pytest.CaptureFixture[str]
) -> tuple[int, dict[str, dict[str, str]]]:
    code = run(["--project-dir", str(project_dir), "doctor", "--json"])
    checks = json.loads(capsys.readouterr().out)
    return code, {check["name"]: check for check in checks}


def test_doctor_all_tools_present_and_store_missing_is_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _tools(monkeypatch)
    save_user_config(UserConfig(secrets_dir=tmp_path / "absent"), _config_file())

    code, checks = _doctor_json(tmp_path, capsys)

    assert code == 0
    for name in ("Python", "Docker CLI", "Docker daemon", "git", "ssh-keygen", "age", "rclone"):
        assert checks[name]["status"] == "OK", checks[name]
    assert checks["Secrets store"]["status"] == "WARN"
    assert "ansible-deploy setup" in checks["Secrets store"]["hint"]
    assert "Project id" not in checks


def test_doctor_fails_when_required_tools_are_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _tools(monkeypatch, missing=frozenset({"docker", "git", "ssh-keygen", "age", "rclone"}))

    code, checks = _doctor_json(tmp_path, capsys)

    assert code == 1
    assert checks["Docker CLI"]["status"] == "FAIL"
    assert "Docker daemon" not in checks
    assert checks["git"]["status"] == "FAIL"
    assert checks["ssh-keygen"]["status"] == "FAIL"
    assert checks["age"]["status"] == "WARN"
    assert checks["rclone"]["status"] == "WARN"


@pytest.mark.parametrize(
    ("platform", "stderr", "hint"),
    [
        ("win32", "error during connect", "Start Docker Desktop"),
        ("darwin", "Cannot connect to the Docker daemon", "Start Docker Desktop"),
        ("linux", "Cannot connect to the Docker daemon", "systemctl start docker"),
        ("linux", "permission denied while trying to connect", "usermod -aG docker"),
    ],
)
def test_doctor_daemon_failure_hints_are_platform_specific(
    monkeypatch: pytest.MonkeyPatch, platform: str, stderr: str, hint: str
) -> None:
    _tools(
        monkeypatch,
        docker_info=subprocess.CompletedProcess(["docker", "info"], 1, "", stderr),
    )
    monkeypatch.setattr(doctor.sys, "platform", platform)

    daemon = doctor._docker_checks()[1]

    assert daemon.status == "FAIL"
    assert hint in daemon.hint


def test_doctor_daemon_timeout_is_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    _tools(monkeypatch, docker_info=subprocess.TimeoutExpired(["docker", "info"], 15))

    daemon = doctor._docker_checks()[1]

    assert daemon.status == "FAIL"
    assert "did not answer" in daemon.detail


def test_doctor_reports_unprotected_store_as_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _tools(monkeypatch)
    store = tmp_path / "loose"
    store.mkdir()
    if os.name != "nt":
        store.chmod(0o755)
    save_user_config(UserConfig(secrets_dir=store), _config_file())

    code, checks = _doctor_json(tmp_path, capsys)

    assert code == 1
    assert checks["Secrets store"]["status"] == "FAIL"
    expected = "icacls" if sys.platform == "win32" else "chmod 700"
    assert expected in checks["Secrets store"]["hint"]


def test_doctor_invalid_config_is_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _tools(monkeypatch)
    _config_file().write_text("secrets_dir = 'relative'\n", encoding="utf-8")

    code, checks = _doctor_json(tmp_path, capsys)

    assert code == 1
    assert checks["Secrets store"]["status"] == "FAIL"


def test_doctor_project_checks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project = _project(tmp_path)
    store = tmp_path / "store"
    calls = _tools(monkeypatch, ignored=False)
    # setup runs doctor itself; the mocked tools keep that run hermetic.
    assert run(["--project-dir", str(project), "setup", "--secrets-dir", str(store)]) == 0
    assert "ansible-deploy doctor" in capsys.readouterr().out

    code, checks = _doctor_json(project, capsys)

    assert code == 0
    assert checks["Project id"] == {
        "name": "Project id",
        "status": "OK",
        "detail": PROJECT_ID,
        "hint": "",
    }
    assert checks["Project secrets"]["status"] == "WARN"
    assert str(store / PROJECT_ID) in checks["Project secrets"]["detail"]
    assert checks["Project .gitignore"]["status"] == "WARN"
    assert any("check-ignore" in call for call in calls)


def test_doctor_gitignore_fallback_without_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _tools(monkeypatch, missing=frozenset({"git"}))
    (tmp_path / ".gitignore").write_text("node_modules/\n.deploy-state/\n", encoding="utf-8")

    assert doctor._gitignore_check(tmp_path).status == "OK"
    (tmp_path / ".gitignore").write_text("node_modules/\n", encoding="utf-8")
    assert doctor._gitignore_check(tmp_path).status == "WARN"


def test_doctor_table_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _tools(monkeypatch, missing=frozenset({"docker"}))

    assert run(["--project-dir", str(tmp_path), "doctor"]) == 1

    output = capsys.readouterr().out
    assert "ansible-deploy doctor" in output
    assert "FAIL" in output
    assert "check(s) failed" in output
