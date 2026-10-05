from pathlib import Path

import yaml

ROOT = Path(__file__).parents[1]
RUNTIME = ROOT / "src/deploy_cli/runtime"


def _text(path: str) -> str:
    return (RUNTIME / path).read_text(encoding="utf-8")


def _yaml(path: str) -> list[dict[str, object]]:
    document = yaml.safe_load(_text(path))
    assert isinstance(document, list)
    return document


def test_demo_frontend_binds_upstream_to_loopback() -> None:
    compose = yaml.safe_load(
        (ROOT / "examples/demo-app/deploy/compose.stage.yml").read_text(encoding="utf-8")
    )

    assert compose["services"]["frontend"]["ports"] == ["127.0.0.1:8080:8080"]


def test_reboot_is_marker_conditional_and_uses_moscow_calendar() -> None:
    tasks = _yaml("ansible/roles/reboot_timer/tasks/main.yml")
    copies = [task["ansible.builtin.copy"] for task in tasks if "ansible.builtin.copy" in task]
    contents = "\n".join(str(copy["content"]) for copy in copies)  # type: ignore[index]

    assert "ConditionPathExists=/var/run/reboot-required" in contents
    assert "OnCalendar=*-*-* {{ reboot_time }}:00 {{ reboot_timezone }}" in contents
    assert "Persistent=false" in contents


def test_sensitive_application_files_are_delivered_as_0600() -> None:
    tasks = _yaml("ansible/roles/application/tasks/main.yml")
    sensitive_copies = [
        task
        for task in tasks
        if task.get("name")
        in {
            "Deliver release Compose definition",
            "Deliver immutable release Compose definition",
            "Deliver immutable release environment securely",
        }
    ]

    assert all(task["ansible.builtin.copy"]["mode"] == "0600" for task in sensitive_copies)  # type: ignore[index]
    env_task = next(task for task in sensitive_copies if "environment" in str(task["name"]))
    assert env_task["no_log"] is True


def test_release_metadata_and_rollback_preserve_safe_permissions_and_health() -> None:
    application = _text("ansible/roles/application/tasks/main.yml")
    rollback = _text("ansible/playbooks/rollback.yml")

    assert "releases/{{ deployment_version }}" in application
    guard = _text("ansible/roles/environment_guard/tasks/main.yml")
    abort = _text("ansible/playbooks/abort_release.yml")
    finalize = _text("ansible/roles/release_finalize/tasks/main.yml")
    restore = _text("ansible/roles/release_restore/tasks/main.yml")
    assert "different input checksum" in application
    assert "check_mode: false" in application
    assert "final_previous" in application
    assert "Reject any change to authoritative host identity" in guard
    assert "Reject bootstrap access after the environment has been claimed" in guard
    assert "require_unclaimed_environment" in guard
    assert "deployment_identity_files.results[0].stat.exists" in guard
    assert "deployment_identity_files.results[1].stat.exists" in guard
    assert "committed_release_metadata_file.stat.exists" in guard
    assert "pull: never" in rollback
    assert "pull: never" in abort
    assert "original_previous" in restore
    assert "Commit externally verified current release" in finalize
    assert "mode: \"0600\"" in finalize
    assert "Verify rollback public HTTPS endpoint" in rollback
    assert "Verify recovered public HTTPS endpoint" in abort
    assert "Restore original current-version metadata" in restore


def test_compose_failures_collect_bounded_safe_diagnostics() -> None:
    diagnostics = _text("ansible/roles/compose_diagnostics/tasks/main.yml")
    application = _text("ansible/roles/application/tasks/main.yml")
    rollback = _text("ansible/playbooks/rollback.yml")
    abort = _text("ansible/playbooks/abort_release.yml")
    monitoring = _text("ansible/roles/monitoring/tasks/main.yml")
    collector = _text("ansible/roles/collector/tasks/main.yml")

    assert "ansible.builtin.command:\n    argv:" in diagnostics
    assert "ps\n      - --all\n      - --format\n      - json" in diagnostics
    assert "'logs', '--no-color', '--tail', '100'" in diagnostics
    assert "failed_when: false" in diagnostics
    for forbidden in ("compose config", "inspect", "printenv", "ansible.builtin.shell"):
        assert forbidden not in diagnostics
    for workflow in (application, rollback, abort, monitoring, collector):
        assert "name: compose_diagnostics" in workflow
        assert "safe diagnostics are shown above" in workflow
    for workflow in (application, monitoring, collector):
        assert "- config\n          - --quiet" in workflow
        assert "configuration validation failed" in workflow
        assert "image pull or daemon reconciliation failed" in workflow
        assert "diagnostics are unavailable" in workflow
    assert rollback.count("- config\n              - --quiet") == 1
    assert "Rollback Compose configuration validation failed" in rollback
    assert "rollback_compose_failure_reason" in rollback
    assert "Collect rollback diagnostics without masking the original failure" in rollback
    assert "Safe rollback Compose diagnostics are unavailable" in rollback
    assert abort.count("- config\n              - --quiet") == 2
    assert "Recovered Compose configuration validation failed" in abort
    assert "Failed candidate Compose configuration validation failed" in abort
    assert "release_recovery_failure_reason" in abort
    assert "Collect release recovery diagnostics without masking the original failure" in abort
    assert "Safe release recovery Compose diagnostics are unavailable" in abort


def test_legacy_stage_is_verified_before_secure_snapshot_and_commit() -> None:
    adoption = _text("ansible/roles/legacy_adoption/tasks/main.yml")
    site = _text("ansible/playbooks/site.yml")
    snapshot = _yaml("ansible/roles/legacy_snapshot/tasks/main.yml")
    names = [str(task.get("name")) for task in snapshot]

    assert site.index("role: legacy_adoption") < site.index("role: environment_identity")
    assert adoption.index("Validate legacy Compose portability before identity commit") < (
        adoption.index("Wait for every legacy Compose container to become healthy")
    )
    assert adoption.index("Verify legacy public HTTPS endpoint before adoption") < adoption.index(
        "Snapshot and commit verified legacy release"
    )
    env_task = next(task for task in snapshot if "environment securely" in str(task.get("name")))
    assert env_task["no_log"] is True
    assert env_task["ansible.builtin.copy"]["mode"] == "0600"  # type: ignore[index]
    assert names.index("Persist immutable legacy release metadata") < names.index(
        "Commit verified legacy release as current"
    )


def test_firewall_has_only_expected_public_ports() -> None:
    tasks = _yaml("ansible/roles/hardening/tasks/main.yml")
    ports_task = next(task for task in tasks if task.get("name") == "Allow public service ports")

    assert ports_task["loop"] == ["{{ deploy_ssh_port }}", "80", "443"]


def test_ssh_hardening_uses_early_dropin_and_reconnects_after_firewall() -> None:
    tasks = _yaml("ansible/roles/hardening/tasks/main.yml")
    installed = next(
        task for task in tasks if task.get("name") == "Install early hardened SSH drop-in"
    )
    names = {str(task.get("name")) for task in tasks}

    assert installed["ansible.builtin.copy"]["dest"].endswith("/00-deploy-hardening.conf")  # type: ignore[index,union-attr]
    assert "Validate installed effective SSH configuration before reload" in names
    assert "Verify SSH access after firewall activation" in names


def test_nginx_bootstrap_does_not_replace_existing_tls_configuration() -> None:
    tasks = _yaml("ansible/roles/reverse_proxy/tasks/main.yml")
    bootstrap = next(
        task for task in tasks if task.get("name") == "Install HTTP bootstrap virtual host"
    )
    hook = next(
        task for task in tasks if task.get("name") == "Install safe Certbot Nginx deployment hook"
    )

    assert "not existing_certificate.stat.exists" in str(bootstrap["when"])
    assert hook["ansible.builtin.copy"]["owner"] == "root"  # type: ignore[index]
    assert "nginx -t" in hook["ansible.builtin.copy"]["content"]  # type: ignore[index]


def test_nginx_changes_queue_validation_then_reload_in_handler_order() -> None:
    tasks = _yaml("ansible/roles/reverse_proxy/tasks/main.yml")
    handlers = _yaml("ansible/roles/reverse_proxy/handlers/main.yml")
    nginx_changes = [task for task in tasks if "notify" in task]

    assert [handler["name"] for handler in handlers] == ["Validate nginx", "Reload nginx"]
    assert nginx_changes
    assert all(task["notify"] == ["Validate nginx", "Reload nginx"] for task in nginx_changes)


def test_application_proxy_renders_configured_body_size_and_timeouts() -> None:
    tasks = _yaml("ansible/roles/reverse_proxy/tasks/main.yml")
    expected = (
        "client_max_body_size {{ app_client_max_body_size }};\n"
        "        proxy_read_timeout {{ app_proxy_read_timeout }}s;\n"
        "        proxy_send_timeout {{ app_proxy_read_timeout }}s;\n"
        "        proxy_pass http://127.0.0.1:{{ app_upstream_port }};"
    )
    for name in ("Install HTTP bootstrap virtual host", "Install HTTPS virtual host"):
        task = next(task for task in tasks if task.get("name") == name)
        content = task["ansible.builtin.copy"]["content"]  # type: ignore[index]

        assert content.count("proxy_pass ") == 1, name
        assert expected in content, name


def test_reverse_proxy_limits_are_not_part_of_immutable_identity() -> None:
    for path in (
        "ansible/roles/environment_guard/tasks/main.yml",
        "ansible/roles/environment_identity/tasks/main.yml",
        "ansible/roles/application/tasks/main.yml",
        "ansible/roles/legacy_snapshot/tasks/main.yml",
        "ansible/roles/legacy_adoption/tasks/main.yml",
    ):
        text = _text(path)

        assert "app_client_max_body_size" not in text, path
        assert "app_proxy_read_timeout" not in text, path


def test_ssh_check_mode_does_not_depend_on_staged_files() -> None:
    tasks = _yaml("ansible/roles/hardening/tasks/main.yml")
    staged = [task for task in tasks if "staged" in str(task.get("name", "")).lower()]
    inspection = next(
        task
        for task in tasks
        if task.get("name") == "Inspect current effective SSH configuration in check mode"
    )

    assert all("not ansible_check_mode" in str(task["when"]) for task in staged)
    assert inspection["check_mode"] is False
    assert "ansible_check_mode" in str(inspection["when"])


def test_disabled_hardening_controls_have_explicit_off_state() -> None:
    tasks = _yaml("ansible/roles/hardening/tasks/main.yml")
    by_name = {str(task.get("name")): task for task in tasks}

    assert by_name["Disable firewall when explicitly configured off"]["when"] == (
        "not hardening_controls.firewall"
    )
    fail2ban = by_name["Stop and disable fail2ban when explicitly configured off"]
    assert fail2ban["ansible.builtin.service"]["enabled"] is False  # type: ignore[index]
    assert fail2ban["ansible.builtin.service"]["state"] == "stopped"  # type: ignore[index]


def test_monitoring_contract_has_retention_provisioning_tls_and_no_raw_public_ports() -> None:
    monitoring = _text("ansible/roles/monitoring/tasks/main.yml")

    assert "retention_enabled: true" in monitoring
    assert "retention_period: {{ monitoring_retention_days }}d" in monitoring
    assert "url: http://loki:3100" in monitoring
    assert "central-logs" in monitoring
    assert "environment=~" in monitoring and "service=~" in monitoring
    assert '127.0.0.1:{{ monitoring_loki_port }}:3100' in monitoring
    assert '127.0.0.1:{{ monitoring_grafana_port }}:3000' in monitoring
    assert '"0.0.0.0:' not in monitoring
    assert "ssl_protocols TLSv1.2 TLSv1.3" in monitoring
    assert "auth_basic_user_file" in monitoring
    assert "location = /loki/api/v1/push" in monitoring
    assert "mode: \"0600\"" in monitoring
    assert "no_log: true" in monitoring


def test_collector_contract_has_bounded_labels_docker_journald_and_no_ports() -> None:
    tasks = _text("ansible/roles/collector/tasks/main.yml")
    config = _text("ansible/roles/collector/templates/config.alloy.j2")

    for label in ("environment", "service", "container", "host", "level"):
        assert label in config
    assert "discovery.docker" in config
    assert "loki.source.docker" in config
    assert "loki.source.journal" in config
    assert "/var/run/docker.sock:/var/run/docker.sock:ro" in tasks
    assert "/var/log/journal:/var/log/journal:ro" in tasks
    assert "password_file = \"/run/secrets/push.password\"" in config
    assert "ports:" not in tasks


def test_observability_playbooks_are_separate_from_application_release_flow() -> None:
    for playbook in ("monitoring.yml", "monitoring_update.yml", "collector.yml"):
        content = _text(f"ansible/playbooks/{playbook}")
        assert "role: application" not in content
        assert "release_finalize" not in content
        assert "release_restore" not in content


def test_backup_every_operation_converges_runtime_config_and_credentials() -> None:
    tasks = _text("ansible/roles/backup/tasks/main.yml")
    converge_guard = "backup_action in ['setup', 'run', 'list', 'restore']"

    assert tasks.count(converge_guard) >= 4


def test_restore_application_preflights_compose_and_preserves_original_failure() -> None:
    tasks = _text("ansible/roles/restore_application/tasks/main.yml")

    assert "config\n          - --quiet" in tasks
    assert "failed_when: false" in tasks
    assert "restore_preparation_failure" in tasks
    assert "Secondary Restore diagnostics failed" in tasks
    assert "wait: false" in tasks


def _extra_env_delivery(path: str) -> dict[str, object]:
    tasks = _yaml(path)
    return next(task for task in tasks if "extra environments securely" in str(task.get("name")))


def test_extra_env_files_are_delivered_like_primary_env_without_logging() -> None:
    release = _extra_env_delivery("ansible/roles/application/tasks/main.yml")
    restore = _extra_env_delivery("ansible/roles/restore_application/tasks/main.yml")

    for task, base in (
        (release, "{{ app_dir }}/releases/{{ deployment_version }}/"),
        (restore, "{{ app_dir }}/current/"),
    ):
        copy = task["ansible.builtin.copy"]
        assert task["no_log"] is True
        assert task["loop"] == "{{ app_extra_env_files | default([]) }}"
        assert copy == {  # type: ignore[comparison-overlap]
            "src": "{{ item.src }}",
            "dest": base + "{{ item.dest }}",
            "owner": "{{ deploy_user }}",
            "group": "{{ deploy_user }}",
            "mode": "0600",
        }
    # Immutable release: extras are written once, together with .env and before metadata.
    assert release["when"] == "not release_metadata_file.stat.exists"
    names = [str(task.get("name")) for task in _yaml("ansible/roles/application/tasks/main.yml")]
    assert names.index("Deliver immutable release environment securely") < names.index(
        "Deliver immutable release extra environments securely"
    ) < names.index("Write immutable release metadata last")


def test_rollback_and_recovery_reuse_extra_env_files_from_the_release_directory() -> None:
    # Extras live inside releases/<version>/ next to compose.yml and .env, so rollback,
    # abort and release_restore switch them together with the release symlink.
    for path in ("ansible/playbooks/rollback.yml", "ansible/playbooks/abort_release.yml"):
        text = _text(path)
        assert "app_extra_env_files" not in text
        assert "project_src:" in text
    assert "app_extra_env_files" not in _text("ansible/roles/release_restore/tasks/main.yml")


def test_restore_removes_undeclared_extra_env_files_before_delivery() -> None:
    tasks = _yaml("ansible/roles/restore_application/tasks/main.yml")
    names = [str(task.get("name")) for task in tasks]
    find = tasks[names.index("Find Restore extra environments from earlier restores")]
    remove = tasks[names.index("Remove undeclared Restore extra environments")]

    assert find["ansible.builtin.find"] == {  # type: ignore[comparison-overlap]
        "paths": "{{ app_dir }}/current",
        "patterns": ["*.env"],
        "file_type": "file",
        "hidden": False,
    }
    assert remove["ansible.builtin.file"]["state"] == "absent"  # type: ignore[index]
    assert "map(attribute='dest')" in str(remove["when"])
    assert names.index("Deliver Restore environment securely") < names.index(
        "Remove undeclared Restore extra environments"
    ) < names.index("Deliver Restore extra environments securely")
