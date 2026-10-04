# Windows: подготовка рабочей машины

Установите Python 3.12+, Git for Windows, Docker Desktop и OpenSSH Client. В PowerShell:

```powershell
python -m venv .venv-deploy
.\.venv-deploy\Scripts\Activate.ps1
python -m pip install C:\path\to\ansible-deploy
docker version
ssh -V
deploy --help
```

Временный override внешнего store для CI/изолированного теста:

```powershell
$env:ANSIBLE_DEPLOY_SECRETS_DIR = 'C:\secure\myapp-secrets'
```

Каталог должен принадлежать текущему пользователю; не размещайте его внутри Git
worktree или облачной sync-папки. Продолжайте по
[каноническому runbook](../runbook.md).
