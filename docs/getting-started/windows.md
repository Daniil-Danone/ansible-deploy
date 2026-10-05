# Windows: подготовка рабочей машины

Установите Python 3.12+, [uv](https://docs.astral.sh/uv/getting-started/installation/), Git for Windows, Docker Desktop и OpenSSH Client. В PowerShell:

```powershell
uv tool install "git+https://github.com/Daniil-Danone/ansible-deploy.git@v0.1.0"
ansible-deploy --version
docker version
ssh -V
ansible-deploy --help
```

CLI ставится глобально через [uv](https://docs.astral.sh/uv/) из приватного репозитория
с закреплённым тегом; доступ берётся из ваших Git credentials. Вариант через SSH,
обновление и установка из wheel релиза — в [руководстве по обновлению](../guides/upgrading.md).

Временный override внешнего store для CI/изолированного теста:

```powershell
$env:ANSIBLE_DEPLOY_SECRETS_DIR = 'C:\secure\myapp-secrets'
```

Каталог должен принадлежать текущему пользователю; не размещайте его внутри Git
worktree или облачной sync-папки. Продолжайте по
[каноническому runbook](../runbook.md).
