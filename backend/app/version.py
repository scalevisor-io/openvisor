"""Spoke application version, read from the VERSION file baked into the image
(bump backend/app/VERSION on release). Reported to the hub via /api/hub/info
and the heartbeat so the fleet view can show what each spoke runs."""
from functools import lru_cache
from pathlib import Path

_VERSION_FILE = Path(__file__).resolve().parent / "VERSION"


@lru_cache
def get_version() -> str:
    try:
        return _VERSION_FILE.read_text().strip() or "unknown"
    except OSError:
        return "unknown"
