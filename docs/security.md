# Безопасность

- Получайте SSH fingerprint только out-of-band через консоль провайдера.
- Не коммитьте `.deploy-state`, private keys, `app.env`, registry auth.
- Для VPS используйте отдельный read-only registry token, не publish token.
- Base64 в Docker auth не шифрует credential; защищайте файл ACL/mode и backup.
- Разделяйте Stage/Prod серверы, domains, env, Compose, remote dirs и желательно keys.
- Пользователь `deploy` привилегирован: NOPASSWD sudo и Docker group эквивалентны
  значительному root-доступу.
- Root password login после bootstrap отключается, но root account не блокируется.
- UFW может заменить прежние правила. Bootstrap предназначен для dedicated VPS.
- Secret values редактируются в runtime output, sensitive Ansible tasks используют
  `no_log`, однако не публикуйте полные diagnostic logs без просмотра.
- Digest защищает точность image, но не доказывает безопасность содержимого: сканируйте
  dependencies/images и защищайте registry account MFA.

CLI не является secrets manager, PKI или backup системой. Ротацию tokens/keys,
off-site backup и проверку восстановления планируйте отдельно.
