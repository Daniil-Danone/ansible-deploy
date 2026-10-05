# Конфигурация

## Commit-safe и secret data

`.deploy/` содержит YAML schema, project identity, template hashes и `.env.example`.
Реальные secrets/keys находятся только под root из `deploy secrets path`. В schema v2
secret fields — нормализованные относительные имена внутри этого root; absolute paths,
`..`, symlink/reparse escape, hardlinks и небезопасные permissions отклоняются.

## Global

`.deploy/config/global.yml` использует schema 1:

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

## Stage/Production schema v2

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

Optional `collector` содержит HTTPS Loki push URL, username и внешний
`password_file`; его remote directory не пересекается с application directory.

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

Неизвестные поля запрещены строгими Pydantic models. Ошибка конфигурации возвращает
код `2` и не должна включать secret value.
