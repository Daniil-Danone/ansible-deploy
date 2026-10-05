# Demo-приложение

Пример `examples/demo-app` — минимальный backend и frontend. Локальный Compose строит
исходники; deploy Compose содержит только registry images. Frontend одновременно
служит gateway: слушает `127.0.0.1:8080`, отдаёт `/health` и проксирует `/api` в backend.

## Быстрый маршрут

1. Скопируйте каталог, выполните `deploy project init` и инициализируйте Git.
2. Проверьте `compose.local.yml` и оба health endpoint.
3. Заполните `.deploy/environments/stage/config.yml` реальными IP, fingerprint, доменом.
4. Выполните `deploy images publish stage ... --ask-token --ask-pull-token`.
5. Закоммитьте изменённый `deploy/compose.stage.yml`.
6. Выполните `deploy stage --ask-bootstrap-password`, затем `deploy status stage`.

Runtime `app.env` создайте во внешнем root из `deploy secrets path`. Полный порядок:
[канонический runbook](../runbook.md).

## Что важно увидеть

- `.deploy/images.yml` связывает Compose services с build context и Dockerfile;
- `deploy images publish` меняет только `image:` и сохраняет остальные строки;
- в deploy Compose появляются `@sha256:`, а не mutable tags;
- `.deploy-state/` не попадает в Git, а keys/`app.env`/registry auth вообще не
  размещаются в worktree;
- VPS скачивает готовые образы: Dockerfile и исходники туда не загружаются.

Demo — учебный пример, а не production-шаблон базы данных. Для реальной архитектуры
перейдите к [руководству по настоящему проекту](real-project.md).
