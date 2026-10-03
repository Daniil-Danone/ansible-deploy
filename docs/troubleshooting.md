# Решение проблем

## DNS указывает на `198.18.*`

Mihomo/Clash в fake-IP mode перехватывает DNS. Отключите TUN/System Proxy либо добавьте
домен в `fake-ip-filter`, очистите DNS cache. CLI должен увидеть общий реальный адрес у
domain и `server.host`.

## Fingerprint не совпадает или изменился

Остановитесь. Сверьте `ssh-keygen -lf ... -E sha256` через web/serial-консоль провайдера.
Не удаляйте `.deploy-state/.../known_hosts` до выяснения причины: это может быть MITM
или переустановленный VPS.

## `UNPROTECTED PRIVATE KEY FILE`, mode `0777`

Актуальный CLI не передаёт Windows bind mount OpenSSH напрямую: он копирует key в
container tmpfs с `0600`. Обновите CLI из `develop`. Не меняйте Windows ACL на Everyone;
исходный файл должен иметь owner-only ACL.

## `Permission denied` после bootstrap или нет доступа к Docker

Повторите `deploy stage`: актуальный lifecycle переподключается managed user после
добавления групп. Проверка: `ssh -i KEY deploy@HOST "sudo -n true && docker info"`.

## `wincred`, `desktop` или `osxkeychain` на сервере

Вы скопировали Docker Desktop config с credential helper. Удалите только ignored
`registry-auth.json` проекта и снова выполните publisher с `--ask-pull-token`. CLI
создаст portable inline auth. Не копируйте `~/.docker/config.json`.

## `pull access denied`

Проверьте package name/digest, срок server token, `read:packages`, SSO организации и
наличие host registry в auth. Для Docker Hub проверьте Read-only token и namespace.

## Не определяется версия

Проект должен иметь commit: `git init`, `git add .`, `git commit`. Альтернатива —
`--version` с реальным lowercase SHA длиной 7–40 символов.

## ACME/Certbot не выпускает сертификат

DNS должен уже резолвиться публично, provider firewall и UFW пропускать 80/443, домен
не должен проксироваться сервисом, скрывающим origin во время первичной проверки.
Проверьте, что другой процесс не занимает порты.

## Dry-run нового сервера падает

Это ожидаемо: dry-run не генерирует key, не использует root password и ничего не меняет.
Сначала обычный bootstrap, затем `deploy stage --dry-run`.

## `status` успешен, но хочется полной диагностики

`status` делает только HTTPS GET. Дополнительно проверьте SSH, `docker compose ps`,
`journalctl`, `df -h`, `sudo ufw status verbose` и `sudo ss -lntup`.
