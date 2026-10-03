# ansible-deploy

`ansible-deploy` — CLI для воспроизводимого развёртывания Docker Compose-приложений
на выделенном Ubuntu 24.04 сервере. Проект хранит `.deploy/`, Compose и секретные
env-файлы, а устанавливаемый Python-пакет приносит фиксированный Ansible runtime.

CLI локально собирает и публикует образы в GHCR или Docker Hub, закрепляет их по
digest, подготавливает сервер, включает HTTPS и запускает приложение. Исходный код
на VPS не копируется и там не собирается.

## С чего начать

Выберите свою рабочую систему. Каждая инструкция автономна: от установки инструментов
до первого HTTPS-ответа demo-приложения.

- [Windows](docs/getting-started/windows.md)
- [macOS](docs/getting-started/macos.md)
- [Ubuntu](docs/getting-started/ubuntu.md)

## Возможности

- первичная настройка чистого Ubuntu 24.04 по root-паролю;
- автоматическое создание отдельного Ed25519-ключа и пользователя `deploy`;
- Docker, Nginx, Certbot, UFW, Fail2ban и unattended upgrades;
- сборка и публикация нескольких образов в GHCR или Docker Hub;
- приватные registry с отдельным read-only токеном сервера;
- digest-pinned Production, проверка DNS/SSH host key/Compose/env;
- обновление управляемого состояния сервера и откат приложения Production.
- отдельный monitoring VPS с Grafana/Loki и Alloy-сборщиками Docker/journald;
- HTTPS для Grafana, authenticated HTTPS Loki push и закрытые raw-порты.

## Поддерживаемая матрица

| Часть | Поддерживается |
|---|---|
| Рабочая машина | Windows, macOS, Ubuntu; Python 3.12+, Git, Docker, OpenSSH |
| Целевой сервер | отдельный чистый Ubuntu 24.04 VPS |
| Registry | private/public GHCR и Docker Hub |
| Окружения | Stage, изолированный Production и отдельный Monitoring |
| Application | один Compose-проект, один домен, upstream `127.0.0.1:8080` |

## Границы проекта

CLI рассчитан на один домен, один сервер и один upstream `127.0.0.1:8080`. Он не
автоматизирует DNS и firewall провайдера, secrets manager, миграции и backup базы,
multi-host/rolling/zero-downtime deploy, очистку старых релизов и образов. Rollback
не откатывает базу данных.

> UFW может сбросить существующие правила. Используйте отдельный чистый сервер, а не
> VPS с другими приложениями. Поддерживаемая и проверенная цель — Ubuntu 24.04;
> дистрибутив пока не определяется автоматически.

## Жизненный цикл

```text
локальный Git commit
  -> build и push images
  -> проверка immutable digest
  -> обновление Compose
  -> проверка DNS и SSH host key
  -> bootstrap root (только первый раз)
  -> deploy-пользователь + Docker/Nginx/TLS
  -> HTTPS health check
```

## Документация

- [Карта документации](docs/README.md)
- Практика: [demo-app](docs/guides/demo-app.md),
  [настоящий проект](docs/guides/real-project.md),
  [приватные registry](docs/guides/private-registries.md),
  [Production](docs/guides/production.md), [централизованные логи](docs/guides/monitoring.md)
- Концепции: [как всё работает](docs/concepts/how-it-works.md),
  [SSH и ключи](docs/concepts/ssh-and-keys.md),
  [состояние сервера](docs/concepts/server-state.md)
- Справочник: [CLI](docs/reference/cli.md),
  [конфигурация](docs/reference/configuration.md),
  [структура проекта](docs/reference/project-layout.md)
- [Безопасность](docs/security.md) · [Решение проблем](docs/troubleshooting.md)

## Для разработчиков CLI

```bash
python3 -m venv .venv
source .venv/bin/activate       # Windows: .\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
python -m pytest
```

Новые приложения используют project-local `.deploy/`. Корневые `config/` и
`environments/` оставлены только как fixture обратной совместимости.
