#!/bin/sh
set -eu

secret_path=/dev/shm/ansible-bootstrap-password
exec 3<"$secret_path"
rm -f "$secret_path"
cat <&3
