#!/bin/sh
# E2E_MERGE_CMD hook for the CI git server: merge the build branch ($E2E_BRANCH)
# into main the way a customer would from their own clone (a real merge commit,
# not a ref update), so the platform's merge sweep detects it over SSH within
# one Beat tick and deploys the demo.
set -eu
COMPOSE=${COMPOSE:-"docker compose -f compose.base.yml -f compose.dev.yml -f ci/compose.e2e.yml"}
: "${E2E_BRANCH:?E2E_BRANCH is the branch to merge}"
REPO_PATH=${E2E_REPO_PATH:-/srv/git/todo.git}
$COMPOSE exec -T -u git -e "BRANCH=$E2E_BRANCH" -e "REPO=$REPO_PATH" gitserver sh -ec '
  tmp=$(mktemp -d)
  git clone -q "$REPO" "$tmp"
  cd "$tmp"
  git fetch -q origin "$BRANCH"
  git -c user.name=e2e-customer -c user.email=customer@example.org \
      merge --no-ff -q -m "Merge $BRANCH" FETCH_HEAD
  git push -q origin HEAD:main
  echo "merge: main is now"; git -C "$REPO" log --oneline -3 main
  rm -rf "$tmp"
'
