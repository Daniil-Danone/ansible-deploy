import base64
import hashlib
import subprocess
from pathlib import Path

import pytest

from deploy_cli.redaction import Redactor
from deploy_cli.runner import AnsibleRunner, RunnerError, _fingerprint


def _host_key(raw: bytes) -> tuple[str, str]:
    encoded = base64.b64encode(raw).decode("ascii")
    line = f"example.com ssh-ed25519 {encoded}"
    digest = base64.b64encode(hashlib.sha256(raw).digest()).decode("ascii").rstrip("=")
    return line, f"SHA256:{digest}"


def test_openssh_fingerprint_is_calculated_from_key_blob() -> None:
    line, expected = _host_key(b"test-public-key-blob")

    assert _fingerprint(line) == expected


def test_changed_host_key_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    old_line, _ = _host_key(b"old-key")
    new_line, new_fingerprint = _host_key(b"new-key")
    state = tmp_path / ".deploy-state/stage"
    state.mkdir(parents=True)
    (state / "known_hosts").write_text(old_line + "\n", encoding="utf-8")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, new_line + "\n", ""),
    )
    runner = AnsibleRunner(tmp_path, Redactor([]))

    with pytest.raises(RunnerError, match="changed") as raised:
        runner.trust_host("example.com", 22, [new_fingerprint])

    assert raised.value.exit_code == 3


def test_host_trust_is_namespaced_by_environment(tmp_path: Path) -> None:
    stage = AnsibleRunner(tmp_path, Redactor([]), environment="stage")
    prod = AnsibleRunner(tmp_path, Redactor([]), environment="prod")

    assert stage.state_dir != prod.state_dir
    assert stage.state_dir == tmp_path / ".deploy-state/stage"
    assert prod.state_dir == tmp_path / ".deploy-state/prod"


def test_unreachable_ssh_scan_uses_ssh_exit_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 1, "", "timeout"),
    )
    runner = AnsibleRunner(tmp_path, Redactor([]))

    with pytest.raises(RunnerError, match="scan failed") as raised:
        runner.trust_host("example.com", 22, ["SHA256:" + "A" * 43])

    assert raised.value.exit_code == 4
