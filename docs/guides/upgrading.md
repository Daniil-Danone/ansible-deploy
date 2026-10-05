# Обновление CLI и project scaffold

Python package и application repository имеют независимые версии. Установка нового
CLI не должна молча менять `.deploy/` или Compose.

## Порядок

1. Обновите CLI тем же способом, которым он был установлен. Пакет предоставляет
   отдельную команду `deploy`, поэтому не устанавливайте его в global Python. Для
   virtual environment:

   ```bash
   python -m pip install --upgrade /path/to/ansible-deploy
   python -m pip show ansible-deploy
   ```

   Для изолированного tool environment выберите один менеджер:

   ```bash
   uv tool install --force --reinstall /path/to/ansible-deploy
   uv tool list --show-paths
   # либо
   pipx install --force /path/to/ansible-deploy
   pipx list
   ```

   На Windows замените source path, например на
   `C:\Code\MyRepos\ansible-deploy\ansible-deploy`. Проверьте, какой executable будет
   запущен: `Get-Command deploy` в PowerShell или `command -v deploy` в POSIX shell, а
   затем `deploy --help`. У CLI пока нет отдельного `--version`; версию установленного
   пакета показывают `pip show`, `uv tool list` или `pipx list`.
2. Из application repository выполните dry inspection:

   ```bash
   deploy project sync --check
   ```

3. Если код `0`, scaffold актуален. Код `1` означает новые/обновляемые файлы или
   конфликт, а не частично выполненный deploy.
4. Примените additive update и проверьте diff:

   ```bash
   deploy project sync
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

## Откат

До commit используйте обычный review/diff и удалите только явно созданные кандидаты.
После commit откатывайте application repository через Git. Откат Python package не
понижает scaffold автоматически; более старый CLI fail closed, если project template
version новее поддерживаемой.
