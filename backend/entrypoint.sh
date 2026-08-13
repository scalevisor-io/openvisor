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

case "$1" in
  api)
    # Compose runs migrations here (default). The K8s chart applies them via a
    # dedicated pre-upgrade/post-install Job and sets RUN_MIGRATIONS_ON_START=0.
    if [ "${RUN_MIGRATIONS_ON_START:-1}" = "1" ]; then run_migrations; fi
    exec uvicorn app.main:app --host 0.0.0.0 --port 8000
    ;;
  api-dev)
    if [ "${RUN_MIGRATIONS_ON_START:-1}" = "1" ]; then run_migrations; fi
    exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
    ;;
  migrate)
    # One-shot: used by the K8s migration Job (helm hook).
    run_migrations
    ;;
  worker)
    exec celery -A app.workers.celery_app worker --loglevel=info --concurrency=4 -Q celery,dev
    ;;
  beat)
    exec celery -A app.workers.celery_app beat --loglevel=info
    ;;
  *)
    exec "$@"
    ;;
esac
