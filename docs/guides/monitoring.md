# Централизованные логи

1. Настройте monitoring config и DNS, затем выполните `deploy trust monitoring`.
2. Создайте заготовку secret-файла командой `deploy secrets init monitoring` и заполните
   в `environments/monitoring/monitoring.env` Grafana password, Loki username и crypt
   SHA-512 hash пароля.
3. Там же создайте отдельные `environments/{stage,prod}/collector.password` и
   ограничьте доступ текущим пользователем (`chmod 600` либо Windows ACL).

Hash считает сам CLI внутри runtime-контейнера, локальный OpenSSL не нужен. Пароль
вводится только в скрытом prompt (дважды) и не попадает в argv, shell history или на
диск; в stdout печатается единственная строка `$6$...` для `LOKI_PUSH_PASSWORD_HASH`:

```text
deploy secrets hash-password
```

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

Приёмка требует VPS/DNS: создайте свежую запись в Stage и Prod и найдите обе в Grafana
по environment/service. Локальные contract tests эту сетевую проверку не подменяют.
