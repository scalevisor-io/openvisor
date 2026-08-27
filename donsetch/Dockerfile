# DonSeTch web-research sidecar (§web research).
#
# Ubuntu 24.04, not the python:3.12-slim the other sidecars use: the upstream
# release binary is linked against glibc 2.38+, and Debian 12 ships 2.36 - it
# will not start there.
#
# The engine is fetched from its published GitHub release and checksum-verified
# at build time. It is AGPL-3.0 and rides here UNMODIFIED, as a separate
# executable behind a process boundary - see README.md in this directory.
FROM ubuntu:24.04

ARG DONSETCH_VERSION=3.2.4
# sha256 of donsetch-linux-x64.tar.gz for the pinned version, from the release's
# published .sha256 asset. Bumping the version REQUIRES bumping this.
ARG DONSETCH_SHA256=8db2dc4130464015b168d35f92f6dcd57bce0d9dedded83d3400def3bb77154a
# Tier-2 escalation (bot walls, JS-only pages) needs a Chrome-family browser.
# Build with --build-arg WITH_BROWSER=0 for a search-only image (~700MB smaller);
# fetch/crawl then serve tier-1 pages and fail walled ones with wall.challenge.
ARG WITH_BROWSER=1

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl python3 python3-venv \
    && rm -rf /var/lib/apt/lists/*

RUN set -eux; \
    curl -fsSL -o /tmp/donsetch.tar.gz \
      "https://github.com/dondai44423/donsetch/releases/download/v${DONSETCH_VERSION}/donsetch-linux-x64.tar.gz"; \
    echo "${DONSETCH_SHA256}  /tmp/donsetch.tar.gz" | sha256sum -c -; \
    tar xzf /tmp/donsetch.tar.gz -C /usr/local/bin donsetch; \
    chmod +x /usr/local/bin/donsetch; \
    rm -f /tmp/donsetch.tar.gz; \
    /usr/local/bin/donsetch version

# Chrome + Xvfb for the ghost browser. Xvfb lets it run HEADFUL off-screen, which
# is what makes the fingerprint hold up; donsetch starts the server on :99 itself.
RUN if [ "$WITH_BROWSER" = "1" ]; then \
      set -eux; \
      apt-get update; \
      apt-get install -y --no-install-recommends gnupg xvfb fonts-liberation; \
      curl -fsSL https://dl.google.com/linux/linux_signing_key.pub \
        | gpg --dearmor -o /usr/share/keyrings/google-chrome.gpg; \
      echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google-chrome.gpg] https://dl.google.com/linux/chrome/deb/ stable main" \
        > /etc/apt/sources.list.d/google-chrome.list; \
      apt-get update; \
      apt-get install -y --no-install-recommends google-chrome-stable; \
      rm -rf /var/lib/apt/lists/*; \
    fi

WORKDIR /srv
RUN python3 -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir fastapi "uvicorn[standard]"
ENV PATH="/opt/venv/bin:$PATH"

COPY bridge.py README.md ./

# The engine writes its learned state (cookie lifetimes, page fingerprints, the
# rerank model) under $HOME/.cache/donsetch. Non-root, with a real home so a
# read-only-root deployment only needs that one volume writable.
RUN useradd --create-home --uid 10001 donsetch && chown -R donsetch:donsetch /srv
USER donsetch
ENV HOME=/home/donsetch

EXPOSE 3000
CMD ["uvicorn", "bridge:app", "--host", "0.0.0.0", "--port", "3000"]
