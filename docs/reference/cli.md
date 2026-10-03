# Справочник CLI

Глобальные options ставятся **до** subcommand:

```text
deploy [--project-dir PATH] [--verbose] COMMAND ...
```

- `--project-dir`: корень приложения; по умолчанию current directory.
- `--verbose`: расширенный runtime output. Секреты редактируются, но logs всё равно
  храните осторожно.

## Deploy

```text
deploy stage [--dry-run] [--ask-bootstrap-password] [--version SHA]
deploy prod  [--dry-run] [--ask-bootstrap-password] [--version SHA] [--yes]
```

Без `--version` берётся `git rev-parse HEAD`; допустимы 7–40 lowercase hex. Key
автоматически создаётся только не-dry deploy. Password prompt скрытый; его нельзя
совмещать с dry-run. Prod просит ввести `prod`, `--yes` предназначен для automation.

Managed user проверяется первым. На первом сервере `--ask-bootstrap-password` разрешает
root/bootstrap password; без флага возможен заранее установленный key `bootstrap_user`.

## Images

```text
deploy images publish {stage,prod} --registry {ghcr,dockerhub} --namespace NAME
  [--username NAME] [--ask-token] [--pull-username NAME]
  [--ask-pull-token] [--tag TAG]
```

Build/push выполняется локально, Compose обновляется digest. Publish token не сохраняется;
pull token используется для server auth. Prod confirmation эта команда не выполняет.

## Status, server и rollback

```text
deploy status {stage,prod}
deploy server update {stage,prod,all} [--dry-run] [--yes]
deploy rollback prod [--yes]
```

`status` делает только HTTPS GET к `domain + health_path`; он не проверяет SSH,
контейнеры или ресурсы. `server update` не выпускает application release. Rollback
возвращает предыдущую app-конфигурацию/images, но не БД.

## Коды завершения

| Код | Смысл |
|---:|---|
| 0 | успех |
| 1 | только обработанный `KeyboardInterrupt` |
| 2 | configuration, confirmation, paths или DNS |
| 3 | host fingerprint/environment guard/bootstrap state |
| 4 | SSH/access verification |
| 5 | runtime, provisioning, Docker image operation |
| 6 | application deployment transaction |
| 7 | health check |
| 9 | rollback |

Неожиданное исключение не преобразуется в универсальный код: Python traceback и его
exit status сохраняются, чтобы дефект не маскировался.
