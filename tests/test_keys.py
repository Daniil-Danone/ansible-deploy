import os
import shutil
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
import yaml

from deploy_cli import keys
from deploy_cli.config import ConfigurationError, load_configuration
from deploy_cli.keys import ensure_deploy_key
from deploy_cli.secret_file import secure_secret_permissions

pytestmark = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="OpenSSH is unavailable")


def _config(tmp_path: Path):
    _, config = load_configuration(Path(__file__).parents[1], "stage")
    config.server.ssh_key = tmp_path / "private" / "deploy-key"
    config.server.public_key = tmp_path / "public" / "deploy-key.pub"
    return config


def _external_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    project = tmp_path / "project"
    shutil.copytree(Path(__file__).parents[1] / "examples/demo-app", project)
    trusted_base = tmp_path / "external-store"
    trusted_base.mkdir()
    secure_secret_permissions(trusted_base)
    monkeypatch.setenv(
        "ANSIBLE_DEPLOY_SECRETS_DIR", str(trusted_base / "project-secrets")
    )
    _, config = load_configuration(project, "stage")
    return config


def test_missing_deploy_key_pair_is_created_once(tmp_path: Path) -> None:
    config = _config(tmp_path)

    message = ensure_deploy_key(config)
    first_private = config.server.ssh_key.read_bytes()
    second_message = ensure_deploy_key(config)

    assert "Created deploy key" in message
    assert config.server.public_key.read_text(encoding="utf-8").startswith("ssh-ed25519 ")
    assert config.server.ssh_key.read_bytes() == first_private
    assert "Using existing deploy key" in second_message


def test_write_exclusive_fsync_failure_removes_partially_written_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "partial-key"
    monkeypatch.setattr(
        keys.os,
        "fsync",
        lambda descriptor: (_ for _ in ()).throw(OSError("synthetic fsync failure")),
    )

    with pytest.raises(ConfigurationError, match="partial file was removed"):
        keys._write_exclusive(destination, b"partially-written-key", 0o600)

    assert not destination.exists()


def test_write_exclusive_reports_partial_file_rollback_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "partial-key"
    monkeypatch.setattr(
        keys.os,
        "fsync",
        lambda descriptor: (_ for _ in ()).throw(OSError("synthetic fsync failure")),
    )
    original_unlink = Path.unlink

    def failing_unlink(target: Path, *args, **kwargs):
        if target == destination:
            raise OSError("DO-NOT-DISCLOSE-key-path")
        return original_unlink(target, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", failing_unlink)
    with pytest.raises(ConfigurationError, match="rollback") as raised:
        keys._write_exclusive(destination, b"partially-written-key", 0o600)

    assert raised.value.__cause__ is None
    assert "DO-NOT-DISCLOSE" not in str(raised.value)
    assert destination.exists()


def test_write_exclusive_never_removes_an_existing_file(tmp_path: Path) -> None:
    destination = tmp_path / "existing-key"
    destination.write_bytes(b"foreign-existing-content")

    with pytest.raises(ConfigurationError, match="Refusing to replace"):
        keys._write_exclusive(destination, b"replacement", 0o600)

    assert destination.read_bytes() == b"foreign-existing-content"


@pytest.mark.parametrize("fault", ["partial-write", "flush", "close"])
def test_write_exclusive_removes_partial_file_for_stream_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    destination = tmp_path / "partial-key"
    original_fdopen = os.fdopen
    marker = f"DO-NOT-DISCLOSE-{fault}"

    class FaultingStream:
        def __init__(self, descriptor: int) -> None:
            self.wrapped = original_fdopen(descriptor, "wb")

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            self.wrapped.close()
            if fault == "close":
                raise OSError(marker)
            return False

        def write(self, content: bytes) -> int:
            if fault == "partial-write":
                self.wrapped.write(content[:4])
                self.wrapped.flush()
                raise OSError(marker)
            return self.wrapped.write(content)

        def flush(self) -> None:
            self.wrapped.flush()
            if fault == "flush":
                raise OSError(marker)

        def fileno(self) -> int:
            return self.wrapped.fileno()

    monkeypatch.setattr(keys.os, "fdopen", lambda descriptor, mode: FaultingStream(descriptor))

    with pytest.raises(ConfigurationError) as raised:
        keys._write_exclusive(destination, b"partially-written-key", 0o600)

    assert not destination.exists()
    assert marker not in "".join(traceback.format_exception(raised.value))


@pytest.mark.parametrize("failed_label", ["private key", "public key"])
def test_pair_rollback_reports_each_cleanup_failure_and_continues_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failed_label: str
) -> None:
    paths = {
        "private key": tmp_path / "private",
        "public key": tmp_path / "public",
    }
    for path in paths.values():
        path.write_bytes(b"partial")
    marker = "DO-NOT-DISCLOSE-pair-cleanup"
    original_unlink = Path.unlink

    def failing_unlink(target: Path, *args, **kwargs):
        if target == paths[failed_label]:
            raise OSError(marker)
        return original_unlink(target, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", failing_unlink)
    with pytest.raises(ConfigurationError, match=f"partial {failed_label}") as raised:
        keys._rollback_published_keys([(path, label) for label, path in paths.items()])

    assert paths[failed_label].exists()
    other_label = "public key" if failed_label == "private key" else "private key"
    assert not paths[other_label].exists()
    assert marker not in "".join(traceback.format_exception(raised.value))


def test_pair_rollback_combines_nested_public_and_private_cleanup_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    private_key = tmp_path / "private"
    private_key.write_bytes(b"partial-private")
    monkeypatch.setattr(
        Path,
        "unlink",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            OSError("DO-NOT-DISCLOSE-private-cleanup")
        ),
    )

    with pytest.raises(ConfigurationError) as raised:
        keys._rollback_published_keys(
            [(private_key, "private key")],
            failure=keys._KeyRollbackFailure({"public key"}),
        )

    rendered = "".join(traceback.format_exception(raised.value))
    assert "private key" in str(raised.value)
    assert "public key" in str(raised.value)
    assert "DO-NOT-DISCLOSE" not in rendered
    assert private_key.exists()


def test_public_wrapper_preserves_safe_public_key_rollback_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _external_config(tmp_path, monkeypatch)
    ensure_deploy_key(config)
    public_key = config.server.public_key
    public_key.unlink()
    marker = "DO-NOT-DISCLOSE-public-cleanup"
    original_unlink = Path.unlink
    monkeypatch.setattr(
        keys.os,
        "fsync",
        lambda descriptor: (_ for _ in ()).throw(OSError("sensitive-fsync-value")),
    )

    def failing_unlink(target: Path, *args, **kwargs):
        if target == public_key:
            raise OSError(marker)
        return original_unlink(target, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", failing_unlink)
    with pytest.raises(ConfigurationError, match="partial public key may remain") as raised:
        ensure_deploy_key(config)

    rendered = "".join(traceback.format_exception(raised.value))
    assert raised.value.__cause__ is None
    assert marker not in rendered
    assert "sensitive-fsync-value" not in rendered
    assert str(public_key) not in rendered
    assert public_key.exists()


def test_public_key_is_restored_from_existing_private_key(tmp_path: Path) -> None:
    config = _config(tmp_path)
    ensure_deploy_key(config)
    expected = config.server.public_key.read_text(encoding="utf-8").split()[:2]
    config.server.public_key.unlink()

    message = ensure_deploy_key(config)

    assert "Restored deploy public key" in message
    assert config.server.public_key.read_text(encoding="utf-8").split()[:2] == expected


def test_public_key_without_private_key_is_rejected_without_overwrite(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.server.public_key.parent.mkdir(parents=True)
    config.server.public_key.write_text("ssh-ed25519 existing\n", encoding="utf-8")

    with pytest.raises(ConfigurationError, match="private key is missing"):
        ensure_deploy_key(config)

    assert config.server.public_key.read_text(encoding="utf-8") == "ssh-ed25519 existing\n"
    assert not config.server.ssh_key.exists()


def test_same_key_paths_are_rejected_before_creating_parent(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.server.public_key = config.server.ssh_key

    with pytest.raises(ConfigurationError, match="must be different"):
        ensure_deploy_key(config)

    assert not config.server.ssh_key.parent.exists()


def test_symlink_key_destination_is_rejected(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.server.ssh_key.parent.mkdir(parents=True)
    target = tmp_path / "target"
    target.write_text("do-not-touch", encoding="utf-8")
    try:
        config.server.ssh_key.symlink_to(target)
    except OSError:
        pytest.skip("symlink creation is unavailable")

    with pytest.raises(ConfigurationError, match="symlink"):
        ensure_deploy_key(config)

    assert target.read_text(encoding="utf-8") == "do-not-touch"


def test_reported_symlink_is_rejected_before_side_effects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    original = Path.is_symlink
    monkeypatch.setattr(
        Path,
        "is_symlink",
        lambda path: path == config.server.ssh_key or original(path),
    )

    with pytest.raises(ConfigurationError, match="symlink"):
        ensure_deploy_key(config)

    assert not config.server.ssh_key.parent.exists()


def test_yaml_key_path_keeps_symlink_evidence_until_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = Path(__file__).parents[1]
    (tmp_path / "config").mkdir()
    environment_dir = tmp_path / "environments/stage"
    environment_dir.mkdir(parents=True)
    (tmp_path / "config/global.yml").write_text(
        (source / "config/global.yml").read_text(encoding="utf-8"), encoding="utf-8"
    )
    raw = yaml.safe_load((source / "environments/stage/config.yml").read_text(encoding="utf-8"))
    configured_key = tmp_path / "linked-key"
    raw["server"]["ssh_key"] = str(configured_key)
    raw["server"]["public_key"] = str(tmp_path / "key.pub")
    (environment_dir / "config.yml").write_text(yaml.safe_dump(raw), encoding="utf-8")
    _, config = load_configuration(tmp_path, "stage")
    assert config.server.ssh_key == configured_key

    original = Path.is_symlink
    monkeypatch.setattr(
        Path,
        "is_symlink",
        lambda path: path == configured_key or original(path),
    )
    with pytest.raises(ConfigurationError, match="cannot contain a symlink"):
        ensure_deploy_key(config)


def test_hardlinked_key_paths_are_rejected(tmp_path: Path) -> None:
    config = _config(tmp_path)
    ensure_deploy_key(config)
    config.server.public_key.unlink()
    os.link(config.server.ssh_key, config.server.public_key)

    with pytest.raises(ConfigurationError, match="alias the same file"):
        ensure_deploy_key(config)


def test_concurrent_key_creation_publishes_one_unchanged_pair(tmp_path: Path) -> None:
    config = _config(tmp_path)
    ready = threading.Barrier(2)

    def create(_: int) -> str:
        ready.wait()
        return ensure_deploy_key(config)

    with ThreadPoolExecutor(max_workers=2) as pool:
        messages = list(pool.map(create, range(2)))

    assert sum("Created deploy key" in message for message in messages) == 1
    assert sum("Using existing deploy key" in message for message in messages) == 1
    original_private = config.server.ssh_key.read_bytes()
    ensure_deploy_key(config)
    assert config.server.ssh_key.read_bytes() == original_private


def test_pair_lock_thread_stress_serializes_before_acl_validation(tmp_path: Path) -> None:
    workers = 8
    for iteration in range(12):
        directory = tmp_path / str(iteration)
        directory.mkdir()
        lock_path = directory / "pair.lock"
        barrier = threading.Barrier(workers)
        active = 0
        maximum_active = 0
        guard = threading.Lock()

        def acquire(
            _: int,
            barrier: threading.Barrier = barrier,
            lock_path: Path = lock_path,
            guard: threading.Lock = guard,
        ) -> None:
            nonlocal active, maximum_active
            barrier.wait()
            with keys._pair_lock(lock_path):
                with guard:
                    active += 1
                    maximum_active = max(maximum_active, active)
                sum(range(2000))
                with guard:
                    active -= 1

        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(acquire, range(workers)))

        assert maximum_active == 1
