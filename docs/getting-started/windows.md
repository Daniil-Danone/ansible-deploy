# Первый deploy с Windows

## Результат

Вы соберёте demo локально, опубликуете приватные образы, а затем получите
`https://stage.example.com/health` на отдельном Ubuntu 24.04 VPS. Заменяйте значения
`OWNER`, IP, домен и email своими.

## 1. Подготовьте Windows

Нужны Python 3.12+, Git, Docker Desktop с Compose/Buildx и OpenSSH Client. PowerShell:

```powershell
winget install --id Python.Python.3.12 -e
winget install --id Git.Git -e
winget install --id Docker.DockerDesktop -e
```

Перезапустите PowerShell и запустите Docker Desktop. OpenSSH обычно уже включён. Если
`ssh` не найден, в PowerShell от администратора выполните:

```powershell
Add-WindowsCapability -Online -Name OpenSSH.Client~~~~0.0.1.0
```

Проверьте инструменты:

```powershell
py -3.12 --version
git --version
docker info
docker compose version
docker buildx version
ssh -V
```

Каждая команда должна завершиться успешно. `docker info` дополнительно подтверждает,
что запущен Docker daemon.

## 2. Установите CLI из `develop`

```powershell
py -3.12 -m venv $HOME\.venvs\ansible-deploy
& $HOME\.venvs\ansible-deploy\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install "ansible-deploy @ git+https://github.com/Daniil-Danone/ansible-deploy.git@develop"
deploy --help
```

`venv` изолирует зависимости CLI от глобального Python. В новом PowerShell снова
выполняйте строку `Activate.ps1`. Если PowerShell запрещает локальные скрипты:
`Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`.

## 3. Создайте отдельный demo-проект

Склонируйте `develop` только как источник demo, затем скопируйте пример **рядом**, а не
внутрь репозитория CLI:

```powershell
Set-Location C:\Code\MyRepos
git clone --branch develop --single-branch https://github.com/Daniil-Danone/ansible-deploy.git ansible-deploy-source
Copy-Item -Recurse C:\Code\MyRepos\ansible-deploy-source\examples\demo-app C:\Code\MyRepos\demo-app
Set-Location C:\Code\MyRepos\demo-app
Copy-Item .deploy\environments\stage\.env.example .deploy\environments\stage\app.env
git init
git add .
git commit -m "feat: initialize demo application"
```

Это уже независимый проект. Commit обязателен: его SHA — версия релиза и базовый tag
образов. `app.env`, ключи и registry auth исключены из Git.

## 4. Проверьте приложение локально

```powershell
docker compose -f compose.local.yml up --build -d
curl.exe --fail http://127.0.0.1:8080/health
curl.exe --fail http://127.0.0.1:8080/api/health
docker compose -f compose.local.yml down
```

Первая команда собирает и запускает demo, две следующие проверяют frontend и backend,
последняя удаляет тестовые контейнеры.

## 5. Подготовьте VPS и DNS

Нужен отдельный чистый Ubuntu 24.04 VPS: публичный IP, root-пароль и разрешённые у
провайдера TCP-порты SSH (обычно 22), 80 и 443. Создайте DNS A-запись домена на IP.

```powershell
Resolve-DnsName stage.example.com
```

Ответ должен содержать IP VPS. Адрес `198.18.*` обычно означает fake-IP Mihomo/Clash:
выключите TUN/System Proxy или добавьте домен в `fake-ip-filter`, затем выполните
`Clear-DnsClientCache`.

Через доверенную web/serial-консоль провайдера, не через первое SSH-соединение,
выполните на VPS:

```bash
ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub -E sha256
```

Скопируйте `SHA256:...`: это **host key fingerprint**, удостоверяющий сервер.

## 6. Разберитесь с тремя секретами

- Root-пароль нужен один раз для bootstrap и не сохраняется.
- Host key принадлежит серверу; его fingerprint защищает от подключения к подмене.
- Deploy key принадлежит вашему проекту. CLI автоматически создаёт незашифрованную
  Ed25519-пару при первом обычном deploy, только если отсутствуют обе части:
  `.deploy/keys/stage_ed25519` и `.pub`. Private key остаётся локально и игнорируется
  Git, public key устанавливается пользователю `deploy`. В runtime копия ключа получает
  `0600` даже при Windows bind mount; исходник защищается owner-only ACL.

CLI не перезаписывает существующую пару и отклоняет половину пары. Сделайте защищённый
backup private key: без него новый компьютер не сможет войти как `deploy`. Для prod
рекомендуется отдельная пара. Passphrase сейчас не поддерживается автоматическим flow.

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

В `.deploy/environments/stage/app.env` оставьте `APP_ENV=stage`. Здесь будут runtime
секреты приложения; файл не коммитится.

## 8. Опубликуйте приватные образы

Создайте **два разных** token: publish с записью и server pull только с чтением.
Для GHCR откройте GitHub → Settings → Developer settings → Personal access tokens →
Tokens (classic) → Generate new token. Быстрые ссылки:
[publish `write:packages`](https://github.com/settings/tokens/new?scopes=write:packages) и
[отдельный pull `read:packages`](https://github.com/settings/tokens/new?scopes=read:packages).
Для организации может потребоваться Configure SSO. Fine-grained token здесь не подходит.
Для Docker Hub откройте Account settings → Personal access tokens и создайте отдельные
Read/Write и Read-only token по
[официальной инструкции Docker](https://docs.docker.com/security/access-tokens/).

GHCR:

```powershell
deploy images publish stage `
  --registry ghcr --namespace OWNER --username OWNER `
  --ask-token --ask-pull-token
```

Docker Hub — та же операция с другим registry:

```powershell
deploy images publish stage `
  --registry dockerhub --namespace OWNER --username OWNER `
  --ask-token --ask-pull-token
```

Первый скрытый ввод — publish token, второй — read-only token VPS. CLI локально строит
и push-ит все образы, проверяет каждый immutable digest, обновляет Compose и создаёт
portable `registry-auth.json`. Он не копирует исходники на сервер.

```powershell
Select-String deploy\compose.stage.yml -Pattern '@sha256:'
git add deploy\compose.stage.yml
git commit -m "chore: pin stage image digests"
```

Commit фиксирует точный набор образов. Packages можно оставить Private.

## 9. Выполните первый deploy

```powershell
deploy stage --ask-bootstrap-password
```

CLI безопасно запрашивает пароль до запуска workflow, но использует его только если
managed probe обнаружил чистый сервер и нужен bootstrap. Затем создаёт ключ/`deploy`,
проверяет passwordless sudo и продолжает уже управляемый deploy.
Advanced-вариант: если у `bootstrap_user` заранее установлен SSH public key, первый
deploy можно запустить без password flag. Следующие deploy используют `deploy`.
На сервере устанавливаются Docker, Nginx, Certbot, UFW, Fail2ban, unattended upgrades
и reboot timer (timezone сейчас `Europe/Moscow`); root password SSH login отключается,
но root account целиком не блокируется. `deploy` получает `NOPASSWD sudo` и группу
`docker` — это привилегированный пользователь. Registry auth попадает в
`/home/deploy/.docker/config.json`, releases — под `/srv/demo-stage`, Nginx проксирует
домен на `127.0.0.1:8080`, Certbot выпускает сертификат.

Повторные операции обычно запускайте без password flag — root больше не нужен:

```powershell
deploy stage
deploy stage --dry-run
deploy status stage
curl.exe --fail --location https://stage.example.com/health
```

`--dry-run` на совершенно чистом сервере намеренно не делает bootstrap и завершится
ошибкой. `status` проверяет только публичный HTTPS GET, а не SSH/контейнеры/диск.

## 10. Критерии готовности

```powershell
ssh -i .deploy\keys\stage_ed25519 deploy@203.0.113.10 "sudo -n true"
ssh -i .deploy\keys\stage_ed25519 deploy@203.0.113.10 "sudo ufw status verbose"
ssh -i .deploy\keys\stage_ed25519 deploy@203.0.113.10 "sudo ss -lntup"
```

HTTPS отвечает, повторный deploy успешен, снаружи открыты только SSH/80/443, приложение
слушает loopback `8080`. Zero downtime не обещается.

Если что-то упало, найдите симптом в [решении проблем](../troubleshooting.md).
