# Что CLI настраивает на сервере

Цель — выделенный чистый Ubuntu 24.04 LTS или 26.04 LTS. CLI пока не определяет дистрибутив автоматически.

- создаёт managed пользователя `deploy`, authorized key, NOPASSWD sudo, группу Docker;
- устанавливает Docker Engine/Compose, Nginx, Certbot, UFW, Fail2ban;
- включает unattended upgrades и условный reboot timer; timezone по умолчанию
  `Europe/Moscow` из `.deploy/config/global.yml`;
- оставляет снаружи SSH/80/443, а application принимает только
  `127.0.0.1:8080`;
- размещает releases ниже настроенного `/srv/...` или `/opt/...`;
- сохраняет registry auth как `/home/deploy/.docker/config.json` с `0600`;
- отключает password SSH login и получает/обновляет Let's Encrypt certificate.

UFW может сбросить уже существующие правила — не применяйте bootstrap к shared VPS.
Root account не блокируется целиком. Docker group и passwordless sudo дают `deploy`
высокие привилегии.

## Непрерывность named volumes

Данные приложения живут в Docker named volumes с именем `<compose project>_<ключ>`:
Compose project для Production всегда `myapp_prod`, для Stage — последний сегмент
`application.remote_dir`. Политика CLI запрещает `name:`/`external:`/driver options у
volumes, поэтому имя детерминировано. Если ключ volume в Compose переименовать или
поменять project name, `docker compose up` молча создаст **новый пустой** volume, и
приложение стартует на пустой БД.

Поэтому каждый deploy поверх уже активного release (`<remote_dir>/current` существует)
до transaction и до любого изменения контейнеров сравнивает top-level `volumes:` нового
Compose с Compose активного release и с `docker volume ls` и fail closed, если:

- новый Compose больше не объявляет volume активного release (данные останутся на
  сервере, но перестанут монтироваться);
- новый Compose объявляет volume, которого на сервере ещё нет (он был бы создан пустым).

Сообщение перечисляет полные имена volumes. Проверка работает и в `--dry-run`. Первый
deploy (нет `current`) не затрагивается. Намеренное добавление или удаление volume
подтверждается одноразово для конкретного deploy:

```bash
deploy prod --yes --allow-volume-change cache
```

Флаг повторяемый, не сохраняется в config и не влияет на checksum release, поэтому
следующий deploy снова проверяется полностью. `deploy rollback prod` проверяет, что все
volumes предыдущего release ещё существуют; volume, добавленный текущим release, при
откате просто перестаёт монтироваться (данные не удаляются). Abort/recovery возвращает
уже работавший Compose, а Restore намеренно наполняет volumes изолированного окружения
из backup, поэтому там проверка не нужна.

## Повторное применение

`deploy server update ENV` повторно сводит управляемое состояние. Он не обновляет DNS,
provider firewall, application release, миграции или backup job. Backup schedule
управляется отдельной `deploy backup setup prod`. `server update all` обрабатывает
Stage, Prod и Monitoring и сообщает накопленные ошибки.
