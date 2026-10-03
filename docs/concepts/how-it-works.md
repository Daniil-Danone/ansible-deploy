# Как работает deploy

1. CLI читает project-local `.deploy/`, валидирует пути, env и Compose.
2. `images publish` локально строит каждый image, push-ит случайно уникальный tag,
   получает digest из push и проверяет immutable `repository@digest` через registry.
3. Только после успеха всех images Compose атомарно обновляется под lock.
4. Deploy проверяет, что DNS domain и server имеют общий реальный IP.
5. `ssh-keyscan` сверяется с fingerprint из доверенной консоли и сохранённым
   `.deploy-state/<environment>/known_hosts`.
6. CLI сначала пробует managed user. На первом запуске явно разрешённый root password
   либо заранее установленный key `bootstrap_user` создаёт managed account.
7. Packaged Ansible runtime запускается локально в Docker; секретный SSH key копируется
   во внутренний tmpfs с `0600`.
8. На VPS применяется server state, загружается релиз и выполняется HTTPS health check.

Git SHA идентифицирует релиз. Один SHA нельзя незаметно переиспользовать с другими
Compose/env/config inputs. Исходники и build context на сервер не загружаются.

Stage допускает более мягкие ограничения, Production требует digest у каждого image
и запрещает `build:`. Deploy транзакционен на уровне application release, но не обещает
zero downtime и не включает транзакцию базы данных.
