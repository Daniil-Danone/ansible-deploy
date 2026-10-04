# Финальный этап ansible-deploy

| Итерация | Результат наружу | Готово, когда | Прогресс |
|---|---|---|---|
| 1. External secret storage | `.deploy/` безопасно коммитится, реальные secrets/keys живут вне Git worktree | init/audit/migrate работают на Windows/Linux/macOS и CI; legacy layout мигрируется fail-closed | [2]/8 |
| 2. Backup и Restore | Production backup шифруется, подтверждается в Google Drive и безопасно восстанавливается на отдельный target | marker row и marker file восстановлены на Restore Drill; tamper/partial upload fail closed | [ ] |
| 3. CI/CD | Quality → Stage → approval → Production выполняется с immutable image и health gates | broken gate блокирует deploy; Production ждёт reviewer; concurrency сериализует окружение | [ ] |
| 4. Финальная приёмка и документация | Новый пользователь проходит единый красивый runbook на чистых VPS | все acceptance-команды и security scan зелёные; открыт PR `develop -> main`, merge не выполняется | [ ] |

Порядок обязателен: Backup/Restore и CI используют external secret store, а
финальная документация фиксирует уже проверенные интерфейсы команд.
