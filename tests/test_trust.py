import shutil
from pathlib import Path

import pytest
import yaml

from deploy_cli import cli
from deploy_cli.runner import AnsibleRunner, RunnerError, ScannedHostKey
from deploy_cli.secret_file import secure_secret_permissions

DEMO_FINGERPRINT = "SHA256:" + "A" * 43
SCANNED_FINGERPRINT = "SHA256:" + "b" * 43
SECOND_FINGERPRINT = "SHA256:" + "c" * 43


@pytest.fixture(autouse=True)
def _external_secret_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    trusted_base = tmp_path / "trusted external base"
    trusted_base.mkdir()
    secure_secret_permissions(trusted_base)
    monkeypatch.setenv("ANSIBLE_DEPLOY_SECRETS_DIR", str(trusted_base / "project secrets"))


def _scanned(*fingerprints: str) -> tuple[ScannedHostKey, ...]:
    return tuple(
        ScannedHostKey(
            line=f"192.0.2.10 ssh-ed25519 blob{index}",
            key_type="ssh-ed25519",
            fingerprint=fingerprint,
        )
        for index, fingerprint in enumerate(fingerprints)
    )


@pytest.fixture
def stub_scan(monkeypatch: pytest.MonkeyPatch):
    def install(*fingerprints: str) -> None:
        monkeypatch.setattr(AnsibleRunner, "build_image", lambda self: None)
        monkeypatch.setattr(
            AnsibleRunner,
            "scan_host_keys",
            lambda self, host, port: _scanned(*fingerprints),
        )

    return install


def _demo_project(tmp_path: Path) -> Path:
    source = Path(__file__).parents[1] / "examples/demo-app"
    project = tmp_path / "application"
    shutil.copytree(source, project, ignore=shutil.ignore_patterns(".deploy-state"))
    return project


def _initialized_project(tmp_path: Path) -> Path:
    project = tmp_path / "scaffold"
    project.mkdir()
    assert cli.run(["--project-dir", str(project), "project", "init"]) == 0
    return project


def _stage_config(project: Path) -> Path:
    return project / ".deploy/environments/stage/config.yml"


def _fingerprints(path: Path) -> list[str]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    return list(raw["server"]["host_key_fingerprints"])


def test_trust_writes_scanned_fingerprints_and_keeps_the_rest_of_the_file(
    tmp_path: Path, stub_scan, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _initialized_project(tmp_path)
    path = _stage_config(project)
    before = path.read_text(encoding="utf-8")
    stub_scan(SECOND_FINGERPRINT, SCANNED_FINGERPRINT)

    assert cli.run(["--project-dir", str(project), "trust", "stage"]) == 0

    after = path.read_text(encoding="utf-8")
    assert _fingerprints(path) == sorted([SCANNED_FINGERPRINT, SECOND_FINGERPRINT])
    head, _, tail = before.partition("  host_key_fingerprints: []\n")
    assert after.startswith(head)
    assert after.endswith(tail)
    assert "# Filled by `ansible-deploy trust stage`" in after
    output = capsys.readouterr().out
    assert f"[FINGERPRINT] ssh-ed25519 {SCANNED_FINGERPRINT}" in output


def test_trust_inserts_the_block_when_the_key_is_absent(tmp_path: Path, stub_scan) -> None:
    project = _initialized_project(tmp_path)
    path = _stage_config(project)
    kept = [
        line
        for line in path.read_text(encoding="utf-8").splitlines(keepends=True)
        if "host_key_fingerprints" not in line
    ]
    path.write_text("".join(kept), encoding="utf-8")
    stub_scan(SCANNED_FINGERPRINT)

    assert cli.run(["--project-dir", str(project), "trust", "stage"]) == 0

    assert _fingerprints(path) == [SCANNED_FINGERPRINT]
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert raw["application"]["remote_dir"] == "/srv/myapp-stage"
    assert raw["server"]["ssh_key"] == "keys/stage_ed25519"


def test_changed_host_key_is_rejected_without_force(
    tmp_path: Path, stub_scan, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _demo_project(tmp_path)
    path = _stage_config(project)
    before = path.read_bytes()
    stub_scan(SCANNED_FINGERPRINT)

    assert cli.run(["--project-dir", str(project), "trust", "stage"]) == 2

    assert path.read_bytes() == before
    assert "--force" in capsys.readouterr().err


def test_force_overwrites_a_changed_host_key(tmp_path: Path, stub_scan) -> None:
    project = _demo_project(tmp_path)
    path = _stage_config(project)
    stub_scan(SCANNED_FINGERPRINT)

    assert cli.run(["--project-dir", str(project), "trust", "stage", "--force"]) == 0

    assert _fingerprints(path) == [SCANNED_FINGERPRINT]


def test_print_never_modifies_the_configuration(
    tmp_path: Path, stub_scan, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _demo_project(tmp_path)
    path = _stage_config(project)
    before = path.read_bytes()
    stub_scan(SCANNED_FINGERPRINT)

    assert cli.run(["--project-dir", str(project), "trust", "stage", "--print"]) == 0

    assert path.read_bytes() == before
    output = capsys.readouterr().out
    assert SCANNED_FINGERPRINT in output
    assert "was not modified" in output


def test_already_trusted_host_key_reports_no_change(
    tmp_path: Path, stub_scan, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _demo_project(tmp_path)
    path = _stage_config(project)
    before = path.read_bytes()
    stub_scan(DEMO_FINGERPRINT)

    assert cli.run(["--project-dir", str(project), "trust", "stage"]) == 0

    assert path.read_bytes() == before
    assert "unchanged" in capsys.readouterr().out


def test_untrusted_environment_fails_before_contacting_the_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(self: AnsibleRunner, host: str, port: int) -> tuple[ScannedHostKey, ...]:
        pytest.fail("the host was scanned before its fingerprints were trusted")

    monkeypatch.setattr(AnsibleRunner, "scan_host_keys", forbidden)
    runner = AnsibleRunner(tmp_path, cli.Redactor([]), environment="stage")

    with pytest.raises(RunnerError, match="ansible-deploy trust stage") as raised:
        runner.trust_host("example.com", 22, [])

    assert raised.value.exit_code == 2
    assert not (tmp_path / ".deploy-state").exists()
