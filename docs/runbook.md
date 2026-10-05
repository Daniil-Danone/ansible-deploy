# Канонический deployment runbook

Этот документ задаёт общий порядок для Windows, macOS и Ubuntu. Сначала установите
платформенные зависимости по ссылке из [карты документации](README.md). Все команды
выполняются из application repository.

## 1. Инициализировать или обновить commit-safe scaffold

```bash
deploy project init
git add .deploy deploy
git commit -m "chore: initialize deployment configuration"
```

`project init` безопасно дополняет старый stage-only проект Production, Monitoring и
Restore skeleton. `.deploy/project-id` коммитится и остаётся стабильным после clone.
`.deploy/template-state.yml` хранит версию шаблона и hashes, но не секреты.

Отредактируйте global/environment configs, `.deploy/images.yml` и Compose. Зафиксируйте
SSH host fingerprint из доверенного канала, а не из первого подключения.

## 2. Подготовить внешний secret store

```bash
deploy secrets path
```

Результат — project-scoped root реальных файлов. Создайте в нём с owner-only правами:

```text
environments/stage/app.env
environments/prod/app.env
environments/monitoring/monitoring.env
environments/*/registry-auth.json
keys/*_ed25519
backup/rclone.conf
backup/age.key
```

Пути schema v2 относительны этому root. Не копируйте файлы в `.deploy/`, не передавайте
значения через CLI arguments и не печатайте их в CI logs.

SSH key можно импортировать существующей парой или не создавать заранее: первый
обычный deploy атомарно создаст Ed25519 pair по configured paths. Dry-run ключи не
создаёт. Не создавайте только одну половину пары и не используйте один private key для
Stage и Production.

## 3. Stage

```bash
deploy images publish stage --registry ghcr --namespace OWNER --username OWNER --ask-token --ask-pull-token
deploy stage --ask-bootstrap-password --version GIT_SHA
deploy status stage
```

На чистом VPS первый запуск должен быть обычным deploy: dry-run не выполняет bootstrap.
После bootstrap проверьте следующую итерацию через `deploy stage --dry-run --version
GIT_SHA`, затем повторите обычный deploy и status. Проверьте DNS, provider firewall,
host fingerprint, Compose ports и health endpoint. Повторный `deploy stage` должен быть
безопасен и иметь только объяснимые изменения.

## 4. Production

Production использует отдельные VPS, domain, remote directory, Compose, SSH key, env
и registry credential. Images должны быть закреплены digest.

```bash
deploy images publish prod --registry ghcr --namespace OWNER --username OWNER --ask-token --ask-pull-token
deploy prod --ask-bootstrap-password --version GIT_SHA
deploy status prod
```

Интерактивно введите `prod`; `--yes` предназначен для защищённой CI boundary. На уже
подготовленном VPS сначала допустим `deploy prod --dry-run --version GIT_SHA`. Не
продолжайте, если `deploy stage`, `deploy status stage` и health gate для того же Git
SHA не прошли успешно.

## 5. Monitoring и collectors

```bash
deploy monitoring deploy --ask-bootstrap-password
deploy monitoring status
deploy collectors deploy all --yes
deploy collectors status all
```

На чистом Monitoring VPS dry-run также не выполняет bootstrap. После первого deploy
проверяйте изменения через `deploy monitoring deploy --dry-run`, затем применяйте их
обычной командой.

В Grafana вручную подтвердите свежие записи Stage и Production с метками environment,
host и service. HTTPS health monitoring не доказывает доставку логов collectors.

## 6. Backup и Restore Drill

```bash
deploy backup setup prod
deploy backup run prod
deploy backup list prod
deploy backup restore prod --target restore --backup BACKUP_ID
```

Restore выполняйте на отдельном `restore`, никогда поверх Production. Полный порядок:
[backup/restore](guides/backup-restore.md).

## 7. Обновление состояния

```bash
deploy server update stage --dry-run
deploy server update stage
deploy server update prod
deploy server update monitoring
```

Для нового CLI сначала используйте `deploy project sync --check`; конфликты описаны в
[upgrade guide](guides/upgrading.md).

## 8. Приёмка

Локально воспроизводимо:

```bash
python -m pytest -o addopts= -q -ra
python -m ruff check .
python -m mypy
yamllint .
ansible-lint ansible
make syntax-check
git diff --check
```

Вручную: HTTPS Stage/Prod, SSH/UFW, повторный deploy, Grafana logs, remote backup
checksum, содержательный Restore Drill, protected Production approval и serialized
deploy. Записывайте дату, Git SHA, target и наблюдение; отсутствие доступов отмечайте
как `not verified`, а не как успех. При падении следуйте
[диагностическому дереву](troubleshooting.md).
