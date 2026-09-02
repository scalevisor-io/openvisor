"""The verify-workflow guidance the agent reads must never hand it a foreground
service command. Prod 2026-09-02: `_APP_VERIFY_WORKFLOW` said `docker compose up
--build` (no `-d`); the agent copied it, the attached server never returned, and
the run sat blocked for 36 minutes. Every other place the platform spells its
own boot detaches - this pins the last one to the same shape."""
import re

from app.services import speciality

_FOREGROUND_UP = re.compile(r"docker compose (?:-f \S+ )*up(?! -d)(?: --build)?")


def _overlay_texts():
    return {name: getattr(speciality, name)
            for name in dir(speciality)
            if name.startswith("_") and name.isupper() and isinstance(getattr(speciality, name), str)}


def test_no_overlay_hands_the_agent_a_foreground_compose_up():
    offenders = {n: m.group(0) for n, t in _overlay_texts().items()
                 for m in [_FOREGROUND_UP.search(t)] if m}
    assert not offenders, offenders


def test_verify_workflow_detaches_and_says_how_to_look():
    text = speciality._APP_VERIFY_WORKFLOW
    assert "docker compose up -d --build" in text
    assert "Detach every service" in text
    assert "logs --tail 60" in text
