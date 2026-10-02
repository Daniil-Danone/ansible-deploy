import socket
from pathlib import Path
from unittest.mock import Mock

from deploy_cli.config import load_configuration
from deploy_cli.workflow import deploy, dns_preflight


def test_deploy_verifies_managed_access_before_hardening(tmp_path: Path) -> None:
    repo = Path(__file__).parents[1]
    global_config, config = load_configuration(repo, "stage")
    key = tmp_path / "id_ed25519"
    public_key = tmp_path / "id_ed25519.pub"
    env = tmp_path / "stage.env"
    key.write_text("private-placeholder", encoding="utf-8")
    public_key.write_text("ssh-ed25519 AAAATEST test", encoding="utf-8")
    env.write_text("APP_ENV=stage", encoding="utf-8")
    config.server.ssh_key = key
    config.server.public_key = public_key
    config.application.env_file = env
    runner = Mock()

    deploy(repo, global_config, config, runner, dry_run=False)

    names = [call.args[0] for call in runner.playbook.call_args_list]
    assert names == [
        "bootstrap.yml",
        "verify_deploy_access.yml",
        "site.yml",
        "health.yml",
    ]
    assert runner.playbook.call_args_list[1].kwargs["exit_code"] == 4
    assert runner.playbook.call_args_list[-1].kwargs["exit_code"] == 7


def test_dry_run_does_not_mutate_bootstrap_access(tmp_path: Path) -> None:
    repo = Path(__file__).parents[1]
    global_config, config = load_configuration(repo, "stage")
    public_key = tmp_path / "id_ed25519.pub"
    env = tmp_path / "stage.env"
    public_key.write_text("ssh-ed25519 AAAATEST test", encoding="utf-8")
    env.write_text("APP_ENV=stage", encoding="utf-8")
    config.server.public_key = public_key
    config.application.env_file = env
    runner = Mock()

    deploy(repo, global_config, config, runner, dry_run=True)

    runner.playbook.assert_called_once()
    assert runner.playbook.call_args.args[0] == "site.yml"
    assert runner.playbook.call_args.kwargs["check"] is True


def test_dns_preflight_resolves_server_hostname(monkeypatch) -> None:
    repo = Path(__file__).parents[1]
    _, config = load_configuration(repo, "stage")
    config.server.host = "vps.example.net"

    def fake_getaddrinfo(host, port, *, type):
        del port, type
        address = "203.0.113.7" if host in {config.domain, config.server.host} else "192.0.2.1"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 0))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)

    dns_preflight(config)


def test_dns_preflight_normalizes_ipv6(monkeypatch) -> None:
    repo = Path(__file__).parents[1]
    _, config = load_configuration(repo, "stage")
    config.server.host = "2001:db8::1"

    def fake_getaddrinfo(host, port, *, type):
        del host, port, type
        return [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2001:0db8:0:0::1", 0, 0, 0))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)

    dns_preflight(config)
