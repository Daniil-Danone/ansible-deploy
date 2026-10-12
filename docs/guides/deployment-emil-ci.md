# Настройка CI для uwords

Это инструкция для `Emil307/uwords`. Она продолжает базовую настройку проекта и
описывает отдельный CI, который создаёт `ansible-deploy` при `project init` или
`project sync`.

Итоговая схема:

- pull request в `main` или Stage-ветку → отдельный workflow `CI`;
- push в `main` или Stage-ветку → отдельный workflow `CI`;
- `.github/workflows/ci.yml` запускает единую команду `bash .deploy/ci.sh`;
- delivery workflow перед сборкой и деплоем запускает тот же `.deploy/ci.sh`;
- оба workflow перед проверками устанавливают Temurin JDK 21 и Node.js 22;
- команды проверок хранятся в репозитории, а не в GitHub Variable.

Важно: workflow появится во вкладке GitHub Actions только после commit и push.
До настройки `.deploy/ci.sh` CI намеренно работает в режиме fail-closed и падает:
это не позволяет случайно начать delivery без проверок проекта.

---

## 1. Что создаёт ansible-deploy

Нужен `ansible-deploy v0.5.7` или новее. После публикации релиза обновить CLI и
проверить версию:

```bash
uv tool install --force \
  'git+https://github.com/Daniil-Danone/ansible-deploy.git@v0.5.7'
ansible-deploy --version
```

Из корня `uwords` выполнить:

```bash
git fetch origin
git checkout sskier-develop
git pull --ff-only origin sskier-develop

ansible-deploy project sync --check
ansible-deploy project sync
git status --short
git diff -- .deploy/ci.sh .github/workflows/ci.yml .github/workflows/deploy.yml
```

Если рабочая Stage-ветка называется не `sskier-develop`, сначала передать её при
инициализации проекта вместе с Production-веткой:

```bash
ansible-deploy project init \
  --stage-branch <stage-branch> \
  --production-branch main \
  --tool-sha <проверенный-полный-40-символьный-SHA-ansible-deploy>
```

После sync в application repository должны быть:

- `.github/workflows/ci.yml` — управляемый GitHub Actions workflow;
- `.deploy/ci.sh` — принадлежащий проекту entrypoint с командами CI;
- `.github/workflows/deploy.yml` — delivery workflow, который вызывает тот же entrypoint;
- `.deploy/template-state.yml` — состояние scaffold для последующих обновлений.

`project sync` управляет структурой workflow, но после первой генерации сохраняет
проектное содержимое `.deploy/ci.sh`. Поэтому команды можно менять вместе с кодом
приложения и проверять в том же pull request.

---

## 2. Настройка .deploy/ci.sh для uwords

Сгенерированный файл сначала содержит placeholder с `exit 1`. Заменить его целиком
на следующий скрипт:

```bash
#!/usr/bin/env bash
set -euo pipefail

# GitHub-hosted Ubuntu runners expose JDK 21 through JAVA_HOME_21_X64.
# Локально можно заранее выбрать JDK 21 обычным JAVA_HOME.
if [ -n "${JAVA_HOME_21_X64:-}" ]; then
  export JAVA_HOME="$JAVA_HOME_21_X64"
  export PATH="$JAVA_HOME/bin:$PATH"
fi

java_major="$(java -version 2>&1 | awk -F '[\".]' 'NR == 1 { print $2 }')"
if [ "$java_major" != "21" ]; then
  printf 'uwords CI requires Java 21, got Java %s\n' "$java_major" >&2
  exit 1
fi

./gradlew --no-daemon check

(
  cd frontend
  npm ci
  npm run typecheck
  npm run lint
  npm run format:check
  npm test
  npm run build
)
```

Эти команды следуют фактической структуре `uwords`:

- корневой Gradle project использует Java toolchain 21;
- `./gradlew check` компилирует модули и запускает их проверки и unit-тесты;
- frontend имеет собственный `package-lock.json` и scripts `typecheck`, `lint`,
  `format:check`, `test`, `build`;
- `npm ci` воспроизводимо устанавливает зависимости из lock-файла.

Python acceptance tests сюда намеренно не добавлены: им нужны внешние services и
отдельно описанное тестовое окружение. Их нельзя выдавать за надёжную проверку в
чистом GitHub runner. Когда для acceptance будет создана инфраструктура CI, её
нужно добавить отдельным job или явным блоком в `.deploy/ci.sh`.

Перед push запустить entrypoint локально в Linux, WSL или Git Bash с JDK 21 и
Node.js 22:

```bash
bash .deploy/ci.sh
```

Скрипт должен завершиться с кодом `0`. Не удалять проверки только ради зелёного run:
исправлять нужно причину сбоя или явно пересматривать CI-контракт проекта.

---

## 3. Конфликт .deploy-new при обновлении

Если новая версия `ansible-deploy` меняет базовый шаблон `.deploy/ci.sh`, а в
`uwords` файл уже настроен, sync не перезапишет проектные команды. Он сохранит новый
кандидат рядом, например:

```text
.deploy/ci.sh.deploy-new
```

Нужно вручную сравнить файлы:

```bash
git diff --no-index -- .deploy/ci.sh .deploy/ci.sh.deploy-new
```

Перенести полезные изменения scaffold в рабочий `.deploy/ci.sh`, проверить
`bash .deploy/ci.sh`, удалить только обработанный `.deploy-new` и повторить:

```bash
ansible-deploy project sync --check
```

Ожидается отсутствие pending updates и conflicts. Не заменять настроенный uwords
скрипт кандидатом целиком без review.

---

## 4. Commit и push в uwords

Перед commit проверить только ожидаемые изменения:

```bash
git status --short
git diff --check
git diff -- .deploy .github
bash .deploy/ci.sh
```

Затем закоммитить сгенерированные workflow, настроенный entrypoint и состояние
template одним Conventional Commit, например:

```text
ci: configure project checks in GitHub Actions
```

После этого отправить рабочую ветку:

```bash
git push --set-upstream origin <рабочая-ветка>
```

Только после push GitHub увидит `.github/workflows/ci.yml`. Локальная генерация
ничего не устанавливает на GitHub и не создаёт run.

---

## 5. Настройки GitHub Actions

Открыть `Emil307/uwords → Settings → Actions → General` и проверить:

- Actions разрешены для репозитория;
- разрешены actions, которые используются workflow;
- Workflow permissions достаточно оставить `Read repository contents permission`;
- для CI не включать лишнее право записи.

`.github/workflows/ci.yml` использует только checkout и команды репозитория. Для
самого CI не нужны GitHub Environments, deployment secrets или repository variables.

`ansible-deploy project init/sync` создаёт только файлы в рабочем дереве. Он не
создаёт GitHub Settings, Secrets, Variables, Environments, branch protection или
rulesets — их настраивает владелец репозитория вручную.

Старая repository variable `QUALITY_COMMAND` больше не используется ни отдельным
CI, ни delivery workflow. После merge обновлённого scaffold её можно удалить в
`Settings → Secrets and variables → Actions → Variables`, чтобы не оставлять
ложный источник конфигурации.

Deployment variables и secrets из CD-инструкции (`REGISTRY_PREFIX`, health URLs,
registry credentials, secret store) остаются нужны delivery workflow. Удаление
`QUALITY_COMMAND` на них не влияет.

---

## 6. Первый запуск и pull request

После push открыть `Emil307/uwords → Actions → CI`. Проверить последний run или
использовать GitHub CLI:

```bash
gh run list --repo Emil307/uwords --workflow ci.yml --limit 5
gh run watch --repo Emil307/uwords
```

Ожидаемый результат job `quality`:

```text
checkout
  → Temurin JDK 21
  → Node.js 22
  → bash .deploy/ci.sh
      → Java 21
      → ./gradlew --no-daemon check
      → npm ci
      → typecheck / lint / format:check / test / build
```

Создать pull request рабочей ветки в Stage-ветку. В PR должен появиться check
`CI / quality`. Сначала добиться зелёного отдельного CI, затем проверять delivery.

Если workflow ещё не попал в default branch, он всё равно выполняется для подходящих
push/PR events, но его постоянная карточка и ручное управление в Actions становятся
предсказуемыми после merge workflow в default branch.

---

## 7. Required status check и ruleset

Сначала нужен хотя бы один выполненный run: только после него GitHub обычно предлагает
имя check в списке required status checks.

В `Emil307/uwords → Settings → Rules → Rulesets` создать или обновить ruleset для
`main` и Stage-ветки:

- target — branches;
- enforcement status — Active;
- включить `Require a pull request before merging`;
- включить `Require status checks to pass`;
- добавить точный check `CI / quality`;
- включить требование актуальной ветки перед merge, если команда готова к повторным run;
- запретить bypass всем, кому он не нужен для аварийного восстановления.

Если в интерфейсе используется классическая Branch protection rule, настроить те же
требования там. Не создавать одновременно пересекающиеся правила с разным смыслом.

Не выбирать одноимённый job из другого workflow наугад. Required check должен быть
именно от workflow `CI`; точное имя сверить в успешном PR run.

---

## 8. Связь CI и CD

Отдельный `.github/workflows/ci.yml` отвечает за раннюю обратную связь в PR и является
удобным required status check. `.github/workflows/deploy.yml` перед image build и
деплоем снова вызывает тот же `.deploy/ci.sh`.

Это намеренное повторение на границе delivery:

- PR нельзя смержить без зелёного CI;
- delivery проверяет именно commit, который собирается и разворачивается;
- набор команд не расходится между GitHub Variable и файлами проекта;
- изменение проверок проходит code review вместе с приложением.

Зелёный CI сам по себе ничего не деплоит. Stage и Production по-прежнему управляются
условиями и Environments из delivery workflow и отдельной CD-инструкции.

---

## 9. Короткая диагностика

| Ошибка | Что проверить |
|---|---|
| Workflow не виден в Actions | Файл закоммичен и запушен; путь ровно `.github/workflows/ci.yml`; workflow попал в default branch для постоянного отображения |
| `CI is not configured` | Placeholder в `.deploy/ci.sh` не заменён uwords-командами или изменение не попало в push |
| Java не найдена или версия не 21 | В workflow сохранён managed setup Temurin 21; локально выбран JDK 21 через `JAVA_HOME` |
| Node/npm имеют неожиданную версию | В workflow сохранён managed setup Node.js 22; не удалён setup-step при ручном merge `.deploy-new` |
| `Permission denied: ./gradlew` | Вернуть executable bit: `git update-index --chmod=+x gradlew` |
| `npm ci` сообщает о lock mismatch | Синхронно обновить и закоммитить `frontend/package.json` и `frontend/package-lock.json` |
| Frontend падает на format | Локально выполнить formatter, проверить diff и закоммитить результат; не заменять `format:check` на write-команду в CI |
| `*.deploy-new` conflict | Смержить candidate вручную в основной файл, удалить обработанный candidate, повторить `project sync --check` |
| Required check нельзя выбрать | Сначала получить run `CI / quality` в этом репозитории и обновить страницу ruleset |
| CI зелёный, deploy красный | Проверить отдельный run `Application delivery`: registry, Environments, secret store, health URL и CD-настройки |
| Удалили `QUALITY_COMMAND`, CI не изменился | Это ожидаемо: команды теперь берутся только из закоммиченного `.deploy/ci.sh` |

При диагностике всегда открывать первый упавший command в `quality`, а не только
последний итог job. `set -euo pipefail` завершает script на первой реальной ошибке.

---

## 10. Чеклист

- [ ] Подтверждены Stage-ветка и Production-ветка `main`.
- [ ] Выполнен `ansible-deploy project sync`.
- [ ] Созданы `.github/workflows/ci.yml` и `.deploy/ci.sh`.
- [ ] Fail-closed placeholder заменён командами uwords.
- [ ] Локально используются Java 21 и совместимый Node.js; в Actions закреплён Node.js 22.
- [ ] `bash .deploy/ci.sh` проходит полностью.
- [ ] Нет необработанных `*.deploy-new` conflicts.
- [ ] `ansible-deploy project sync --check` проходит.
- [ ] Workflow и entrypoint закоммичены и запушены.
- [ ] В GitHub Actions появился workflow `CI`.
- [ ] Первый `CI / quality` зелёный.
- [ ] Создан PR в Stage-ветку и check виден в PR.
- [ ] `CI / quality` добавлен в required status checks для защищённых веток.
- [ ] Проверены `Settings → Actions → General` и минимальные permissions.
- [ ] Старая variable `QUALITY_COMMAND` удалена после обновления workflow.
- [ ] Команда понимает, что GitHub Settings/Secrets/Variables CLI не создаёт.
- [ ] Delivery отдельно проверен после зелёного CI.
