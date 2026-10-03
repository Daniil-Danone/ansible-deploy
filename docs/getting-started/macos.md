# Первый deploy с macOS

## Результат

Вы локально проверите demo, опубликуете приватные образы и развернёте HTTPS-приложение
на отдельном Ubuntu 24.04 VPS. Заменяйте `OWNER`, IP, домен и email своими значениями.

## 1. Подготовьте macOS

Установите Python, Git и Docker Desktop; OpenSSH уже входит в macOS:

```bash
brew install python@3.12 git
brew install --cask docker
open -a Docker
python3.12 --version
git --version
docker info
docker compose version
docker buildx version
ssh -V
```

`docker info` должен подтвердить запущенный daemon. Если Homebrew отсутствует, сначала
установите его с официального сайта brew.sh.

## 2. Изолированно установите CLI

```bash
python3.12 -m venv "$HOME/.venvs/ansible-deploy"
source "$HOME/.venvs/ansible-deploy/bin/activate"
python -m pip install --upgrade pip
python -m pip install "ansible-deploy @ git+https://github.com/Daniil-Danone/ansible-deploy.git@develop"
deploy --help
```

`venv` не даёт зависимостям CLI менять глобальный Python. В новом Terminal повторяйте
команду `source`.

## 3. Создайте отдельный demo-проект

Склонируйте `develop` как источник demo и скопируйте пример **рядом**, не внутрь
репозитория CLI:

```bash
mkdir -p "$HOME/src"
git clone --branch develop --single-branch \
  https://github.com/Daniil-Danone/ansible-deploy.git "$HOME/src/ansible-deploy-source"
cp -R "$HOME/src/ansible-deploy-source/examples/demo-app" "$HOME/demo-app"
cd "$HOME/demo-app"
cp .deploy/environments/stage/.env.example .deploy/environments/stage/app.env
git init
git add .
git commit -m "feat: initialize demo application"
```

Commit нужен не формально: CLI использует его SHA как версию релиза и базовый tag
образов. `app.env`, deploy keys и registry auth уже исключены из Git.

## 4. Проверьте demo локально

```bash
docker compose -f compose.local.yml up --build -d
curl --fail http://127.0.0.1:8080/health
curl --fail http://127.0.0.1:8080/api/health
docker compose -f compose.local.yml down
```

Команды собирают demo, проверяют frontend/backend и останавливают тестовые контейнеры.

## 5. Подготовьте сервер, DNS и fingerprint

Нужен отдельный чистый Ubuntu 24.04 VPS: публичный IP, root-пароль и открытые у
провайдера TCP-порты SSH (обычно 22), 80 и 443. Создайте DNS A-запись домена на IP:

```bash
dig +short stage.example.com
```

Должен вернуться IP VPS. `198.18.*` означает fake-IP Mihomo/Clash: отключите TUN либо
добавьте домен в `fake-ip-filter`, затем очистите DNS-кэш/перезапустите клиент.

В доверенной web/serial-консоли провайдера выполните на VPS:

```bash
ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub -E sha256
```

Скопируйте `SHA256:...`; не узнавайте первый fingerprint по тому же непроверенному SSH.

## 6. Пароль, host key и deploy key

- Root-пароль одноразово открывает bootstrap и не сохраняется.
- Host key — удостоверение VPS; закреплённый fingerprint защищает от MITM.
- Deploy key — локальная Ed25519-пара проекта. На первом обычном deploy CLI создаёт
  незашифрованные `.deploy/keys/stage_ed25519` и `.pub`, только если отсутствуют обе.
  Private key остаётся локально, public key устанавливается пользователю `deploy`.

CLI не перезаписывает ключи и отклоняет неполную пару. Файлы игнорируются Git; private
key получает `0600`, но всё равно сохраните его в защищённом backup. Для prod используйте
другой ключ. Passphrase пока не поддерживается автоматическим flow.

## 7. Настройте Stage

`.deploy/environments/stage/config.yml`:

```yaml
---
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
    - SHA256:REPLACE_WITH_43_CHARACTER_FINGERPRINT
application:
  compose: deploy/compose.stage.yml
  env_file: .deploy/environments/stage/app.env
  registry_auth_file: .deploy/environments/stage/registry-auth.json
  remote_dir: /srv/demo-stage
  allowed_loopback_ports: [8080]
  allowed_bind_paths: []
  required_env_vars: [APP_ENV]
domain: stage.example.com
acme_email: you@example.com
health_path: /health
```

В `.deploy/environments/stage/app.env` укажите `APP_ENV=stage`; здесь же будут секреты
приложения. Файл не коммитится.

## 8. Опубликуйте private images

Подготовьте **два разных** token. Для GHCR: GitHub → Settings → Developer settings →
Personal access tokens → Tokens (classic) → Generate new token. Создайте
[publish `write:packages`](https://github.com/settings/tokens/new?scopes=write:packages) и
[отдельный pull `read:packages`](https://github.com/settings/tokens/new?scopes=read:packages).
Организации может понадобиться Configure SSO; Fine-grained token не подходит.
Для Docker Hub: Account settings → Personal access tokens; создайте отдельные
Read/Write и Read-only token по
[официальной инструкции](https://docs.docker.com/security/access-tokens/).

```bash
deploy images publish stage \
  --registry ghcr --namespace OWNER --username OWNER \
  --ask-token --ask-pull-token
```

Для Docker Hub замените `ghcr` на `dockerhub`. Первый скрытый ввод — publish token,
второй — read-only token для VPS. CLI локально build/push-ит все образы, получает и
проверяет immutable digest, обновляет Compose и создаёт portable `registry-auth.json`
с `0600`. Не копируйте `~/.docker/config.json`: Docker Desktop обычно использует
`osxkeychain`, которого нет на VPS.

```bash
grep '@sha256:' deploy/compose.stage.yml
git add deploy/compose.stage.yml
git commit -m "chore: pin stage image digests"
```

Packages можно оставить Private; исходники на VPS не отправляются.

## 9. Первый и последующие deploy

```bash
deploy stage --ask-bootstrap-password
```

CLI безопасно запрашивает пароль до workflow, но использует его только если managed
probe обнаружил чистый сервер. Затем создаёт ключ/`deploy`, проверяет `sudo -n` и
продолжает уже через managed user. Advanced
вариант: если у `bootstrap_user` заранее установлен SSH public key, первый deploy можно
запустить без password flag. Следующие deploy всегда используют `deploy`.

На VPS устанавливаются Docker, Nginx, Certbot, UFW, Fail2ban, unattended upgrades и
reboot timer (`Europe/Moscow`). Root password SSH login отключается, но root account не
блокируется полностью. `deploy` получает NOPASSWD sudo и группу Docker (оба дают большие
привилегии). Auth лежит в `/home/deploy/.docker/config.json`, релизы — `/srv/demo-stage`.
Nginx проксирует единственный домен на `127.0.0.1:8080`, Certbot выпускает TLS.

Последующие deploy обычно запускайте без password flag:

```bash
deploy stage
deploy stage --dry-run
deploy status stage
curl --fail --location https://stage.example.com/health
```

Dry-run чистого сервера не bootstrap-ит его и завершается ошибкой. `status` — только
HTTPS GET, не полная диагностика. Zero downtime не гарантируется.

## 10. Приёмка

```bash
chmod 600 .deploy/keys/stage_ed25519
ssh -i .deploy/keys/stage_ed25519 deploy@203.0.113.10 'sudo -n true'
ssh -i .deploy/keys/stage_ed25519 deploy@203.0.113.10 'sudo ufw status verbose'
ssh -i .deploy/keys/stage_ed25519 deploy@203.0.113.10 'sudo ss -lntup'
```

HTTPS отвечает, повторный deploy успешен, снаружи открыты лишь SSH/80/443, upstream
слушает loopback `8080`. Ошибки разобраны в [troubleshooting](../troubleshooting.md).
