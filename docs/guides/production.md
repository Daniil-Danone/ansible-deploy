# Production

Production должен быть изолирован от Stage: другой server host/IP, domain,
`remote_dir`, Compose и env. Отдельный SSH key рекомендуется, хотя CLI это не навязывает.

## Перед deploy

- все services используют `repository@sha256:<64 hex>`;
- ни у одного service нет `build:`;
- PostgreSQL/Redis используют named volumes;
- миграции backward-compatible и выполняются отдельным контролируемым процессом;
- есть проверенный backup и процедура restore базы.

```bash
deploy images publish prod --registry ghcr --namespace OWNER --username OWNER \
  --ask-token --ask-pull-token
git add deploy/compose.prod.yml
git commit -m "chore: pin production images"
deploy prod --ask-bootstrap-password --version <git-sha>
deploy prod --dry-run --version <git-sha>
deploy status prod
```

Изменяющая Production-команда просит ввести `prod`; в CI используйте `--yes`. Команда
`images publish prod` сама Production confirmation не спрашивает: внимательно проверяйте
окружение и diff до commit. На чистом VPS сначала нужен реальный deploy с
`--ask-bootstrap-password` и интерактивным подтверждением `prod`. Dry-run возможен только
после bootstrap на доступном managed host: pristine dry-run ничего не меняет и падает.

## Обновление и rollback

```bash
deploy server update prod --dry-run
deploy server update prod
deploy rollback prod
```

Rollback возвращает предыдущие Compose/env/images и проверяет health. Он **не**
откатывает схему или данные БД. CLI не обещает rolling/zero-downtime deploy и не удаляет
старые releases/images автоматически.
