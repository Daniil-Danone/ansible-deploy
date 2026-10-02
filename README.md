# Stage deployment

`deploy stage` provisions a clean Ubuntu 24.04 host and deploys a minimal fixture
application behind host Nginx with a Let's Encrypt certificate. Ansible runs in a
pinned Docker execution environment, so the host only needs Python 3.12 and Docker.

## Safety model

Bootstrap creates `deploy`, installs its public key and grants passwordless sudo.
The CLI reconnects as that account and runs `sudo -n true` **before** disabling
password authentication. Only ports 22, 80 and 443 are allowed by UFW. The fixture
binds its upstream to `127.0.0.1`; database, Redis and application ports are never
published publicly. Application `.env` and Compose files are installed as `0600`.

## Configure and deploy

1. Copy `environments/stage/.env.example` to `environments/stage/stage.env` and
   fill it locally. This file is ignored by Git.
2. Edit `environments/stage/config.yml`: real VPS IP, users, SSH key/public-key
   paths, domain and ACME email. Record the VPS host-key SHA256 fingerprint obtained
   through a trusted provider console/out-of-band channel. Point DNS A/AAAA records
   at the VPS first. The CLI rejects both a first connection with a different key
   and any later host-key change; trusted keys persist in `.deploy-state/known_hosts`.
3. Install the CLI: `python -m pip install -e .`.
4. Preview managed-host changes: `deploy stage --dry-run`. Bootstrap is deliberately
   not attempted in check mode; the `deploy` account must already exist.
5. Deploy: `deploy stage`.
6. Verify: `deploy status stage` and run the acceptance checks below.

Reapply OS controls to an existing Stage server with
`deploy server update stage`; `--dry-run` is supported. Any new hardening control
is added to the versioned global schema and the `hardening` role, then exercised on
Stage through this command.

Exit codes are `0` success, `1` general/interrupted, `2` configuration or DNS,
`3` host authentication, `4` SSH/access verification, `5` provisioning/runtime,
`6` deployment and `7` health check. Values read from the env file are redacted from streamed output;
Ansible also uses `no_log` for env delivery. Do not pass secrets through arguments.

## Acceptance runbook

Run twice to demonstrate convergence:

```text
deploy stage
deploy stage
deploy status stage
```

Then verify on the VPS (replace the host):

```text
ssh -i <key> deploy@<host> 'sudo -n true'
ssh -i <key> deploy@<host> 'sudo sshd -T | grep -E "passwordauthentication no|permitrootlogin prohibit-password"'
ssh -i <key> deploy@<host> 'sudo ufw status verbose'
ssh -i <key> deploy@<host> 'sudo ss -lntup'
ssh -i <key> deploy@<host> 'stat -c "%a %n" /srv/myapp/.env /srv/myapp/compose.yml'
ssh -i <key> deploy@<host> 'systemctl list-timers deploy-reboot-if-required.timer'
ssh -i <key> deploy@<host> 'systemctl cat deploy-reboot-if-required.service deploy-reboot-if-required.timer'
curl --fail --show-error --location https://<domain>/health
```

Expected: password login disabled; passwordless sudo succeeds; only the configured SSH port, 80 and 443
are public; upstream listens only on `127.0.0.1:8080`; both delivered files are
`600`; timer calendar includes `Europe/Moscow` and service has
`ConditionPathExists=/var/run/reboot-required`. On the second deployment, review
the Ansible recap: unexpected changes are a defect. A real Stage VPS, DNS and ACME
email are required for this E2E gate; local tests cannot honestly replace it.

## Local quality gates

```text
python -m pytest
python -m ruff check .
python -m mypy
yamllint .
ansible-lint ansible
ansible-playbook ansible/playbooks/site.yml --syntax-check -i tests/fixtures/inventory.yml
docker build -t ansible-deploy:local .
```

The checked-in Compose is only a `/health` fixture. Replace it and its loopback
upstream contract with the real application Compose before Production. Every
published port must use `127.0.0.1` and be listed in `allowed_loopback_ports`;
host networking, interpolation in `ports`/`network_mode`, Compose `include`/`extends`
and wildcard/public bindings are rejected before Ansible starts. Networks must be
Compose-managed bridge/default networks; external networks, custom runtime names,
container network sharing and unsupported/interpolated drivers are rejected.
`remote_dir` must
be a normalized child of `/srv` or `/opt`; traversal and system-root paths are rejected.
