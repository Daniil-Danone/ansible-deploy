import base64
import os
import shutil
import stat
import subprocess
import sys
import traceback
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from deploy_cli import cli as cli_module
from deploy_cli import config as config_module
from deploy_cli import keys, secret_store
from deploy_cli import runner as runner_module
from deploy_cli.cli import run
from deploy_cli.config import (
    ConfigurationError,
    load_configuration,
    validate_environment_file,
    validate_local_inputs,
    validate_observability_inputs,
    validate_production_isolation,
    validate_registry_auth,
)
from deploy_cli.keys import ensure_deploy_key
from deploy_cli.redaction import Redactor
from deploy_cli.runner import AnsibleRunner, RunnerError
from deploy_cli.secret_file import secure_secret_permissions
from deploy_cli.workflow import deployment_manifest


def _v2_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    source = Path(__file__).parents[1] / "examples/demo-app"
    project = tmp_path / "schema v2 project"
    shutil.copytree(source, project)
    trusted_base = tmp_path / "external store base"
    _secure_directory(trusted_base)
    root = (trusted_base / "external secrets").resolve()
    monkeypatch.setenv("ANSIBLE_DEPLOY_SECRETS_DIR", str(root))
    return project, root


def _environment_config(project: Path, environment: str = "stage") -> Path:
    return project / f".deploy/environments/{environment}/config.yml"


def _set_sensitive_name(
    project: Path, field: str, value: str, *, environment: str = "stage"
) -> None:
    path = _environment_config(project, environment)
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    section, name = field.split(".", 1)
    raw[section][name] = value
    path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")


def _secure_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    secure_secret_permissions(path)


def _grant_other_read(path: Path) -> None:
    """Make a secret readable by others. An inherited owner-only ACL alone is accepted."""
    if os.name != "nt":
        path.chmod(0o644)
        return
    icacls = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/icacls.exe"
    subprocess.run(  # noqa: S603 - fixed Windows ACL utility and arguments
        [str(icacls), str(path), "/grant", "*S-1-5-32-545:(R)"],
        capture_output=True,
        check=True,
    )


def _write_external_file(config, path: Path, content: bytes) -> None:
    context = config.external_secret_context
    assert context is not None
    project, root, trusted_base, validate_base = context
    secret_store.ensure_external_parent_for_write(
        project,
        root,
        path,
        trusted_base=trusted_base,
        validate_trusted_base=validate_base,
    )
    path.write_bytes(content)
    secure_secret_permissions(path)


def _link_directory(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        if sys.platform != "win32":
            pytest.skip("directory symlinks are unavailable")
        result = subprocess.run(  # noqa: S603 - fixed Windows junction command
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],  # noqa: S607
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            pytest.skip("Windows junction creation is unavailable")


def test_external_parent_creation_tolerates_concurrent_creator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    trusted_base = tmp_path / "trusted"
    _secure_directory(trusted_base)
    root = trusted_base / "secrets"
    target = root / "environment" / "app.env"
    original_mkdir = Path.mkdir
    raced = False

    def racing_mkdir(path: Path, *args, **kwargs) -> None:
        nonlocal raced
        if path == root and not raced:
            raced = True
            original_mkdir(path, *args, **kwargs)
            secure_secret_permissions(path)
            raise FileExistsError(path)
        original_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", racing_mkdir)

    secret_store.ensure_external_parent_for_write(
        project,
        root,
        target,
        trusted_base=trusted_base,
        validate_trusted_base=True,
    )

    assert raced
    assert target.parent.is_dir()


def test_t5_schema_v2_resolves_every_sensitive_name_inside_external_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, root = _v2_project(tmp_path, monkeypatch)

    _, stage = load_configuration(project, "stage")
    _, monitoring = load_configuration(project, "monitoring")

    assert stage.schema_version == 2
    assert stage.application.env_file == root / "environments/stage/app.env"
    assert stage.application.registry_auth_file == root / "environments/stage/registry-auth.json"
    assert stage.server.ssh_key == root / "keys/stage_ed25519"
    assert stage.server.public_key == root / "keys/stage_ed25519.pub"
    assert stage.collector is not None
    assert stage.collector.password_file == root / "environments/stage/collector.password"
    assert monitoring.schema_version == 2
    assert monitoring.monitoring.secrets_file == root / "environments/monitoring/monitoring.env"
    assert monitoring.server.ssh_key == root / "keys/monitoring_ed25519"
    assert monitoring.server.public_key == root / "keys/monitoring_ed25519.pub"


@pytest.mark.parametrize("value", ["", "../outside-secret", "safe/../../outside-secret"])
def test_t6_schema_v2_rejects_empty_or_parent_sensitive_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    project, _ = _v2_project(tmp_path, monkeypatch)
    _set_sensitive_name(project, "application.env_file", value)

    with pytest.raises(ConfigurationError, match="normalized relative paths"):
        load_configuration(project, "stage")


def test_t6_schema_v2_rejects_absolute_sensitive_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, _ = _v2_project(tmp_path, monkeypatch)
    _set_sensitive_name(project, "application.env_file", str((tmp_path / "outside").resolve()))

    with pytest.raises(ConfigurationError, match="normalized relative paths"):
        load_configuration(project, "stage")


def test_t6_project_deploy_directory_cannot_be_external_secret_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, _ = _v2_project(tmp_path, monkeypatch)
    monkeypatch.setenv("ANSIBLE_DEPLOY_SECRETS_DIR", str(project / ".deploy"))

    with pytest.raises(ConfigurationError, match="Invalid schema v2"):
        load_configuration(project, "stage")


def test_t7_link_escape_from_external_root_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, root = _v2_project(tmp_path, monkeypatch)
    outside = tmp_path / "outside"
    outside.mkdir()
    _secure_directory(root)
    link = root / "environments"
    _link_directory(link, outside)

    with pytest.raises(ConfigurationError, match="Invalid schema v2 application environment"):
        load_configuration(project, "stage")


@pytest.mark.parametrize("kind", ["directory", "permissions", "hardlink"])
def test_t8_external_secret_must_be_regular_owner_only_without_hardlinks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    project, root = _v2_project(tmp_path, monkeypatch)
    _set_sensitive_name(project, "application.env_file", "checks/application-secret")
    candidate = root / "checks/application-secret"
    _secure_directory(root)
    _secure_directory(candidate.parent)
    if kind == "directory":
        candidate.mkdir()
    else:
        candidate.write_bytes(b"synthetic-test-value\n")
        if kind == "permissions":
            _grant_other_read(candidate)
        else:
            secure_secret_permissions(candidate)
            os.link(candidate, root / "checks/application-secret-alias")

    with pytest.raises(ConfigurationError, match="Invalid schema v2 application environment"):
        load_configuration(project, "stage")


def test_t8_owner_only_regular_external_secret_is_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, root = _v2_project(tmp_path, monkeypatch)
    _set_sensitive_name(project, "application.env_file", "checks/application-secret")
    candidate = root / "checks/application-secret"
    _secure_directory(root)
    _secure_directory(candidate.parent)
    candidate.write_bytes(b"synthetic-test-value\n")
    secure_secret_permissions(candidate)

    _, stage = load_configuration(project, "stage")

    assert stage.application.env_file == candidate


def test_t9_validation_error_hides_sensitive_name_and_file_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, root = _v2_project(tmp_path, monkeypatch)
    sensitive_name = "private/DO-NOT-DISCLOSE-name"
    sensitive_value = "DO-NOT-DISCLOSE-value"
    _set_sensitive_name(project, "application.env_file", sensitive_name)
    candidate = root / sensitive_name
    _secure_directory(root)
    _secure_directory(candidate.parent)
    candidate.write_text(sensitive_value, encoding="utf-8")
    _grant_other_read(candidate)

    with pytest.raises(ConfigurationError) as raised:
        load_configuration(project, "stage")

    rendered = repr(raised.value) + str(raised.value) + repr(raised.value.__cause__)
    assert sensitive_name not in rendered
    assert "DO-NOT-DISCLOSE-name" not in rendered
    assert sensitive_value not in rendered


@pytest.mark.parametrize(
    "value",
    [
        "dot/./name",
        "double//separator",
        "back\\slash",
        "//unc/share",
        "C:/drive/name",
        "name:stream",
        "CON.txt",
        "nested/lpt9.log",
        "trailing-dot./name",
        "trailing-space /name",
        "control/na\x00me",
        "decomposed/e\u0301",
    ],
)
def test_schema_v2_portable_name_validator_rejects_nonportable_forms(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    project, _ = _v2_project(tmp_path, monkeypatch)
    _set_sensitive_name(project, "application.env_file", value)

    with pytest.raises(ConfigurationError, match="normalized relative paths"):
        load_configuration(project, "stage")


@pytest.mark.parametrize(
    ("environment", "field"),
    [
        ("stage", "server.ssh_key"),
        ("stage", "server.public_key"),
        ("stage", "application.env_file"),
        ("stage", "application.registry_auth_file"),
        ("stage", "collector.password_file"),
        ("monitoring", "server.ssh_key"),
        ("monitoring", "server.public_key"),
        ("monitoring", "monitoring.secrets_file"),
    ],
)
def test_every_schema_v2_sensitive_field_uses_portable_name_validator(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    environment: str,
    field: str,
) -> None:
    project, _ = _v2_project(tmp_path, monkeypatch)
    _set_sensitive_name(project, field, "portable\\escape", environment=environment)

    with pytest.raises(ConfigurationError, match="normalized relative paths"):
        load_configuration(project, environment)


@pytest.mark.parametrize("schema_version", [None, 1, 3])
def test_schema_version_missing_or_unknown_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    schema_version: int | None,
) -> None:
    project, _ = _v2_project(tmp_path, monkeypatch)
    path = _environment_config(project)
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if schema_version is None:
        raw.pop("schema_version")
    else:
        raw["schema_version"] = schema_version
    path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")

    with pytest.raises(ConfigurationError, match="only schema_version: 2 is supported"):
        load_configuration(project, "stage")


def test_external_root_cannot_contain_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, _ = _v2_project(tmp_path, monkeypatch)
    monkeypatch.setattr(secret_store, "_validate_external_ancestry", lambda *args, **kwargs: None)
    monkeypatch.setenv("ANSIBLE_DEPLOY_SECRETS_DIR", str(project.parent))

    with pytest.raises(ConfigurationError, match="Invalid schema v2"):
        load_configuration(project, "stage")


def test_external_root_cannot_be_sibling_inside_parent_git_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    worktree = tmp_path / "repository"
    _secure_directory(worktree)
    project = worktree / "nested-project"
    source = Path(__file__).parents[1] / "examples/demo-app"
    shutil.copytree(source, project)
    (worktree / ".git").mkdir()
    sibling_root = worktree / "sibling-secrets"
    _secure_directory(sibling_root)
    monkeypatch.setenv("ANSIBLE_DEPLOY_SECRETS_DIR", str(sibling_root))

    with pytest.raises(ConfigurationError, match="Invalid schema v2"):
        load_configuration(project, "stage")


def test_override_parent_must_be_owner_only_trust_anchor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, _ = _v2_project(tmp_path, monkeypatch)
    unsafe_parent = tmp_path / "unsafe override parent"
    unsafe_parent.mkdir()
    if os.name != "nt":
        unsafe_parent.chmod(0o777)
    monkeypatch.setenv(
        "ANSIBLE_DEPLOY_SECRETS_DIR", str(unsafe_parent / "project root")
    )

    with pytest.raises(ConfigurationError, match="Invalid schema v2"):
        load_configuration(project, "stage")


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode contract")
def test_posix_external_root_mode_0755_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, root = _v2_project(tmp_path, monkeypatch)
    root.mkdir(mode=0o755)
    root.chmod(0o755)

    with pytest.raises(ConfigurationError, match="Invalid schema v2"):
        load_configuration(project, "stage")


def test_posix_owner_only_root_metadata_rejects_mode_0755(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(secret_store, "_current_uid", lambda: 1234)
    metadata = SimpleNamespace(st_mode=stat.S_IFDIR | 0o755, st_uid=1234, st_nlink=1)

    with pytest.raises(secret_store.SecretStoreError, match="owner-only"):
        secret_store._validate_posix_metadata(
            metadata,
            directory=True,
            secret=False,
            owner_only_directory=True,
        )


@pytest.mark.skipif(os.name != "nt", reason="Windows ACL contract")
@pytest.mark.parametrize("target_kind", ["root", "secret"])
def test_windows_everyone_read_acl_is_rejected_deterministically(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target_kind: str,
) -> None:
    project, root = _v2_project(tmp_path, monkeypatch)
    _secure_directory(root)
    target = root
    if target_kind == "secret":
        _set_sensitive_name(project, "application.env_file", "checks/unsafe-acl")
        _secure_directory(root / "checks")
        target = root / "checks/unsafe-acl"
        target.write_bytes(b"synthetic-secret")
        secure_secret_permissions(target)
    icacls = shutil.which("icacls")
    if icacls is None:
        pytest.skip("icacls is unavailable")
    result = subprocess.run(  # noqa: S603 - resolved Windows system utility
        [icacls, str(target), "/grant", "*S-1-1-0:(R)"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.skip("Unable to install deterministic unsafe ACL")
    try:
        with pytest.raises(ConfigurationError, match="Invalid schema v2"):
            load_configuration(project, "stage")
    finally:
        secure_secret_permissions(target)


def test_external_root_link_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, root = _v2_project(tmp_path, monkeypatch)
    actual = tmp_path / "actual external root"
    _secure_directory(actual)
    _link_directory(root, actual)

    with pytest.raises(ConfigurationError, match="Invalid schema v2"):
        load_configuration(project, "stage")


def test_external_final_file_link_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, root = _v2_project(tmp_path, monkeypatch)
    _set_sensitive_name(project, "application.env_file", "checks/linked-secret")
    _secure_directory(root)
    _secure_directory(root / "checks")
    outside = tmp_path / "outside-secret"
    outside.write_text("synthetic\n", encoding="utf-8")
    secure_secret_permissions(outside)
    try:
        (root / "checks/linked-secret").symlink_to(outside)
    except OSError:
        pytest.skip("file symlinks are unavailable")

    with pytest.raises(ConfigurationError, match="Invalid schema v2"):
        load_configuration(project, "stage")


def test_missing_v2_secret_error_chain_and_cli_stderr_hide_configured_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project, _ = _v2_project(tmp_path, monkeypatch)
    sensitive_name = "private/DO-NOT-DISCLOSE-missing"
    _set_sensitive_name(project, "application.env_file", sensitive_name)
    _, config = load_configuration(project, "stage")

    with pytest.raises(ConfigurationError) as raised:
        validate_local_inputs(config, require_ssh=False, require_public_key=False)
    chain = "".join(
        repr(item)
        for item in (raised.value, raised.value.__cause__, raised.value.__context__)
    )
    assert sensitive_name not in chain
    assert str(tmp_path) not in chain

    assert run(["--repo", str(project), "stage", "--dry-run", "--version", "abcdef0"]) == 2
    stderr = capsys.readouterr().err
    assert "application environment" in stderr
    assert "stage" in stderr
    assert sensitive_name not in stderr
    assert str(tmp_path) not in stderr


def test_sensitive_read_failure_is_generic_across_validator_workflow_and_cli(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project, _ = _v2_project(tmp_path, monkeypatch)
    _, config = load_configuration(project, "stage")
    sensitive_marker = "DO-NOT-DISCLOSE-read-failure"
    _write_external_file(config, config.application.env_file, b"APP_ENV=stage\n")
    assert config.application.registry_auth_file is not None
    _write_external_file(
        config,
        config.application.registry_auth_file,
        b'{"auths":{"https://index.docker.io/v1/":{"auth":"'
        + base64.b64encode(b"user:token")
        + b'"}}}\n',
    )
    original_read_bytes = Path.read_bytes

    def failing_read(path: Path) -> bytes:
        if path == config.application.env_file:
            raise OSError(f"{sensitive_marker}: {path}")
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", failing_read)
    operations = (
        lambda: validate_environment_file(config),
        lambda: deployment_manifest(config),
    )
    for operation in operations:
        with pytest.raises(ConfigurationError) as raised:
            operation()
        rendered = "".join(traceback.format_exception(raised.value))
        assert sensitive_marker not in rendered
        assert str(config.application.env_file) not in rendered
        assert raised.value.__cause__ is None
        assert raised.value.__suppress_context__

    assert run(["--repo", str(project), "stage", "--dry-run", "--version", "abcdef0"]) == 2
    stderr = capsys.readouterr().err
    assert "application environment" in stderr
    assert sensitive_marker not in stderr
    assert str(config.application.env_file) not in stderr


@pytest.mark.parametrize(
    "error_type", [secret_store.SecretStoreError, OSError, ValueError]
)
def test_shared_external_use_boundary_redacts_operational_failures_for_all_callers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    error_type: type[Exception],
) -> None:
    project, _ = _v2_project(tmp_path, monkeypatch)
    _, config = load_configuration(project, "stage")
    marker = f"DO-NOT-DISCLOSE-shared-use-{error_type.__name__}"
    sensitive_path = str(config.application.env_file)
    monkeypatch.setattr(
        config_module,
        "validate_external_file_for_use",
        lambda *args, **kwargs: (_ for _ in ()).throw(error_type(marker)),
    )
    operations = (
        lambda: config_module.validate_external_input_for_use(
            config,
            config.application.env_file,
            field="application environment",
        ),
        lambda: config_module.read_external_secret_bytes(
            config,
            config.application.env_file,
            field="application environment",
        ),
        lambda: config_module.read_external_secret_text(
            config,
            config.application.env_file,
            field="application environment",
        ),
        lambda: validate_local_inputs(
            config,
            require_ssh=False,
            require_public_key=False,
        ),
        lambda: validate_environment_file(config),
        lambda: deployment_manifest(config),
    )

    for operation in operations:
        with pytest.raises(ConfigurationError) as raised:
            operation()
        rendered = "".join(traceback.format_exception(raised.value))
        assert marker not in rendered
        assert sensitive_path not in rendered
        assert "application environment" in str(raised.value)
        assert config.environment in str(raised.value)
        assert raised.value.__cause__ is None
        assert raised.value.__suppress_context__

    assert run(["--repo", str(project), "stage", "--dry-run", "--version", "abcdef0"]) == 2
    stderr = capsys.readouterr().err
    assert marker not in stderr
    assert sensitive_path not in stderr
    assert "application environment" in stderr
    assert config.environment in stderr


@pytest.mark.parametrize(
    "error_type", [secret_store.SecretStoreError, OSError, ValueError]
)
def test_runner_external_mount_boundary_redacts_operational_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error_type: type[Exception],
) -> None:
    project, root = _v2_project(tmp_path, monkeypatch)
    marker = f"DO-NOT-DISCLOSE-runner-mount-{error_type.__name__}"
    sensitive_path = root / "sensitive mount name"
    runner = AnsibleRunner(
        project,
        Redactor([]),
        environment="stage",
        external_secret_root=root,
    )
    runner._pending_external_mounts = [
        ("application environment", sensitive_path, True)
    ]
    monkeypatch.setattr(
        runner_module,
        "validate_external_file_for_use",
        lambda *args, **kwargs: (_ for _ in ()).throw(error_type(marker)),
    )

    with pytest.raises(RunnerError) as raised:
        runner._validate_external_mounts(7)

    rendered = "".join(traceback.format_exception(raised.value))
    assert raised.value.exit_code == 7
    assert marker not in rendered
    assert str(sensitive_path) not in rendered
    assert "application environment" in str(raised.value)
    assert "stage" in str(raised.value)
    assert raised.value.__cause__ is None
    assert raised.value.__suppress_context__


@pytest.mark.parametrize(
    "error_type", [secret_store.SecretStoreError, OSError, ValueError]
)
def test_config_external_path_error_suppresses_sensitive_low_level_chain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error_type: type[Exception],
) -> None:
    project, _ = _v2_project(tmp_path, monkeypatch)
    marker = "DO-NOT-DISCLOSE-configured-path"
    monkeypatch.setattr(
        config_module,
        "resolve_external_file",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            error_type(marker)
        ),
    )

    with pytest.raises(ConfigurationError) as raised:
        load_configuration(project, "stage")

    assert marker not in "".join(traceback.format_exception(raised.value))
    assert raised.value.__cause__ is None
    assert raised.value.__suppress_context__


def test_observability_validation_suppresses_operational_exception_chain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, _ = _v2_project(tmp_path, monkeypatch)
    _, config = load_configuration(project, "monitoring")
    marker = "DO-NOT-DISCLOSE-monitoring-value"
    sensitive_path = str(config.monitoring.secrets_file)
    monkeypatch.setattr(
        config_module,
        "validate_external_input_for_use",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError(marker)),
    )

    with pytest.raises(ConfigurationError) as raised:
        validate_observability_inputs(config)

    rendered = "".join(traceback.format_exception(raised.value))
    assert marker not in rendered
    assert sensitive_path not in rendered
    assert config.environment in str(raised.value)
    assert raised.value.__cause__ is None


@pytest.mark.parametrize("failure_point", ["read", "permissions"])
def test_registry_validation_suppresses_operational_exception_chain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_point: str,
) -> None:
    project, _ = _v2_project(tmp_path, monkeypatch)
    _, config = load_configuration(project, "stage")
    marker = f"DO-NOT-DISCLOSE-registry-{failure_point}"
    sensitive_path = str(config.application.registry_auth_file)
    encoded = base64.b64encode(b"user:token").decode("ascii")
    if failure_point == "read":
        monkeypatch.setattr(
            config_module,
            "read_external_secret_text",
            lambda *args, **kwargs: (_ for _ in ()).throw(OSError(marker)),
        )
    else:
        monkeypatch.setattr(
            config_module,
            "read_external_secret_text",
            lambda *args, **kwargs: (
                '{"auths":{"ghcr.io":{"auth":"' + encoded + '"}}}'
            ),
        )
        monkeypatch.setattr(
            config_module,
            "validate_secret_permissions",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                secret_store.SecretStoreError(marker)
            ),
        )

    with pytest.raises(ConfigurationError) as raised:
        validate_registry_auth(config)

    rendered = "".join(traceback.format_exception(raised.value))
    assert marker not in rendered
    assert sensitive_path not in rendered
    assert config.environment in str(raised.value)
    assert raised.value.__cause__ is None


def test_low_level_secret_store_operational_error_suppresses_sensitive_chain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, _ = _v2_project(tmp_path, monkeypatch)
    marker = "DO-NOT-DISCLOSE-low-level-path"

    def fail_open(*args, **kwargs):
        raise OSError(marker)

    if os.name == "nt":
        monkeypatch.setattr(secret_store, "_windows_open", fail_open)
    else:
        monkeypatch.setattr(secret_store.os, "open", fail_open)

    with pytest.raises(secret_store.SecretStoreError) as raised:
        secret_store.resolve_external_file(project, Path("private/file"))

    assert marker not in "".join(traceback.format_exception(raised.value))
    assert raised.value.__cause__ is None
    assert raised.value.__suppress_context__


def test_runner_revalidates_external_mount_after_configuration_load(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, root = _v2_project(tmp_path, monkeypatch)
    _set_sensitive_name(project, "server.ssh_key", "keys/private-key")
    _secure_directory(root)
    keys = root / "keys"
    _secure_directory(keys)
    key = keys / "private-key"
    key.write_text("synthetic-key\n", encoding="utf-8")
    secure_secret_permissions(key)
    _, config = load_configuration(project, "stage")

    held = root / "held-keys"
    keys.rename(held)
    replacement = tmp_path / "replacement-keys"
    _secure_directory(replacement)
    replacement_key = replacement / "private-key"
    replacement_key.write_text("replacement\n", encoding="utf-8")
    secure_secret_permissions(replacement_key)
    _link_directory(keys, replacement)
    runner = AnsibleRunner(
        project,
        Redactor([]),
        external_secret_root=root,
    )
    monkeypatch.setattr(runner, "_run", lambda *args, **kwargs: None)

    with pytest.raises(RunnerError, match="SSH private key"):
        runner.playbook("deploy.yml", project / "inventory.yml", {}, config.server.ssh_key)


def test_v2_runner_accepts_valid_external_mount(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, root = _v2_project(tmp_path, monkeypatch)
    _set_sensitive_name(project, "server.ssh_key", "keys/private-key")
    _secure_directory(root)
    _secure_directory(root / "keys")
    key = root / "keys/private-key"
    key.write_text("synthetic-key\n", encoding="utf-8")
    secure_secret_permissions(key)
    _, config = load_configuration(project, "stage")
    runner = AnsibleRunner(project, Redactor([]), external_secret_root=root)
    calls: list[list[str]] = []
    monkeypatch.setattr(runner, "_run", lambda args, **kwargs: calls.append(list(args)))

    runner.playbook("deploy.yml", project / "inventory.yml", {}, config.server.ssh_key)

    assert f"{key}:/run/secrets-source/ssh_key:ro" in calls[0]


@pytest.mark.skipif(os.name != "nt", reason="Windows owner SID contract")
def test_windows_external_component_owner_must_match_current_sid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, root = _v2_project(tmp_path, monkeypatch)
    _secure_directory(root)
    monkeypatch.setattr(secret_store, "_windows_owner_sid", lambda handle: "S-1-5-18")

    with pytest.raises(ConfigurationError, match="Invalid schema v2"):
        load_configuration(project, "stage")


@pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="OpenSSH is unavailable")
def test_t14_v2_key_pair_is_created_only_external_and_never_overwritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, root = _v2_project(tmp_path, monkeypatch)
    _, config = load_configuration(project, "stage")

    ensure_deploy_key(config)
    private_before = config.server.ssh_key.read_bytes()
    public_before = config.server.public_key.read_bytes()
    ensure_deploy_key(config)

    assert config.server.ssh_key.is_relative_to(root)
    assert config.server.public_key.is_relative_to(root)
    assert config.server.ssh_key.read_bytes() == private_before
    assert config.server.public_key.read_bytes() == public_before
    assert not (project / ".deploy/keys").exists()


@pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="OpenSSH is unavailable")
def test_t14_key_pair_publish_failure_rolls_back_new_private_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, _ = _v2_project(tmp_path, monkeypatch)
    _, config = load_configuration(project, "stage")
    monkeypatch.setattr(
        keys,
        "_write_exclusive",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("synthetic failure")),
    )

    with pytest.raises(ConfigurationError, match="deploy key"):
        ensure_deploy_key(config)

    assert not config.server.ssh_key.exists()
    assert not config.server.public_key.exists()


def test_t16_runner_mounts_only_individual_external_files_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, root = _v2_project(tmp_path, monkeypatch)
    _, config = load_configuration(project, "stage")
    assert config.collector is not None
    files = [
        (config.server.ssh_key, b"PRIVATE-KEY-CONTENT"),
        (config.application.env_file, b"APP_ENV=stage\nENV-SECRET-CONTENT"),
        (config.application.registry_auth_file, b"REGISTRY-SECRET-CONTENT"),
        (config.collector.password_file, b"COLLECTOR-SECRET-CONTENT"),
    ]
    for path, content in files:
        assert path is not None
        _write_external_file(config, path, content)
    inventory = project / "inventory.yml"
    inventory.write_text("all: {}\n", encoding="utf-8")
    observed: dict[str, object] = {}

    class Process:
        stdout = StringIO()
        stdin = None

        @staticmethod
        def poll() -> int:
            return 0

        @staticmethod
        def wait(timeout=None) -> int:
            del timeout
            return 0

    real_popen = subprocess.Popen

    def popen(arguments, **kwargs):
        if arguments[0] != "docker":
            return real_popen(arguments, **kwargs)
        observed["arguments"] = arguments
        observed["environment"] = kwargs["env"]
        return Process()

    monkeypatch.setattr(subprocess, "Popen", popen)
    context = config.external_secret_context
    assert context is not None
    runner = AnsibleRunner(
        project,
        Redactor([]),
        external_secret_root=context[1],
        external_trusted_base=context[2],
        validate_external_trusted_base=context[3],
    )
    runner.playbook(
        "deploy.yml",
        inventory,
        {},
        config.server.ssh_key,
        env_file=config.application.env_file,
        registry_auth_file=config.application.registry_auth_file,
        observability_secret_file=config.collector.password_file,
    )

    arguments = observed["arguments"]
    environment = observed["environment"]
    assert isinstance(arguments, list)
    assert isinstance(environment, dict)
    expected_mounts = {
        f"{config.server.ssh_key}:/run/secrets-source/ssh_key:ro",
        f"{config.application.env_file}:/run/secrets/app_env:ro",
        f"{config.application.registry_auth_file}:/run/secrets/registry_auth:ro",
        f"{config.collector.password_file}:/run/secrets/observability:ro",
    }
    assert expected_mounts.issubset(arguments)
    assert not any(argument.startswith(f"{root}:") for argument in arguments)
    rendered = "\n".join(arguments) + "\n" + "\n".join(environment.values())
    for _, content in files:
        assert content.decode() not in rendered


def test_t16_launch_boundary_rejects_root_swap_before_popen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, root = _v2_project(tmp_path, monkeypatch)
    _, config = load_configuration(project, "stage")
    _write_external_file(config, config.server.ssh_key, b"synthetic-key")
    inventory = project / "inventory.yml"
    inventory.write_text("all: {}\n", encoding="utf-8")
    context = config.external_secret_context
    assert context is not None
    runner = AnsibleRunner(
        project,
        Redactor([]),
        external_secret_root=context[1],
        external_trusted_base=context[2],
        validate_external_trusted_base=context[3],
    )

    def swap_root() -> None:
        held = root.with_name("held-root")
        root.rename(held)
        replacement = root.with_name("replacement-root")
        _secure_directory(replacement)
        _link_directory(root, replacement)

    monkeypatch.setattr(runner_module, "_before_subprocess_launch", swap_root)
    monkeypatch.setattr(runner, "_docker_cleanup", lambda *args, **kwargs: 0)
    real_popen = subprocess.Popen

    def guarded_popen(arguments, **kwargs):
        if arguments[0] != "docker":
            return real_popen(arguments, **kwargs)
        raise AssertionError("Popen must not run after root substitution")

    monkeypatch.setattr(subprocess, "Popen", guarded_popen)

    with pytest.raises(RunnerError, match="SSH private key"):
        runner.playbook("deploy.yml", inventory, {}, config.server.ssh_key)


def _set_extra_env_files(
    project: Path, entries: list[dict[str, str]], *, environment: str = "stage"
) -> None:
    path = _environment_config(project, environment)
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    raw["application"]["extra_env_files"] = entries
    path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")


BOT_ENV = {"source": "environments/stage/bot.env", "target": "bot.env"}


def test_extra_env_file_source_resolves_inside_external_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, root = _v2_project(tmp_path, monkeypatch)
    _set_extra_env_files(project, [BOT_ENV])

    _, stage = load_configuration(project, "stage")

    [extra] = stage.application.extra_env_files
    assert extra.source == root / "environments/stage/bot.env"
    assert extra.target == "bot.env"


def test_extra_env_files_default_to_empty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project, _ = _v2_project(tmp_path, monkeypatch)

    _, stage = load_configuration(project, "stage")

    assert stage.application.extra_env_files == []


@pytest.mark.parametrize(
    "target",
    [".env", "bot", "bot.txt", "-bot.env", ".bot.env", "nested/bot.env", "../bot.env", "a b.env"],
)
def test_extra_env_file_target_must_be_plain_safe_env_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, target: str
) -> None:
    project, _ = _v2_project(tmp_path, monkeypatch)
    _set_extra_env_files(project, [{**BOT_ENV, "target": target}])

    with pytest.raises(ConfigurationError, match="extra_env_files"):
        load_configuration(project, "stage")


@pytest.mark.parametrize(
    "source", ["", "../outside.env", "/abs/bot.env", "portable\\escape", "C:/bot.env"]
)
def test_extra_env_file_source_uses_portable_name_validator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: str
) -> None:
    project, _ = _v2_project(tmp_path, monkeypatch)
    _set_extra_env_files(project, [{**BOT_ENV, "source": source}])

    with pytest.raises(ConfigurationError, match="normalized relative paths"):
        load_configuration(project, "stage")


@pytest.mark.parametrize(
    ("second", "message"),
    [
        ({"source": "environments/stage/other.env", "target": "bot.env"}, "targets"),
        ({"source": "environments/stage/bot.env", "target": "other.env"}, "sources"),
    ],
)
def test_extra_env_file_targets_and_sources_must_be_unique(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, second: dict[str, str], message: str
) -> None:
    project, _ = _v2_project(tmp_path, monkeypatch)
    _set_extra_env_files(project, [BOT_ENV, second])

    with pytest.raises(ConfigurationError, match=f"extra_env_files {message} must be unique"):
        load_configuration(project, "stage")


@pytest.mark.parametrize(
    "source",
    ["environments/stage/app.env", "environments/stage/registry-auth.json", "keys/stage_ed25519"],
)
def test_extra_env_file_cannot_alias_another_secret_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: str
) -> None:
    project, _ = _v2_project(tmp_path, monkeypatch)
    _set_extra_env_files(project, [{**BOT_ENV, "source": source}])

    with pytest.raises(ConfigurationError, match="collides with protected"):
        load_configuration(project, "stage")


def test_production_extra_env_file_cannot_reuse_stage_env_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, _ = _v2_project(tmp_path, monkeypatch)
    _set_extra_env_files(
        project,
        [{"source": "environments/stage/app.env", "target": "bot.env"}],
        environment="prod",
    )
    _, prod = load_configuration(project, "prod")

    with pytest.raises(ConfigurationError, match="env file"):
        validate_production_isolation(project, prod)


def test_missing_extra_env_file_fails_preflight_without_disclosing_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, root = _v2_project(tmp_path, monkeypatch)
    sensitive = "private/DO-NOT-DISCLOSE-bot.env"
    _set_extra_env_files(project, [{"source": sensitive, "target": "bot.env"}])
    _, config = load_configuration(project, "stage")
    _write_external_file(config, config.application.env_file, b"APP_ENV=stage\n")
    assert config.application.registry_auth_file is not None
    _write_external_file(config, config.application.registry_auth_file, b"{}")

    with pytest.raises(ConfigurationError) as raised:
        validate_local_inputs(config, require_ssh=False, require_public_key=False)

    message = str(raised.value)
    assert "application extra environment bot.env" in message
    assert sensitive not in message
    assert str(root) not in message


@pytest.mark.parametrize("kind", ["directory", "permissions", "hardlink"])
def test_extra_env_file_must_be_regular_owner_only_without_hardlinks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    project, root = _v2_project(tmp_path, monkeypatch)
    _set_extra_env_files(project, [{"source": "checks/bot.env", "target": "bot.env"}])
    candidate = root / "checks/bot.env"
    _secure_directory(root)
    _secure_directory(candidate.parent)
    if kind == "directory":
        candidate.mkdir()
    else:
        candidate.write_bytes(b"DB_PASSWORD=synthetic-bot-value\n")
        if kind == "permissions":
            _grant_other_read(candidate)
        else:
            secure_secret_permissions(candidate)
            os.link(candidate, root / "checks/bot-alias.env")

    with pytest.raises(ConfigurationError, match="Invalid schema v2 application extra environment"):
        load_configuration(project, "stage")


def test_extra_env_file_values_are_redacted_checksummed_and_mounted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, root = _v2_project(tmp_path, monkeypatch)
    _set_extra_env_files(project, [BOT_ENV])
    _, config = load_configuration(project, "stage")
    _write_external_file(config, config.application.env_file, b"APP_ENV=stage\n")
    [extra] = config.application.extra_env_files
    _write_external_file(config, extra.source, b"DB_PASSWORD=synthetic-bot-db-value\n")
    assert config.application.registry_auth_file is not None
    _write_external_file(config, config.application.registry_auth_file, b"{}")

    redactor = Redactor(cli_module._operation_secrets(config))
    assert "synthetic-bot-db-value" not in redactor("failed: synthetic-bot-db-value")
    validate_environment_file(config)

    first, _ = deployment_manifest(config)
    extra.source.write_bytes(b"DB_PASSWORD=rotated-bot-db-value\n")
    second, _ = deployment_manifest(config)
    assert first != second

    runner = AnsibleRunner(project, Redactor([]), external_secret_root=root)
    calls: list[list[str]] = []
    monkeypatch.setattr(runner, "_run", lambda args, **kwargs: calls.append(list(args)))
    monkeypatch.setattr(runner_module, "validate_external_file_for_use", lambda *a, **k: None)
    runner.playbook(
        "deploy.yml",
        project / "inventory.yml",
        {},
        config.server.ssh_key,
        extra_env_files=config.application.extra_env_files,
    )
    assert f"{extra.source}:/run/secrets/app_extra_env/bot.env:ro" in calls[0]


def test_unsafe_secret_permissions_error_explains_cause_without_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, root = _v2_project(tmp_path, monkeypatch)
    sensitive_name = "private/DO-NOT-DISCLOSE-acl"
    _set_sensitive_name(project, "application.env_file", sensitive_name)
    candidate = root / sensitive_name
    _secure_directory(candidate.parent)
    candidate.write_text("APP_ENV=stage\n", encoding="utf-8")
    secure_secret_permissions(candidate)
    _grant_other_read(candidate)

    with pytest.raises(ConfigurationError) as raised:
        load_configuration(project, "stage")

    message = str(raised.value)
    assert message.startswith(
        "Invalid schema v2 application environment: Secret file is not owner-only; fix: "
    )
    assert ("icacls" if os.name == "nt" else "chmod 600") in message
    assert "ansible-deploy secrets path" in message
    assert "DO-NOT-DISCLOSE" not in message
    assert str(tmp_path) not in message
    assert raised.value.__cause__ is None


@pytest.mark.skipif(os.name != "nt", reason="Windows ACL inheritance contract")
def test_windows_secret_created_by_plain_tools_inside_store_is_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, root = _v2_project(tmp_path, monkeypatch)
    _secure_directory(root)
    _, initial = load_configuration(project, "stage")
    # Like Explorer or an editor: no explicit ACL, everything inherits from the root.
    env_file = initial.application.env_file
    env_file.parent.mkdir(parents=True, exist_ok=True)
    env_file.write_text("APP_ENV=stage\n", encoding="utf-8")

    _, config = load_configuration(project, "stage")

    config_module.validate_external_input_for_use(
        config, config.application.env_file, field="application environment"
    )


def test_missing_secret_at_use_time_explains_cause_without_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, root = _v2_project(tmp_path, monkeypatch)
    _secure_directory(root)
    sensitive_name = "private/DO-NOT-DISCLOSE-missing-reason"
    _set_sensitive_name(project, "application.env_file", sensitive_name)
    _, config = load_configuration(project, "stage")

    with pytest.raises(ConfigurationError) as raised:
        config_module.validate_external_input_for_use(
            config, config.application.env_file, field="application environment"
        )

    message = str(raised.value)
    assert message.startswith("Required application environment is unavailable for stage: ")
    assert "missing in the secret store" in message
    assert "ansible-deploy secrets path" in message
    assert "DO-NOT-DISCLOSE" not in message
    assert str(tmp_path) not in message
