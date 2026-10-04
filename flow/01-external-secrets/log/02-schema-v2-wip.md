# Шаг 2 · Schema v2 и external writers — WIP checkpoint

Статус: шаг не закрыт, критерии T5–T9 и T14–T16 не отмечены выполненными.
Checkpoint создан по просьбе владельца, чтобы очистить рабочее дерево перед
ручным тестированием monitoring на `develop`.

## Реализовано, но ещё не принято

- Schema v2 и внешний project-scoped secret root.
- Portable sensitive paths, ownership/ACL и reparse validation.
- External SSH key и registry-auth writers.
- Повторная проверка secret mounts перед запуском runner.
- Fault/rollback и redaction regressions.

## Оставшиеся замечания независимого review

- `ensure_deploy_key` маскирует ошибку неудавшегося rollback partial public key.
- Два permission validator сохраняют sensitive filesystem error в exception chain.
- Windows concurrent key creation имеет редкую гонку `open → ACL → lock`.
- Нужны дополнительные fault-тесты partial write/close и pair-level rollback.
- После исправлений требуется заполнить T5–T9/T14–T16 red→green evidence и
  повторить независимое security-review.

## Последние проверки

- Полный pytest до последнего review: 282 passed, 11 skipped.
- Ruff: passed.
- mypy: passed.
- `git diff --check`: passed.

Гейты: зона «секреты / крипто» — НЕ ПРОЙДЕНА; checkpoint содержит незакрытые major findings.
Гейты: зона «публичная граница» — НЕ ПРОЙДЕНА; public key workflow требует исправления и повторного review.
