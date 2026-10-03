# ansible-deploy

`ansible-deploy` is an installable CLI that provisions Ubuntu 24.04 servers and
deploys Docker Compose applications behind Nginx and Let's Encrypt. The application
project owns `.deploy/`, Compose and secret env files; the CLI wheel owns the pinned
Ansible runtime. A checkout of this repository is not needed to deploy an application.

```powershell
cd C:\Code\MyRepos\my-app
python -m pip install "ansible-deploy @ git+https://github.com/Daniil-Danone/ansible-deploy.git@develop"
deploy stage --ask-bootstrap-password
deploy status stage
```

The first command can bootstrap a password-only root account, creates the deployment
SSH key when absent, hardens the host and deploys immutable registry images. Source code
is never built or uploaded on the server.

- [Complete usage guide](GUIDE.md)
- [Minimal backend/frontend example](examples/demo-app)

For contributors, the legacy root `config/` and `environments/` layout remains accepted
as a compatibility fixture. New applications must use project-local `.deploy/`.
