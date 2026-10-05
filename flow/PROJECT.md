# Профиль проекта

## Что это за система

Project-local Python CLI и packaged Ansible runtime для безопасного управления
Stage, Production и Monitoring VPS. Самая высокая цена ошибки — раскрытие
секретов, восстановление данных не на тот сервер или неконтролируемый deploy в
Production.

## Стек

| Слой | Технологии |
|---|---|
| CLI | Python 3.12, argparse, Pydantic, PyYAML |
| Хранилище | project YAML + внешний файловый secret store |
| Клиент | CLI, без отдельного UI |
| Инфраструктура | Ansible, Docker Compose, Nginx, GitHub Actions |
| Тесты | pytest, Ruff, mypy, yamllint, ansible-lint, syntax-check |

## Репозитории и ветки

| Репозиторий | Путь | Основная ветка | Продовая |
|---|---|---|---|
| ansible-deploy | `C:\Code\MyRepos\ansible-deploy\ansible-deploy` | develop | main |

Внешний `C:\Code\MyRepos\ansible-deploy` не является Git-репозиторием и
содержит `PLAN.md` и `DEPLOYMENT_SPEC.md`.

## Команды

```bash
# линтер
python -m ruff check .
yamllint .
ANSIBLE_ROLES_PATH=src/deploy_cli/runtime/ansible/roles ansible-lint src/deploy_cli/runtime/ansible

# типы
python -m mypy

# тесты (все)
python -m pytest -o addopts= -q -ra

# тесты (модуль)
python -m pytest -o addopts= -q tests/<module>.py

# сборка
python -m build
docker build -t ansible-deploy:local src/deploy_cli/runtime

# инфраструктурные контракты
make syntax-check
make alloy-validate
python .codex/tools/verify.py
git diff --check
```

Команды записываются в **тихой форме**: вывод каждой из них целиком попадает в
контекст и оплачивается до конца сессии. `pytest -q <путь>`, а не `pytest`;
`git diff --stat` или `git diff -- <файл>`, а не `git diff`;
`docker compose logs --tail=100 <сервис>`, а не `docker compose logs`.
Агент берёт команду отсюда, а не сочиняет свою.

## Слои и зависимости

`cli -> config/models -> workflow -> runner -> packaged Ansible runtime`.
Ansible runtime живёт только в `src/deploy_cli/runtime/` — корневых копий нет.
Application release, monitoring и backup workflows не смешивают транзакции и
rollback semantics.

## Зоны риска

Пути, задев которые, работа обязана пройти дополнительные проверки
(см. `.codex/guidelines/gates.md`). Оставить только реально существующие зоны.

| Зона | Пути |
|---|---|
| секреты / крипто | `src/deploy_cli/config.py`, `src/deploy_cli/keys.py`, `src/deploy_cli/images.py`, `src/deploy_cli/runner.py`, `src/deploy_cli/secret_store.py`, `environments/`, `examples/` |
| фон / очереди / интеграции | `src/deploy_cli/runtime/ansible/roles/backup/`, `src/deploy_cli/runtime/ansible/playbooks/backup*.yml`, `.github/workflows/` |
| публичная граница | `src/deploy_cli/cli.py`, `src/deploy_cli/models.py` |

## Стайлгайды

- Специального стайлгайда нет; повторять существующие конструкции проекта.
- Минимальные изменения, строгие Pydantic-модели, без секретных значений в
  argv/env/logs.

## Инварианты

- `.deploy/` целиком commit-safe: реальные секреты и private keys там запрещены.
- Значения секретов не попадают в Git, argv, process environment, логи и ошибки.
- Все секретные пути канонизируются, ограничиваются своим external root и
  проверяются на symlink/reparse escape и безопасные permissions.
- Production restore не используется для drill: source backup и restore target
  являются разными понятиями.
- Деструктивный Production deploy/restore требует явного подтверждения.
- Миграции БД не применяются агентом.
- `main` не мержится агентом.

## Чего в этом проекте НЕ делаем

- Не строим hosted secret manager; используем внешний project-scoped store.
- Не сохраняем bootstrap/root password.
- Не открываем Loki/Grafana raw ports наружу.
- Не имитируем успешный VPS/DNS/Drive E2E без реальной инфраструктуры.
