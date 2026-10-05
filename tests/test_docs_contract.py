import re
from pathlib import Path

ROOT = Path(__file__).parents[1]


def _markdown_files() -> list[Path]:
    return [ROOT / "README.md", *(ROOT / "docs").rglob("*.md")]


def test_local_markdown_links_resolve() -> None:
    missing: list[str] = []
    pattern = re.compile(r"\[[^]]+\]\((?!https?://|#)([^)#]+)(?:#[^)]+)?\)")
    for document in _markdown_files():
        for target in pattern.findall(document.read_text(encoding="utf-8")):
            resolved = (document.parent / target).resolve()
            if not resolved.exists():
                missing.append(f"{document.relative_to(ROOT)} -> {target}")
    assert not missing, "\n".join(missing)


def test_docs_do_not_restore_schema_v1_secret_layout_claims() -> None:
    prohibited = (
        ".deploy/environments/stage/app.env",
        ".deploy/environments/prod/app.env",
        "backup/restore БД также пока вне CLI",
        "secrets manager, migrations, backup/restore",
        "optional secret внутри project root",
        "Зафиксируйте\nSSH host fingerprint из доверенного канала",
    )
    stale: list[str] = []
    for document in _markdown_files():
        text = document.read_text(encoding="utf-8")
        for phrase in prohibited:
            if phrase in text:
                stale.append(f"{document.relative_to(ROOT)}: {phrase}")
    assert not stale, "\n".join(stale)


def test_host_key_trust_stays_a_deliberate_documented_step() -> None:
    """Trust is now a CLI command, but it must never become trust-on-first-connection."""
    runbook = (ROOT / "docs/runbook.md").read_text(encoding="utf-8")
    for environment in ("stage", "prod", "monitoring", "restore"):
        assert f"deploy trust {environment}" in runbook
    assert "deploy secrets init" in runbook
    assert "консолью\nпровайдера" in runbook or "консолью провайдера" in runbook
    prohibited = ("StrictHostKeyChecking=no", "StrictHostKeyChecking no", "accept-new")
    offenders = [
        f"{document.relative_to(ROOT)}: {phrase}"
        for document in _markdown_files()
        for phrase in prohibited
        if phrase in document.read_text(encoding="utf-8")
    ]
    assert not offenders, "\n".join(offenders)


def test_cli_reference_documents_every_subcommand_of_the_parser() -> None:
    from deploy_cli.cli import _parser

    reference = (ROOT / "docs/reference/cli.md").read_text(encoding="utf-8")
    actions = _parser()._subparsers._group_actions[0]  # type: ignore[union-attr]
    for name in actions.choices:
        assert name in reference, name


def test_os_guides_delegate_common_flow_to_canonical_runbook() -> None:
    for name in ("windows.md", "macos.md", "ubuntu.md"):
        text = (ROOT / "docs/getting-started" / name).read_text(encoding="utf-8")
        assert "../runbook.md" in text
        assert "deploy stage" not in text
        assert "deploy prod" not in text
