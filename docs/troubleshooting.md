# Диагностика и безопасное восстановление

Сначала сохраните exit code, этап и Git SHA. Не меняйте сервер вручную, пока не понятна
причина: следующий идемпотентный запуск должен сходиться к описанному состоянию.

## Дерево проверки

1. `deploy project sync --check` — устаревший scaffold или conflict?
2. `deploy <environment> --dry-run` — config/path/Compose validation?
3. DNS указывает на ожидаемый IP, provider firewall пропускает SSH/80/443?
4. `host key is not trusted yet` — выполните `deploy trust <environment>` и сверьте
   напечатанные fingerprint'ы с provider console.
5. Docker/Compose этап упал? Используйте автоматически напечатанные sanitized
   `docker compose ps` и ограниченные tails упавших services. В `--verbose` смотрите
   Ansible task, но не включайте shell tracing.
6. Containers healthy, а public health нет — проверьте Nginx, TLS, loopback port и
   health path.
7. Повторите ту же команду только после исправления config/credential/infra cause.

## SSH host key не доверен или изменился

`SSH host key is not trusted yet for <env>` (код `2`) означает пустой
`server.host_key_fingerprints`: запустите `deploy trust <env>` и закоммитьте результат.

`Scanned SSH host key does not match the fingerprints already trusted` (код `2`) —
`trust` отказался перезаписывать список. Это либо переустановленный сервер, либо MITM:
сверьте ключ с консолью провайдера и только после этого повторите с `--force`.
`SSH host key does not match a configured SHA256 fingerprint` (код `3`) и `SSH host key
changed since the previous trusted connection` (код `3`) приходят из самой операции —
сервер не тот, которому доверяет config или сохранённый `.deploy-state/<env>/known_hosts`.
`SSH host key scan failed` (код `4`) — сервер недоступен по SSH, проверьте DNS, port и
provider firewall.

## Project sync conflict

Основной файл не изменён. Сравните его с `*.deploy-new`, перенесите новые schema fields,
удалите candidate и снова запустите `sync --check`. Не удаляйте
`.deploy/template-state.yml` для обхода конфликта.

## Compose failed / output hidden

CLI обязан показать status и tail только проблемных services с redaction и без env.
Перед запуском сервисов CLI выполняет безвыводную проверку `docker compose config --quiet`:
ошибка `configuration validation failed` означает некорректный Compose, а
`image pull or daemon reconciliation failed` — проблему pull или Docker daemon. Исходный
класс ошибки и exit code сохраняются, даже если автоматическая диагностика недоступна.
Если диагностический блок сам не выполнился, используйте SSH как emergency evidence:

```bash
docker compose -f /srv/APP/docker-compose.yml ps --all
docker compose -f /srv/APP/docker-compose.yml logs --tail=100 SERVICE
```

Не запускайте `docker inspect`, `docker compose config` или `env`: они могут раскрыть
environment values. Не вставляйте полный log в публичный issue без review.

## Внешний secret store пуст

`deploy secrets init` создаёт каталоги и заготовки, но не значения. `[FILL]` в отчёте
перечисляет переменные, которые обязан заполнить оператор; `registry-auth.json`,
`collector.password`, `rclone.conf` и `age.key` создаются вручную. Команда не
перезаписывает существующие файлы, поэтому её можно повторять.

## External secret error

Проверьте `deploy secrets path`, existence, owner-only permissions и что path не является
symlink/junction/cloud-sync alias. Значение секрета не нужно показывать. После clone
committed project-id сохранит namespace; `--new-project-id` намеренно создаёт пустой
новый namespace и не копирует secrets.

### `Invalid schema v2 <field>: ... is not owner-only`

После двоеточия CLI печатает причину: что именно не прошло проверку (`Secret file`,
`Secret store directory`, отсутствующий файл, link) — без имён и абсолютных путей, они
считаются чувствительными. Какой файл имеется в виду, видно по полю (`application
environment` → `application.env_file` в `.deploy/environments/<env>/config.yml`);
корень store покажет `ansible-deploy secrets path`.

Owner-only означает: на POSIX — `chmod 600` для файлов и `chmod 700` для директорий;
на Windows — в ACL ровно одна запись, Full control текущего пользователя. Файлы и
папки, созданные внутри store через Explorer, редактор или `New-Item`, наследуют такую
запись (`icacls` показывает `(I)(F)` / `(I)(OI)(CI)(F)`) и принимаются. Ошибка значит,
что в ACL есть лишние записи (другие пользователи, группы, deny) или наследование от
непривата. Исправление на Windows (cmd; в PowerShell — `$env:USERNAME`):

```bat
icacls <file> /inheritance:r /grant:r "%USERNAME%:F"
icacls <dir> /inheritance:r /grant:r "%USERNAME%:(OI)(CI)F"
icacls <path> /remove <другой-аккаунт>
```

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
