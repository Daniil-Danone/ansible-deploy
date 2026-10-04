import os
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
import yaml

from deploy_cli import keys
from deploy_cli.config import ConfigurationError, load_configuration
from deploy_cli.keys import ensure_deploy_key

pytestmark = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="OpenSSH is unavailable")


def _config(tmp_path: Path):
    _, config = load_configuration(Path(__file__).parents[1], "stage")
    config.server.ssh_key = tmp_path / "private" / "deploy-key"
    config.server.public_key = tmp_path / "public" / "deploy-key.pub"
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
