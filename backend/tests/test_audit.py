"""§audit action tracing + the demo lifecycle lock: the audit line carries a
hashed actor and the route TEMPLATE only (no bodies, prompts, or raw emails),
and concurrent demo actions collapse to one via the per-project redis lock.
"""
import logging
from types import SimpleNamespace

from app.core import audit
from app.workers import tasks


def test_actor_hash_is_stable_and_opaque():
    h = audit.actor_hash("Jean@Example.ORG ")
    assert h == audit.actor_hash("jean@example.org")  # normalized
    assert len(h) == 16
    assert "jean" not in h and "@" not in h
    assert audit.actor_hash(None) == "anonymous"
    assert audit.actor_hash("") == "anonymous"


def _request(method="POST", route_path="/api/projects/{project_id}/actions"):
    return SimpleNamespace(
        method=method,
        scope={"route": SimpleNamespace(path=route_path)},
        url=SimpleNamespace(path="/api/projects/1234-raw-id/actions?secret=1"),
    )


def test_log_action_logs_mutations_with_template_only(caplog):
    with caplog.at_level(logging.INFO, logger="audit"):
        audit.log_action(_request(), "user@example.org")
    assert len(caplog.records) == 1
    line = caplog.records[0].getMessage()
    assert "{project_id}" in line          # route template, not the raw path
    assert "1234-raw-id" not in line
    assert "secret" not in line
    assert "user@example.org" not in line  # only the hash
    assert audit.actor_hash("user@example.org") in line


def test_log_action_skips_reads(caplog):
    with caplog.at_level(logging.INFO, logger="audit"):
        audit.log_action(_request(method="GET"), "user@example.org")
    assert not caplog.records


def test_demo_lock_serializes_per_project():
    pid = "test-demo-lock-project"
    tasks._demo_unlock(pid)  # clean slate
    try:
        assert tasks._demo_lock(pid, "start")
        assert not tasks._demo_lock(pid, "start")   # duplicate no-ops
        assert not tasks._demo_lock(pid, "stop")    # stop serializes with start
        assert tasks._demo_lock("other-project", "start")  # per-project, not global
    finally:
        tasks._demo_unlock(pid)
        tasks._demo_unlock("other-project")
    assert tasks._demo_lock(pid, "start")           # released -> reacquirable
    tasks._demo_unlock(pid)
