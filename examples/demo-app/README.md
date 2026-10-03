# Demo application

This is a minimal backend/frontend project that owns its deployment configuration.
Run it locally with `docker compose -f compose.local.yml up --build`.

The CLI does **not** upload source or build images on the server. Before deployment,
build and push both images, inspect their registry digests, and replace `OWNER` and the
all-zero digest placeholders in `deploy/compose.stage.yml` (and Production separately).
See the repository root `GUIDE.md` for exact commands.

The CLI can perform that workflow atomically from this directory:

```text
deploy images publish stage --registry ghcr --namespace OWNER --username OWNER --ask-token --ask-pull-token
```

The first prompt is the publish token; the second must be a separate read-only pull token.
The command creates the ignored portable `registry-auth.json` once, so private images can
be pulled by the server. Existing valid auth is preserved. Do not copy a Docker Desktop
credential-helper config and do not commit this generated file.
