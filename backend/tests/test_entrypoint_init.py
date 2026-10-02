"""Orphan reaping (prod 2026-10-01): the worker ran celery as PID 1, which reaps
only its own pool. git detaches a maintenance process after every fetch; each
one stayed a zombie, the knowledge sync fetches every five minutes, and after
two weeks the container sat at its task limit - every fork refused, git
knowledge sync dead, and a customer build parked on a push the repository had
never been asked about. Every long-lived mode now runs under an init.
"""
import re
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]


def test_every_long_lived_mode_execs_under_the_init():
    script = (BACKEND / "entrypoint.sh").read_text()
    init = re.search(r'^INIT="(tini\b[^"]*--)"$', script, re.M)
    assert init, "entrypoint.sh must define the init wrapper"
    servers = [ln.strip() for ln in script.splitlines()
               if re.match(r"\s*exec\b.*\b(uvicorn|celery)\b", ln)]
    assert len(servers) == 4  # api, api-dev, worker, beat
    assert all(ln.startswith("exec $INIT ") for ln in servers), servers


def test_the_image_ships_the_init():
    dockerfile = BACKEND / "Dockerfile"
    if dockerfile.exists():  # not copied into the image the suite can also run in
        assert re.search(r"apt-get install[^&]*\btini\b", dockerfile.read_text(), re.S)
