# Конфигурация

## `global.yml`

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

Неизвестные поля запрещены. Timezone — имя IANA.

## Environment `config.yml`

Обязательные группы: `schema_version`, `environment`, `server`, `application`, `domain`,
`acme_email`; `health_path` по умолчанию `/health`.

- `server.host`, `ssh_port`: DNS/IP и порт VPS;
- `bootstrap_user`: initial account, обычно `root`;
- `deploy_user`: managed account, обычно `deploy`;
- `ssh_key`, `public_key`: relative project paths или absolute paths существующей пары;
- `host_key_fingerprints`: один или несколько точных OpenSSH SHA256 fingerprints;
- `compose`, `env_file`: файлы приложения относительно project root;
- `registry_auth_file`: optional secret внутри project root;
- `remote_dir`: нормальный путь ниже `/srv` или `/opt`, минимум один дочерний сегмент;
- `allowed_loopback_ports`: разрешённые `127.0.0.1` published ports;
- `allowed_bind_paths`: разрешённые absolute server bind sources;
- `required_env_vars`: уникальные uppercase имена;
- `domain`, `acme_email`: TLS target и контакт Let's Encrypt.
- `collector.push_url`: только HTTPS endpoint `/loki/api/v1/push`;
- `collector.username`, `collector.password_file`: Basic Auth collector и отдельный
  локальный secret-файл внутри project root.
- `collector.remote_dir`: отдельный leaf ниже `/opt` или `/srv`; он не может совпадать,
  содержать или находиться внутри application `remote_dir`.

`APP_ENV` в env обязан совпадать с environment. Значения env не печатаются.

## `.deploy/images.yml`

```yaml
schema_version: 1
services:
  api:
    context: backend
    dockerfile: Dockerfile
    image: myapp-api
environments:
  stage: {compose: deploy/compose.stage.yml}
  prod: {compose: deploy/compose.prod.yml}
```

Build paths не выходят за project root. Целевой Compose должен иметь явные block mapping
и простые `image:` строки; anchors, aliases, merge keys, duplicate/implicit mappings
отклоняются, чтобы хирургическое обновление не меняло смысл YAML.

## Compose policy

- self-contained файл, без `include`, `extends`, `network_mode`;
- published port только `127.0.0.1:HOST:CONTAINER` и HOST в allowlist;
- named volumes объявлены; relative binds запрещены, absolute binds требуют allowlist;
- разрешены declared bridge networks и `internal: true`; external/host/unmanaged запрещены;
- interpolation в networks/volumes запрещена;
- Production: каждый service имеет digest-pinned image, `build:` запрещён.

Stage не требует digest у всех сторонних images, поэтому его immutability слабее. Для
реальной воспроизводимости закрепляйте digest в обоих окружениях.

## Monitoring `config.yml`

Monitoring использует те же `server`, `domain`, `acme_email`, но вместо `application`
содержит `monitoring`: `remote_dir`, `secrets_file`, `retention_days`, loopback-порты
Grafana/Loki. Secret env содержит `GF_SECURITY_ADMIN_USER`,
`GF_SECURITY_ADMIN_PASSWORD`, `LOKI_PUSH_USERNAME`, `LOKI_PUSH_PASSWORD_HASH`.
Реальный файл игнорируется Git и монтируется read-only, без содержимого в argv,
process env или Ansible extra-vars.

`collector.push_url` разбирается структурно: разрешён только
`https://host[:port]/loki/api/v1/push`, без userinfo, query, fragment и whitespace.
Monitoring/application/collector runtime directories не могут указывать на системные
корни и обязаны быть нормализованными отдельными каталогами ниже `/opt` или `/srv`.
