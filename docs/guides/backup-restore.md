# Backup, Restore Drill и disaster recovery

Production backup описывается секцией `backup` в
`.deploy/environments/prod/config.yml`. Config commit-safe; реальные
`backup/rclone.conf` и `backup/age.key` находятся во внешнем secret store.
Сгенерированный scaffold содержит полную, но выключенную (`enabled: false`)
секцию. Перед включением замените remote/recipient и настройте источники:

Сначала установите локальные `age` и `rclone`, получите root через `deploy secrets
path` и создайте каталог `backup` с owner-only permissions. Затем сгенерируйте новую
identity и выведите соответствующий public recipient:

```bash
age-keygen -o /external/root/backup/age.key
age-keygen -y /external/root/backup/age.key
rclone --config /external/root/backup/rclone.conf config
```

В PowerShell используйте путь, который вернул `deploy secrets path`, например:

```powershell
$secretRoot = deploy secrets path
New-Item -ItemType Directory -Force "$secretRoot\backup"
age-keygen -o "$secretRoot\backup\age.key"
age-keygen -y "$secretRoot\backup\age.key"
rclone --config "$secretRoot\backup\rclone.conf" config
```

Скопируйте только напечатанный `age1...` recipient в commit-safe config. Private
`AGE-SECRET-KEY-...` остаётся исключительно в `age.key`. В интерактивном rclone создайте
remote с тем же именем, которое стоит до двоеточия в `backup.remote` (`gdrive` в примере
ниже). После создания ограничьте доступ к обоим файлам текущим пользователем; не
печатайте и не коммитьте их содержимое.

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

Каждый архив содержит зашифрованную immutable-карту источников со стабильными ID,
типами и относительными destinations. Restore использует эту карту из выбранного
backup, а не текущий порядок `include`: перестановка, добавление или удаление
источников в новой конфигурации не может молча направить старые данные в другой путь.
Отсутствующая, повреждённая или не совпадающая с содержимым карта останавливает
restore до записи данных.

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
