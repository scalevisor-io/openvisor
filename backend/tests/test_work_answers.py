"""§work answers: the agent answering questions about its OWN work on a build
project - the `answer` chat intent, the guaranteed reply to an "@agent"/"@ai"
mention, answering inside a request's thread and while a build is in flight, the
evidence pack the answer is grounded in, and the spend guards. Mirrors
test_thread_classifier's style: committed throwaway org, tasks open their own
sessions.
"""
import pytest
from sqlalchemy import delete, select, update

from app.core.db import SyncSession
from app.models import (
    CreditTransaction, DevRun, Message, Organization, Project, ProjectRepo,
    Request, StatusChange,
)
from app.services import events, work_context
from app.workers import tasks


class FakeRedis:
    def __init__(self):
        self.store: dict = {}

    def set(self, key, val, nx=False, ex=None):
        if nx and key in self.store:
            return None
        self.store[key] = val
        return True

    def get(self, key):
        return self.store.get(key)

    def delete(self, key):
        self.store.pop(key, None)

    def incr(self, key):
        self.store[key] = int(self.store.get(key, 0)) + 1
        return self.store[key]

    def expire(self, key, ttl):
        return True

    def publish(self, channel, payload):
        return 0


@pytest.fixture
def fake_redis(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr(events, "get_sync_redis", lambda: fake)
    return fake


@pytest.fixture
def quiet(monkeypatch):
    ws: list = []
    monkeypatch.setattr(events, "publish_sync", lambda pid, ev: ws.append((pid, ev)))
    monkeypatch.setattr(tasks.celery, "send_task", lambda *a, **k: None)
    return ws


@pytest.fixture
def org_id():
    with SyncSession() as db:
        org = Organization(name="WorkAnswers Test Org", credit_balance=100.0)
        db.add(org)
        db.commit()
        oid = org.id
    try:
        yield oid
    finally:
        with SyncSession() as db:
            pids = db.execute(select(Project.id).where(Project.org_id == oid)).scalars().all()
            if pids:
                db.execute(update(Project).where(Project.id.in_(pids))
                           .values(dev_request_id=None))
                db.execute(delete(DevRun).where(DevRun.project_id.in_(pids)))
                db.execute(delete(ProjectRepo).where(ProjectRepo.project_id.in_(pids)))
                db.execute(delete(Message).where(Message.project_id.in_(pids)))
                db.execute(delete(StatusChange).where(StatusChange.project_id.in_(pids)))
                db.execute(delete(Request).where(Request.project_id.in_(pids)))
            db.execute(delete(CreditTransaction).where(CreditTransaction.org_id == oid))
            db.execute(delete(Project).where(Project.org_id == oid))
            db.execute(delete(Organization).where(Organization.id == oid))
            db.commit()


def _project(oid, tmp_path, **kw):
    kw.setdefault("name", "Bird game")
    kw.setdefault("description", "A physics game where birds knock down structures.")
    kw.setdefault("kind", "ai")
    kw.setdefault("status", "development")
    kw.setdefault("dev_run_state", "done")
    kw.setdefault("workspace_path", str(tmp_path))
    with SyncSession() as db:
        p = Project(org_id=oid, **kw)
        db.add(p)
        db.commit()
        return p.id


def _request(pid, **kw):
    kw.setdefault("type", "bug")
    kw.setdefault("handling", "ai")
    kw.setdefault("status", "done")
    kw.setdefault("title", "Add gravity physics")
    with SyncSession() as db:
        req = Request(project_id=pid, **kw)
        db.add(req)
        db.commit()
        return req.id


def _msg(pid, thread, body, author="customer"):
    with SyncSession() as db:
        m = Message(project_id=pid, thread=thread, author=author, body=body)
        db.add(m)
        db.commit()
        return m.id


def _classify_with(monkeypatch, verdict):
    calls: list = []

    def fake(db, project, context, body, **kw):
        calls.append(context)
        return verdict

    monkeypatch.setattr(tasks.pipeline, "classify_chat_intent", fake)
    monkeypatch.setattr(tasks, "_project_model_config", lambda db, project: ("", "k", "m"))
    return calls


def _capture_answers(monkeypatch):
    """Intercept the answer dispatch (the responder is exercised separately)."""
    sent: list = []
    monkeypatch.setattr(tasks.answer_work_question, "apply_async",
                        lambda args=None, **k: sent.append(args))
    return sent


# ------------------------------------------------------------------ mentions

@pytest.mark.parametrize("body,expected", [
    ("@agent can you explain?", True),
    ("@ai what did you do", True),
    ("Hey @Agent - status?", True),
    ("ping @AI please", True),
    ("mail me at bob@ai.com", False),
    ("see docs/@ai for details", False),
    ("the against argument", False),
    ("no mention here", False),
])
def test_mention_detection(body, expected):
    assert tasks.mentions_agent(body) is expected


def test_mention_is_answered_even_when_the_verdict_is_none(org_id, tmp_path, quiet,
                                                           monkeypatch):
    pid = _project(org_id, tmp_path, gitlab_project_id=1)
    mid = _msg(pid, "main", "@agent thanks for the update")
    _classify_with(monkeypatch, {"intent": "none", "request_type": None, "summary": None})
    sent = _capture_answers(monkeypatch)

    tasks.classify_chat_message(pid, mid)

    assert sent == [[pid, mid, "main"]]


def test_plain_none_without_a_mention_stays_silent(org_id, tmp_path, quiet, monkeypatch):
    pid = _project(org_id, tmp_path, gitlab_project_id=1)
    mid = _msg(pid, "main", "thanks!")
    _classify_with(monkeypatch, {"intent": "none", "request_type": None, "summary": None})
    sent = _capture_answers(monkeypatch)

    tasks.classify_chat_message(pid, mid)

    assert sent == []


def test_mention_survives_an_llm_outage(org_id, tmp_path, quiet, monkeypatch):
    """classify_chat_intent fails safe to none - a mention must still be answered."""
    pid = _project(org_id, tmp_path, gitlab_project_id=1)
    mid = _msg(pid, "main", "@ai is the demo up?")
    monkeypatch.setattr(tasks.pipeline, "classify_chat_intent",
                        lambda db, p, c, b, **kw: {"intent": "none", "request_type": None,
                                                   "summary": None})
    monkeypatch.setattr(tasks, "_project_model_config", lambda db, project: ("", "k", "m"))
    sent = _capture_answers(monkeypatch)

    tasks.classify_chat_message(pid, mid)

    assert sent == [[pid, mid, "main"]]


def test_mention_falls_back_to_answering_when_the_action_is_unavailable(
        org_id, tmp_path, quiet, monkeypatch):
    """A confirm verdict with no proposal pending used to be silence."""
    pid = _project(org_id, tmp_path, gitlab_project_id=1)
    mid = _msg(pid, "main", "@agent go ahead")
    _classify_with(monkeypatch, {"intent": "confirm", "request_type": None, "summary": None})
    sent = _capture_answers(monkeypatch)

    tasks.classify_chat_message(pid, mid)

    assert sent == [[pid, mid, "main"]]


# ------------------------------------------------------------------ routing

def test_answer_intent_routes_to_the_responder(org_id, tmp_path, quiet, monkeypatch):
    pid = _project(org_id, tmp_path, gitlab_project_id=1)
    mid = _msg(pid, "main", "can you explain what was done?")
    _classify_with(monkeypatch, {"intent": "answer", "request_type": None, "summary": None})
    sent = _capture_answers(monkeypatch)

    tasks.classify_chat_message(pid, mid)

    assert sent == [[pid, mid, "main"]]


def test_a_question_in_a_delivered_requests_thread_is_answered(org_id, tmp_path, quiet,
                                                               monkeypatch):
    """The reported gap: a done request's thread had no reachable verdict at all."""
    pid = _project(org_id, tmp_path, gitlab_project_id=1)
    rid = _request(pid, status="done")
    mid = _msg(pid, f"request:{rid}", "Can you explain what was done ?", author="admin")
    calls = _classify_with(monkeypatch, {"intent": "answer", "request_type": None,
                                         "summary": None})
    sent = _capture_answers(monkeypatch)

    tasks.classify_chat_message(pid, mid)

    assert len(calls) == 1  # the thread branch now reaches the classifier
    assert sent == [[pid, mid, f"request:{rid}"]]


def test_a_question_during_a_running_build_is_answered_but_starts_nothing(
        org_id, tmp_path, quiet, monkeypatch):
    pid = _project(org_id, tmp_path, gitlab_project_id=1, dev_run_state="running")
    mid = _msg(pid, "main", "what are you doing right now?")
    calls = _classify_with(monkeypatch, {"intent": "answer", "request_type": None,
                                         "summary": None})
    sent = _capture_answers(monkeypatch)
    dispatched: list = []
    monkeypatch.setattr(tasks.run_development, "apply_async",
                        lambda args=None, kwargs=None, **k: dispatched.append(args))

    tasks.classify_chat_message(pid, mid)

    assert "no - one is already in flight" in calls[0]
    assert sent == [[pid, mid, "main"]]
    assert dispatched == []


def test_a_new_request_during_a_running_build_is_filed_not_swallowed(
        org_id, tmp_path, quiet, monkeypatch):
    """Filing spends nothing and dispatches nothing, so a request stated
    mid-run becomes a proposed Request awaiting the go-ahead - it must never
    fall back to a work answer (prod regression: the ask was swallowed)."""
    pid = _project(org_id, tmp_path, gitlab_project_id=1, dev_run_state="running")
    mid = _msg(pid, "main", "add CSV export")
    _classify_with(monkeypatch, {"intent": "new_request", "request_type": "feature",
                                 "summary": "CSV export"})
    sent = _capture_answers(monkeypatch)
    dispatched: list = []
    monkeypatch.setattr(tasks.run_development, "apply_async",
                        lambda args=None, kwargs=None, **k: dispatched.append(args))
    monkeypatch.setattr(tasks.title_request, "apply_async",
                        lambda args=None, **k: None)

    tasks.classify_chat_message(pid, mid)

    assert sent == [] and dispatched == []
    with SyncSession() as db:
        req = db.execute(select(Request).where(Request.project_id == pid)
                         ).scalars().one()
        assert req.status == "proposed" and req.type == "feature"
        # §12 one-click confirm: the ack carries the meta the SPA/hub render as
        # ✓/✗ buttons wired to the deterministic start/cancel actions
        ack = db.execute(select(Message).where(
            Message.project_id == pid, Message.thread == "main",
            Message.author == "agent")).scalars().one()
        assert ack.meta == {"kind": "confirm_request", "request_id": req.id}


def test_a_dispatching_verdict_during_a_running_build_still_answers_instead(
        org_id, tmp_path, quiet, monkeypatch):
    """resume/confirm start a build NOW, so with no free slot a mention falls
    back to answering instead of dispatching."""
    pid = _project(org_id, tmp_path, gitlab_project_id=1, dev_run_state="running")
    mid = _msg(pid, "main", "@agent yes go ahead")
    _classify_with(monkeypatch, {"intent": "confirm", "request_type": None,
                                 "summary": None})
    sent = _capture_answers(monkeypatch)
    dispatched: list = []
    monkeypatch.setattr(tasks.handle_request, "apply_async",
                        lambda args=None, **k: dispatched.append(args))

    tasks.classify_chat_message(pid, mid)

    assert sent == [[pid, mid, "main"]] and dispatched == []


# ------------------------------------------------------------------ evidence pack

def test_work_context_carries_the_facts_an_answer_needs(org_id, tmp_path):
    pid = _project(org_id, tmp_path, dev_branch="fix/add-gravity-physics",
                   dev_pr_url="https://gitlab.example/mr/10", tokens_consumed=5367175,
                   cost_credits=27.7877)
    rid = _request(pid, status="done")
    with SyncSession() as db:
        db.get(Request, rid).work_summary = "Added a gravity integrator to the step loop."
        db.get(Request, rid).pr_urls = [{"number": 10, "url": "https://gitlab.example/mr/10",
                                         "provider": "gitlab"}]
        db.commit()
    with SyncSession() as db:
        db.add(ProjectRepo(project_id=pid, ssh_uri="git@github.com:acme/game.git",
                           provider="github", is_push_target=True))
        db.add(ProjectRepo(project_id=pid, ssh_uri="git@github.com:acme/assets.git",
                           provider="github", role="secondary"))
        db.commit()
    with SyncSession() as db:
        project = db.get(Project, pid)
        req = db.get(Request, rid)
        ctx = work_context.build_context(
            db, project, req,
            git_facts={"commits": ["Add gravity to structures"],
                       "files": [{"path": "src/physics.js", "status": "modified",
                                  "added": 40, "removed": 3}]},
            feed_events=[{"kind": "phase", "title": "Boot check passed"}],
            memory_block="- GITHUB_TOKEN (secret, value withheld)")

    assert "Add gravity physics" in ctx  # the request
    assert "Added a gravity integrator" in ctx  # its published summary
    assert "src/physics.js" in ctx and "Add gravity to structures" in ctx  # git facts
    assert "Boot check passed" in ctx  # build feed
    assert "fix/add-gravity-physics" in ctx and "27.7877" in ctx
    assert "value withheld" in ctx  # secrets never carry their value
    # the repositories panel: the working repo is named as such, the rest as context
    assert "CONNECTED REPOSITORIES" in ctx
    assert "git@github.com:acme/game.git (github) - working repo" in ctx
    assert "git@github.com:acme/assets.git (github) - read-only context" in ctx


def test_work_context_names_the_platform_repo_when_none_is_connected(org_id, tmp_path):
    """No connected rows: the platform GitLab repo is the implicit working repo
    (and a project with neither simply has no repositories block)."""
    pid = _project(org_id, tmp_path, gitlab_ssh_url="git@gitlab.internal:g/p.git")
    with SyncSession() as db:
        ctx = work_context.build_context(db, db.get(Project, pid), None)
    assert "platform-managed GitLab repository - working repo" in ctx

    bare = _project(org_id, tmp_path)
    with SyncSession() as db:
        ctx = work_context.build_context(db, db.get(Project, bare), None)
    assert "CONNECTED REPOSITORIES" not in ctx


def test_work_context_never_leaks_a_secret_memory_value(org_id, tmp_path):
    pid = _project(org_id, tmp_path)
    with SyncSession() as db:
        ctx = work_context.build_context(db, db.get(Project, pid), None,
                                         memory_block="- API_KEY (secret, value withheld)")
    assert "value withheld" in ctx


# ------------------------------------------------------------------ the responder

def _answer_setup(monkeypatch, answer="I added a gravity integrator."):
    monkeypatch.setattr(tasks, "_project_model_config", lambda db, p: ("", "k", "gpt-test"))
    monkeypatch.setattr(tasks.pipeline, "load_prompt", lambda name: f"PROMPT:{name}")
    monkeypatch.setattr("app.services.pricing.is_priced", lambda m: True)
    monkeypatch.setattr(tasks, "_work_git_facts", lambda db, p, r: None)
    monkeypatch.setattr(tasks, "_work_feed_events", lambda db, p: [])
    billed: list = []
    monkeypatch.setattr(tasks, "_bill_chat_usages",
                        lambda db, p, usages, detail: billed.append((usages, detail)))
    sent: list = []

    def fake_chat(messages, **kw):
        sent.append(messages)
        return answer, {"model": "gpt-test", "input_tokens": 100, "output_tokens": 20}

    monkeypatch.setattr(tasks.llm, "chat", fake_chat)
    return sent, billed


def test_responder_posts_a_billed_answer_in_the_asking_thread(org_id, tmp_path, quiet,
                                                              fake_redis, monkeypatch):
    pid = _project(org_id, tmp_path)
    rid = _request(pid, status="done")
    thread = f"request:{rid}"
    mid = _msg(pid, thread, "Can you explain what was done ?", author="admin")
    sent, billed = _answer_setup(monkeypatch)

    tasks.answer_work_question(pid, mid, thread)

    with SyncSession() as db:
        posted = db.execute(select(Message).where(
            Message.project_id == pid, Message.author == "agent",
            Message.thread == thread)).scalars().all()
    assert len(posted) == 1
    assert posted[0].body == "I added a gravity integrator."
    assert posted[0].meta == {"answers": mid}
    assert billed and billed[0][1] == "Work answer"
    # the prompt carried the work context, and the thread history was replayed
    assert "PROMPT:work_answer.md" in sent[0][0]["content"]
    assert "Add gravity physics" in sent[0][0]["content"]
    assert sent[0][-1]["content"].endswith("Can you explain what was done ?")


def test_responder_answers_each_message_once(org_id, tmp_path, quiet, fake_redis,
                                             monkeypatch):
    pid = _project(org_id, tmp_path)
    mid = _msg(pid, "main", "what did you build?")
    _answer_setup(monkeypatch)

    tasks.answer_work_question(pid, mid, "main")
    tasks.answer_work_question(pid, mid, "main")

    with SyncSession() as db:
        posted = db.execute(select(Message).where(
            Message.project_id == pid, Message.author == "agent")).scalars().all()
    assert len(posted) == 1


def test_responder_refuses_on_an_empty_wallet(org_id, tmp_path, quiet, fake_redis,
                                              monkeypatch):
    with SyncSession() as db:
        db.get(Organization, org_id).credit_balance = 0.0
        db.commit()
    pid = _project(org_id, tmp_path)
    mid = _msg(pid, "main", "what did you build?")
    sent, billed = _answer_setup(monkeypatch)

    tasks.answer_work_question(pid, mid, "main")

    assert sent == [] and billed == []
    with SyncSession() as db:
        body = db.execute(select(Message.body).where(
            Message.project_id == pid, Message.author == "agent")).scalars().first()
    assert "credit balance is empty" in body


def test_responder_caps_a_runaway_org(org_id, tmp_path, quiet, fake_redis, monkeypatch):
    monkeypatch.setattr(tasks.settings, "work_answer_rate_per_10min", 1)
    pid = _project(org_id, tmp_path)
    sent, _ = _answer_setup(monkeypatch)

    tasks.answer_work_question(pid, _msg(pid, "main", "q1"), "main")
    tasks.answer_work_question(pid, _msg(pid, "main", "q2"), "main")
    tasks.answer_work_question(pid, _msg(pid, "main", "q3"), "main")

    assert len(sent) == 1  # one answer, then the cap
    with SyncSession() as db:
        bodies = db.execute(select(Message.body).where(
            Message.project_id == pid, Message.author == "agent")).scalars().all()
    assert sum("faster than I can answer" in b for b in bodies) == 1


def test_kill_switch_silences_the_dispatch(org_id, tmp_path, quiet, monkeypatch):
    monkeypatch.setattr(tasks.settings, "work_answer_enabled", False)
    pid = _project(org_id, tmp_path, gitlab_project_id=1)
    mid = _msg(pid, "main", "@agent explain please")
    _classify_with(monkeypatch, {"intent": "answer", "request_type": None, "summary": None})
    sent = _capture_answers(monkeypatch)

    tasks.classify_chat_message(pid, mid)

    assert sent == []
