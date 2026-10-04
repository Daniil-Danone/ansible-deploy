# Диагностика и безопасное восстановление

Сначала сохраните exit code, этап и Git SHA. Не меняйте сервер вручную, пока не понятна
причина: следующий идемпотентный запуск должен сходиться к описанному состоянию.

## Дерево проверки

1. `deploy project sync --check` — устаревший scaffold или conflict?
2. `deploy <environment> --dry-run` — config/path/Compose validation?
3. DNS указывает на ожидаемый IP, provider firewall пропускает SSH/80/443?
4. SSH fingerprint совпадает с данными provider console?
5. Docker/Compose этап упал? Используйте автоматически напечатанные sanitized
   `docker compose ps` и ограниченные tails упавших services. В `--verbose` смотрите
   Ansible task, но не включайте shell tracing.
6. Containers healthy, а public health нет — проверьте Nginx, TLS, loopback port и
   health path.
7. Повторите ту же команду только после исправления config/credential/infra cause.

## Project sync conflict

Основной файл не изменён. Сравните его с `*.deploy-new`, перенесите новые schema fields,
удалите candidate и снова запустите `sync --check`. Не удаляйте
`.deploy/template-state.yml` для обхода конфликта.

## Compose failed / output hidden

CLI обязан показать status и tail только проблемных services с redaction и без env.
Если диагностический блок сам не выполнился, используйте SSH как emergency evidence:

```bash
docker compose -f /srv/APP/docker-compose.yml ps --all
docker compose -f /srv/APP/docker-compose.yml logs --tail=100 SERVICE
```

Не запускайте `docker inspect`, `docker compose config` или `env`: они могут раскрыть
environment values. Не вставляйте полный log в публичный issue без review.

## External secret error

Проверьте `deploy secrets path`, existence, owner-only permissions и что path не является
symlink/junction/cloud-sync alias. Значение секрета не нужно показывать. После clone
committed project-id сохранит namespace; `--new-project-id` намеренно создаёт пустой
новый namespace и не копирует secrets.

## Partial deploy

Ansible tasks идемпотентны; исправьте первопричину и повторите command. Application
release финализируется только после Compose/HTTPS health. Для Production используйте
`deploy rollback prod`, если предыдущий release metadata существует и причина связана
с новой application version. Rollback не восстанавливает DB.

## Backup/restore

Checksum/decrypt/upload/import failure — неуспех, даже если часть files появилась.
Не удаляйте temporary/evidence вручную до review. Restore Drill повторяйте на отдельном
target по [runbook](guides/backup-restore.md); Production не используйте как drill.

## Что приложить к закрытому incident

Версия CLI, application Git SHA, environment, точная команда без secrets, exit code,
failed stage/task, sanitized diagnostics и подтверждение последующего health. Secret
values, private keys, env dumps и registry auth не прикладываются.
