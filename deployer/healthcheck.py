"""Shared demo readiness probe (PROMPT §17): `compose up -d` succeeds even when
the app container immediately crash-loops (e.g. a Dockerfile that forgot to COPY
its entrypoint file), which used to publish a live-looking demo URL that answers
502 behind Traefik. Both backends (main.py docker-exec, k8s.py pod-exec) run
these scripts inside the demo's DinD namespace, where the compose port publish
binds: alive = anything answers HTTP on the port (any status code - a 404 is
still a running server), dead = nothing accepts a connection before the timeout.
"""
import os

# Seconds the app gets to answer HTTP after compose up. 0 disables the gate.
TIMEOUT_S = int(os.environ.get("DEMO_HEALTHCHECK_TIMEOUT", "60") or "60")

READY = "APP_READY"


def probe_script(port: int, timeout_s: int) -> str:
    # busybox wget: exit 0 on 2xx, "server returned error" on any other HTTP
    # status - both mean a server is listening. Connection refused / timeouts
    # produce neither. 127.0.0.1, not localhost: busybox may resolve ::1 first
    # while docker-proxy only bound v4.
    return (
        f'deadline=$(( $(date +%s) + {int(timeout_s)} )); '
        f'while [ "$(date +%s)" -lt "$deadline" ]; do '
        f'out=$(wget -O /dev/null -T 5 "http://127.0.0.1:{int(port)}/" 2>&1) '
        f'&& {{ echo {READY}; exit 0; }}; '
        f'case "$out" in *"server returned error"*) echo {READY}; exit 0;; esac; '
        f'sleep 2; done; echo APP_NOT_READY; exit 1'
    )


def diagnostics_script(port: int, workdir: str) -> str:
    """Inner compose state + app log tail, shipped back in the 500 detail so the
    failure lands in chat / the build panel instead of a silent 502."""
    proj = "/project" if workdir in (".", "") else f"/project/{workdir}"
    return (
        f'export PORT={int(port)} && cd {proj} && '
        "files='-f compose.demo.yml'; "
        "[ -f compose.base.yml ] && files='-f compose.base.yml -f compose.demo.yml'; "
        'docker compose $files ps -a 2>&1; '
        'echo "--- app logs (tail) ---"; '
        'docker compose $files logs --tail 60 2>&1 | tail -c 2000'
    )
