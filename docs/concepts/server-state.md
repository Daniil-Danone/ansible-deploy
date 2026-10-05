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

`deploy server update ENV` повторно сводит управляемое состояние. Он не обновляет DNS,
provider firewall, application release, миграции или backup job. Backup schedule
управляется отдельной `deploy backup setup prod`. `server update all` обрабатывает
Stage, Prod и Monitoring и сообщает накопленные ошибки.
