from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_user_config(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Never let a developer's real per-user config.toml influence test results."""
    config_dir: Path = tmp_path_factory.mktemp("user-config")
    monkeypatch.setenv("ANSIBLE_DEPLOY_CONFIG", str(config_dir / "config.toml"))
