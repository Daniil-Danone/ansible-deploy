# ansible-deploy

`ansible-deploy` — Python CLI с упакованным Ansible runtime для воспроизводимого
развёртывания Docker Compose-приложения на отдельных Ubuntu 24.04/26.04 LTS VPS. CLI управляет
Stage, Production, Monitoring, резервными копиями и восстановлением; приложение
доставляется как immutable image.

## Быстрый путь

```bash
uv tool install "git+https://github.com/Daniil-Danone/ansible-deploy.git@v0.1.0"
ansible-deploy --version
cd /path/to/application
ansible-deploy project init
ansible-deploy secrets path
ansible-deploy secrets init
ansible-deploy trust stage
ansible-deploy images publish stage --registry ghcr --namespace OWNER --ask-token
ansible-deploy stage
ansible-deploy status stage
```

CLI ставится глобально через [uv](https://docs.astral.sh/uv/) прямо из приватного
репозитория, с закреплённым тегом релиза; доступ берётся из ваших Git credentials.
Через SSH: `uv tool install "git+ssh://git@github.com/Daniil-Danone/ansible-deploy.git@v0.1.0"`.
Обновление, установка из wheel релиза и pipx — в [руководстве по обновлению](docs/guides/upgrading.md).
Старое имя команды `deploy` остаётся алиасом `ansible-deploy`.

После `project init` замените example host/domain и Compose; fingerprint SSH host key
записывает `ansible-deploy trust <environment>`. Реальные
`app.env`, registry credentials, SSH private keys, backup credentials и age identity
создавайте только во внешнем каталоге, который печатает `ansible-deploy secrets path`.
`.deploy/` целиком предназначен для commit-safe конфигурации.

Полная последовательность с Production, Monitoring, backup и диагностикой:
[канонический runbook](docs/runbook.md). Для установки инструментов выберите только
свою ОС: [Windows](docs/getting-started/windows.md),
[macOS](docs/getting-started/macos.md), [Ubuntu](docs/getting-started/ubuntu.md).

## Поддерживаемая матрица

| Компонент | Контракт |
|---|---|
| Управляющая машина | Windows, macOS, Ubuntu; Python 3.12+, Git, Docker, OpenSSH |
| Целевые серверы | отдельные чистые Ubuntu 24.04 LTS или 26.04 LTS VPS |
| Окружения | Stage, изолированные Production, Monitoring и Restore Drill |
| Registry | GHCR или Docker Hub, public/private |
| Приложение | Docker Compose, один домен и loopback upstream `127.0.0.1:8080` |
| Секреты | внешний project-scoped store; schema v2 содержит относительные имена |
| CI/CD | reusable GitHub Actions workflow, immutable SHA, protected Production Environment |

## Безопасное обновление CLI и проекта

Обновление Python-пакета не меняет application repository само по себе:

```bash
deploy project sync --check
deploy project sync
git diff -- .deploy deploy
```

CLI обновляет только файлы, чей hash совпадает с ранее установленным шаблоном.
Изменённый пользователем config/Compose остаётся нетронутым; новая версия появляется
рядом как `*.deploy-new`, а команда возвращает ненулевой код. Подробнее:
[обновление проекта](docs/guides/upgrading.md).

## Границы

DNS, firewall облачного провайдера, GitHub Environments и выдача внешних credentials
настраиваются вручную. Миграции приложения не запускаются CLI. Restore Production
выполняется только на отдельный target; rollback приложения не откатывает базу.
Live VPS/DNS/Google Drive acceptance требует реальные доступы и не считается
подтверждённым локальными тестами.

## Документация

- [Карта документации](docs/README.md)
- [Конфигурация schema v2](docs/reference/configuration.md)
- [CLI](docs/reference/cli.md) · [структура проекта](docs/reference/project-layout.md)
- [CI/CD](docs/guides/ci-cd.md) · [backup/restore/DR](docs/guides/backup-restore.md)
- [Безопасность](docs/security.md) · [диагностика](docs/troubleshooting.md)

## Версионирование и релиз

Любое изменение поднимает версию пакета в `src/deploy_cli/__init__.py`:
каждая правка доезжает до пользователей отдельным релизом, а не копится в
`develop`. Проверка `version-bump` в CI отклоняет pull request, если версия
совпадает с версией базовой ветки или ниже её.

Версия по semver: исправление — patch, новая команда или флаг — minor,
несовместимое изменение конфигурации или политики — major.

Релиз: `develop` вливается в `main`, затем на `main` ставится тег `vX.Y.Z`,
совпадающий с `__version__`. Тег запускает workflow `release`, который сверяет
тег с версией пакета, прогоняет тесты, собирает wheel и публикует GitHub
Release. Тег, не совпадающий с версией, падает на первом же шаге.

```bash
git switch main && git merge --ff-only develop && git push
git tag -a v0.2.0 -m v0.2.0 && git push origin v0.2.0
```

## Проверка разработки CLI

```bash
python -m pip install -e ".[dev]"
python -m pytest -o addopts= -q -ra
python -m ruff check .
python -m mypy
yamllint .
```
