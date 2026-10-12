#!/usr/bin/env bash
set -euo pipefail

# TODO: Replace this placeholder with this project's real, non-interactive checks.
# Keep every required lint, type-check and test command here: both CI and delivery
# run this version-controlled entrypoint and stop on the first failure.
printf '%s\n' \
  'CI is not configured. Edit .deploy/ci.sh and commit the project checks.' >&2
exit 1
