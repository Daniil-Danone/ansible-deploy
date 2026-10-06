# CLI

Глобальные options ставятся перед командой:

```text
deploy [--project-dir PATH] [--verbose] COMMAND ...
```

Основные команды:

```text
deploy project init [--stage-branch BRANCH] [--production-branch BRANCH] [--tool-sha SHA]
deploy project sync [--check]
deploy secrets path [--new-project-id]
deploy secrets init [<stage|prod|monitoring|restore|all>]
deploy secrets hash-password
deploy secrets rotate-collector-password [--environment <stage|prod|all>] [--password-stdin]
deploy secrets check-collector-password [<stage|prod|all>]
deploy trust <stage|prod|monitoring|restore> [--print] [--force]
deploy images publish <stage|prod> ...
deploy stage [--dry-run] [--version SHA] [--allow-volume-change NAME ...]
deploy prod [--dry-run] [--version SHA] [--yes] [--allow-volume-change NAME ...]
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

`project init` создаёт также `.github/workflows/deploy.yml`: push в Stage-ветку
автоматически деплоит Stage, Production запускается вручную только из Production-ветки.
Defaults — `develop` и `main`. Выбор хранится в `.deploy/cd.yml`; после изменения
настроек `project sync` обновляет управляемый workflow, сохраняя локальные ветки.
`--tool-sha` принимает только полный 40-символьный lowercase SHA CLI. Без него остаётся
placeholder, который нужно заполнить перед первым запуском CD. Оставшуюся настройку
Environments, secrets, registry, health URLs и quality checks описывает
[CI/CD guide](../guides/ci-cd.md).

`--dry-run` применяет Ansible check mode там, где он безопасен, и не выполняет
bootstrap с password. Production-changing commands требуют интерактивного `prod`; в
non-interactive protected CI используется `--yes`.

`--allow-volume-change NAME` (можно повторять) подтверждает, что Compose volume `NAME`
намеренно добавлен или удалён в этом release. Без него deploy поверх активного release
останавливается до изменения контейнеров, если набор named volumes не совпадает с
активным release и существующими Docker volumes — см.
[непрерывность named volumes](../concepts/server-state.md#непрерывность-named-volumes).

## Доверие SSH host key

`trust <environment>` сканирует host key сервера из config этого окружения
(`server.host`, `server.ssh_port`), печатает по строке `[FINGERPRINT] <тип ключа>
SHA256:...` и записывает значения в `server.host_key_fingerprints` файла
`.deploy/environments/<environment>/config.yml`. Переписывается только этот блок,
остальной файл остаётся байт-в-байт.

- список совпадает с просканированным — `[OK] ... unchanged`, код `0`;
- список пуст — записывается, код `0`;
- список непустой и отличается — файл не меняется, код `2`: сервер либо переустановлен,
  либо соединение перехвачено. Сверьте ключ с консолью провайдера и повторите с `--force`;
- `--print` печатает fingerprint'ы и ничего не пишет;
- `--force` перезаписывает расхождение.

Пока `host_key_fingerprints` пуст, deploy, update, rollback, monitoring, collectors и
backup останавливаются до обращения к серверу с кодом `2` и подсказкой запустить
`ansible-deploy trust <environment>`. Scan недоступен — код `4`, несовпадение или смена
ключа у уже доверенного окружения — код `3`.

## Внешние секреты

`secrets path` печатает project-scoped root. `secrets init [<environment>|all]`
(по умолчанию `all`) создаёт в нём owner-only `environments/<env>/`, `keys/`, `backup/` и
заготовку secret-файла из закоммиченного `.deploy/environments/<env>/.env.example` под
именем из `application.env_file` (для monitoring — `monitoring.secrets_file`).
Существующий файл не перезаписывается и его права не меняются. Отчёт печатает
`[CREATE]`, `[KEEP]` и `[FILL] <файл>: <переменные>` — пустые значения заготовки и
`required_env_vars`, которых в ней нет. Команда идемпотентна и печатает только имена
переменных и относительные имена файлов.

`secrets hash-password` дважды скрыто запрашивает пароль, считает crypt SHA-512 внутри
runtime-контейнера (`openssl passwd -6`) и печатает в stdout только итоговый хеш; пароль
не попадает в argv, вывод и на диск. Несовпадение паролей — код `2`.

`secrets rotate-collector-password` генерирует новый случайный push-password и
раскладывает обе его формы за один проход: plaintext в `collector.password` окружений
(`--environment`, по умолчанию `all` — stage и prod) и crypt SHA-512 hash в
`LOKI_PUSH_PASSWORD_HASH` файла `monitoring.secrets_file`. Остальные строки monitoring
secret-файла, их порядок и комментарии остаются байт-в-байт, plaintext пишется ровно
одной строкой без завершающего line ending, каждый файл заменяется атомарно и остаётся
owner-only. Пароль не печатается и не попадает в argv; `--password-stdin` читает готовый
пароль из stdin для automation. Отчёт — `[UPDATE]` по каждому файлу и `[OK]`; отсутствие
строки `LOKI_PUSH_PASSWORD_HASH=` — код `2`, и ни один файл не меняется.

`secrets check-collector-password [<environment>|all]` ничего не меняет: считает hash
plaintext'а с солью из `LOKI_PUSH_PASSWORD_HASH` и сравнивает с ним. Совпадение — `[OK]
<env> collector password matches the monitoring hash` и код `0`; расхождение — `[ERROR]`
по окружению и код `2`. Эту же проверку делает preflight `collectors deploy|update`: при
расхождении deploy останавливается до обращения к серверу, а без monitoring-окружения в
проекте проверка тихо пропускается.

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
