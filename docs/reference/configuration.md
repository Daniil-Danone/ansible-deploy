# Конфигурация

## Commit-safe и secret data

`.deploy/` содержит YAML schema, project identity, template hashes и `.env.example`.
Реальные secrets/keys находятся только под root из `deploy secrets path`. В schema v2
secret fields — нормализованные относительные имена внутри этого root; absolute paths,
`..`, symlink/reparse escape, hardlinks и небезопасные permissions отклоняются.

## Global

`.deploy/config/global.yml` использует собственную версию формата 1. Это не версия
deployment environment schema:

```yaml
schema_version: 1
global:
  security_updates:
    enabled: true
    reboot:
      enabled: true
      only_when_required: true
      time: "04:00"
      timezone: Europe/Moscow
  hardening:
    profile: default
    controls:
      ssh: true
      firewall: true
      fail2ban: true
      unattended_upgrades: true
```

## Deployment environments: только schema v2

Stage, Production, Monitoring и Restore принимают только `schema_version: 2`.
Environment schema 1 больше не поддерживается и отклоняется до изменения ключей,
файлов, сети или состояния сервера. Версии `global.yml`, `images.yml`,
`.deploy/template-state.yml` и backup archive — независимые форматы и остаются равны 1.

```yaml
schema_version: 2
environment: stage
server:
  host: stage.example.com
  ssh_port: 22
  bootstrap_user: root
  deploy_user: deploy
  ssh_key: keys/stage_ed25519
  public_key: keys/stage_ed25519.pub
  host_key_fingerprints: [SHA256:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA]
application:
  compose: deploy/compose.stage.yml
  env_file: environments/stage/app.env
  registry_auth_file: environments/stage/registry-auth.json
  remote_dir: /srv/myapp-stage
  allowed_loopback_ports: [8080]
  required_env_vars: [APP_ENV]
domain: stage.example.com
acme_email: ops@example.com
health_path: /health
```

`application.compose` — project-relative commit-safe file. Secret fields `env_file` и
`registry_auth_file`, а также server key names резолвятся относительно внешнего root.
Production обязан иметь отдельные paths/remote_dir и digest-pinned images.

### Дополнительные secret env files

Если несколько services читают одинаковые имена переменных с разными значениями
(например, bot и backend оба ждут `DB_PASSWORD`), объявите дополнительные внешние env
files в Stage, Production или Restore:

```yaml
application:
  env_file: environments/stage/app.env
  extra_env_files:
    - source: environments/stage/bot.env   # путь во внешнем store
      target: bot.env                      # имя файла рядом с compose.yml на сервере
```

- `source` подчиняется тем же правилам, что `env_file`: нормализованный относительный
  путь внутри внешнего store, regular owner-only file без hardlinks. Отсутствие или
  небезопасные права файла останавливают deploy на preflight; путь не попадает в ошибки.
- `target` — простое имя файла по шаблону `^[A-Za-z0-9][A-Za-z0-9._-]*\.env$`
  (до 68 символов): без каталогов и `..`, не `.env`.
- `source` и `target` уникальны; `source` не может совпадать с `env_file`, ключами,
  registry/collector/backup secrets или файлами конфигурации. Production не может
  использовать env files Stage.
- `required_env_vars` и проверка `APP_ENV` применяются только к основному `env_file`;
  extra files проверяются как читаемые UTF-8 dotenv.
- значения из extra files маскируются в выводе так же, как значения `.env`, и входят в
  checksum release: изменение extra file требует новой версии deploy.

Service подключает файл через Compose `env_file`:

```yaml
services:
  bot:
    image: registry.example.com/bot@sha256:...
    env_file: [bot.env]
```

На сервере extra files доставляются в `releases/<version>/` рядом с `.env` с теми же
owner, mode `0600` и `no_log`. Rollback и восстановление прерванного release
переключают их вместе с release directory. Release directories неизменяемы, поэтому
устаревшие targets старых версий остаются только в своих releases; Restore использует
постоянный `current/` и перед доставкой удаляет из него не объявленные `*.env`.

Optional `collector` содержит HTTPS Loki push URL, username и внешний
`password_file`; его remote directory не пересекается с application directory.

### Reverse proxy limits

Optional `reverse_proxy` в Stage/Production/Restore настраивает host Nginx, который
проксирует `domain` на `127.0.0.1:<upstream port>`:

```yaml
reverse_proxy:
  client_max_body_size: 12m   # default 1m
  proxy_read_timeout: 120     # seconds, default 60
```

- `client_max_body_size` — Nginx size: целое число без ведущего нуля и опциональный
  суффикс `k`/`m`/`g` (регистр не важен, `12M` нормализуется в `12m`). `0`/`off` не
  принимаются: снять лимит целиком нельзя.
- `proxy_read_timeout` — целое число секунд `1..3600`. То же значение применяется к
  `proxy_send_timeout`, чтобы медленная отправка большого upload к приложению не
  обрывалась раньше ожидания ответа; отдельного ключа нет.
- Без секции поведение совпадает с прежним (Nginx defaults `1m` и `60s`). Неизвестные
  ключи отклоняются.

Лимиты применяются в HTTP bootstrap и HTTPS virtual host приложения при deploy, `deploy server update`
и `deploy backup restore`. Они не входят в immutable identity окружения и release checksum, поэтому
их можно менять между релизами, в том числе при повторном deploy той же версии.
Monitoring Nginx эти настройки не принимает.

## Monitoring

Monitoring schema v2 использует общий `server`, `domain`, `acme_email` и секцию:

```yaml
monitoring:
  remote_dir: /opt/ansible-deploy/monitoring
  secrets_file: environments/monitoring/monitoring.env
  retention_days: 30
```

Monitoring env с Grafana/Loki credentials находится только во внешнем store.

## Backup/Restore

Production `backup` задаёт schedule, remote, external `credentials_file`, external
`age_identity_file`, public recipient, include и retention. Restore target находится в
`.deploy/environments/restore/config.yml`, имеет `environment: restore` и
`source_environment: prod`; runtime secrets лежат под `environments/restore/`.
Актуальные поля создаёт `deploy project sync`; проверяйте их через CLI после обновления.

## Compose policy

- запрещены `include`, `extends`, host/external/unmanaged networks;
- published ports только `127.0.0.1:HOST:CONTAINER` из allowlist;
- named volumes объявлены; bind paths абсолютны и явно разрешены;
- Production images закреплены `@sha256:...`, `build:` запрещён;
- `APP_ENV` во внешнем env совпадает с environment.
- secret-like ключи и значения в Compose `environment` запрещены; credentials должны
  находиться во внешнем `application.env_file`, указанном только относительным именем.
- service `env_file` (строка, список или длинная форма `- path: x` с optional boolean
  `required`) может ссылаться только на `.env` или объявленный
  `application.extra_env_files[].target`; другие пути отклоняются.

Неизвестные поля запрещены строгими Pydantic models. Ошибка конфигурации возвращает
код `2` и не должна включать secret value.
