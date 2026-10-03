# Demo application

This is a minimal backend/frontend project that owns its deployment configuration.
Run it locally with `docker compose -f compose.local.yml up --build`.

The CLI does **not** upload source or build images on the server. Before deployment,
build and push both images, inspect their registry digests, and replace `OWNER` and the
all-zero digest placeholders in `deploy/compose.stage.yml` (and Production separately).
See the repository root `GUIDE.md` for exact commands.
