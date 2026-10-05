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
    )
    stale: list[str] = []
    for document in _markdown_files():
        text = document.read_text(encoding="utf-8")
        for phrase in prohibited:
            if phrase in text:
                stale.append(f"{document.relative_to(ROOT)}: {phrase}")
    assert not stale, "\n".join(stale)


def test_os_guides_delegate_common_flow_to_canonical_runbook() -> None:
    for name in ("windows.md", "macos.md", "ubuntu.md"):
        text = (ROOT / "docs/getting-started" / name).read_text(encoding="utf-8")
        assert "../runbook.md" in text
        assert "deploy stage" not in text
        assert "deploy prod" not in text
