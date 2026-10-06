# GitHub Actions CI/CD

Репозиторий CLI предоставляет reusable contract
`.github/workflows/reusable-deploy.yml`. `project init` создаёт в application repository
управляемый caller `.github/workflows/deploy.yml` и настройки `.deploy/cd.yml`:

```bash
ansible-deploy project init --stage-branch develop --production-branch main --tool-sha FULL_40_CHARACTER_SHA
```

Ветки по умолчанию — `develop` для Stage и `main` для Production. Обе можно выбрать
при init. При смене веток или версии CLI измените `stage_branch`, `production_branch`
или `tool_sha` в `.deploy/cd.yml` и выполните `project sync`: он обновит workflow,
сохранив настройки и комментарии config. Ручные изменения самого workflow сохраняются
по обычному conflict contract: новая версия появляется как `deploy.yml.deploy-new`.
Без `--tool-sha` init оставляет безопасный `TOOL_FULL_SHA` placeholder; перед запуском
его обязательно заменяют в `.deploy/cd.yml` на проверенный полный commit SHA через sync.
GitHub не запускает reusable workflow с несуществующим placeholder ref.

`.github/examples/application-deploy.yml` показывает вариант с Python quality checks.
Сгенерированный caller использует repository variable `QUALITY_COMMAND` для команд
проверки конкретного приложения; если она пуста, дополнительные проверки пропускаются.

## Границы pipeline

| Событие | Разрешённая работа |
|---|---|
| Pull request | только quality/test, без build/push, deploy и environment secrets |
| Push в выбранную Stage-ветку | quality, matrix build/push всех application images, monitoring, Stage и HTTPS health |
| Manual dispatch из выбранной Production-ветки с `deploy_production=true` | quality, matrix build/push, monitoring, Production после approval |
| Dispatch из другой ветки или без `deploy_production` | только quality, без build/push и deploy |

Production job привязан к GitHub Environment `production`. Именно required reviewers
этого Environment являются approval boundary; `workflow_dispatch` сам по себе approval
не заменяет. Deploy одного repository/environment сериализуется, активный запуск не
отменяется новым. Каждый job имеет timeout.

## Настроить вручную

1. Создайте Environments `build`, `monitoring`, `stage` и `production`.
2. Для `production` включите required reviewers и запрет self-review, если доступно.
3. В `build` создайте secrets `REGISTRY_USERNAME` и `REGISTRY_TOKEN` с минимальными
   правами на push только в application image repositories. Задайте repository variables
   `REGISTRY_PREFIX` (например, `ghcr.io/owner`), `REGISTRY_HOST` (по умолчанию `ghcr.io`),
   `STAGE_HEALTH_URL`, `PRODUCTION_HEALTH_URL` и `QUALITY_COMMAND`. Имена repositories
   берутся из `image` каждого service в `.deploy/images.yml`.
4. Установите GitHub CLI (`gh`), выполните `gh auth login --hostname github.com`
   и из application project
   загрузите external secret store командой `ansible-deploy secrets github upload
   --repo OWNER/REPO` (подробности ниже). Она создаст/обновит
   `ANSIBLE_DEPLOY_SECRET_STORE_JSON` в `stage`, `production` и `monitoring`. Reusable
   job читает его после входа в соответствующий Environment. Caller явно передаёт
   только `CLI_REPOSITORY_TOKEN` и `ANSIBLE_DEPLOY_SECRET_STORE_JSON` через контракт
   `workflow_call.secrets`; `secrets: inherit` не используется.
5. В `monitoring`, `stage` и `production` создайте `CLI_REPOSITORY_TOKEN`: fine-grained token с
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
7. Задайте `tool_sha` в `.deploy/cd.yml` как проверенный 40-символьный commit SHA
   `ansible-deploy`, затем запустите `project sync`; branch/tag не принимаются.
   GitHub требует наличия workflow в default branch для кнопки ручного запуска:
   сначала добавьте scaffold в default branch, затем выбирайте Production-ветку в Run workflow.
8. Заполните **все** собираемые services в `.deploy/images.yml`, замените health URLs и
   `QUALITY_COMMAND` приложения. `deploy project sync` должен установить управляемый
   helper `.deploy/ci_image_contract.py`; не копируйте его вручную из случайной версии.
9. Защитите выбранные Stage/Production-ветки: required quality check, review и запрет
   прямого push. В Environment `production` ограничьте deployment branches выбранной
   Production-веткой; проверка ветки caller дополняет это ограничение.
10. Все внешние Actions закреплены полными commit SHA. Обновляйте SHA и комментарий
    версии вместе, только после review соответствующего upstream release; mutable
    `@vN`, branch и tag в рабочих workflow запрещены.

## Загрузка external secret store

### Контракт secrets и GitHub Environments

Reusable workflow объявляет оба deployment secrets с `required: false` намеренно:
caller job с `uses` ещё не привязан к Environment и может передать пустое значение.
GitHub разрешает environment secrets позже, когда запускает called job с
`environment: monitoring` или `environment: ${{ inputs.environment }}` и выполняет
его protection rules. Одноимённый secret этого Environment имеет приоритет над
переданным caller значением. Оба значения обязательны для выполнения deployment:
первый validation step проверяет их уже внутри called job до checkout и
материализации store. Сообщения об отсутствующих secrets содержат только их имена.
Это позволяет хранить отдельный JSON в каждом Environment без repository-level
дублирования Production credentials.

Если validation сообщает, что secret отсутствует, хотя `gh secret list --env ...`
показывает его имя, проверьте failing job: `stage / Reconcile monitoring` использует
Environment `monitoring`, а `stage / Deploy stage` — `stage`. Проверьте оба имени
secrets именно в Environment failing job и в caller repository. Затем проверьте,
что `tool_sha` закреплён на версии с `workflow_call.secrets`, а в обоих caller jobs
есть явный `secrets` mapping. После обновления CLI измените `tool_sha` в `.deploy/cd.yml`,
выполните `project sync` и закоммитьте обновлённый workflow. Повторный запуск старого
run использует старую версию workflow; для исправления нужен новый запуск нового
commit. Значения secrets в логах не проверяйте и в repository secrets не копируйте.

### Команда загрузки

Environments должны уже существовать. У авторизованного в `gh` аккаунта должны быть
права на управление их secrets. В терминале application project выполните:

```bash
gh auth login --hostname github.com
ansible-deploy secrets github upload --repo OWNER/REPO --check
ansible-deploy secrets github upload --repo OWNER/REPO
```

Для `uwords` в собственном форке используйте `--repo Daniil-Danone/uwords`. Repository
всегда задаётся явно и находится на `github.com`: default host и `GH_HOST` игнорируются.
CLI читает пути из `.deploy/environments/<environment>/config.yml`
и существующие безопасные файлы external store, затем собирает отдельный JSON для
каждого Environment. `stage` загружается в `stage`, `prod` — в `production`,
`monitoring` — в `monitoring`; backup/restore credentials не включаются.

В набор входят private/public SSH keys, application env, все
`application.extra_env_files[].source`, optional registry auth и collector password;
для monitoring — keys и `monitoring.secrets_file`. JSON mapping содержит portable
relative paths и base64 исходных байтов. Все выбранные наборы проходят локальные
проверки до первой загрузки. `--check` дополнительно проверяет авторизацию и доступ к
repository, но не права записи или наличие Environments. JSON не должен превышать
GitHub limit 48 KiB. Команда передаёт payload только через stdin `gh`, без временных
файлов, вывода значений или внешних путей.

После изменения секретов можно обновить только нужные окружения:

```bash
ansible-deploy secrets github upload --repo OWNER/REPO --environment stage --environment monitoring
```

Option `--environment` повторяется; без него загружаются все три окружения. Повторная
загрузка заменяет существующее значение secret. При ошибке сети часть загрузок могла
уже завершиться — устраните причину и повторите команду. `CLI_REPOSITORY_TOKEN`,
`REGISTRY_USERNAME`, `REGISTRY_TOKEN` и repository variables задаются отдельно.

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
Stage или Production получают полную map для выбранного SHA. Production запускается
из своей ветки и не деплоит её commit в Stage; image map собирается и проверяется один
раз в рамках этого ручного запуска.

## Monitoring в CD

Перед каждым application deploy reusable workflow выполняет `monitoring deploy` в
GitHub Environment `monitoring`. Эта команда проверяет managed SSH access, при первом
запуске bootstrap-ит незанятый monitoring сервер, а на повторных полностью reconciles
сервер, monitoring Compose/config и проверяет health. Таким образом, обновления CLI
и monitoring-конфига применяются при очередном CD без отдельного ручного update.
SSH fingerprints должны быть предварительно подтверждены через `trust monitoring`;
первичная автоматическая установка требует key-based bootstrap access.

`secrets github upload` создаёт в `monitoring` отдельный `ANSIBLE_DEPLOY_SECRET_STORE_JSON`
с monitoring secret и private/public SSH keys по путям из monitoring config.
Credentials мониторинга не нужно дублировать в app Environments. Все обновления общего
monitoring сервера сериализованы в одной concurrency group независимо от app target;
ошибка monitoring блокирует application deploy. Environment `production` по-прежнему
требует approval перед получением app secrets и деплоем Production.

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
- Stage автоматически запускается только для своей ветки; Production доступен только
  вручную из своей ветки, а неверный dispatch не запускает image build/deploy;
- monitoring reconciles до application deploy и получает только свои environment secrets;
- Production находится в `Waiting` до reviewer approval;
- два запуска одного environment не исполняют deploy параллельно;
- cleanup step выполняется после искусственно сломанного deploy;
- health failure завершает workflow ненулевым кодом.

Структурные pytest подтверждают YAML contract локально, но не доказывают реальные
Environment rules, permissions runner, registry или VPS connectivity.
