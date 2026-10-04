# Шаг 1 · Project identity и внешний secret root

## Изменено

- `src/deploy_cli/secret_store.py` — canonical project-id, OS-specific external
  root, race-safe read/rotation через held handles/descriptors.
- `src/deploy_cli/cli.py` — `deploy secrets path [--new-project-id]`.
- `tests/test_secret_store.py` — T1–T4, platform и adversarial regressions.
- `examples/demo-app/.deploy/project-id` — commit-safe UUID identity.
- `pyproject.toml` — vendored flow tooling исключён из project Ruff scope.
- `.agents/`, `.codex/`, `flow/` — установлен flow для финального этапа.

## Критерий

- T1–T4 закрыты: `tests/test_secret_store.py` — 16 passed, 2 platform skips.
- Совместный CLI/project-local regression: 45 passed, 6 platform skips.
- `deploy secrets path` выводит только внешний project-scoped путь.

## Красный → зелёный

- Identity loader → random UUID: T1 красный; защита возвращена — зелёный.
- Rotation → оставить старый ID: T2 красный; защита возвращена — зелёный.
- Canonical/reparse guards отключены: T3 красный; защита возвращена — зелёный.
- OS/override resolver отключён: T4 красный; защита возвращена — зелёный.
- Windows directory handle освобождён до операции: adversarial read/write tests
  прочитали/изменили внешний UUID; held handle возвращён — оба зелёные.
- Marker скопирован между namespace: T2 красный; копирование удалено — зелёный.

## Проверки и ревью

- `python -m pytest -o addopts= -q tests/test_secret_store.py tests/test_cli.py tests/test_project_local.py` — 45 passed, 6 skipped.
- `python -m ruff check .` — passed.
- `python -m mypy` — passed, 59 source files.
- `git diff --check` — passed.
- Reviewer round 1: найден TOCTOU read/write и ложноположительный T2; исправлено.
- Reviewer round 2: найден macOS real-home leak в T2; тест изолирован через temp HOME.

Гейты: зона «секреты / крипто» — красные заглушки и два security-review круга выполнены; все major исправлены.
Гейты: зона «публичная граница» — CLI contract проверен reviewer и regression tests; blocker/major после исправлений не осталось.
