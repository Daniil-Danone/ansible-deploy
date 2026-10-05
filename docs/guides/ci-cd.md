# GitHub Actions CI/CD

Репозиторий CLI предоставляет reusable contract
`.github/workflows/reusable-deploy.yml`; application repository копирует и адаптирует
`.github/examples/application-deploy.yml`.

## Границы pipeline

| Событие | Разрешённая работа |
|---|---|
| Pull request | только quality/test, без build/push, deploy и environment secrets |
| Push в `develop` | quality, matrix build/push всех application images, затем Stage и HTTPS health |
| Manual dispatch | quality, matrix build/push, Stage, затем Production после approval |

Production job привязан к GitHub Environment `production`. Именно required reviewers
этого Environment являются approval boundary; `workflow_dispatch` сам по себе approval
не заменяет. Deploy одного repository/environment сериализуется, активный запуск не
отменяется новым. Каждый job имеет timeout.

## Настроить вручную

1. Создайте Environments `build`, `stage` и `production`.
2. Для `production` включите required reviewers и запрет self-review, если доступно.
3. В `build` создайте secrets `REGISTRY_USERNAME` и `REGISTRY_TOKEN` с минимальными
   правами на push только в application image repositories. В example workflow замените
   `REGISTRY_HOST` и `REGISTRY_PREFIX`; имена repositories берутся из `image` каждого
   service в `.deploy/images.yml`.
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
8. Заполните **все** собираемые services в `.deploy/images.yml`, замените health URLs и
   команды quality приложения. `deploy project sync` должен установить управляемый
   helper `.deploy/ci_image_contract.py`; не копируйте его вручную из случайной версии.
9. Защитите `develop`: required quality check, review и запрет прямого push.
10. Все внешние Actions закреплены полными commit SHA. Обновляйте SHA и комментарий
    версии вместе, только после review соответствующего upstream release; mutable
    `@vN`, branch и tag в рабочих workflow запрещены.

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

`image_plan` читает `.deploy/images.yml` и строит matrix из каждого объявленного service.
Каждый matrix job checkout-ит один полный `github.sha`, строит свой context/Dockerfile,
push-ит tag с полным SHA, получает digest из registry и повторно проверяет immutable
`repository@sha256:...`. Результирующие artifacts содержат только service, SHA и
immutable reference — секреты в них не записываются — и хранятся один день.

`collect_images` требует ровно один результат для каждого service: отсутствующий,
лишний, повторный, относящийся к другому SHA/repository или невалидный digest блокирует
deploy. Только после этого формируется единый JSON `service -> repository@sha256:digest`.
Stage получает эту полную map. Production зависит от успешного Stage и получает
**буквально тот же job output**, не пересобирая images.

Reusable workflow повторно валидирует SHA/map, checkout-ит application SHA и через тот
же helper требует точного совпадения service set с `.deploy/images.yml`. Во временном
checkout все объявленные Compose services получают соответствующие immutable references.
Tool также закреплён полным SHA. Production config validation отклоняет `build:` и image
без digest. После deploy выполняются `deploy status` и отдельный HTTPS request с retry.
Неполная map, отсутствующий Compose service или недоступный private CLI завершают
pipeline до deploy.

## Что проверять в GitHub UI

- PR запускает только quality и не получает build/deploy jobs или environment secrets;
- сломанный quality блокирует Stage/Production;
- matrix содержит все services из `.deploy/images.yml`, каждый tag соответствует полному SHA;
- Stage применяет полный image map, Production применяет тот же map после успешного Stage;
- Production находится в `Waiting` до reviewer approval;
- два запуска одного environment не исполняют deploy параллельно;
- cleanup step выполняется после искусственно сломанного deploy;
- health failure завершает workflow ненулевым кодом.

Структурные pytest подтверждают YAML contract локально, но не доказывают реальные
Environment rules, permissions runner, registry или VPS connectivity.
