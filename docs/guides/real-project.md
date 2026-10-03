# Настоящий проект: backend, frontend и admin

## Рекомендуемая схема

Оставьте один публичный gateway. Он принимает трафик на `127.0.0.1:8080`, отдаёт
frontend на `/`, admin на `/admin` и проксирует API на `/api`. Backend, PostgreSQL,
Redis и UI-контейнеры не публикуют host ports и общаются в declared bridge network.

```text
Internet -> Nginx/HTTPS на VPS -> 127.0.0.1:8080 gateway
                                      |-- /       frontend
                                      |-- /admin  admin
                                      `-- /api    backend -> PostgreSQL/Redis
```

CLI поддерживает один domain и один upstream. Если frontend/admin — статические файлы,
удобно собрать их multi-stage Dockerfile и скопировать artifacts в gateway image.

## Структура

```text
my-app/
├── backend/Dockerfile
├── frontend/Dockerfile
├── admin/Dockerfile
├── gateway/Dockerfile
├── gateway/nginx.conf
├── deploy/compose.stage.yml
├── deploy/compose.prod.yml
└── .deploy/
    ├── config/global.yml
    ├── images.yml
    └── environments/stage/
        ├── config.yml
        └── app.env
```

## Описание сборок

`.deploy/images.yml`:

```yaml
---
schema_version: 1
services:
  backend:
    context: backend
    dockerfile: Dockerfile
    image: myapp-backend
  frontend:
    context: frontend
    dockerfile: Dockerfile
    image: myapp-frontend
  admin:
    context: admin
    dockerfile: Dockerfile
    image: myapp-admin
  gateway:
    context: gateway
    dockerfile: Dockerfile
    image: myapp-gateway
environments:
  stage:
    compose: deploy/compose.stage.yml
  prod:
    compose: deploy/compose.prod.yml
```

Имена services обязаны совпадать с Compose. Context и Dockerfile остаются внутри
project root.

## Deploy Compose

Ниже каркас; добавьте корректные команды и healthchecks своих images. После publisher
placeholder images будут заменены реальными digest.

```yaml
services:
  backend:
    image: ghcr.io/OWNER/myapp-backend@sha256:0000000000000000000000000000000000000000000000000000000000000000
    env_file: .env
    depends_on:
      postgres:
        condition: service_healthy
    networks: [private]
    healthcheck:
      test: ["CMD", "wget", "-q", "-O", "-", "http://127.0.0.1:8000/health"]
      interval: 10s
      timeout: 3s
      retries: 12
  frontend:
    image: ghcr.io/OWNER/myapp-frontend@sha256:0000000000000000000000000000000000000000000000000000000000000000
    networks: [private]
  admin:
    image: ghcr.io/OWNER/myapp-admin@sha256:0000000000000000000000000000000000000000000000000000000000000000
    networks: [private]
  gateway:
    image: ghcr.io/OWNER/myapp-gateway@sha256:0000000000000000000000000000000000000000000000000000000000000000
    ports: ["127.0.0.1:8080:8080"]
    networks: [private]
    depends_on:
      backend: {condition: service_healthy}
    healthcheck:
      test: ["CMD", "wget", "-q", "-O", "-", "http://127.0.0.1:8080/health"]
      interval: 10s
      timeout: 3s
      retries: 12
  postgres:
    image: postgres@sha256:REPLACE_WITH_REAL_64_HEX_DIGEST
    env_file: .env
    volumes: ["postgres-data:/var/lib/postgresql/data"]
    networks: [private]
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U $${POSTGRES_USER}"]
      interval: 10s
      timeout: 3s
      retries: 12
  redis:
    image: redis@sha256:REPLACE_WITH_REAL_64_HEX_DIGEST
    volumes: ["redis-data:/data"]
    networks: [private]
networks:
  private:
    driver: bridge
    internal: true
volumes:
  postgres-data:
  redis-data:
```

Третьесторонние images (`postgres`, `redis`) не описывайте в `images.yml`: выберите
версию и закрепите её реальным digest вручную. Custom declared `bridge` и `internal`
network допустимы; external/host/unmanaged network запрещены. Относительные bind mounts
запрещены; используйте named volumes или явно разрешённые absolute server paths.

`app.env` содержит `APP_ENV=stage`, DSN, пароли БД и ключи приложения. Он не попадает
в image или Git. Укажите имена обязательных переменных в `required_env_vars`.

## Релиз

```bash
deploy images publish stage --registry ghcr --namespace OWNER --username OWNER \
  --ask-token --ask-pull-token
git add deploy/compose.stage.yml
git commit -m "chore: prepare stage release"
deploy stage --ask-bootstrap-password
```

CLI не запускает DB migrations. Выполняйте их отдельно и делайте backward-compatible,
потому что rollback приложения не откатывает данные. Backup/restore БД также пока вне
CLI; универсальный backup — будущая roadmap-возможность после проектирования хранения,
шифрования, retention и проверки восстановления.

## Сейчас не поддерживается

Автоматизация DNS/provider firewall, secrets manager, migrations, backup/restore,
DB rollback, multi-host, rolling/zero downtime, автоматическая очистка releases/images
и несколько доменов.
