FROM python:3.12-slim

# tini: entrypoint.sh runs every long-lived mode under it. uvicorn and celery
# reap only their own children, and git detaches a maintenance process after a
# fetch - as PID 1 they kept each one as a zombie until the container's task
# limit refused every fork.
RUN apt-get update && apt-get install -y --no-install-recommends \
    git openssh-client curl tini && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY alembic.ini entrypoint.sh ./
COPY alembic ./alembic
COPY app ./app
COPY tests ./tests
RUN chmod +x entrypoint.sh && mkdir -p /workspaces

EXPOSE 8000
