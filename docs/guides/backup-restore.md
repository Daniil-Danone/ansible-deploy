# Backup, Restore Drill и disaster recovery

Эта инструкция предназначена оператору Production. Она настраивает ежедневный
зашифрованный backup в Google Drive, проверяет его ручным запуском и описывает
восстановление на отдельном Restore VPS. Команды выполняйте из корня репозитория
приложения, где находятся `.deploy/` и `deploy/`.

> Restore Drill подтверждает, что конкретный backup можно расшифровать и импортировать
> в изолированное окружение. Это не автоматизированный Production DR: CLI намеренно
> запрещает восстанавливать backup поверх Production.

## 1. Подготовить инструменты и внешний secret store

На операторской машине должны быть установлены CLI `ansible-deploy` (команда `deploy`
остаётся алиасом), `age`/`age-keygen` и `rclone`. Проверьте их до настройки:

```bash
deploy --version
age --version
age-keygen --version
rclone version
```

Получите project-scoped каталог секретов и создайте его штатную структуру:

```bash
deploy secrets path
deploy secrets init
```

Используйте **ровно тот абсолютный путь**, который напечатал `deploy secrets path`.
Каталог `backup/` и файлы в нём не должны находиться в Git-репозитории. На Linux/macOS
проверьте owner-only permissions:

```bash
SECRET_ROOT="$(deploy secrets path)"
mkdir -p "$SECRET_ROOT/backup"
chmod 700 "$SECRET_ROOT" "$SECRET_ROOT/backup"
```

В PowerShell:

```powershell
$secretRoot = (deploy secrets path).Trim()
New-Item -ItemType Directory -Force "$secretRoot\backup"
```

`deploy secrets init` создаёт каталоги с доступом только владельцу. Если каталог был
создан или скопирован вручную, отдельно проверьте его ACL: доступ к `backup` и будущим
файлам должен иметь только текущий пользователь (плюс системная учётная запись/администратор
по принятой политике машины). Не печатайте содержимое `.env`, `rclone.conf` и `age.key`.

## 2. Создать age identity

Создайте новую identity специально для Production backup и получите из неё public
recipient:

```bash
age-keygen -o "$SECRET_ROOT/backup/age.key"
chmod 600 "$SECRET_ROOT/backup/age.key"
age-keygen -y "$SECRET_ROOT/backup/age.key"
```

В PowerShell:

```powershell
age-keygen -o "$secretRoot\backup\age.key"
age-keygen -y "$secretRoot\backup\age.key"
```

В `.deploy/environments/prod/config.yml` копируется только строка `age1...`, которую
вывела последняя команда. Private identity, начинающаяся с `AGE-SECRET-KEY-`, остаётся
только в `backup/age.key`. Сохраните отдельную защищённую копию identity: без неё
backup невозможно расшифровать.

## 3. Настроить rclone remote для Google Drive

Запустите интерактивную настройку, записывая конфигурацию во внешний secret store:

```bash
rclone --config "$SECRET_ROOT/backup/rclone.conf" config
chmod 600 "$SECRET_ROOT/backup/rclone.conf"
```

PowerShell-вариант:

```powershell
rclone --config "$secretRoot\backup\rclone.conf" config
```

В диалоге rclone:

1. Выберите `n` (new remote) и имя `gdrive`.
2. Выберите storage `drive` (Google Drive).
3. Укажите выданные организацией Client ID/Client Secret либо оставьте поля пустыми,
   если разрешён встроенный rclone client.
4. Выберите минимально достаточный scope и пройдите авторизацию Google в браузере.
5. Не включайте Shared Drive, если backup действительно не должен храниться в нём;
   для Shared Drive выберите его только согласно политике организации.
6. Подтвердите сохранение remote и выйдите из конфигуратора.

Проверьте remote без вывода конфигурации и токенов:

```bash
rclone --config "$SECRET_ROOT/backup/rclone.conf" lsd gdrive:
```

Имя до двоеточия в `backup.remote` обязано совпадать с именем remote (`gdrive`). Не
коммитьте `rclone.conf`: файл содержит credential/token Google Drive.

## 4. Описать полный Production backup

В `.deploy/environments/prod/config.yml` включите backup. Ниже полный пример для
Compose-сервиса PostgreSQL `postgres`, базы `app`, пользователя `app`, каталога uploads
и постоянного файла приложения. Замените имена и пути на фактические значения вашего
Production Compose; секреты базы здесь не нужны и не должны появляться в config.

```yaml
backup:
  enabled: true
  # Всегда UTC. 21:15 UTC = 03:15 следующего дня в Омске (UTC+6).
  schedule: "21:15"
  remote: gdrive:backups/production
  credentials_file: backup/rclone.conf
  age_identity_file: backup/age.key
  age_recipient: age1replace_with_the_public_recipient_printed_by_age_keygen
  include:
    - type: postgres
      service: postgres
      database: app
      user: app
    - type: directory
      path: /srv/myapp-prod/shared/uploads
      restore_destination: shared/uploads
    - type: file
      path: /srv/myapp-prod/shared/application-state.json
      restore_destination: shared/application-state.json
  retention:
    daily: 7
    weekly: 4
    monthly: 6
```

`age_recipient` должен быть настоящим public recipient формата `age1...` из шага 2,
а не значением-заглушкой из примера. `credentials_file` и `age_identity_file`
разрешаются относительно каталога из `deploy secrets path`.

Для `file`, `directory` и `glob` поле `restore_destination` обязательно. Это
нормализованный относительный POSIX-путь внутри `application.remote_dir` Restore VPS;
он не может быть абсолютным, содержать `..` или пересекаться с другим destination.
Для `postgres` destination не задаётся. Хотя бы одно retention-окно должно быть
ненулевым.

Внутри каждого архива хранится зашифрованная карта источников с их типами и
destinations. Restore использует карту выбранного backup, а не текущий порядок
`include`: последующая перестановка источников не перенаправит старые данные в другой
путь. Несовпадение карты и содержимого останавливает restore.

`schedule` — строго `HH:MM` в UTC, независимо от timezone рабочей станции и VPS. Омск
использует UTC+6: чтобы запускать backup в 03:15 по Омску, задайте `21:15` UTC
предыдущего календарного дня. У timer есть случайная задержка до пяти минут, поэтому
не стройте проверку свежести на точной минуте запуска.

PostgreSQL dump и файловые источники снимаются последовательно. Это **не** единый
атомарный snapshot и не гарантирует cross-resource consistency между строками БД и
файлами, которые менялись во время backup. Если приложению нужна такая гарантия,
добавьте согласованный application-level quiesce/snapshot процесс до запуска backup.

## 5. Установить timer и проверить первый backup

```bash
deploy backup setup prod
deploy backup run prod
deploy backup list prod
```

`setup` идемпотентно устанавливает root-owned runtime, service и timer на Production
VPS. `run` считается успешным только после PostgreSQL dump, архивации файлов,
шифрования age, checksum, проверенной загрузки полного набора и retention. `list`
показывает только завершённые backup IDs. Сохраните полный ID из вывода; не угадывайте
имя remote-файла.

Проверьте timer и журнал непосредственно на Production VPS:

```bash
sudo systemctl list-timers ansible-deploy-backup.timer --all
sudo systemctl status ansible-deploy-backup.timer
sudo systemctl status ansible-deploy-backup.service
sudo journalctl -u ansible-deploy-backup.service --since "48 hours ago" --no-pager
sudo cat /var/lib/ansible-deploy/backup/last-result.json
```

В `last-result.json` ожидается `"status": "ok"`; `"status": "failed"` требует
разбора журнала и повторного ручного запуска после исправления причины. Файл не
заменяет проверку возраста backup: используйте timestamp последнего успешного запуска
service и timestamp/ID последнего полного набора из `deploy backup list prod`.

### Freshness monitoring и alerting

Встроенный timer сам не отправляет уведомления. Настройте внешний мониторинг, который
после ожидаемого окна запуска:

- сигнализирует, если timer/service failed или отсутствует;
- сигнализирует, если нет успешного запуска и нового полного backup дольше допустимого
  RPO плюс запас на `RandomizedDelaySec=300` и длительность backup;
- периодически сверяет, что `deploy backup list prod` возвращает свежий полный набор,
  а не только что локальный service завершился;
- немедленно поднимает alert при `status: failed` в `last-result.json` или ошибке в
  journal.

До подключения конкретной системы мониторинга это обязательная ручная ежедневная
проверка, а статус следует фиксировать как `manual / not verified`, не как настроенный
alerting.

## 6. Подготовить отдельный Restore VPS

Restore Drill требует полностью отдельной инфраструктуры:

1. Создайте чистый Ubuntu 24.04/26.04 VPS, который не является Production VPS.
2. Создайте отдельную DNS-запись, например `restore.example.com`, на его IP и дождитесь
   разрешения DNS. Production DNS не меняйте.
3. В `.deploy/environments/restore/config.yml` оставьте `environment: restore` и
   `source_environment: prod`; укажите отдельные `server.host`, `domain` и
   `application.remote_dir` (например `/srv/myapp-restore`).
4. Используйте отдельную пару `keys/restore_ed25519` / `.pub`, не Production SSH key.
5. Подготовьте отдельный `environments/restore/app.env`. Не копируйте Production `.env`
   вслепую: выдайте только необходимые Restore credentials и изолируйте внешние
   интеграции, фоновые jobs, email, платежи и webhooks.
6. Подготовьте `environments/restore/registry-auth.json` с read-only pull-доступом к
   нужным immutable images.
7. Проверьте `deploy/compose.restore.yml`: images должны соответствовать версии
   восстанавливаемого приложения, сервис/volume paths — Restore окружению, а наружу не
   должны уходить Production-интеграции.
8. Выполните `deploy trust restore`, сверьте fingerprint с консолью VPS-провайдера и
   сохраните полученное commit-safe изменение.

Secret-файлы Restore создаются во внешнем каталоге из `deploy secrets path`, а не в
репозитории. Для password-only первого подключения добавьте к restore-команде
`--ask-bootstrap-password`; дальнейшая работа идёт через отдельный managed SSH key.

## 7. Выполнить интерактивный Restore Drill

Перед запуском создайте проверяемые маркеры в Production: известную строку в таблице
БД и известный файл внутри backup directory. Зафиксируйте их ожидаемые значения,
время создания, выбранный backup ID и checksum из результата backup.

```bash
deploy backup list prod
deploy backup restore prod --target restore --backup BACKUP_ID
```

Не добавляйте `--yes`: CLI напечатает source, target, Restore host и backup ID и
попросит интерактивное подтверждение. Ещё раз убедитесь, что target — `restore`, host
не Production, DNS отдельный, затем подтвердите операцию. Команда подготовит Restore
VPS, поднимет Compose для импорта, скачает полный набор, проверит manifest/checksum,
расшифрует данные, восстановит files и импортирует PostgreSQL.

После завершения обязательно проверьте вручную (CLI `status` поддерживает только
Stage/Production, поэтому Restore проверяется напрямую):

```bash
curl --fail --show-error --silent https://restore.example.com/health
ssh -i "$(deploy secrets path)/keys/restore_ed25519" deploy@restore.example.com \
  'cd /srv/myapp-restore/current && docker compose ps'
```

- HTTPS health на Restore domain возвращает ожидаемый успешный ответ;
- сервисы Compose не находятся в restart loop, в их логах нет ошибок импорта;
- marker row существует в Restore PostgreSQL и содержит ожидаемые значения;
- marker file существует по `restore_destination`, совпадает по содержимому и доступен
  приложению;
- прикладной smoke-test читает восстановленные данные, а внешние Production-интеграции
  действительно отключены;
- checksum/backup ID совпадают с выбранным набором.

Один только green health endpoint не доказывает успешное восстановление данных.
Ошибка download/checksum/decrypt/import должна считаться неуспешным drill, даже если
часть файлов уже появилась. Cleanup orphan/partial remote objects и частично
восстановленных данных не автоматизирован: диагностируйте состояние и очищайте его
вручную; безопаснее пересоздать отдельный Restore VPS перед повтором.

После проверки удалите Restore VPS/DNS и временные credentials согласно принятой
политике либо остановите VPS до следующего drill. Production backup не удаляйте.

Если автоматический rollback файлового restore сам завершится ошибкой, команда
остановится и напечатает путь к сохранённому recovery-каталогу на Restore VPS. Не
повторяйте restore и не удаляйте этот каталог: сначала вручную верните данные из его
подкаталога `rollback/` в указанные destinations, проверьте владельца и permissions,
а recovery-каталог удалите только после проверки восстановленного состояния. Импорт
PostgreSQL и замена файлов не образуют общую транзакцию, поэтому после любой ошибки
сверяйте оба состояния до следующей попытки.

## 8. RPO, RTO и периодичность drill

До ввода backup в эксплуатацию зафиксируйте для приложения:

- **RPO** — максимально допустимую потерю данных. При одном ежедневном backup базовый
  RPO не лучше 24 часов и может быть хуже при пропущенном/долгом запуске; выбранный
  schedule и freshness-alert должны укладываться в заявленный RPO.
- **RTO** — максимально допустимое время восстановления. Измеряйте от решения начать
  recovery до подтверждённых health, DB marker, file marker и smoke-test, включая
  создание VPS, DNS propagation, pull images и импорт данных.

Проводите Restore Drill регулярно и после значимых изменений Compose, PostgreSQL,
persistent paths, credentials или backup runtime. В журнале drill сохраняйте дату,
backup ID, возраст backup, checksum, измеренный RPO/RTO и результаты каждой проверки.

## 9. Production disaster recovery

Restore Drill команда не является процедурой восстановления Production и не должна
использоваться для обхода Production safeguards. При реальной потере Production:

1. Сохраните evidence, остановите автоматические deploy jobs и определите момент
   инцидента.
2. Выберите последний **проверенный** backup, соответствующий принятому RPO.
3. Поднимите чистый Production VPS, восстановите provider firewall, обновите Production
   host/DNS и дождитесь DNS propagation.
4. Сверьте новый SSH fingerprint через `deploy trust prod`, подготовьте/ротируйте SSH,
   registry, application и backup credentials; не переиспользуйте скомпрометированные
   ключи.
5. Выполните обычный идемпотентный Production deploy с правильными immutable images и
   Compose. Миграции приложения запускаются только по отдельной согласованной процедуре.
6. Восстановите данные по отдельно утверждённой Production recovery процедуре с
   явным change approval; встроенный изолированный restore намеренно не пишет в prod.
7. Проверьте HTTPS, DB/file markers, прикладной smoke-test, collectors, alerts и создайте
   новый подтверждённый backup.

Реальная загрузка в Google Drive, retention, freshness alerts и содержательный restore
считаются подтверждёнными только после live-проверки с credentials заказчика и
отдельным Restore VPS. Локальные tests доказывают лишь command/config contract,
защиту от unsafe paths, checksum/tamper fail-closed и структуру Ansible automation.
