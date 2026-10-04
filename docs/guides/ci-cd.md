# GitHub Actions CI/CD

Репозиторий CLI предоставляет reusable contract
`.github/workflows/reusable-deploy.yml`; application repository копирует и адаптирует
`.github/examples/application-deploy.yml`.

## Границы pipeline

| Событие | Разрешённая работа |
|---|---|
| Pull request | только quality/test, без build/push, deploy и environment secrets |
| Push в `develop` | quality, build/push immutable image, затем Stage deploy и HTTPS health |
| Manual dispatch | quality, build/push, Stage, затем Production после approval |

Production job привязан к GitHub Environment `production`. Именно required reviewers
этого Environment являются approval boundary; `workflow_dispatch` сам по себе approval
не заменяет. Deploy одного repository/environment сериализуется, активный запуск не
отменяется новым. Каждый job имеет timeout.

## Настроить вручную

1. Создайте Environments `build`, `stage` и `production`.
2. Для `production` включите required reviewers и запрет self-review, если доступно.
3. В `build` создайте secrets `REGISTRY_USERNAME` и `REGISTRY_TOKEN` с минимальными
   правами на push только в application image repository. В example workflow замените
   `REGISTRY_HOST` и `IMAGE_REPOSITORY` на значения своего registry/repository.
4. В `stage` и `production` создайте secret `ANSIBLE_DEPLOY_SECRET_STORE_JSON`. Reusable
   job читает его после входа в соответствующий Environment; caller не передаёт этот
   secret через `workflow_call`.
5. В `stage` и `production` создайте `CLI_REPOSITORY_TOKEN`: fine-grained token с
   единственным доступом `Contents: read` к приватному `ansible-deploy`. Если политика
   требует GitHub App, адаптируйте workflow для выпуска короткоживущего installation
   token из защищённых App ID/private-key secrets и передайте результат в те же
   validation/checkout steps; сам installation token постоянно не храните. Reusable
   workflow явно передаёт token в checkout CLI и устанавливает
   `persist-credentials: false`; caller `github.token` не имеет доступа к приватному CLI
   repository и fallback на него запрещён.
6. В настройках приватного `ansible-deploy` разрешите application repository вызывать
   reusable workflows: **Settings → Actions → General → Access**. Не открывайте доступ
   организации шире необходимого.
7. Замените оба `TOOL_FULL_SHA` в caller workflow на один проверенный 40-символьный
   commit SHA `ansible-deploy`, не на branch/tag.
8. Замените health URLs, `compose_file`, `compose_service`, build context и команды
   quality приложения.
9. Защитите `develop`: required quality check, review и запрет прямого push.

Secret JSON — mapping относительного пути external store на base64 bytes:

```json
{
  "environments/stage/app.env": "QVBQX0VOVj1zdGFnZQo=",
  "keys/stage_ed25519": "BASE64_PRIVATE_KEY_BYTES"
}
```

Не добавляйте JSON в repository, artifacts, step summary или debug output. Reusable
workflow материализует files под `${{ runner.temp }}` с restrictive modes и удаляет
root в `always()`. Workflow не включает `set -x`, не печатает payload и не загружает
store как artifact. Для разных Environments используйте разные JSON и credentials.

## Immutable contract

Build job checkout-ит полный `github.sha`, строит и push-ит tag с этим полным SHA,
получает digest из registry и повторно проверяет immutable `repository@sha256:...`.
Только проверенные `deployment_sha`, `image_repository` и `image_digest` становятся job
outputs. Stage зависит от quality и build и передаёт outputs в reusable deploy.
Production зависит от того же build и успешного Stage, поэтому получает **тот же digest**,
а не пересобирает image.

Reusable workflow повторно валидирует SHA/digest, checkout-ит application SHA и во
временном checkout закрепляет полученный digest в указанном Compose service. Tool
также закреплён полным SHA. Production config validation отклоняет
`build:` и image без digest. После deploy выполняются `deploy status` и отдельный HTTPS
request с retry. Пустой/невалидный digest, отсутствующий service или недоступный private
CLI завершают pipeline до deploy.

## Что проверять в GitHub UI

- PR запускает только quality и не получает build/deploy jobs или environment secrets;
- сломанный quality блокирует Stage/Production;
- build tag соответствует полному commit SHA, Stage использует выданный registry digest;
- Production использует тот же digest, что успешно прошёл Stage;
- Production находится в `Waiting` до reviewer approval;
- два запуска одного environment не исполняют deploy параллельно;
- cleanup step выполняется после искусственно сломанного deploy;
- health failure завершает workflow ненулевым кодом.

Структурные pytest подтверждают YAML contract локально, но не доказывают реальные
Environment rules, permissions runner, registry или VPS connectivity.
