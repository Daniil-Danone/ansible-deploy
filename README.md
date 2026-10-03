# Stage and Production deployment

`deploy stage` provisions a clean Ubuntu 24.04 host and deploys a minimal fixture
application behind host Nginx with a Let's Encrypt certificate. Ansible runs in a
pinned Docker execution environment, so the host only needs Python 3.12 and Docker.

`deploy prod` uses the same roles with independent Production configuration, secrets,
server runtime and local SSH trust state. The checked-in Production Compose is a safe
acceptance fixture, not the real application; replace it with a reviewed immutable-image
Compose before any real Production rollout.

## Safety model

Bootstrap creates `deploy`, installs its public key and grants passwordless sudo.
The CLI reconnects as that account and runs `sudo -n true` **before** disabling
password authentication. Only ports 22, 80 and 443 are allowed by UFW. The fixture
binds its upstream to `127.0.0.1`; database, Redis and application ports are never
published publicly. Application `.env` and Compose files are installed as `0600`.

## Quickstart

The workstation needs Python 3.12+, Docker and the OpenSSH client. For a new Stage VPS:

1. Point the domain's DNS A/AAAA record at the VPS.
2. Copy `environments/stage/.env.example` to `environments/stage/stage.env`, then set
   application values and review `environments/stage/docker-compose.yml`.
3. Edit `environments/stage/config.yml`: set the VPS address, domain, ACME email and the
   SSH host-key SHA256 fingerprint shown by the provider console. Normally the configured
   SSH key paths can stay unchanged.
4. Install and deploy:

   ```text
   python -m pip install -e .
   deploy stage
   ```

   If the initial `root` SSH login uses a password instead of a preinstalled key:

   ```text
   deploy stage --ask-bootstrap-password
   ```

   The password is requested with hidden input, used only for the initial bootstrap and
   never written to config, inventory or local state. The CLI creates the Ed25519 deploy
   key automatically when both configured key files are absent, installs its public half
   on the server and uses that key for all subsequent connections. Existing keys are never
   rotated; a missing `.pub` is safely restored from its private key.
5. Verify: `deploy status stage`.

`--ask-bootstrap-password` cannot be combined with `--dry-run`, because check mode does not
create the managed `deploy` account. A normal `deploy stage --dry-run` is useful after the
first deployment. Reapply OS controls with `deploy server update stage`.

The CLI rejects a lone public key when its matching private key is absent instead of
overwriting it. It also pins the server host key in the environment-specific
`.deploy-state` directory and rejects later host-key changes.

Key publication is serialized per key pair and exclusive: concurrent deploy commands never
replace each other's files, and symlink/aliased private and public destinations are rejected.
On Windows and Linux, the private file keeps the permissions created by `ssh-keygen`; the CLI
then asks the platform OpenSSH client to read it and fails if its permissions or ACL are not
accepted. Bootstrap passwords preserve spaces and tabs exactly; NUL, CR and LF are rejected
because they would make the one-shot stdin protocol ambiguous. The container exposes the
password to Ansible through a single-use executable password helper backed by `/dev/shm`;
the helper unlinks its backing file before returning the exact bytes.

## Advanced configuration

Obtain the configured host-key fingerprint through the provider's trusted web/serial
console rather than trusting an unauthenticated network scan. `bootstrap_user` defaults to
`root`; a key-only initial login remains the default when `--ask-bootstrap-password` is not
passed. The application env file is ignored by Git and must never be committed.

For an already bootstrapped server, `deploy stage --dry-run` previews managed-host changes;
bootstrap is deliberately not attempted in check mode, so the `deploy` account must already
exist. `deploy server update stage --dry-run` is also supported. Any new hardening control
is added to the versioned global schema and the `hardening` role, then exercised on
Stage through this command.

## Production deploy and rollback

1. Copy `environments/prod/.env.example` to `environments/prod/prod.env` and fill it
   locally. `prod.env` is ignored; do not pass secrets through arguments or commit them.
   An Ansible-Vault-rendered local env file is compatible with the same contract.
   Export private-registry credentials to Docker `config.json` at
   `environments/prod/registry-auth.json` (also ignored), preferably by rendering it from
   Vault. The file is mounted read-only into the execution container and installed as
   the deploy user's `~/.docker/config.json` with mode `0600` and redacted Ansible output.
2. Replace the fixture `environments/prod/docker-compose.yml` with the reviewed
   application Compose. Every Production image must be pinned by `@sha256:<digest>`;
   mutable/missing tags and server-side builds are rejected. Keep every published upstream
   on `127.0.0.1` and list the port in `allowed_loopback_ports`. Relative bind mounts are
   rejected. Persistent data must use named volumes or an explicitly approved normalized
   absolute path from `allowed_bind_paths`, so release directories never become database
   storage.
3. Edit `environments/prod/config.yml` with Production-only VPS, key paths, out-of-band host
   fingerprint, domain, ACME email and `/srv` or `/opt` runtime. Never reuse Stage paths.
   The key is created automatically on the first real deployment when both files are absent.
4. Preview with `deploy prod --dry-run --version <git-sha>`; dry-run is read-only and
   does not require confirmation.
5. Deploy interactively with `deploy prod --version <git-sha>`, then type `prod` after
   reviewing the printed environment, host and domain. Automation must explicitly use
   `deploy prod --yes --version <git-sha>`. For an initial password-only root login, add
   `--ask-bootstrap-password`; the Production confirmation is completed first, followed by
   the hidden password prompt.
6. Verify with `deploy status prod`. Reapply shared controls with
   `deploy server update prod` or, in deterministic Stage-then-Prod order,
   `deploy server update all`. The `all` form attempts both environments, reports each
   failed target and returns failure if either update fails. Mutating commands that include
   Production require the same confirmation (or `--yes`); `server update ... --dry-run`
   does not.

Versions are lowercase 7-40 character Git SHAs. If omitted, the infrastructure
repository `HEAD` is used; for a real application, pass the immutable application/image
Git SHA explicitly. Each release stores Compose, env and metadata under
`<remote_dir>/releases/<sha>` with mode `0600` for sensitive files. `current` and
`previous` are committed server-side links. Compose, env, immutable image references and
the relevant environment configuration are covered by one checksum. Reusing a SHA with
different inputs is rejected.

Deployment is an explicit transaction: the candidate is reconciled with bounded waiting,
every Compose service and the loopback endpoint are checked, then Nginx/TLS and public HTTPS
health are verified. Only after all checks pass does finalization update `current` and
`previous`. Any failure runs recovery, restores both original links (including their absence),
reconciles the original release without pulling images and verifies its service, loopback and
public health before clearing transaction state.

`deploy rollback prod` requires Production confirmation and swaps `current` with
`previous`, reconciles Compose and verifies loopback and public HTTPS health. If health
fails, it restores the release that was active before rollback and exits with code `9`.
Rollback covers application Compose/config/env only. **It never rolls back database
schema or data**; migrations must be backward-compatible or handled by a separately
reviewed database recovery procedure. Only one previous release is directly addressable,
while older release directories remain for audit/manual retention.

Local runtime state is separated as `.deploy-state/stage` and `.deploy-state/prod`,
including inventory and `known_hosts`. The config loader requires the selected environment,
the config's `environment`, and env-file `APP_ENV` to agree. The server-side `environment`
identity additionally prevents deploying one environment into another environment's runtime.
The authoritative host marker `/etc/ansible-deploy/identity.json` is checked before every
remote mutation in deploy, update and rollback; the point-1 text marker is accepted only for
a constrained one-time migration to the known legacy Stage layout. Compose project and Nginx
site names are environment-specific. Stage deliberately retains the legacy Compose project
name `myapp` from point 1 so existing named volumes and containers are reconciled in place;
Production uses `myapp_prod` and cannot collide with it.

After the first committed release, domain, health path and upstream port are immutable for
that server. The pre-mutation guard compares requested routing with committed release
metadata. The authoritative `/etc/ansible-deploy/identity.json` also pins environment,
canonical runtime directory, Compose project, Nginx site and routing before deploy, update
or rollback can mutate the host. To change any of these fields, provision a new server or
follow a separately reviewed explicit reset procedure; changing to another directory on the
same managed host is rejected.

The first Stage deployment after point 1 detects the legacy `/srv/myapp/compose.yml` and
`.env`. Without running `compose up/down`, it proves that every declared legacy service is
present, running and healthy after a bounded wait, and both loopback/public health pass.
Before changing identity or release pointers, the raw legacy Compose is parsed: interpolation,
relative `env_file`, configs/secrets/build/extends and relative binds other than the exact
read-only point-1 `./fixture-nginx.conf` mount are rejected. Named volumes and configured,
normalized absolute bind paths remain supported. Only then are Compose and env copied with
the point-1 `fixture-nginx.conf` asset at mode `0600` into a synthetic immutable
`legacy-<sha256>` release and committed as `current`.
The Stage Compose project remains `myapp`, so a later candidate failure can reconcile the
snapshot against the original containers and named volumes. A failed legacy verification
stops before the old Compose project or committed pointers are changed.

Exit codes are `0` success, `1` general/interrupted, `2` configuration or DNS,
`3` host authentication, `4` SSH/access verification, `5` provisioning/runtime,
`6` deployment, `7` health check and `9` rollback. Values read from the env file are redacted from streamed output;
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
make syntax-check
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
