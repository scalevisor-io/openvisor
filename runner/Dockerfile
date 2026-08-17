# Openvisor OpenHands dev runner - one sandboxed container per development job.
# Drives the OpenHands SDK (v1) headless against the mounted project workspace.
# uv is used because pip's resolver can't solve openhands-ai's opentelemetry graph.
FROM python:3.12-slim

# docker.io is the DAEMON only on Debian - the client is the separate
# docker-cli package (a Recommends, which --no-install-recommends skips): the
# sandbox shipped dockerd with no `docker` for months before §dev-docker.
# ripgrep and tmux are the AGENT's tools, not conveniences: the SDK warns about
# both at startup ("For best agent performance, install tmux"), and the grep/glob
# tools this driver adds are the ACI the agent explores a repository through -
# without rg they fall back to slower paths, on a run that already spends most of
# its steps reading.
RUN apt-get update && apt-get install -y --no-install-recommends \
    git make openssh-client curl ca-certificates docker.io docker-cli \
    ripgrep tmux \
    && rm -rf /var/lib/apt/lists/*

# gh + glab so the agent drives GitHub/GitLab through their CLIs (issues, PRs,
# cross-repo reads) instead of hand-rolled curl - customers' KBs assume them.
# gh from GitHub's apt repo (tracks stable); glab pinned from GitLab's releases.
ARG GLAB_VERSION=1.112.0
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
      -o /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
      > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update && apt-get install -y --no-install-recommends gh \
    && rm -rf /var/lib/apt/lists/* \
    && curl -fsSL -o /tmp/glab.deb \
      "https://gitlab.com/gitlab-org/cli/-/releases/v${GLAB_VERSION}/downloads/glab_${GLAB_VERSION}_linux_$(dpkg --print-architecture).deb" \
    && dpkg -i /tmp/glab.deb && rm /tmp/glab.deb

RUN pip install --no-cache-dir uv \
    && uv pip install --system --no-cache openhands-ai==1.8.0

# docker compose v2 plugin: docker.io ships CLI+dockerd but no compose, and the
# §dev-docker inner daemon (entrypoint, DEV_DOCKER=1) exists precisely so the
# agent can boot the project's compose stack. Pinned like glab; after the heavy
# openhands layer so its cache survives plugin bumps.
ARG COMPOSE_VERSION=2.39.1
RUN mkdir -p /usr/local/lib/docker/cli-plugins \
    && curl -fsSL -o /usr/local/lib/docker/cli-plugins/docker-compose \
      "https://github.com/docker/compose/releases/download/v${COMPOSE_VERSION}/docker-compose-linux-$(uname -m)" \
    && chmod +x /usr/local/lib/docker/cli-plugins/docker-compose

COPY run_dev.py /run_dev.py
COPY live_events.py /live_events.py
COPY leak_scan.py /leak_scan.py
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

WORKDIR /workspace
# PYTHONDONTWRITEBYTECODE: the driver's imported modules (live_events) must never
# shed .pyc caches into /workspace - a __pycache__ once landed in a customer commit.
ENV OPENHANDS_SUPPRESS_BANNER=1 \
    LOG_ALL_EVENTS=true \
    PYTHONDONTWRITEBYTECODE=1

ENTRYPOINT ["/entrypoint.sh"]
