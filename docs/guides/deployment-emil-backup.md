# Настройка backup и Restore Drill для uwords

Это продолжение `deployment-emil.md` и `deployment-emil-cd.md`. Предполагается,
что schema v2, внешний secret store, SSH-доступ и Production уже настроены.

Инструкция привязана к текущему `Emil307/uwords`. В Production подтверждены:

- каталог приложения `/srv/uwords-prod`;
- PostgreSQL-сервисы `postgres` и `bot-postgres`;
- база/пользователь основного приложения `uwords` / `uwords`;
- база/пользователь бота `uwords_bot` / `uwords_bot`;
- Docker volumes `postgres-data` и `bot-postgres-data`;
- Redis работает без persistence, а пользовательские файлы хранятся в Timeweb S3.

Итоговая схема:

```text
Production VPS
  -> pg_dump двух PostgreSQL
  -> контрольный file marker
  -> tar + age encryption
  -> rclone -> Google Drive

Google Drive
  -> отдельный Restore VPS
  -> checksum + decrypt
  -> import двух PostgreSQL + восстановление file marker
  -> health + DB/file checks
```

Минимальная версия provider — `ansible-deploy v0.5.5`. Backup не включает
Timeweb S3: для S3 нужна отдельная политика versioning/replication поставщика.

---

## 1. Предварительная проверка

Работать нужно из корня локального checkout `uwords`:

```bash
cd /path/to/uwords

ansible-deploy --version
ansible-deploy secrets path
ansible-deploy validate prod
```

Перед первым backup подтвердить актуальный Compose-контракт:

```bash
docker compose -f deploy/compose.prod.yml config --services
docker compose -f deploy/compose.prod.yml config --volumes
```

Ожидаются сервисы `postgres`, `bot-postgres` и volumes `postgres-data`,
`bot-postgres-data`. Если имена изменились, сначала обновить backup YAML ниже.
Перед применением также подтвердить runtime-имена БД без печати secret env:

```bash
ssh -i "$(ansible-deploy secrets path)/keys/prod_ed25519" deploy@45.129.126.49

cd /srv/uwords-prod/current
docker compose exec postgres sh -c \
  'test "$POSTGRES_DB" = uwords && test "$POSTGRES_USER" = uwords'
docker compose exec bot-postgres sh -c \
  'test "$POSTGRES_DB" = uwords_bot && test "$POSTGRES_USER" = uwords_bot'
exit
```

Команда должна завершиться без вывода. Если она вернула ненулевой код, значения
Production отличаются от Compose defaults и `.env.example`: **уточнить** фактические
имена у владельца secret store и заменить `database`/`user` в backup YAML, не выводя
сам `.env`.

> Сейчас `deploy/compose.restore.yml` в uwords остаётся scaffold-заглушкой с сервисом
> `app` и образом `ghcr.io/example/myapp`. **Restore Drill запускать нельзя**, пока
> этот файл не приведён к изолированному эквиваленту Production Compose для uwords.

## 2. Установить инструменты

На операторской машине нужны `ansible-deploy`, `age`, `rclone`, `git` и SSH client.
Для macOS:

```bash
brew install uv age rclone git

uv tool install --force \
  'git+https://github.com/Daniil-Danone/ansible-deploy.git@v0.5.5'

ansible-deploy --version
age --version
age-keygen --version
rclone version
```

Ожидается `ansible-deploy 0.5.5` или новее.

## 3. Подготовить внешний secret store

```bash
ansible-deploy secrets init
SECRET_ROOT="$(ansible-deploy secrets path)"
mkdir -p "$SECRET_ROOT/backup"
chmod 700 "$SECRET_ROOT" "$SECRET_ROOT/backup"
```

Backup использует файлы:

```text
$(ansible-deploy secrets path)/backup/age.key
$(ansible-deploy secrets path)/backup/rclone.conf
```

Они находятся вне Git. Не печатать и не пересылать содержимое `.env`, `age.key`,
`rclone.conf`, `registry-auth.json` и secret store JSON.

## 4. Создать age identity

Создать отдельный ключ именно для Production backup:

```bash
age-keygen -o "$SECRET_ROOT/backup/age.key"
chmod 600 "$SECRET_ROOT/backup/age.key"
age-keygen -y "$SECRET_ROOT/backup/age.key"
```

Последняя команда выводит public recipient вида `age1...`. Только его можно добавить
в `.deploy/environments/prod/config.yml`. Строка `AGE-SECRET-KEY-...` остаётся в
`age.key` и должна иметь отдельную защищённую копию: без неё restore невозможен.

## 5. Настроить Google Drive в rclone

```bash
rclone --config "$SECRET_ROOT/backup/rclone.conf" config
chmod 600 "$SECRET_ROOT/backup/rclone.conf"
```

В интерактивном диалоге:

1. Выбрать `n` и назвать remote `gdrive`.
2. Выбрать storage `drive`.
3. Указать организационные Client ID/Secret или оставить пустыми, если разрешён
   встроенный client rclone.
4. Выбрать минимально достаточный scope и пройти авторизацию Google.
5. Shared Drive выбирать только если он действительно выделен под backup.
6. Сохранить remote и выйти.

Проверить доступ, не выводя конфигурацию:

```bash
rclone --config "$SECRET_ROOT/backup/rclone.conf" lsd gdrive:
rclone --config "$SECRET_ROOT/backup/rclone.conf" mkdir gdrive:backups/uwords/production
```

## 6. Добавить готовый backup YAML

В `.deploy/environments/prod/config.yml` добавить верхнеуровневый блок `backup`
рядом с `application`, `collector` и `domain`:

```yaml
backup:
  enabled: true
  # UTC: 21:15 = 03:15 следующего дня в Омске (UTC+6).
  schedule: "21:15"
  remote: gdrive:backups/uwords/production
  credentials_file: backup/rclone.conf
  age_identity_file: backup/age.key
  age_recipient: age1REPLACE_WITH_OUTPUT_OF_AGE_KEYGEN_Y
  include:
    - type: postgres
      service: postgres
      database: uwords
      user: uwords
    - type: postgres
      service: bot-postgres
      database: uwords_bot
      user: uwords_bot
    - type: file
      path: /srv/uwords-prod/shared/backup-drill-marker.txt
      restore_destination: shared/backup-drill-marker.txt
  retention:
    daily: 7
    weekly: 4
    monthly: 6
```

Заменить только `age_recipient` на public recipient из предыдущего шага. Прежде чем
устанавливать timer, создать безопасный file marker на Production VPS:

```bash
ssh -i "$SECRET_ROOT/keys/prod_ed25519" deploy@45.129.126.49

install -d -m 0750 /srv/uwords-prod/shared
printf "%s\n" "uwords-backup-marker-v1" \
  > /srv/uwords-prod/shared/backup-drill-marker.txt
chmod 0640 /srv/uwords-prod/shared/backup-drill-marker.txt
exit
```

Это технический marker для проверки файловой ветки restore. Пользовательские файлы
uwords находятся в S3 и не дублируются этим backup. Redis настроен с `--save ""` и
`--appendonly no`, поэтому он намеренно не включён.

Расписание всегда задаётся в UTC. Омск — UTC+6, поэтому `21:15` UTC означает `03:15`
следующего календарного дня в Омске. Systemd добавляет случайную задержку до пяти
минут.

Проверить конфигурацию перед применением:

```bash
ansible-deploy validate prod
git diff -- .deploy/environments/prod/config.yml
```

## 7. Установить и проверить первый backup

```bash
ansible-deploy backup setup prod
ansible-deploy backup run prod
ansible-deploy backup list prod
```

`run` успешен только после dump обеих БД, архивации marker, шифрования, checksum,
полной загрузки и retention. Сохранить полный backup ID из `list`; имя remote-файла
не угадывать.

На Production VPS проверить systemd и журнал:

```bash
ssh -i "$SECRET_ROOT/keys/prod_ed25519" deploy@45.129.126.49

sudo systemctl list-timers ansible-deploy-backup.timer --all
sudo systemctl status ansible-deploy-backup.timer
sudo systemctl status ansible-deploy-backup.service
sudo journalctl -u ansible-deploy-backup.service --since "48 hours ago" --no-pager
sudo cat /var/lib/ansible-deploy/backup/last-result.json
```

Ожидаются активный timer и `"status": "ok"`. Не копировать в тикеты весь журнал,
не проверив его на секреты.

## 8. Настроить freshness monitoring

Встроенный timer не отправляет уведомления. До интеграции с системой мониторинга
Эмиль ежедневно проверяет:

```bash
ansible-deploy backup list prod
```

Также на VPS проверяются `systemctl is-failed ansible-deploy-backup.service` и
`last-result.json`. Alert должен срабатывать, если:

- timer/service отсутствует или находится в failed;
- `last-result.json` содержит `failed`;
- после ожидаемого окна нет нового полного backup;
- возраст последнего полного набора превышает согласованный RPO с запасом на пять
  минут случайной задержки и фактическую длительность backup.

Конкретный alerting backend для uwords сейчас **уточнить**. До его выбора статус
фиксировать как `manual / not verified`, а не как настроенный alerting.

## 9. Подготовить отдельный Restore VPS

Нужен новый VPS, который не является `45.129.126.49`:

1. Создать чистый Ubuntu VPS и отдельную DNS-запись. Host/domain/IP — **уточнить**.
2. Исправить `deploy/compose.restore.yml`: он должен содержать изолированные сервисы
   uwords, включая `postgres` и `bot-postgres`, с совместимыми immutable images.
3. В `.deploy/environments/restore/config.yml` заменить scaffold-значения
   `restore.example.com` и `/srv/myapp-restore` на реальные. Рекомендуемый remote dir
   `/srv/uwords-restore`; подтвердить его до изменения.
4. Создать отдельные `keys/restore_ed25519` и `.pub` во внешнем secret store.
5. Создать `environments/restore/app.env` без Production-токенов внешних интеграций.
   Email, Telegram webhook, фоновые jobs и внешние API должны быть отключены или
   направлены на тестовые endpoints.
6. Создать `environments/restore/registry-auth.json` с read-only pull-доступом.
7. Выполнить trust только после сверки fingerprint через консоль VPS-провайдера:

```bash
ansible-deploy trust restore
ansible-deploy validate restore
git diff -- .deploy/environments/restore/config.yml deploy/compose.restore.yml
```

Production DNS не менять. Production SSH key и Production `.env` не копировать.

## 10. Первый Restore Drill

### DB markers

В uwords нет документированной безопасной служебной таблицы для drill-marker. До
запуска **уточнить у разработчика** две существующие строки, которые можно однозначно
проверить после restore:

| БД | Что зафиксировать без персональных данных |
|---|---|
| `uwords` | таблица, business key и ожидаемый неперсональный атрибут |
| `uwords_bot` | таблица, business key и ожидаемый неперсональный атрибут |

Посмотреть список таблиц без вывода `.env` и паролей можно на Production VPS:

```bash
cd /srv/uwords-prod/current
docker compose exec postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "\\dt"'
docker compose exec bot-postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "\\dt"'
```

Не создавать временную таблицу в Production и не вставлять искусственные данные без
согласования. Записать только ключи и ожидаемые неперсональные значения, не дамп строк.

### File marker и backup

Проверить marker без раскрытия секретов и создать свежий backup:

```bash
ssh -i "$SECRET_ROOT/keys/prod_ed25519" deploy@45.129.126.49 \
  'test "$(cat /srv/uwords-prod/shared/backup-drill-marker.txt)" = "uwords-backup-marker-v1"'

ansible-deploy backup run prod
ansible-deploy backup list prod
```

Зафиксировать backup ID, время и checksum из результата. Затем выполнить restore,
подставив полный ID:

```bash
ansible-deploy backup restore prod --target restore --backup BACKUP_ID
```

Не добавлять `--yes`. В prompt ещё раз проверить, что target/host относятся к Restore,
а не Production.

После завершения:

```bash
curl --fail --silent --show-error https://RESTORE_DOMAIN/health

ssh -i "$SECRET_ROOT/keys/restore_ed25519" deploy@RESTORE_DOMAIN \
  'cd /srv/uwords-restore/current && docker compose ps'

ssh -i "$SECRET_ROOT/keys/restore_ed25519" deploy@RESTORE_DOMAIN \
  'test "$(cat /srv/uwords-restore/shared/backup-drill-marker.txt)" = "uwords-backup-marker-v1"'
```

В обеих Restore-БД выполнить заранее согласованные read-only запросы к DB markers.
Дополнительно проверить health, отсутствие restart loop, прикладной smoke-test и то,
что Production-интеграции отключены. Один green `/health` не доказывает restore данных.

## 11. Если restore или rollback завершился ошибкой

После ошибки не запускать restore повторно вслепую. Проверить сообщение CLI, журнал,
состояние обеих БД и каждого destination.

Если CLI сообщает об ошибке автоматического rollback, он оставляет recovery-каталог
на Restore VPS. Не удалять его. Сначала вернуть данные из его подкаталога `rollback/`
в указанные destinations, проверить owner/permissions и только затем удалить recovery.

PostgreSQL import и замена файлов не образуют общую транзакцию. После частичного сбоя
безопаснее пересоздать отдельный Restore VPS и повторить drill с тем же проверенным
backup, чем очищать его на месте. Production при этом не трогать.

## 12. RPO, RTO и журнал drill

Значения для uwords сейчас **уточнить и согласовать**:

| Метрика | Как зафиксировать |
|---|---|
| RPO | Максимально допустимая потеря данных; ежедневный backup даёт базово не лучше 24 часов |
| RTO | От решения начать recovery до подтверждённых health, двух DB markers, file marker и smoke-test |
| Drill frequency | Рекомендуется регулярно и после изменений Compose, PostgreSQL, paths или backup runtime |

В журнал каждого drill записать дату, backup ID, возраст/checksum, результаты двух DB
markers и file marker, фактические RPO/RTO и обнаруженные проблемы. Секреты и
персональные данные в журнал не помещать.

## 13. Короткая диагностика

| Ошибка | Что проверить |
|---|---|
| `backup is not enabled` | Блок `backup.enabled: true` в Production config |
| Не найден `rclone.conf` / `age.key` | `ansible-deploy secrets path`, имена `backup/...`, owner-only permissions |
| `age_recipient` invalid | В config должен быть public `age1...`, не private identity |
| `pg_dump` не находит сервис | `docker compose ... config --services`; ожидаются `postgres`, `bot-postgres` |
| Google Drive access denied | Remote `gdrive`, OAuth scope, доступ к `backups/uwords/production` |
| Timer не запускается | `systemctl status`, `list-timers`, `journalctl`, UTC schedule |
| В `list` нет нового ID | Проверить весь набор upload/checksum и retention по journal |
| Restore указывает на `app`/example image | `compose.restore.yml` ещё scaffold; drill остановить |
| Restore host совпадает с Production | Drill остановить; создать отдельный VPS/DNS/key |
| DB marker отсутствует | Проверить выбранный backup ID, обе БД и согласованный read-only query |
| File marker отсутствует | Проверить manifest и `shared/backup-drill-marker.txt` |
| CLI сообщил recovery path | Не повторять restore; вручную восстановить `rollback/` и проверить permissions |

## 14. Финальный чеклист

- [ ] Установлен `ansible-deploy v0.5.5` или новее.
- [ ] `age` и `rclone` установлены.
- [ ] `backup/age.key` и `backup/rclone.conf` находятся только во внешнем secret store.
- [ ] Защищённая копия age identity сохранена отдельно.
- [ ] Remote `gdrive:backups/uwords/production` доступен.
- [ ] Backup включает обе БД: `uwords` и `uwords_bot`.
- [ ] Redis и Timeweb S3 осознанно исключены; политика S3 оформлена отдельно.
- [ ] File marker создан в `/srv/uwords-prod/shared`.
- [ ] `validate`, `backup setup`, `run` и `list` проходят.
- [ ] Timer активен, журнал чист, `last-result.json` содержит `ok`.
- [ ] Freshness alert настроен или ежедневная ручная проверка зафиксирована.
- [ ] `compose.restore.yml` перестал быть scaffold-заглушкой.
- [ ] Restore VPS, DNS, SSH key, env и registry credentials изолированы.
- [ ] Для обеих БД согласованы read-only marker queries.
- [ ] Drill подтвердил health, две БД, file marker и smoke-test.
- [ ] Фактические RPO/RTO записаны и согласованы.
