# macOS: подготовка рабочей машины

Установите Python 3.12+, [uv](https://docs.astral.sh/uv/getting-started/installation/), Git, Docker Desktop и OpenSSH, затем:

```bash
uv tool install "git+https://github.com/Daniil-Danone/ansible-deploy.git@v0.1.0"
ansible-deploy --version
docker version
ssh -V
ansible-deploy --help
```

CLI ставится глобально через [uv](https://docs.astral.sh/uv/) из приватного репозитория
с закреплённым тегом; доступ берётся из ваших Git credentials. Вариант через SSH,
обновление и установка из wheel релиза — в [руководстве по обновлению](../guides/upgrading.md).

Default external store находится в `~/Library/Application Support/ansible-deploy/`.
Временный override:

```bash
export ANSIBLE_DEPLOY_SECRETS_DIR="$HOME/.local-secure/myapp-secrets"
chmod 700 "$ANSIBLE_DEPLOY_SECRETS_DIR"
```

Не размещайте store в Git worktree или cloud sync. Продолжайте по
[каноническому runbook](../runbook.md).
