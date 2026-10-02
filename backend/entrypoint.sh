#!/bin/sh
set -e

# Materialize static_data from the committed templates (copy-if-missing, idempotent).
# Runs in every mode: api/api-dev/migrate/worker/beat each have their own filesystem
# and worker/beat read these files too. Never overwrites an operator-customized file.
for tpl in /app/app/static_data/*.example.json; do
  dst="${tpl%.example.json}.json"
  if [ ! -f "$dst" ]; then
    cp "$tpl" "$dst"
    echo "static_data: materialized $(basename "$dst") from template"
  fi
done

run_migrations() {
  alembic upgrade head
  python -m app.seed
}

# Long-lived modes exec under tini (-s: also when something else is PID 1, e.g.
# compose `init: true` or a shared process namespace). It reaps the orphans the
# server never waits for and forwards SIGTERM, so celery's warm shutdown and
# uvicorn's graceful stop behave as before.
INIT="tini -s --"

case "$1" in
  api)
    # Compose runs migrations here (default). The K8s chart applies them via a
    # dedicated pre-upgrade/post-install Job and sets RUN_MIGRATIONS_ON_START=0.
    if [ "${RUN_MIGRATIONS_ON_START:-1}" = "1" ]; then run_migrations; fi
    exec $INIT uvicorn app.main:app --host 0.0.0.0 --port 8000
    ;;
  api-dev)
    if [ "${RUN_MIGRATIONS_ON_START:-1}" = "1" ]; then run_migrations; fi
    exec $INIT uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
    ;;
  migrate)
    # One-shot: used by the K8s migration Job (helm hook).
    run_migrations
    ;;
  worker)
    exec $INIT celery -A app.workers.celery_app worker --loglevel=info --concurrency=4 -Q celery,dev
    ;;
  beat)
    exec $INIT celery -A app.workers.celery_app beat --loglevel=info
    ;;
  *)
    exec "$@"
    ;;
esac
