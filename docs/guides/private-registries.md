# Приватные GHCR и Docker Hub

## Почему нужны два token

Локальная машина публикует образы, поэтому ей нужен token с записью. Сервер только
скачивает их, поэтому получает **другой** read-only token. Это ограничивает ущерб при
компрометации VPS. Не используйте publish token на сервере.

### GHCR

Создайте Personal access token **classic**:

- локальный: `write:packages`;
- серверный: `read:packages`;
- для package организации может потребоваться `Configure SSO -> Authorize`.

Fine-grained token может не показывать эти scopes. Для private repository GitHub в
некоторых конфигурациях также требует `repo`; выдавайте только реально необходимое.

### Docker Hub

Создайте два access token: Read/Write для publisher и Read-only для VPS.

## Безопасный запуск

```bash
deploy images publish stage \
  --registry ghcr --namespace OWNER --username OWNER \
  --ask-token --ask-pull-token
```

Для другой pull-учётной записи добавьте `--pull-username NAME`. Для Docker Hub замените
registry на `dockerhub`. Token вводятся скрыто и не появляются в history.

В автоматизации доступны `GHCR_TOKEN`, `GHCR_USERNAME`, `GHCR_PULL_TOKEN`,
`GHCR_PULL_USERNAME`; либо соответствующие `DOCKERHUB_*`. `GITHUB_TOKEN` и
`GITHUB_ACTOR` также подходят publisher GHCR. Не печатайте env в CI log.

CLI один раз создаёт configured `registry_auth_file` с inline base64 `username:token`.
Base64 — **не шифрование**: безопасность обеспечивают owner-only ACL на Windows и mode
`0600` на POSIX, игнорирование Git и защищённый компьютер. На VPS файл становится
`/home/deploy/.docker/config.json` с `0600`.

Не копируйте Docker Desktop `~/.docker/config.json`: `credsStore` (`wincred`,
`desktop`, `osxkeychain`) и `credHelpers` отсутствуют на Ubuntu. CLI такие файлы
отклоняет. Существующий корректный auth сохраняется byte-for-byte, автоматически не
ротируется; для ротации удалите именно внешний `registry-auth.json` и повторите publish.

Локальный `docker login` сохраняется в локальной Docker-конфигурации; CLI не делает
logout. Если publish оборвался, часть образов уже может находиться в registry, но
Compose меняется только после успеха и проверки всех images.
