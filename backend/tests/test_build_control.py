"""§14 build control: the stop-development marker/park flow and the §12
classifier's confirm ack. DB-backed in the test_devrun_reaper style (committed
throwaway org, tasks open their own sessions), redis via the running stack.
"""
import uuid

import pytest
from sqlalchemy import delete, select

from app.core.db import SyncSession
from app.models import (
    CreditTransaction, Message, Organization, Project, Request, StatusChange,
)
from app.services import events
from app.workers import tasks


@pytest.fixture
def quiet(monkeypatch):
    """Capture WS events and detach the broker so _post_message/transition_sync
    run without external services."""
    ws: list = []
    monkeypatch.setattr(events, "publish_sync", lambda pid, ev: ws.append((pid, ev)))
    monkeypatch.setattr(tasks.celery, "send_task", lambda *a, **k: None)
    return ws


@pytest.fixture
def org_id():
    with SyncSession() as db:
        org = Organization(name="BuildControl Test Org", credit_balance=100.0)
        db.add(org)
        db.commit()
        oid = org.id
    try:
        yield oid
    finally:
        with SyncSession() as db:
            pids = db.execute(select(Project.id).where(
                Project.org_id == oid)).scalars().all()
            if pids:
                db.execute(delete(Message).where(Message.project_id.in_(pids)))
                db.execute(delete(StatusChange).where(StatusChange.project_id.in_(pids)))
                db.execute(delete(Request).where(Request.project_id.in_(pids)))
            db.execute(delete(CreditTransaction).where(CreditTransaction.org_id == oid))
            db.execute(delete(Project).where(Project.org_id == oid))
            db.execute(delete(Organization).where(Organization.id == oid))
            db.commit()


def _commit_project(oid, tmp_path, **kw):
    kw.setdefault("name", "P")
    kw.setdefault("description", "d")
    kw.setdefault("kind", "ai")
    kw.setdefault("status", "development")
    kw.setdefault("dev_run_state", "running")
    kw.setdefault("workspace_path", str(tmp_path))
    with SyncSession() as db:
        p = Project(org_id=oid, **kw)
        db.add(p)
        db.commit()
        return p.id


# ---- stop marker semantics ----

def test_save_run_publishes_dev_state_changes(org_id, tmp_path, quiet):
    # Every dev_run_state CHANGE pushes a WS "dev" event (the Development panel
    # learns about a run it isn't polling for); same-state updates stay silent.
    pid = _commit_project(org_id, tmp_path, dev_run_state="done")
    with SyncSession() as db:
        p = db.get(Project, pid)
        tasks._save_run(p, "running")
        tasks._save_run(p, "running", logs="progress")  # no state change -> no event
        tasks._save_run(p, "failed", error="boom")
    dev = [ev for _, ev in quiet if ev.get("type") == "dev"]
    assert [e["dev_run_state"] for e in dev] == ["running", "failed"]
    assert all(e["project_id"] == pid for e in dev)


def test_stop_marker_is_consume_once_and_clearable():
    pid = str(uuid.uuid4())
    assert tasks._stop_requested(pid) is False
    events.get_sync_redis().setex(tasks._stop_key(pid), 60, "1")
    assert tasks._stop_requested(pid) is True
    assert tasks._stop_requested(pid) is False  # consumed by the first check
    events.get_sync_redis().setex(tasks._stop_key(pid), 60, "1")
    tasks._clear_stop(pid)
    assert tasks._stop_requested(pid) is False


def test_stop_development_marks_and_kills_running_run(org_id, tmp_path, quiet, monkeypatch):
    pid = _commit_project(org_id, tmp_path, dev_run_state="running")
    killed: list = []
    monkeypatch.setattr(tasks.deployer_client, "stop_dev_job",
                        lambda project_id, run_name="": killed.append(project_id) or {"ok": True})
    tasks.stop_development(pid)
    assert killed == [pid]
    assert events.get_sync_redis().get(tasks._stop_key(pid))
    tasks._clear_stop(pid)


def test_stop_development_noops_when_no_run_in_flight(org_id, tmp_path, quiet, monkeypatch):
    pid = _commit_project(org_id, tmp_path, dev_run_state="failed")
    monkeypatch.setattr(tasks.deployer_client, "stop_dev_job",
                        lambda project_id, run_name="": pytest.fail("must not kill anything"))
    tasks.stop_development(pid)
    assert not events.get_sync_redis().get(tasks._stop_key(pid))


def test_park_stopped_is_failed_and_resumable(org_id, tmp_path, quiet):
    from app.api.serializers import dev_resume_capability
    pid = _commit_project(org_id, tmp_path, dev_run_state="running",
                          gitlab_project_id=1)
    with SyncSession() as db:
        p = db.get(Project, pid)
        tasks._park_stopped(db, p, logs="tail")
        db.commit()
        db.refresh(p)
        assert p.dev_run_state == "failed"
        assert p.dev_run_error == "Stopped at your request"
        assert p.status == "awaiting_customer"
        enabled, _ = dev_resume_capability(p)
        assert enabled
        body = db.execute(select(Message.body).where(
            Message.project_id == pid)).scalars().first()
        assert "stopped at your request" in body.lower()


# ---- classifier confirm ack ----

def test_confirm_acks_in_main_with_request_link(org_id, tmp_path, quiet, monkeypatch):
    pid = _commit_project(org_id, tmp_path, dev_run_state="done",
                          gitlab_project_id=1, demo_deployed_once=True)
    with SyncSession() as db:
        req = Request(project_id=pid, type="feature", handling="ai",
                      status="proposed", title="CSV export")
        msg = Message(project_id=pid, thread="main", author="customer", body="go on")
        db.add_all([req, msg])
        db.commit()
        rid, mid = req.id, msg.id

    monkeypatch.setattr(tasks.pipeline, "classify_chat_intent",
                        lambda *a, **k: {"intent": "confirm", "request_type": None,
                                         "summary": None})
    monkeypatch.setattr(tasks, "_project_model_config",
                        lambda db, project: ("", "k", "m"))
    dispatched: list = []
    monkeypatch.setattr(tasks.handle_request, "apply_async",
                        lambda args=None, **k: dispatched.append(args))

    tasks.classify_chat_message(pid, mid)

    with SyncSession() as db:
        assert db.get(Request, rid).status == "open"
        ack = db.execute(select(Message.body).where(
            Message.project_id == pid, Message.author == "agent")).scalars().first()
        assert ack.startswith('On it - starting "CSV export"')
        assert f"/projects/{pid}/requests/{rid}" in ack
    assert dispatched == [[pid, rid, mid]]


# ---- classifier clarify (§12 clarifying question) ----

_CLARIFY_VERDICT = {
    "intent": "clarify", "request_type": None, "summary": None,
    "question": "Which dashboard should get the export?",
    "options": [{"label": "Admin dashboard", "description": "internal metrics"},
                {"label": "Customer dashboard", "description": None}],
}


def _classify_with(monkeypatch, verdict):
    monkeypatch.setattr(tasks.pipeline, "classify_chat_intent", lambda *a, **k: verdict)
    monkeypatch.setattr(tasks, "_project_model_config", lambda db, project: ("", "k", "m"))


def test_clarify_posts_question_message_with_meta(org_id, tmp_path, quiet, monkeypatch):
    pid = _commit_project(org_id, tmp_path, dev_run_state="done",
                          gitlab_project_id=1, demo_deployed_once=True)
    with SyncSession() as db:
        msg = Message(project_id=pid, thread="main", author="customer",
                      body="add export to the dashboard")
        db.add(msg)
        db.commit()
        mid = msg.id

    _classify_with(monkeypatch, dict(_CLARIFY_VERDICT))
    tasks.classify_chat_message(pid, mid)

    with SyncSession() as db:
        q = db.execute(select(Message).where(
            Message.project_id == pid, Message.author == "agent")).scalars().one()
        assert q.body == _CLARIFY_VERDICT["question"]
        assert q.meta["kind"] == "question"
        assert q.meta["question"] == _CLARIFY_VERDICT["question"]
        assert [o["label"] for o in q.meta["options"]] == [
            "Admin dashboard", "Customer dashboard"]
        assert q.meta["allow_free_text"] is True
    # the question also reaches the WS stream with its meta
    assert any(((ev.get("message") or {}).get("meta") or {}).get("kind") == "question"
               for _, ev in quiet if ev.get("type") == "message")


def test_clarify_never_asks_twice_in_a_row(org_id, tmp_path, quiet, monkeypatch):
    pid = _commit_project(org_id, tmp_path, dev_run_state="done",
                          gitlab_project_id=1, demo_deployed_once=True)
    with SyncSession() as db:
        prior = Message(project_id=pid, thread="main", author="agent",
                        body="Which dashboard should get the export?",
                        meta={"kind": "question",
                              "question": "Which dashboard should get the export?",
                              "options": [{"label": "A"}, {"label": "B"}],
                              "allow_free_text": True})
        reply = Message(project_id=pid, thread="main", author="customer",
                        body="the shiny one")
        db.add_all([prior, reply])
        db.commit()
        mid = reply.id

    _classify_with(monkeypatch, dict(_CLARIFY_VERDICT))
    tasks.classify_chat_message(pid, mid)

    with SyncSession() as db:
        agent_msgs = db.execute(select(Message).where(
            Message.project_id == pid, Message.author == "agent")).scalars().all()
        # only the pre-existing question - no second interrogation
        assert len(agent_msgs) == 1


# ---- classifier activity feedback (§12 reading indicator) ----

_NONE_VERDICT = {"intent": "none", "request_type": None, "summary": None,
                 "question": None, "options": []}


def _main_msg(pid, author="customer", body="hello there"):
    with SyncSession() as db:
        msg = Message(project_id=pid, thread="main", author=author, body=body)
        db.add(msg)
        db.commit()
        return msg.id


def test_classifier_brackets_with_agent_activity_events(org_id, tmp_path, quiet, monkeypatch):
    """The SPA's reading indicator rides two transient WS events: 'reading' once
    the guards pass, 'idle' (carrying the verdict) when classification ends -
    even a silent 'none' leaves visible feedback."""
    pid = _commit_project(org_id, tmp_path, dev_run_state="done",
                          gitlab_project_id=1, demo_deployed_once=True)
    mid = _main_msg(pid)
    _classify_with(monkeypatch, dict(_NONE_VERDICT))

    tasks.classify_chat_message(pid, mid)

    acts = [ev for _, ev in quiet if ev.get("type") == "agent_activity"]
    assert [a["state"] for a in acts] == ["reading", "idle"]
    assert acts[1]["intent"] == "none"
    assert all(a["message_id"] == mid for a in acts)


def test_classifier_idle_event_survives_a_crash(org_id, tmp_path, quiet, monkeypatch):
    """A classification that dies mid-flight must still emit 'idle' - otherwise
    the chat indicator spins until the client-side timeout."""
    pid = _commit_project(org_id, tmp_path, dev_run_state="done",
                          gitlab_project_id=1, demo_deployed_once=True)
    mid = _main_msg(pid)
    monkeypatch.setattr(tasks, "_project_model_config", lambda db, project: ("", "k", "m"))

    def boom(*a, **k):
        raise RuntimeError("provider exploded")

    monkeypatch.setattr(tasks.pipeline, "classify_chat_intent", boom)

    with pytest.raises(RuntimeError):
        tasks.classify_chat_message(pid, mid)

    acts = [ev for _, ev in quiet if ev.get("type") == "agent_activity"]
    assert [a["state"] for a in acts] == ["reading", "idle"]
    assert acts[1]["intent"] is None


def test_classifier_guard_block_emits_no_activity(org_id, tmp_path, quiet, monkeypatch):
    """A message the guards drop (here: agent-authored) never classified - the
    UI must not flash an indicator for it."""
    pid = _commit_project(org_id, tmp_path, dev_run_state="done",
                          gitlab_project_id=1, demo_deployed_once=True)
    mid = _main_msg(pid, author="agent", body="On it - starting the build.")
    _classify_with(monkeypatch, dict(_NONE_VERDICT))

    tasks.classify_chat_message(pid, mid)

    assert not [ev for _, ev in quiet if ev.get("type") == "agent_activity"]


# ---- §14.x stop-on-ownerless (two-phase reaper park) ----

def _set_stop_marker(pid, age_s):
    r = events.get_sync_redis()
    r.setex(tasks._stop_key(pid), tasks.STOP_MARKER_TTL_S - age_s, "1")


def test_ownerless_stop_parks_in_two_ticks(org_id, tmp_path, quiet, monkeypatch):
    pid = _commit_project(org_id, tmp_path, dev_run_state="running",
                          gitlab_project_id=1)
    killed: list = []
    monkeypatch.setattr(tasks.deployer_client, "stop_dev_job",
                        lambda project_id: killed.append(project_id) or {"ok": True})
    _set_stop_marker(pid, age_s=tasks.STOP_ORPHAN_AFTER_S + 30)

    # phase 1: re-kill + confirm marker armed, nothing parked yet
    tasks._reap_ownerless_stops()
    with SyncSession() as db:
        assert db.get(Project, pid).dev_run_state == "running"
    assert killed == [pid]
    assert events.get_sync_redis().get(tasks._stop_reap_key(pid))

    # phase 2: marker still unconsumed -> parked as the requested stop
    tasks._reap_ownerless_stops()
    with SyncSession() as db:
        p = db.get(Project, pid)
        assert p.dev_run_state == "failed"
        assert p.dev_run_error == "Stopped at your request"
        assert p.status == "awaiting_customer"
    r = events.get_sync_redis()
    assert not r.get(tasks._stop_key(pid)) and not r.get(tasks._stop_reap_key(pid))


def test_ownerless_stop_spares_live_runs(org_id, tmp_path, quiet, monkeypatch):
    pid = _commit_project(org_id, tmp_path, dev_run_state="running",
                          gitlab_project_id=1)
    monkeypatch.setattr(tasks.deployer_client, "stop_dev_job",
                        lambda project_id, run_name="": {"ok": True})
    # a FRESH stop marker (live loop hasn't had its window yet) is left alone
    _set_stop_marker(pid, age_s=5)
    tasks._reap_ownerless_stops()
    with SyncSession() as db:
        assert db.get(Project, pid).dev_run_state == "running"
    assert not events.get_sync_redis().get(tasks._stop_reap_key(pid))

    # phase 1 armed, but a live loop consumes the marker before phase 2 -> spared
    _set_stop_marker(pid, age_s=tasks.STOP_ORPHAN_AFTER_S + 30)
    tasks._reap_ownerless_stops()
    assert tasks._stop_requested(pid)  # consume, as a live checkpoint would
    tasks._reap_ownerless_stops()
    with SyncSession() as db:
        assert db.get(Project, pid).dev_run_state == "running"
    events.get_sync_redis().delete(tasks._stop_reap_key(pid))
