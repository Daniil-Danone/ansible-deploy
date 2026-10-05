# Ubuntu: подготовка рабочей машины

Это инструкция для управляющей машины; целевые VPS поддерживаются на Ubuntu 24.04 LTS и 26.04 LTS.

```bash
sudo apt-get update
sudo apt-get install -y python3.12 python3.12-venv git docker.io openssh-client
python3.12 -m venv .venv-deploy
source .venv-deploy/bin/activate
python -m pip install /path/to/ansible-deploy
docker version
ssh -V
deploy --help
```

Default store: `${XDG_DATA_HOME:-$HOME/.local/share}/ansible-deploy/`. Override:

```bash
export ANSIBLE_DEPLOY_SECRETS_DIR="$HOME/.local-secure/myapp-secrets"
install -d -m 700 "$ANSIBLE_DEPLOY_SECRETS_DIR"
```

Не запускайте CLI через `sudo`: owner secrets/SSH keys изменится. Продолжайте по
[каноническому runbook](../runbook.md).
