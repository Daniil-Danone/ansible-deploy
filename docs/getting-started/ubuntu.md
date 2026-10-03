# Первый deploy с Ubuntu

## Результат

С локального Ubuntu 24.04-компьютера вы соберёте demo, опубликуете private images и получите
HTTPS-приложение на отдельном Ubuntu 24.04 VPS. `OWNER`, IP, домен и email замените.

## 1. Подготовьте локальную Ubuntu

Поддерживаемый локальный путь ниже рассчитан на Ubuntu 24.04, где стандартный
`python3` — версии 3.12. Установите инструменты и подключите официальный Docker apt
repository:

```bash
sudo apt update
sudo apt install -y python3 python3-venv git openssh-client ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
  -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo \"$VERSION_CODENAME\") stable" \
  | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null
sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo usermod -aG docker "$USER"
newgrp docker
python3 --version
git --version
docker info
docker compose version
docker buildx version
ssh -V
```

Python должен быть 3.12+, а `docker info` — видеть daemon. Группа `docker` фактически
даёт root-права; добавляйте только доверенного локального пользователя.

## 2. Изолированно установите CLI

```bash
python3 -m venv "$HOME/.venvs/ansible-deploy"
source "$HOME/.venvs/ansible-deploy/bin/activate"
python -m pip install --upgrade pip
python -m pip install "ansible-deploy @ git+https://github.com/Daniil-Danone/ansible-deploy.git@develop"
deploy --help
```

`venv` изолирует зависимости. В каждой новой shell повторяйте `source`.

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

CLI использует commit SHA как версию релиза и tag образов, поэтому commit обязателен.
`app.env`, ключи и registry auth уже игнорируются Git.

## 4. Проверьте demo локально

```bash
docker compose -f compose.local.yml up --build -d
curl --fail http://127.0.0.1:8080/health
curl --fail http://127.0.0.1:8080/api/health
docker compose -f compose.local.yml down
```

Это сборка, две health-проверки и удаление локальных контейнеров.

## 5. Подготовьте VPS, DNS и fingerprint

Нужен отдельный чистый Ubuntu 24.04 VPS, публичный IP, root-пароль и открытые у
провайдера TCP SSH (обычно 22), 80, 443. Создайте DNS A-запись:

```bash
getent ahostsv4 stage.example.com
```

В ответе должен быть IP VPS. `198.18.*` обычно создаёт fake-IP Mihomo/Clash: выключите
TUN или добавьте домен в `fake-ip-filter`, очистите DNS-кэш.

Через web/serial-консоль провайдера выполните на VPS:

```bash
ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub -E sha256
```

Скопируйте `SHA256:...`. Доверенная консоль важна: fingerprint, полученный через ещё
не проверенный SSH, не защищает от подмены.

## 6. Три разных механизма доступа

- Root-пароль только запускает первый bootstrap, CLI его не хранит.
- Host key принадлежит серверу; fingerprint закрепляет его идентичность.
- Deploy key принадлежит проекту. CLI при первом не-dry deploy автоматически создаёт
  незашифрованные `.deploy/keys/stage_ed25519` и `.pub`, если обе части отсутствуют.
  Private key остаётся локально, public key устанавливается пользователю `deploy`.

Ключи игнорируются Git; private key получает `0600`. CLI не перезаписывает пару и
отклоняет только одну существующую часть. Сделайте безопасный backup private key;
используйте отдельный prod key. Passphrase автоматическим flow пока не поддерживается.

## 7. Заполните Stage

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

В `.deploy/environments/stage/app.env` запишите `APP_ENV=stage` и runtime-секреты.
Файл не коммитится.

## 8. Опубликуйте private images

Создайте **два разных** token. Для GHCR: GitHub → Settings → Developer settings →
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

Для Docker Hub замените registry на `dockerhub`. Первый скрытый ввод — publish token,
второй — read-only server token. CLI локально собирает/push-ит образы, проверяет digest,
обновляет Compose и создаёт portable `registry-auth.json` с `0600`; исходники на VPS
не отправляются.

```bash
grep '@sha256:' deploy/compose.stage.yml
git add deploy/compose.stage.yml
git commit -m "chore: pin stage image digests"
```

## 9. Первый deploy

```bash
deploy stage --ask-bootstrap-password
```

CLI безопасно запрашивает пароль до workflow, но использует его только если managed
probe обнаружил чистый сервер. Затем создаёт ключ/`deploy`, проверяет passwordless sudo
и продолжает как managed user.
Advanced path: если у `bootstrap_user` заранее установлен public key, первый deploy
можно вызвать без password flag. В дальнейшем root не используется.

На VPS ставятся Docker, Nginx, Certbot, UFW, Fail2ban, unattended upgrades и reboot
timer (`Europe/Moscow`). Root password login по SSH отключается, но root account не
блокируется полностью. `deploy` получает NOPASSWD sudo и группу Docker — это
привилегированный доступ. Auth хранится в `/home/deploy/.docker/config.json`, релизы —
`/srv/demo-stage`; Nginx ведёт один домен на `127.0.0.1:8080`, Certbot включает TLS.

Последующие deploy обычно запускайте без password flag:

```bash
deploy stage
deploy stage --dry-run
deploy status stage
curl --fail --location https://stage.example.com/health
```

Dry-run на чистом VPS ничего не меняет и не bootstrap-ит сервер. `status` проверяет
только HTTPS GET. Zero downtime не обещается.

## 10. Приёмка

```bash
chmod 600 .deploy/keys/stage_ed25519
ssh -i .deploy/keys/stage_ed25519 deploy@203.0.113.10 'sudo -n true'
ssh -i .deploy/keys/stage_ed25519 deploy@203.0.113.10 'sudo ufw status verbose'
ssh -i .deploy/keys/stage_ed25519 deploy@203.0.113.10 'sudo ss -lntup'
```

HTTPS отвечает, повторный deploy успешен, наружу доступны лишь SSH/80/443, upstream
слушает loopback `8080`. Диагностика: [решение проблем](../troubleshooting.md).
