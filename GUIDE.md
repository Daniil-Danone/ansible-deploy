# Руководство по `ansible-deploy`

## Что делает CLI

CLI запускается из репозитория приложения. Он проверяет локальную конфигурацию,
собирает из собственных package resources изолированный Ansible Docker image,
проверяет SSH host key, подготавливает Ubuntu 24.04, настраивает hardening, Docker,
Nginx и Let's Encrypt, затем запускает заранее собранные registry images через Compose.

CLI не отправляет исходники и не выполняет `docker build` на сервере. Сборка и публикация
образов — отдельный CI/локальный шаг разработчика.

## Требования

- локально: Python 3.12+, Git, Docker и OpenSSH client;
- сервер: чистый Ubuntu 24.04, публичный IP, открытые TCP 22 (или другой SSH), 80, 443;
- DNS A/AAAA домена уже указывает на сервер;
- root-пароль либо начальный SSH key login;
- SHA256 fingerprint host key, полученный через доверенную web/serial-консоль провайдера.

В консоли сервера fingerprint получают так:

```bash
ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub -E sha256
```

## Установка

Из GitHub `develop`:

```powershell
python -m pip install "ansible-deploy @ git+https://github.com/Daniil-Danone/ansible-deploy.git@develop"
deploy --help
```

Для разработки самого CLI:

```powershell
python -m pip install -e C:\Code\MyRepos\ansible-deploy\ansible-deploy
```

CLI можно вызывать из корня приложения либо передать путь явно:

```powershell
deploy stage
deploy --project-dir "C:\Code\Projects\My App" stage
```

`--project-dir` по умолчанию равен текущей директории. Скрытый `--repo` пока сохранён
как deprecated-совместимость, но в новых скриптах использовать его не следует.

## Структура проекта

```text
my-app/
├── backend/
├── frontend/
├── deploy/
│   ├── compose.stage.yml
│   └── compose.prod.yml
├── .deploy/
│   ├── config/global.yml
│   ├── keys/                         # ignored, создаётся CLI
│   └── environments/
│       ├── stage/
│       │   ├── config.yml
│       │   └── app.env               # ignored, секреты
│       └── prod/
│           ├── config.yml
│           ├── app.env               # ignored
│           └── registry-auth.json     # optional, ignored
└── .deploy-state/                     # ignored, inventory + known_hosts
```

Скопировать рабочий пример:

```powershell
Copy-Item -Recurse C:\Code\MyRepos\ansible-deploy\ansible-deploy\examples\demo-app C:\Code\MyRepos\demo-app
cd C:\Code\MyRepos\demo-app
Copy-Item .deploy\environments\stage\.env.example .deploy\environments\stage\app.env
```

Обязательно добавьте в `.gitignore`:

```gitignore
.deploy-state/
.deploy/keys/
.deploy/environments/*/app.env
.deploy/environments/*/registry-auth.json
```

## Конфигурация

`.deploy/config/global.yml` задаёт security updates, условный reboot timer и hardening
controls. Обычно пример можно оставить без изменений.

`.deploy/environments/stage/config.yml`:

```yaml
schema_version: 1
environment: stage
server:
  host: 203.0.113.10
  ssh_port: 22
  bootstrap_user: root
  deploy_user: deploy
  ssh_key: .deploy/keys/stage_ed25519
  public_key: .deploy/keys/stage_ed25519.pub
  host_key_fingerprints:
    - SHA256:REAL_FINGERPRINT_FROM_PROVIDER_CONSOLE
application:
  compose: deploy/compose.stage.yml
  env_file: .deploy/environments/stage/app.env
  remote_dir: /srv/my-app-stage
  allowed_loopback_ports: [8080]
  allowed_bind_paths: []
  required_env_vars: [APP_ENV]
domain: stage.example.com
acme_email: ops@example.com
health_path: /health
```

- `host`, `ssh_port`, `bootstrap_user` — начальный SSH endpoint;
- `deploy_user` — создаваемый управляемый пользователь;
- `ssh_key`/`public_key` — локальная пара; CLI создаёт Ed25519 пару, только если обе части отсутствуют;
- `host_key_fingerprints` — доверенные fingerprints сервера;
- `compose`, `env_file`, `registry_auth_file` — пути относительно корня приложения;
- `remote_dir` — уникальный каталог под `/srv` или `/opt`;
- `allowed_loopback_ports` — разрешённые published ports, только на `127.0.0.1`;
- `allowed_bind_paths` — явно разрешённые абсолютные server bind mounts;
- `required_env_vars` — обязательные имена в env;
- `domain`, `acme_email`, `health_path` — TLS и публичная health-проверка.

Относительные пути не могут выходить за корень проекта. Абсолютные пути допустимы,
например для уже существующего SSH key. `.deploy-state` не может быть symlink.

Env минимум:

```dotenv
APP_ENV=stage
```

`APP_ENV` обязан совпадать с выбранным окружением. CLI читает env для валидации и
redaction, но не печатает значения.

## Сборка и публикация demo images

Проверить пример локально:

```powershell
docker compose -f compose.local.yml up --build
curl.exe http://127.0.0.1:8080/health
curl.exe http://127.0.0.1:8080/api/health
docker compose -f compose.local.yml down
```

Затем войти в registry, собрать и отправить images. Замените `OWNER` и `TAG`:

```powershell
docker login ghcr.io
docker build -t ghcr.io/OWNER/demo-backend:TAG backend
docker build -t ghcr.io/OWNER/demo-frontend:TAG frontend
docker push ghcr.io/OWNER/demo-backend:TAG
docker push ghcr.io/OWNER/demo-frontend:TAG
docker buildx imagetools inspect ghcr.io/OWNER/demo-backend:TAG
docker buildx imagetools inspect ghcr.io/OWNER/demo-frontend:TAG
```

В `deploy/compose.stage.yml` замените `OWNER` и нулевые digest на полученные значения:

```yaml
image: ghcr.io/OWNER/demo-backend@sha256:<64 hex>
```

Для private registry сохраните Docker config JSON в ignored-файл и добавьте в config:

```yaml
application:
  registry_auth_file: .deploy/environments/stage/registry-auth.json
```

Никогда не передавайте registry token аргументом CLI и не коммитьте этот файл.

## Первый Stage deploy с root-паролем

1. Создайте DNS A/AAAA и дождитесь, пока домен резолвится в IP сервера.
2. Заполните Stage config, env и digest-pinned Compose.
3. Запустите Docker Desktop/daemon.
4. Выполните:

```powershell
deploy stage --ask-bootstrap-password
```

Пароль вводится скрыто, передаётся контейнеру через stdin, используется одноразовым
helper и не сохраняется. CLI создаст SSH key, установит public key пользователю `deploy`,
проверит `sudo -n true`, затем отключит password authentication. Повторные deploy:

```powershell
deploy stage
deploy status stage
```

`--ask-bootstrap-password` нельзя сочетать с `--dry-run`. После первого deploy:

```powershell
deploy stage --dry-run
deploy server update stage --dry-run
deploy server update stage
```

## Версия релиза

По умолчанию CLI использует `HEAD` Git-репозитория приложения, а не CLI. Если проект
не является Git-репозиторием или нужен другой immutable SHA, укажите lowercase SHA:

```powershell
deploy stage --version abcdef0123456789
```

Допустимы только 7–40 lowercase hex символов. Повторное использование SHA с другими
Compose/env/config inputs отклоняется.

## Production

Production обязан использовать отдельные VPS, домен, SSH key, Compose, env и remote_dir.
Каждый image должен иметь `@sha256:<digest>`; `build`, mutable tags и публичные bindings
отклоняются.

```powershell
deploy prod --dry-run --version <git-sha>
deploy prod --version <git-sha>
deploy status prod
deploy server update prod
deploy server update all
```

Изменяющие Production-команды требуют ввести `prod`; для CI используется `--yes`.
Первый парольный вход: `deploy prod --ask-bootstrap-password --version <sha>`.

Rollback приложения:

```powershell
deploy rollback prod
```

Rollback возвращает предыдущие Compose/env/images и проверяет health, но **не откатывает
базу данных и миграции**. Миграции должны быть backward-compatible либо иметь отдельный
reviewed recovery plan.

## Ограничения Compose и безопасность

- published port только `127.0.0.1:<allowed>:<container>`;
- запрещены host/external/custom networks, `network_mode`, `include`, `extends`;
- relative bind mounts запрещены; используйте named volumes или approved absolute binds;
- Production images только digest-pinned, server-side builds запрещены;
- SSH host key закрепляется раздельно в `.deploy-state/stage|prod/known_hosts`;
- application env, Compose и registry auth доставляются с mode `0600`;
- секреты редактируются в streamed output, sensitive Ansible tasks используют `no_log`.

## Диагностика

```powershell
Resolve-DnsName stage.example.com
docker info
deploy --project-dir "C:\path with spaces\app" status stage
ssh -i .deploy/keys/stage_ed25519 deploy@SERVER "sudo -n true"
```

Если Git SHA определить нельзя — инициализируйте/закоммитьте проект или используйте
`--version`. Если изменился host key, CLI остановится: проверьте причину через консоль
провайдера; не удаляйте `known_hosts` без out-of-band проверки. При ошибке ACME проверьте
DNS и внешнюю доступность 80/443. При ошибке image pull проверьте digest и registry auth.

Коды завершения:

| Код | Значение |
|---:|---|
| 0 | успех |
| 1 | общая ошибка или прерывание |
| 2 | конфигурация, подтверждение или DNS |
| 3 | SSH host authentication / environment guard |
| 4 | SSH/access verification |
| 5 | provisioning или runtime |
| 6 | deployment transaction |
| 7 | health check |
| 9 | rollback |

## Acceptance после первого deploy

```powershell
deploy stage
deploy status stage
curl.exe --fail --location https://stage.example.com/health
ssh -i .deploy/keys/stage_ed25519 deploy@SERVER "sudo -n true"
ssh -i .deploy/keys/stage_ed25519 deploy@SERVER "sudo ufw status verbose"
ssh -i .deploy/keys/stage_ed25519 deploy@SERVER "sudo ss -lntup"
```

Ожидается: открыты только SSH/80/443, upstream слушает `127.0.0.1:8080`, password login
отключён, HTTPS health успешен. Повторный deploy должен быть сходящимся без downtime.
