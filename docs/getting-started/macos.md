# macOS: подготовка рабочей машины

Установите Python 3.12+, Git, Docker Desktop и OpenSSH, затем:

```bash
python3 -m venv .venv-deploy
source .venv-deploy/bin/activate
python -m pip install /path/to/ansible-deploy
docker version
ssh -V
deploy --help
```

Default external store находится в `~/Library/Application Support/ansible-deploy/`.
Временный override:

```bash
export ANSIBLE_DEPLOY_SECRETS_DIR="$HOME/.local-secure/myapp-secrets"
chmod 700 "$ANSIBLE_DEPLOY_SECRETS_DIR"
```

Не размещайте store в Git worktree или cloud sync. Продолжайте по
[каноническому runbook](../runbook.md).
