import re
from pathlib import Path

from scripts.validate_alloy import ALLOY_IMAGE, render_fixture

ROOT = Path(__file__).parents[1]
ROLES = ROOT / "src/deploy_cli/runtime/ansible/roles"
TEMPLATE = ROLES / "collector/templates/config.alloy.j2"


def _without_strings_and_comments(text: str) -> str:
    without_strings = re.sub(r'"(?:\\.|[^"\\])*"', '""', text)
    return re.sub(r"//[^\n]*", "", without_strings)


def test_rendered_alloy_is_multiline_and_lexically_balanced() -> None:
    rendered = render_fixture()
    structural = _without_strings_and_comments(rendered)
    depth = 0
    for character in structural:
        if character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
            assert depth >= 0

    assert depth == 0
    assert "{%" not in rendered
    assert "{{ app_environment }}" not in rendered
    assert all(line.count(" = ") <= 1 for line in structural.splitlines())
    assert not re.search(r"\{[^\n{}]+=[^\n{}]+\}", structural)


def test_alloy_collects_both_journal_locations_without_hardcoded_path() -> None:
    template = TEMPLATE.read_text(encoding="utf-8")

    assert 'loki.source.journal "system"' in template
    assert "path = \"/run/log/journal\"" not in template
    assert "path = \"/var/log/journal\"" not in template
    assert "/run/log/journal:/run/log/journal:ro" in (
        ROLES / "collector/tasks/main.yml"
    ).read_text(encoding="utf-8")
    assert "/var/log/journal:/var/log/journal:ro" in (
        ROLES / "collector/tasks/main.yml"
    ).read_text(encoding="utf-8")


def test_alloy_level_label_has_only_five_normalized_values() -> None:
    template = TEMPLATE.read_text(encoding="utf-8")

    for level in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
        assert level in template
    assert 'source_labels = ["__journal_priority_keyword"]' in template
    assert "stage.json" in template and 'level = "level",' in template
    assert "stage.logfmt" in template
    assert 'source   = "level"' in template
    assert 'level = "level"' in template
    assert 'regex         = "emerg|alert|crit"' in template
    assert 'regex         = "notice|info"' in template


def test_journal_service_label_is_the_systemd_unit_with_journald_as_fallback() -> None:
    # Relabel runs after the base labels, so `service` is the unit name
    # (ssh.service, docker.service) and "journald" survives only for entries
    # without _SYSTEMD_UNIT. Removing or inverting either half must fail here.
    template = TEMPLATE.read_text(encoding="utf-8")
    relabel_start = template.index('loki.relabel "journal"')
    source_start = template.index('loki.source.journal "system"')
    relabel = template[relabel_start:source_start]
    source = template[source_start:]
    unit_rule = relabel[relabel.index('source_labels = ["__journal__systemd_unit"]') :]
    unit_rule = unit_rule[: unit_rule.index("}")]

    assert relabel_start < source_start
    assert 'regex         = "(.+)"' in unit_rule
    assert 'target_label  = "service"' in unit_rule
    # A replacement here would pin `service` to a constant and kill the granularity.
    assert "replacement" not in unit_rule
    assert "relabel_rules = loki.relabel.journal.rules" in source
    assert 'service     = "journald",' in source
    assert source.index("relabel_rules") < source.index('service     = "journald"')
    # The fallback must stay documented so nobody trusts `service="journald"`.
    assert "systemd unit name" in source[: source.index('service     = "journald"')]
    assert "fallback" in source[: source.index('service     = "journald"')]


def test_alloy_official_validation_gate_uses_same_pinned_image_as_collector() -> None:
    tasks = (ROLES / "collector/tasks/main.yml").read_text(encoding="utf-8")
    workflow = (ROOT / ".github/workflows/checks.yml").read_text(encoding="utf-8")
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")

    assert ALLOY_IMAGE in tasks
    assert "make alloy-validate" in workflow
    assert "python scripts/validate_alloy.py" in makefile
