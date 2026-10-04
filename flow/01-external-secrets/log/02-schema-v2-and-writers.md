# Шаг 2 · Schema v2 и external writers

## Результат

- Все v2 secret paths резолвятся только во внешний project-scoped store.
- Default demo использует schema v2; application compose остаётся project-local.
- SSH key pair и registry auth создаются атомарно во внешнем store.
- Runner монтирует только отдельные secret files read-only и повторно проверяет
  ownership/ACL/reparse непосредственно перед запуском subprocess.
- External root запрещён внутри всего Git worktree; POSIX/Windows ancestry,
  owner, ACL, hardlinks и portable names проверяются fail closed.
- User-facing errors и полный traceback не раскрывают configured path/value.

## Красный → зелёный

- Project-local resolver вместо external root: T5 красный → защита возвращена.
- Path/containment/portable-name guards отключены: T6–T8 красные → возвращены.
- Exception chaining и raw OSError/ValueError возвращены: T9 fault matrix
  раскрыла marker → boundaries восстановлены `from None`.
- Key cleanup отключён для partial write/flush/fsync/close: T14 оставил partial
  files → rollback восстановлен; wrapper безопасно сообщает cleanup failure.
- Registry unlink rollback проглочен: T15 красный → explicit safe failure.
- `:ro` и launch-boundary validation отключены: T16 красный → восстановлены.
- Старый Windows `open → ACL → lock` воспроизвёл PermissionError/nlink race;
  новый mutex + atomic publication + file lock прошёл повторный stress.

## Проверки

- `python -m pytest -o addopts= -q -ra` — 302 passed, 11 platform skips.
- `python -m ruff check .` — passed.
- `python -m mypy` — passed, 59 source files.
- `git diff --check b440d7c` — passed.
- Windows concurrency: 20 reviewer repeats и 10 builder repeats — passed.
- Финальное свежее security-review: PASS, blocker/major нет.
- Temporary sabotage/TODO/FIXME/xfail markers: не найдены.

Гейты: зона «секреты / крипто» — red→green fault matrix выполнена; финальный security-review PASS.
Гейты: зона «публичная граница» — CLI/config/runner errors санитизированы; reviewer PASS.
