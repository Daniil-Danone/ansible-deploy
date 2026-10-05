# CLI

Глобальные options ставятся перед командой:

```text
deploy [--project-dir PATH] [--verbose] COMMAND ...
```

Основные команды:

```text
deploy project init
deploy project sync [--check]
deploy secrets path [--new-project-id]
deploy images publish <stage|prod> ...
deploy stage [--dry-run] [--version SHA]
deploy prod [--dry-run] [--version SHA] [--yes]
deploy status <stage|prod>
deploy rollback prod [--yes]
deploy server update <stage|prod|monitoring|all> [--dry-run] [--yes]
deploy monitoring <deploy|status|update> [--dry-run]
deploy collectors <deploy|status|update> <stage|prod|all> [--dry-run]
deploy backup setup prod
deploy backup run prod
deploy backup list prod
deploy backup restore prod --target restore --backup ID [--yes]
```

`project sync --check` не меняет файлы, но печатает план `[CREATE]`/`[UPDATE]`, conflict
или `[OK]`; он возвращает ненулевой код при pending update или conflict. Обычный sync
никогда не перезаписывает изменённый config/Compose.

`--dry-run` применяет Ansible check mode там, где он безопасен, и не выполняет
bootstrap с password. Production-changing commands требуют интерактивного `prod`; в
non-interactive protected CI используется `--yes`.

Код `0` означает доказанный успех команды, `2` — configuration/security boundary;
runner возвращает ненулевой код underlying operation. Не анализируйте только текст:
automation должна проверять exit code.

## Подготовка рабочей машины

```text
ansible-deploy setup [--secrets-dir PATH | --default-secrets-dir] [--non-interactive|-y]
ansible-deploy config show
ansible-deploy config path
ansible-deploy doctor [--json]
```

`setup` — одноразовый мастер для машины: выбирает каталог внешнего secret store (OS
default или свой абсолютный путь), создаёт его с owner-only правами (`0700` / ACL только
для текущего пользователя), сохраняет выбор в пользовательский `config.toml` и запускает
`doctor`. Повторный запуск идемпотентен. Без TTY (или с `--non-interactive`) вопросы не
задаются, и нужен `--secrets-dir PATH` либо `--default-secrets-dir`.

Пользовательский конфиг (путь переопределяется env `ANSIBLE_DEPLOY_CONFIG`):

| OS | Путь |
| --- | --- |
| Windows | `%APPDATA%\ansible-deploy\config.toml` |
| macOS | `~/Library/Application Support/ansible-deploy/config.toml` |
| Linux | `$XDG_CONFIG_HOME/ansible-deploy/config.toml` или `~/.config/ansible-deploy/config.toml` |

Store проекта выбирается по приоритету: env `ANSIBLE_DEPLOY_SECRETS_DIR` (точный root
одного проекта, для CI) → `secrets_dir` из конфига (`<secrets_dir>/<project-id>`) → OS
default (`<data dir>/ansible-deploy/projects/<project-id>`). `config show` печатает
действующее значение и его источник (env/config/default).

`doctor` проверяет Python, Docker CLI и daemon, `git`, `ssh-keygen`, secret store
(существование и owner-only права), опциональные `age`/`rclone`, а внутри проекта —
`.deploy/project-id`, store проекта и игнорирование `.deploy-state/`. Статусы
`OK`/`WARN`/`FAIL` с подсказкой под OS; код `1`, если есть хотя бы один `FAIL`, иначе `0`.
Ошибки ввода и конфигурации `setup`/`config` возвращают `2`; `setup` после успешной
настройки возвращает код `doctor`.
