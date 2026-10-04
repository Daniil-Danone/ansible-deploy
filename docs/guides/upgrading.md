# Обновление CLI и project scaffold

Python package и application repository имеют независимые версии. Установка нового
CLI не должна молча менять `.deploy/` или Compose.

## Порядок

1. Обновите CLI в virtual environment.
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

## Откат

До commit используйте обычный review/diff и удалите только явно созданные кандидаты.
После commit откатывайте application repository через Git. Откат Python package не
понижает scaffold автоматически; более старый CLI fail closed, если project template
version новее поддерживаемой.
