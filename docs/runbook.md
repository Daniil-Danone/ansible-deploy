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

Отредактируйте global/environment configs, `.deploy/images.yml` и Compose. Host, domain и
acme_email задаются вручную; `server.host_key_fingerprints` остаётся пустым — его
заполняет `deploy trust <environment>` на шаге 3.

## 2. Подготовить внешний secret store

```bash
deploy secrets path
deploy secrets init
```

`secrets path` печатает project-scoped root реальных файлов. `secrets init` создаёт в нём
owner-only каталоги `environments/<env>/`, `keys/` и `backup/` и заготовки secret-файлов
из закоммиченных `.env.example`. Существующий файл никогда не перезаписывается, повторный
запуск идемпотентен. В отчёте `[CREATE]` — созданное, `[KEEP]` — уже существовавшее,
`[FILL]` — переменные, которые обязан заполнить оператор.

Остальные файлы создайте сами с owner-only правами:

```text
environments/*/registry-auth.json
environments/*/collector.password
backup/rclone.conf
backup/age.key
```

Пути schema v2 относительны этому root. Не копируйте файлы в `.deploy/`, не передавайте
значения через CLI arguments и не печатайте их в CI logs. Crypt SHA-512 hash для
`LOKI_PUSH_PASSWORD_HASH` считает `deploy secrets hash-password` — см.
[monitoring](guides/monitoring.md).

SSH key можно импортировать существующей парой или не создавать заранее: первый
обычный deploy атомарно создаст Ed25519 pair по configured paths. Dry-run ключи не
создаёт. Не создавайте только одну половину пары и не используйте один private key для
Stage и Production.

## 3. Stage

```bash
deploy trust stage
deploy images publish stage --registry ghcr --namespace OWNER --username OWNER --ask-token --ask-pull-token
deploy stage --ask-bootstrap-password --version GIT_SHA
deploy status stage
```

`trust stage` сканирует SSH host key сервера и записывает его fingerprint'ы в
`.deploy/environments/stage/config.yml`. Сверьте напечатанные значения с консолью
провайдера и закоммитьте изменение — доверие ключу остаётся осознанным шагом, а не
побочным эффектом первого подключения. Пока список пуст, любая операция завершается
кодом `2`. Если позже ключ изменился, команда ничего не перезаписывает и требует
`--force`.

На чистом VPS первый запуск должен быть обычным deploy: dry-run не выполняет bootstrap.
После bootstrap проверьте следующую итерацию через `deploy stage --dry-run --version
GIT_SHA`, затем повторите обычный deploy и status. Проверьте DNS, provider firewall,
host fingerprint, Compose ports и health endpoint. Повторный `deploy stage` должен быть
безопасен и иметь только объяснимые изменения.

## 4. Production

Production использует отдельные VPS, domain, remote directory, Compose, SSH key, env
и registry credential. Images должны быть закреплены digest.

```bash
deploy trust prod
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
deploy trust monitoring
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
deploy trust restore
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
