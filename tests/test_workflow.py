import socket
from pathlib import Path
from unittest.mock import Mock

import pytest

from deploy_cli.config import load_configuration
from deploy_cli.models import BackupConfig
from deploy_cli.project import sync_project
from deploy_cli.runner import RunnerError
from deploy_cli.workflow import (
    backup_operation,
    deploy,
    deploy_collector,
    deploy_monitoring,
    deployment_manifest,
    dns_preflight,
    restore_backup,
    write_inventory,
)


def test_repeat_deploy_uses_only_managed_access(tmp_path: Path) -> None:
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
        "verify_deploy_access.yml",
        "guard_environment.yml",
        "abort_release.yml",
        "site.yml",
        "health.yml",
        "finalize_release.yml",
    ]
    assert runner.playbook.call_args_list[0].args[1].name == "managed.yml"
    assert runner.playbook.call_args_list[4].kwargs["exit_code"] == 7
    assert runner.playbook.call_args_list[3].args[2]["app_compose_project"] == "myapp"


def test_restore_prepares_target_before_import_and_rechecks_health(tmp_path: Path) -> None:
    repo = tmp_path / "project"
    repo.mkdir()
    sync_project(repo)
    global_config, source = load_configuration(repo, "prod")
    _, target = load_configuration(repo, "restore")
    source.backup = BackupConfig.model_validate(
        {
            "remote": "drive:backups",
            "credentials_file": str(tmp_path / "rclone.conf"),
            "age_identity_file": str(tmp_path / "age.key"),
            "age_recipient": "age1" + "a" * 58,
            "include": [
                {
                    "type": "directory",
                    "path": "/srv/myapp-prod/shared/uploads",
                    "restore_destination": "shared/uploads",
                }
            ],
        }
    )
    target_env = tmp_path / "restore.env"
    target_env.write_text("APP_ENV=restore\n", encoding="utf-8")
    target.schema_version = 1
    target.application.env_file = target_env
    target.application.registry_auth_file = None
    runner = Mock()

    restore_backup(
        repo,
        global_config,
        source,
        target,
        runner,
        backup_id="2026-10-05T010203Z-0123456789abcdef",
    )

    assert [call.args[0] for call in runner.playbook.call_args_list] == [
        "verify_deploy_access.yml",
        "guard_environment.yml",
        "restore_prepare.yml",
        "guard_environment.yml",
        "backup_restore.yml",
        "health.yml",
    ]
    names = [call.args[0] for call in runner.playbook.call_args_list]
    assert names.index("health.yml") > names.index("backup_restore.yml")
    restore_call = runner.playbook.call_args_list[4]
    include = restore_call.args[2]["backup_runtime_config"]["include"]
    assert include == [
        {
            "type": "directory",
            "path": "/srv/myapp-prod/shared/uploads",
            "restore_destination": "shared/uploads",
        }
    ]


def test_restore_bootstraps_pristine_target_before_application_and_import(tmp_path: Path) -> None:
    repo = tmp_path / "project"
    repo.mkdir()
    sync_project(repo)
    global_config, source = load_configuration(repo, "prod")
    _, target = load_configuration(repo, "restore")
    source.backup = BackupConfig.model_validate(
        {
            "remote": "drive:backups",
            "credentials_file": str(tmp_path / "rclone.conf"),
            "age_identity_file": str(tmp_path / "age.key"),
            "age_recipient": "age1" + "a" * 58,
            "include": [{"type": "postgres", "service": "db", "database": "app", "user": "app"}],
        }
    )
    target_env = tmp_path / "restore.env"
    target_env.write_text("APP_ENV=restore\n", encoding="utf-8")
    target.schema_version = 1
    target.application.env_file = target_env
    target.application.registry_auth_file = None
    runner = Mock()
    managed_failed = False

    def fail_first_managed(name, inventory, *args, **kwargs):
        nonlocal managed_failed
        if (
            name == "verify_deploy_access.yml"
            and inventory.name == "managed.yml"
            and not managed_failed
        ):
            managed_failed = True
            raise RunnerError("not bootstrapped", 4)

    runner.playbook.side_effect = fail_first_managed

    restore_backup(
        repo,
        global_config,
        source,
        target,
        runner,
        backup_id="2026-10-05T010203Z-0123456789abcdef",
        bootstrap_password="temporary-password",  # noqa: S106 - synthetic test value
    )

    calls = runner.playbook.call_args_list
    assert [call.args[0] for call in calls[:5]] == [
        "verify_deploy_access.yml",
        "verify_deploy_access.yml",
        "guard_environment.yml",
        "bootstrap.yml",
        "verify_deploy_access.yml",
    ]
    assert [call.kwargs.get("bootstrap_password") for call in calls[:5]] == [
        None,
        "temporary-password",
        "temporary-password",
        "temporary-password",
        None,
    ]
    assert [call.args[0] for call in calls[-3:]] == [
        "guard_environment.yml",
        "backup_restore.yml",
        "health.yml",
    ]


def test_restore_rejects_unsafe_backup_id_before_runner_calls() -> None:
    repo = Path(__file__).parents[1]
    global_config, source = load_configuration(repo, "prod")
    _, target_raw = load_configuration(repo, "stage")
    target_raw.environment = "restore"
    target_raw.source_environment = "prod"
    source.backup = Mock()
    runner = Mock()

    with pytest.raises(RunnerError, match="identifier is invalid"):
        restore_backup(
            repo,
            global_config,
            source,
            target_raw,
            runner,
            backup_id="../../production",
        )

    runner.assert_not_called()
    runner.playbook.assert_not_called()


@pytest.mark.parametrize("action", ["run", "list"])
def test_backup_operations_converge_runtime_and_credentials(
    tmp_path: Path, action: str
) -> None:
    repo = Path(__file__).parents[1]
    global_config, config = load_configuration(repo, "prod")
    credentials = tmp_path / "rclone.conf"
    config.backup = BackupConfig.model_validate(
        {
            "remote": "drive:backups",
            "credentials_file": str(credentials),
            "age_identity_file": str(tmp_path / "age.key"),
            "age_recipient": "age1" + "a" * 58,
            "include": [
                {
                    "type": "file",
                    "path": "/srv/myapp-prod/data",
                    "restore_destination": "data",
                }
            ],
        }
    )
    runner = Mock()

    backup_operation(repo, global_config, config, runner, action=action)

    backup_call = runner.playbook.call_args_list[-1]
    assert backup_call.args[0] == "backup.yml"
    assert backup_call.kwargs["backup_credentials_file"] == credentials
    assert backup_call.args[2]["backup_action"] == action


def test_production_uses_separate_compose_project_name(tmp_path: Path) -> None:
    repo = Path(__file__).parents[1]
    global_config, config = load_configuration(repo, "prod")
    env = tmp_path / "prod.env"
    registry = tmp_path / "registry.json"
    key = tmp_path / "key"
    public_key = tmp_path / "key.pub"
    env.write_text("APP_ENV=prod\n", encoding="utf-8")
    registry.write_text('{"auths": {}}', encoding="utf-8")
    key.write_text("key", encoding="utf-8")
    public_key.write_text("public", encoding="utf-8")
    config.application.env_file = env
    config.application.registry_auth_file = registry
    config.server.ssh_key = key
    config.server.public_key = public_key
    runner = Mock()

    deploy(repo, global_config, config, runner, dry_run=True)

    site = next(call for call in runner.playbook.call_args_list if call.args[0] == "site.yml")
    assert site.args[2]["app_compose_project"] == "myapp_prod"


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

    assert [call.args[0] for call in runner.playbook.call_args_list] == [
        "verify_deploy_access.yml",
        "guard_environment.yml",
        "site.yml",
    ]
    assert runner.playbook.call_args_list[0].kwargs.get("check") is None
    assert all(
        call.kwargs["check"] is True for call in runner.playbook.call_args_list[1:]
    )


def test_password_is_used_only_for_bootstrap_connection(tmp_path: Path) -> None:
    repo = Path(__file__).parents[1]
    global_config, config = load_configuration(repo, "stage")
    env = tmp_path / "stage.env"
    key = tmp_path / "key"
    public_key = tmp_path / "key.pub"
    env.write_text("APP_ENV=stage\n", encoding="utf-8")
    key.write_text("key", encoding="utf-8")
    public_key.write_text("public", encoding="utf-8")
    config.application.env_file = env
    config.server.ssh_key = key
    config.server.public_key = public_key
    runner = Mock()

    managed_probe_failed = False

    def first_managed_probe_fails(name, inventory, *args, **kwargs):
        nonlocal managed_probe_failed
        if (
            name == "verify_deploy_access.yml"
            and inventory.name == "managed.yml"
            and not managed_probe_failed
        ):
            managed_probe_failed = True
            raise RunnerError("deploy account is not installed yet", 3)

    runner.playbook.side_effect = first_managed_probe_fails

    deploy(
        repo,
        global_config,
        config,
        runner,
        dry_run=False,
        bootstrap_password="root-password",  # noqa: S106 - synthetic test value
    )

    calls = runner.playbook.call_args_list
    assert [call.kwargs.get("bootstrap_password") for call in calls[:5]] == [
        None,
        "root-password",
        "root-password",
        "root-password",
        None,
    ]
    assert all(call.kwargs.get("bootstrap_password") is None for call in calls[5:])
    assert [call.args[0] for call in calls[:5]] == [
        "verify_deploy_access.yml",
        "verify_deploy_access.yml",
        "guard_environment.yml",
        "bootstrap.yml",
        "verify_deploy_access.yml",
    ]


def test_repeat_deploy_never_falls_back_to_root_even_if_password_was_supplied(
    tmp_path: Path,
) -> None:
    repo = Path(__file__).parents[1]
    global_config, config = load_configuration(repo, "stage")
    env = tmp_path / "stage.env"
    key = tmp_path / "key"
    public_key = tmp_path / "key.pub"
    env.write_text("APP_ENV=stage\n", encoding="utf-8")
    key.write_text("key", encoding="utf-8")
    public_key.write_text("public", encoding="utf-8")
    config.application.env_file = env
    config.server.ssh_key = key
    config.server.public_key = public_key
    runner = Mock()

    deploy(
        repo,
        global_config,
        config,
        runner,
        dry_run=False,
        bootstrap_password="unused-root-password",  # noqa: S106 - synthetic test value
    )

    assert all(call.args[1].name == "managed.yml" for call in runner.playbook.call_args_list)
    assert all(
        call.kwargs.get("bootstrap_password") is None
        for call in runner.playbook.call_args_list
    )


def test_pre_authorized_bootstrap_key_supports_first_deploy(tmp_path: Path) -> None:
    repo = Path(__file__).parents[1]
    global_config, config = load_configuration(repo, "stage")
    env = tmp_path / "stage.env"
    env.write_text("APP_ENV=stage\n", encoding="utf-8")
    config.application.env_file = env
    runner = Mock()
    managed_probe_failed = False

    def first_managed_probe_fails(name, inventory, *args, **kwargs):
        nonlocal managed_probe_failed
        if inventory.name == "managed.yml" and not managed_probe_failed:
            managed_probe_failed = True
            raise RunnerError("deploy account is not installed yet", 4)

    runner.playbook.side_effect = first_managed_probe_fails

    deploy(repo, global_config, config, runner, dry_run=False)

    calls = runner.playbook.call_args_list
    assert [call.args[0] for call in calls[:5]] == [
        "verify_deploy_access.yml",
        "verify_deploy_access.yml",
        "guard_environment.yml",
        "bootstrap.yml",
        "verify_deploy_access.yml",
    ]
    assert [call.args[1].name for call in calls[:5]] == [
        "managed.yml",
        "bootstrap.yml",
        "bootstrap.yml",
        "bootstrap.yml",
        "managed.yml",
    ]
    assert all(call.kwargs.get("bootstrap_password") is None for call in calls)


def test_dry_run_without_managed_access_fails_without_bootstrap_probe(tmp_path: Path) -> None:
    repo = Path(__file__).parents[1]
    global_config, config = load_configuration(repo, "stage")
    env = tmp_path / "stage.env"
    env.write_text("APP_ENV=stage\n", encoding="utf-8")
    config.application.env_file = env
    runner = Mock()
    runner.playbook.side_effect = RunnerError("publickey denied", 4)

    with pytest.raises(RunnerError, match="pristine server.*bootstrap SSH key") as raised:
        deploy(repo, global_config, config, runner, dry_run=True)

    assert raised.value.exit_code == 4
    assert len(runner.playbook.call_args_list) == 1
    assert runner.playbook.call_args.args[1].name == "managed.yml"


def test_unreachable_managed_and_bootstrap_access_fails_actionably(tmp_path: Path) -> None:
    repo = Path(__file__).parents[1]
    global_config, config = load_configuration(repo, "stage")
    env = tmp_path / "stage.env"
    env.write_text("APP_ENV=stage\n", encoding="utf-8")
    config.application.env_file = env
    runner = Mock()
    runner.playbook.side_effect = RunnerError("publickey denied", 4)

    with pytest.raises(RunnerError, match="bootstrap SSH access also failed") as raised:
        deploy(repo, global_config, config, runner, dry_run=False)

    assert raised.value.exit_code == 4
    assert [call.args[1].name for call in runner.playbook.call_args_list] == [
        "managed.yml",
        "bootstrap.yml",
    ]


def test_managed_identity_mismatch_never_falls_back_to_bootstrap(tmp_path: Path) -> None:
    repo = Path(__file__).parents[1]
    global_config, config = load_configuration(repo, "stage")
    env = tmp_path / "stage.env"
    env.write_text("APP_ENV=stage\n", encoding="utf-8")
    config.application.env_file = env
    runner = Mock()

    def reject_identity(name, *args, **kwargs):
        if name == "guard_environment.yml":
            raise RunnerError("identity mismatch", 3)

    runner.playbook.side_effect = reject_identity

    with pytest.raises(RunnerError, match="identity mismatch"):
        deploy(repo, global_config, config, runner, dry_run=False)

    assert [call.args[0] for call in runner.playbook.call_args_list] == [
        "verify_deploy_access.yml",
        "guard_environment.yml",
    ]
    assert all(call.args[1].name == "managed.yml" for call in runner.playbook.call_args_list)


def test_claimed_host_with_broken_managed_access_is_never_rebootstrapped(
    tmp_path: Path,
) -> None:
    repo = Path(__file__).parents[1]
    global_config, config = load_configuration(repo, "stage")
    env = tmp_path / "stage.env"
    env.write_text("APP_ENV=stage\n", encoding="utf-8")
    config.application.env_file = env
    runner = Mock()

    def reject_claimed_bootstrap(name, inventory, variables, *args, **kwargs):
        if name == "verify_deploy_access.yml" and inventory.name == "managed.yml":
            raise RunnerError("managed key denied", 4)
        if name == "guard_environment.yml" and inventory.name == "bootstrap.yml":
            assert variables["require_unclaimed_environment"] is True
            raise RunnerError("host already claimed", 3)

    runner.playbook.side_effect = reject_claimed_bootstrap

    with pytest.raises(RunnerError, match="host already claimed"):
        deploy(
            repo,
            global_config,
            config,
            runner,
            dry_run=False,
            bootstrap_password="root-password",  # noqa: S106 - synthetic test value
        )

    assert [call.args[0] for call in runner.playbook.call_args_list] == [
        "verify_deploy_access.yml",
        "verify_deploy_access.yml",
        "guard_environment.yml",
    ]


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


def test_inventories_are_namespaced_by_environment(tmp_path: Path) -> None:
    repo = Path(__file__).parents[1]
    _, stage = load_configuration(repo, "stage")
    _, prod = load_configuration(repo, "prod")

    stage_path = write_inventory(tmp_path, stage, bootstrap=False)
    prod_path = write_inventory(tmp_path, prod, bootstrap=False)

    assert stage_path == tmp_path / ".deploy-state/stage/managed.yml"
    assert prod_path == tmp_path / ".deploy-state/prod/managed.yml"


def test_deployment_checksum_covers_secret_and_compose_inputs(tmp_path: Path) -> None:
    repo = Path(__file__).parents[1]
    _, config = load_configuration(repo, "stage")
    env = tmp_path / "stage.env"
    env.write_text("APP_ENV=stage\nVALUE=one\n", encoding="utf-8")
    config.application.env_file = env
    first, images = deployment_manifest(config)
    env.write_text("APP_ENV=stage\nVALUE=two\n", encoding="utf-8")
    second, second_images = deployment_manifest(config)

    assert first != second
    assert images == second_images
    assert all("@sha256:" in image for image in images)


def test_failed_public_health_runs_abort_and_keeps_original_exit_code(tmp_path: Path) -> None:
    repo = Path(__file__).parents[1]
    global_config, config = load_configuration(repo, "stage")
    env = tmp_path / "stage.env"
    key = tmp_path / "key"
    public_key = tmp_path / "key.pub"
    env.write_text("APP_ENV=stage\n", encoding="utf-8")
    key.write_text("key", encoding="utf-8")
    public_key.write_text("public", encoding="utf-8")
    config.application.env_file = env
    config.server.ssh_key = key
    config.server.public_key = public_key
    runner = Mock()

    def playbook(name, *args, **kwargs):
        if name == "health.yml":
            raise RunnerError("public health failed", 7)

    runner.playbook.side_effect = playbook

    with pytest.raises(RunnerError) as raised:
        deploy(repo, global_config, config, runner, dry_run=False)

    assert raised.value.exit_code == 7
    names = [call.args[0] for call in runner.playbook.call_args_list]
    assert names[-2:] == ["health.yml", "abort_release.yml"]


def test_recovery_failure_keeps_original_exit_code_and_reports_both(tmp_path: Path) -> None:
    repo = Path(__file__).parents[1]
    global_config, config = load_configuration(repo, "stage")
    env = tmp_path / "stage.env"
    key = tmp_path / "key"
    public_key = tmp_path / "key.pub"
    env.write_text("APP_ENV=stage\n", encoding="utf-8")
    key.write_text("key", encoding="utf-8")
    public_key.write_text("public", encoding="utf-8")
    config.application.env_file = env
    config.server.ssh_key = key
    config.server.public_key = public_key
    runner = Mock()
    health_failed = False

    def playbook(name, *args, **kwargs):
        nonlocal health_failed
        if name == "health.yml":
            health_failed = True
            raise RunnerError("public health failed", 7)
        if name == "abort_release.yml" and health_failed:
            raise RunnerError("recovery failed", 6)

    runner.playbook.side_effect = playbook

    with pytest.raises(RunnerError) as raised:
        deploy(repo, global_config, config, runner, dry_run=False)

    assert raised.value.exit_code == 7
    assert "recovery also failed" in str(raised.value)


def test_monitoring_deploy_uses_dedicated_order_without_release_playbooks() -> None:
    repo = Path(__file__).parents[1]
    global_config, config = load_configuration(repo, "monitoring")
    runner = Mock()

    deploy_monitoring(repo, global_config, config, runner, dry_run=False)

    names = [call.args[0] for call in runner.playbook.call_args_list]
    assert names == [
        "verify_deploy_access.yml",
        "guard_environment.yml",
        "monitoring.yml",
        "monitoring_status.yml",
    ]
    assert not {"site.yml", "abort_release.yml", "finalize_release.yml"}.intersection(names)
    monitoring = runner.playbook.call_args_list[2]
    assert monitoring.kwargs["observability_secret_file"] == config.monitoring.secrets_file


def test_collector_deploy_guards_identity_then_reconciles_and_verifies() -> None:
    repo = Path(__file__).parents[1]
    global_config, config = load_configuration(repo, "stage")
    runner = Mock()

    deploy_collector(repo, global_config, config, runner, dry_run=False)

    names = [call.args[0] for call in runner.playbook.call_args_list]
    assert names == ["guard_environment.yml", "collector.yml", "collector_status.yml"]
    collector = runner.playbook.call_args_list[1]
    assert config.collector is not None
    assert collector.kwargs["observability_secret_file"] == config.collector.password_file
