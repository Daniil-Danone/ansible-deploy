# GitHub Actions CI/CD

Репозиторий CLI предоставляет reusable contract
`.github/workflows/reusable-deploy.yml`; application repository копирует и адаптирует
`.github/examples/application-deploy.yml`.

## Границы pipeline

| Событие | Разрешённая работа |
|---|---|
| Pull request | quality/test/build без deploy и environment secrets |
| Push в `develop` | quality, затем Stage deploy и HTTPS health |
| Manual dispatch | quality, затем Production job |

Production job привязан к GitHub Environment `production`. Именно required reviewers
этого Environment являются approval boundary; `workflow_dispatch` сам по себе approval
не заменяет. Deploy одного repository/environment сериализуется, активный запуск не
отменяется новым. Каждый job имеет timeout.

## Настроить вручную

1. Создайте Environments `stage` и `production`.
2. Для `production` включите required reviewers и запрет self-review, если доступно.
3. В каждом Environment создайте secret `ANSIBLE_DEPLOY_SECRET_STORE_JSON`. Reusable
   job читает его после входа в соответствующий Environment; caller не передаёт этот
   secret через `workflow_call`.
4. Замените оба `TOOL_FULL_SHA` в caller workflow на один проверенный 40-символьный
   commit SHA `ansible-deploy`, не на branch/tag.
5. Замените health URLs и команды quality приложения.
6. Защитите `develop`: required quality check, review и запрет прямого push.

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

Caller передаёт `github.sha`; reusable workflow принимает только полный lowercase
40-character Git SHA, checkout выполняется на этом ref. Tool также закреплён полным
SHA. Production config validation отклоняет `build:` и image без digest. После deploy
выполняются `deploy status` и отдельный HTTPS request с retry.

## Что проверять в GitHub UI

- PR не получает deploy jobs или environment secrets;
- сломанный quality блокирует Stage/Production;
- Stage deploy соответствует commit SHA;
- Production находится в `Waiting` до reviewer approval;
- два запуска одного environment не исполняют deploy параллельно;
- cleanup step выполняется после искусственно сломанного deploy;
- health failure завершает workflow ненулевым кодом.

Структурные pytest подтверждают YAML contract локально, но не доказывают реальные
Environment rules, permissions runner, registry или VPS connectivity.
