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

Build, push and pin application images from `.deploy/images.yml`:

```powershell
deploy images publish stage --registry ghcr --namespace OWNER --username OWNER --ask-token --ask-pull-token
```

`--ask-token` requests a publish token; `--ask-pull-token` separately requests the
read-only token stored for server pulls. When `application.registry_auth_file` is
configured but that file does not yet exist on disk, the command creates it once as
ignored portable inline auth. Existing valid auth is
preserved byte-for-byte; invalid/helper-backed files are never overwritten. POSIX uses
mode `0600`; Windows uses an owner-only ACL. Do not copy a Docker Desktop/macOS
`config.json` that relies on `credsStore` or `credHelpers` to the VPS.

The first command can bootstrap a password-only root account, creates the deployment
SSH key when absent, hardens the host and deploys immutable registry images. Source code
is never built or uploaded on the server.

- [Complete usage guide](GUIDE.md)
- [Minimal backend/frontend example](examples/demo-app)

Prefer an isolated `venv` or `pipx` installation so the CLI's Python dependencies do
not conflict with packages installed globally.

For contributors, the legacy root `config/` and `environments/` layout remains accepted
as a compatibility fixture. New applications must use project-local `.deploy/`.
