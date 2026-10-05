# Обновление CLI и project scaffold

Python package и application repository имеют независимые версии. Установка нового
CLI не должна молча менять `.deploy/` или Compose.

## Порядок

1. Обновите CLI до нужного тега релиза. CLI ставится как изолированный global tool
   через [uv](https://docs.astral.sh/uv/) из приватного репозитория; доступ берётся из
   ваших Git credentials (HTTPS через credential manager/`gh auth login` или SSH key):

   ```bash
   uv tool install --force "git+https://github.com/Daniil-Danone/ansible-deploy.git@vX.Y.Z"
   # либо через SSH
   uv tool install --force "git+ssh://git@github.com/Daniil-Danone/ansible-deploy.git@vX.Y.Z"
   ansible-deploy --version
   ```

   Без доступа к Git можно поставить wheel из GitHub Release:

   ```bash
   gh release download vX.Y.Z --repo Daniil-Danone/ansible-deploy --pattern "*.whl"
   uv tool install --force ./ansible_deploy-X.Y.Z-py3-none-any.whl
   ```

   `uv tool upgrade ansible-deploy` переустанавливает пакет из того же source; для
   источника, закреплённого на теге, переход на новый тег — это `uv tool install --force`
   с новым `@vX.Y.Z`. Альтернатива uv — `pipx install --force "git+https://...@vX.Y.Z"`.
   Команда `deploy` остаётся алиасом `ansible-deploy`. Проверьте, какой executable будет
   запущен: `Get-Command ansible-deploy` в PowerShell или `command -v ansible-deploy` в
   POSIX shell.
2. Из application repository выполните dry inspection:

   ```bash
   ansible-deploy project sync --check
   ```

3. Если код `0`, scaffold актуален. Код `1` означает новые/обновляемые файлы или
   конфликт, а не частично выполненный deploy.
4. Примените additive update и проверьте diff:

   ```bash
   ansible-deploy project sync
   git status --short
   git diff -- .deploy deploy
   ```

5. Запустите config validation/dry-run и commit только после review.

`.deploy/template-state.yml` хранит scaffold version, CLI version и SHA-256 каждого
установленного managed template. Если текущий hash совпадает с предыдущим template,
CLI может обновить файл. Если config/Compose изменён пользователем, оригинал остаётся
байт-в-байт прежним, новая версия пишется в `*.deploy-new`, вывод содержит
`[CONFLICT]`, exit code ненулевой.

Если до разбора конфликта выходит следующая версия CLI, повторный sync атомарно
обновляет существующий `*.deploy-new` до **текущего** packaged template. Пользовательский
основной файл по-прежнему не меняется. Поэтому candidate всегда относится к последней
установленной версии CLI, а не к первому замеченному конфликту.

Разберите candidate вручную, перенесите нужные поля в основной файл, удалите
`.deploy-new` и повторите sync. Никогда не подменяйте основной файл целиком без review:
там находятся project-specific host, paths, domains и Compose contract.

## Старый stage-only проект

`project sync` добавляет отсутствующие Production, Monitoring и Restore configs/Compose,
не меняя существующий Stage. Existing `.deploy/project-id` не заменяется, поэтому
namespace внешнего secret store остаётся стабильным. Реальные секреты не копируются и
не генерируются scaffold-командой.

## Переход с deployment environment schema 1

Environment schema 1 больше не принимается. Выполните `deploy project sync`, затем
просмотрите созданные основные schema v2 файлы и/или `*.deploy-new` candidates. Старый
корневой layout `config/` + `environments/` обычно получает новые основные файлы под
`.deploy/`; изменённый файл, уже находящийся под `.deploy/`, может получить candidate.
Вручную перенесите только commit-safe значения: hosts, fingerprints, domains, remote
directories и Compose paths. Не копируйте secret values в `.deploy/`.

Каждому secret field задайте нормализованное относительное имя, например
`environments/stage/app.env`. Внешний project root покажет `deploy secrets path` —
создайте referenced files там с ограниченными permissions. Миграция намеренно не
автоматическая: CLI не угадывает, не копирует, не печатает и не коммитит существующие
credentials. Перед Production проверьте `deploy project sync --check` и нужную Stage
команду. Если credential когда-либо находился в Git, одного переноса недостаточно:
удалите старый secret-файл из worktree, отзовите и замените credential, а необходимость
очистки истории согласуйте по
[security runbook](../security.md#перед-commit).

Production разрешайте только после `deploy stage --dry-run`, обычного `deploy stage` и
успешного `deploy status stage` для того же Git SHA, который будет выпущен в Production.

## Takeover of a server deployed by an older CLI

Сервер, который уже обслуживается более старой версией CLI, новый CLI принимает «на
месте» — без переустановки и без переноса данных. Данные живут в Docker named volumes
`<compose project>_<ключ>` (для Production — `myapp_prod_<ключ>`), поэтому главное —
ничего не переименовать.

1. **Оставьте прежними** `environment`, `application.remote_dir`, `domain` и
   `health_path`, а также ключи top-level `volumes:` в Compose. Для Stage project name —
   последний сегмент `remote_dir`, так что его смена тоже означает новые пустые volumes.
2. **Используйте старую пару deploy SSH-ключей**: положите её во внешний secret store
   (`deploy secrets path`) как `keys/<env>_ed25519` и `keys/<env>_ed25519.pub`, например
   `keys/prod_ed25519{,.pub}`. Иначе CLI не получит managed access и предложит bootstrap.
3. **Скопируйте секреты байт-в-байт** (application `env_file`, extra env files, registry
   auth): пароли БД и прочие credentials уже «запечены» в данные volumes, и новые значения
   не подойдут к существующей БД. Не пересоздавайте их генератором.
4. **Сначала dry-run**:

   ```bash
   deploy prod --dry-run
   ```

   Он проверяет доступ, identity сервера и
   [непрерывность named volumes](../concepts/server-state.md#непрерывность-named-volumes):
   если хотя бы один volume нового Compose не существует на сервере или volume активного
   release исчез из Compose, команда остановится и перечислит имена. Исправьте config,
   а не обходите проверку флагом `--allow-volume-change` — он только для действительно
   новых или намеренно удаляемых volumes.
5. Только после чистого dry-run выполните обычный `deploy prod --yes` и
   `deploy status prod`.

## Откат

До commit используйте обычный review/diff и удалите только явно созданные кандидаты.
После commit откатывайте application repository через Git. Откат Python package не
понижает scaffold автоматически; более старый CLI fail closed, если project template
version новее поддерживаемой.
