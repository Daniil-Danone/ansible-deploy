# SSH, пароли и ключи

## Не путайте три сущности

- **Root/bootstrap password**: временный способ войти на новый VPS. Вводится скрыто,
  передаётся helper через stdin, не записывается на диск.
- **SSH host key**: private часть хранится на VPS, fingerprint сверяется локально. Он
  отвечает на вопрос «это тот сервер?».
- **Deploy key**: private часть хранится у оператора, public — у пользователя `deploy`.
  Он отвечает на вопрос «имеет ли этот оператор доступ?».

Deploy key создаётся автоматически при первом обычном deploy, но не при dry-run. CLI
использует Ed25519 без passphrase, создаёт пару только при отсутствии обеих частей,
не перезаписывает существующую и отклоняет symlink/неполную/небезопасную пару.

Пути задаются в `config.yml`; обычно `.deploy/keys/stage_ed25519` и `.pub`. Они ignored.
На POSIX private получает `0600`, на Windows owner-only ACL. В Ansible container ключ
копируется в tmpfs с `0600`, поэтому Docker Desktop mount `0777` не ломает OpenSSH.

Сделайте зашифрованный backup private key. Потеря ключа требует recovery через консоль
провайдера. Компрометация — удаления public key на VPS и выпуска новой пары. Для Stage и
Prod рекомендуются разные пары.

После bootstrap password authentication SSH отключается. Root account полностью не
disabled. `deploy` имеет NOPASSWD sudo и входит в `docker`, поэтому фактически является
привилегированным; берегите его key как root credential.
