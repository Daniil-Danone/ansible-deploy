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

## 3. Stage

```bash
deploy images publish stage --registry ghcr --namespace OWNER --ask-token
deploy stage --dry-run
deploy stage
deploy status stage
```

Проверьте DNS, provider firewall, host fingerprint, Compose ports и health endpoint.
Повторный `deploy stage` должен быть безопасен и иметь только объяснимые изменения.

## 4. Production

Production использует отдельные VPS, domain, remote directory, Compose, SSH key, env
и registry credential. Images должны быть закреплены digest.

```bash
deploy images publish prod --registry ghcr --namespace OWNER --ask-token
deploy prod --dry-run
deploy prod
deploy status prod
```

Интерактивно введите `prod`; `--yes` предназначен для защищённой CI boundary. Не
продолжайте, если Stage для того же Git SHA не прошёл health gate.

## 5. Monitoring и collectors

```bash
deploy monitoring deploy --dry-run
deploy monitoring deploy
deploy monitoring status
deploy collectors deploy all
deploy collectors status all
```

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
