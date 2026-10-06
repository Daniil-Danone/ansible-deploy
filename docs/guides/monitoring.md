# Централизованные логи

1. Настройте monitoring config и DNS, затем выполните `deploy trust monitoring`.
2. Создайте заготовки secret-файлов командой `deploy secrets init` и заполните в
   `environments/monitoring/monitoring.env` Grafana password и Loki username.
3. Выдайте push-password обоим collectors и мониторингу одной командой:

```text
deploy secrets rotate-collector-password
deploy secrets check-collector-password
```

Push-password живёт в двух файлах: plaintext в `environments/{stage,prod}/collector.password`
(его читает Alloy) и crypt SHA-512 hash в `LOKI_PUSH_PASSWORD_HASH` файла
`monitoring.env` (его проверяет nginx). Расхождение между ними — это 401 на каждый push,
пустой Loki и «No data» в Grafana, поэтому обе формы генерирует и раскладывает одна
команда: `rotate-collector-password` создаёт новый случайный пароль, пишет plaintext в
оба окружения (или в одно с `--environment stage|prod`), считает hash внутри
runtime-контейнера и подменяет только строку `LOKI_PUSH_PASSWORD_HASH=`, оставляя
остальной файл байт-в-байт. Пароль не печатается, не попадает в argv и shell history.
Применяется он после раскатки:

```text
deploy monitoring update && deploy collectors deploy all --yes
```

`check-collector-password` ничего не меняет и сверяет plaintext с hash'ем; ту же
проверку делает preflight `collectors deploy|update`, так что рассинхронизация больше не
доходит до сервера молча.

Вручную: `deploy secrets hash-password` дважды скрыто запрашивает пароль и печатает
единственную строку `$6$...` для `LOKI_PUSH_PASSWORD_HASH` — тогда тот же пароль нужно
самостоятельно положить в `collector.password` обоих окружений и ограничить доступ
текущим пользователем (`chmod 600` либо Windows ACL). Ручная вставка hash'а — именно тот
путь, на котором два значения расходятся; предпочитайте `rotate-collector-password`.

Не используйте `echo PASSWORD | openssl ...`: plaintext попадёт в process/pipeline
history или диагностический log. Hash не заменяет отдельный `LOKI_PUSH_PASSWORD`,
который collectors используют для Basic Auth.

```text
deploy monitoring deploy --ask-bootstrap-password
deploy collectors deploy all --yes
deploy monitoring status
deploy collectors status all
```

Monitoring и collectors обновляются независимо от application release:

```text
deploy monitoring update
deploy collectors update all --yes
deploy server update monitoring
```

Grafana доступна по HTTPS. Loki/Grafana raw-порты привязаны к loopback VPS; публичный
Loki push работает только через TLS и Basic Auth. Dashboard фильтрует по bounded labels
`environment`/`service`; Alloy собирает Docker и оба стандартных journal location
(`/run/log/journal`, `/var/log/journal`) с дополнительными `container`/`host`.

Для Docker-логов `service` — это имя Compose-сервиса. Для системных логов `service` —
имя systemd-юнита (`ssh.service`, `docker.service`, …): relabel применяется после
базовых labels и перетирает значение. Значение `service="journald"` остаётся только
для записей без `_SYSTEMD_UNIT` (kernel, audit), поэтому проверять поступление
системных логов в Grafana нужно селектором `{environment="stage"}`, а не
`{service="journald"}`.

Label `level` имеет только пять значений: `DEBUG`, `INFO`, `WARNING`, `ERROR`,
`CRITICAL`. Для journald он получается из bounded systemd priority. Для Docker
поддержан явный application contract: одна JSON- или logfmt-запись содержит поле
`level`; `warn` нормализуется в `WARNING`, `fatal`/`crit` — в `CRITICAL`, неизвестные
значения не становятся label. Произвольные поля приложения намеренно не индексируются.
Retention задаёт `monitoring.retention_days`.

## Ротация Grafana admin password

`GF_SECURITY_ADMIN_PASSWORD` применяется Grafana только при создании новой базы данных,
поэтому простое изменение `monitoring.env` не ротирует существующего администратора.
Безопасная ручная процедура:

1. Войдите в Grafana по HTTPS и создайте временного пользователя с ролью Server Admin.
2. Выйдите и проверьте вход временным администратором.
3. Через Grafana UI смените пароль постоянного администратора; не используйте CLI-флаг,
   shell history, argv или диагностический log для передачи нового пароля.
4. Проверьте новый вход, обновите `GF_SECURITY_ADMIN_PASSWORD` во внешнем secret-файле
   как recovery seed и выполните `deploy monitoring update`.
5. Удалите временного администратора через UI.

Значение recovery seed не изменяет уже существующую Grafana DB, но необходимо для
воспроизводимого восстановления monitoring VPS.

## Troubleshooting: логи не доходят до Loki

Grafana показывает «No data», хотя collectors раскатаны — проверяйте снизу вверх.

1. На stage/prod смотрите, что отвечает push-endpoint:

```text
docker logs ansible-deploy-alloy
```

`401` — push-password collector'а не совпадает с `LOKI_PUSH_PASSWORD_HASH`; `413` —
запись больше лимита nginx.

2. Сверьте обе формы пароля локально (ничего не меняет):

```text
deploy secrets check-collector-password
```

Расхождение лечится `deploy secrets rotate-collector-password` и последующими
`deploy monitoring update && deploy collectors deploy all --yes`.

3. На monitoring-сервере проверьте, что Loki вообще получил записи:

```text
curl -s localhost:3100/loki/api/v1/label/service/values
```

Пустой список при `200` в логах Alloy — проблема в Loki или retention, а не в
авторизации.

Приёмка требует VPS/DNS: создайте свежую запись в Stage и Prod и найдите обе в Grafana
по environment/service. Локальные contract tests эту сетевую проверку не подменяют.
