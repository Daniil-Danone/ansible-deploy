# Ubuntu: подготовка рабочей машины

Это инструкция для управляющей машины; целевые VPS поддерживаются на Ubuntu 24.04.
Дополнительно установите [uv](https://docs.astral.sh/uv/getting-started/installation/).

```bash
sudo apt-get update
sudo apt-get install -y python3.12 python3.12-venv git docker.io openssh-client
uv tool install "git+https://github.com/Daniil-Danone/ansible-deploy.git@v0.1.0"
ansible-deploy --version
docker version
ssh -V
ansible-deploy --help
```

CLI ставится глобально через [uv](https://docs.astral.sh/uv/) из приватного репозитория
с закреплённым тегом; доступ берётся из ваших Git credentials. Вариант через SSH,
обновление и установка из wheel релиза — в [руководстве по обновлению](../guides/upgrading.md).

Default store: `${XDG_DATA_HOME:-$HOME/.local/share}/ansible-deploy/`. Override:

```bash
export ANSIBLE_DEPLOY_SECRETS_DIR="$HOME/.local-secure/myapp-secrets"
install -d -m 700 "$ANSIBLE_DEPLOY_SECRETS_DIR"
```

Не запускайте CLI через `sudo`: owner secrets/SSH keys изменится. Продолжайте по
[каноническому runbook](../runbook.md).
