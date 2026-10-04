# Backup, Restore Drill и disaster recovery

Production backup описывается секцией `backup` в
`.deploy/environments/prod/config.yml`. Config commit-safe; реальные
`backup/rclone.conf` и `backup/age.key` находятся во внешнем secret store.
Сгенерированный scaffold содержит полную, но выключенную (`enabled: false`)
секцию. Перед включением замените remote/recipient и настройте источники:

```yaml
backup:
  enabled: true
  schedule: "03:15"
  remote: gdrive:backups/production
  credentials_file: backup/rclone.conf
  age_identity_file: backup/age.key
  age_recipient: age1aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
  include:
    - type: directory
      path: /srv/myapp-prod/shared/uploads
      restore_destination: shared/uploads
  retention:
    daily: 7
    weekly: 4
    monthly: 6
```

`restore_destination` обязателен для `file`, `directory` и `glob`, задаётся
нормализованным относительным путём и разрешается внутри
`application.remote_dir` выбранного Restore-окружения. Поэтому Production-путь
никогда не переиспользуется на Restore VPS. Для `postgres` destination не нужен.
Хотя бы одно retention-окно должно быть ненулевым.

Минимальный порядок:

```bash
deploy backup setup prod
deploy backup run prod
deploy backup list prod
deploy backup restore prod --target restore --backup BACKUP_ID
```

`setup` применяет schedule идемпотентно. `run` считается успешным только после dump,
архивации выбранных files, шифрования age, checksum, подтверждённой загрузки и retention.
`list` показывает metadata/IDs, но не credentials. Используйте полный ID из списка, не
угадывайте remote filename.

## Restore Drill

`restore` — отдельное окружение в `.deploy/environments/restore/config.yml` с
`source_environment: prod`, отдельным VPS, remote directory, SSH key и runtime
secrets. Source остаётся `prod`; target `prod` для drill запрещён. Команда сама
идемпотентно подготавливает чистый Ubuntu VPS тем же bootstrap/deploy flow; для
password-only bootstrap используйте `--ask-bootstrap-password`.

1. Создайте/проверьте отдельный Restore VPS и DNS.
2. Получите список и выберите backup ID.
3. Запишите ожидаемый checksum, marker row и marker file до восстановления.
4. Запустите restore без `--yes`, сверив напечатанные source/target/backup.
5. Проверьте HTTPS health, checksum, marker row в PostgreSQL и marker file contents.
6. Удалите Restore Drill инфраструктуру по принятой политике; backup не удаляйте.

`--yes` допустим только внутри защищённой автоматизации с уже проверенным target.
Ошибка download/checksum/decrypt/import должна остановить процесс до объявления
успеха. Не подменяйте содержательную проверку одним health endpoint.

## Disaster recovery Production

При потере VPS сначала сохраните evidence и остановите автоматические deploy jobs.
Поднимите чистый Ubuntu 24.04, обновите Production host/fingerprint/DNS, выполните
обычный idempotent Production deploy, затем выберите подтверждённый backup и следуйте
отдельно согласованной процедуре восстановления на Production. Restore Drill команда
не должна использоваться для обхода Production safeguards.

После восстановления проверьте приложение, DB marker, uploads/config files,
collectors, новый backup и documented RPO/RTO. Ротация утраченных SSH/registry/backup
credentials обязательна, если нельзя доказать их сохранность.

## Статус проверки

Локальные tests могут доказать command/config contract, traversal protection,
checksum/tamper fail-closed и Ansible idempotency structure. Реальная загрузка в Google
Drive, retention и содержательный restore считаются подтверждёнными только после run с
credentials заказчика и отдельным Restore VPS; без них статус — `manual / not verified`.
