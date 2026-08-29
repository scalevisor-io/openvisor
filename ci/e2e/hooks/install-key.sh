#!/bin/sh
# E2E_INSTALL_KEY_CMD hook for the CI git server: append the project's deploy
# key (read from stdin) to the git user's authorized_keys. The platform then
# proves write access with its hidden-ref preflight before any build starts.
# COMPOSE: the docker compose command prefix (files + project dir); defaults to
#   COMPOSE="docker compose -f compose.base.yml -f compose.dev.yml -f ci/compose.e2e.yml"
set -eu
COMPOSE=${COMPOSE:-"docker compose -f compose.base.yml -f compose.dev.yml -f ci/compose.e2e.yml"}
key=$(cat)
case "$key" in ssh-*) ;; *) echo "install-key: stdin is not a public key" >&2; exit 2;; esac
printf '%s\n' "$key" | $COMPOSE exec -T -u git gitserver sh -c 'cat >> /home/git/.ssh/authorized_keys'
echo "install-key: deploy key installed (${key%% *} ...${key#* })" | cut -c1-80
