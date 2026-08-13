"""§repo binding: runs are pinned to the repo they build into. acquire_slot
stamps DevRun.repo_id (chain inherits verbatim -> request intent -> project
default), _dev_target honors a bound run's pin (with chain-PR-URL recovery for
pre-binding rows), and branch links derive from the run's own repo - so a
push-target switch never retargets a chain, the merge sweep, or history
(prod regression: an Infrastructure revise dispatched against Storefront).
"""
import pytest
from sqlalchemy import delete, select, update

from app.core.db import SyncSession
from app.models import (
    CreditTransaction, DevRun, Message, Organization, Project, ProjectRepo,
    Request, StatusChange,
)
from app.services import dev_concurrency, events
from app.workers import tasks


@pytest.fixture
def quiet(monkeypatch):
    monkeypatch.setattr(events, "publish_sync", lambda *a, **k: None)
    monkeypatch.setattr(tasks.celery, "send_task", lambda *a, **k: None)


@pytest.fixture
def org_id():
    with SyncSession() as db:
        org = Organization(name="RepoBind Test Org", credit_balance=100.0)
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
                db.execute(delete(Message).where(Message.project_id.in_(pids)))
                db.execute(delete(StatusChange).where(StatusChange.project_id.in_(pids)))
                db.execute(delete(Request).where(Request.project_id.in_(pids)))
                db.execute(delete(ProjectRepo).where(ProjectRepo.project_id.in_(pids)))
            db.execute(delete(CreditTransaction).where(CreditTransaction.org_id == oid))
            db.execute(delete(Project).where(Project.org_id == oid))
            db.execute(delete(Organization).where(Organization.id == oid))
            db.commit()


def _setup(db, oid):
    """A project with two connected repos: infra (push target) + app."""
    p = Project(org_id=oid, name="P", description="d", kind="ai",
                status="development", dev_run_state="idle")
    db.add(p)
    db.flush()
    infra = ProjectRepo(project_id=p.id, provider="github", is_push_target=True,
                        ssh_uri="git@github.com:acme/acme-infrastructure.git")
    app = ProjectRepo(project_id=p.id, provider="github", role="secondary",
                      ssh_uri="git@github.com:acme/Storefront.git")
    db.add_all([infra, app])
    req = Request(project_id=p.id, type="bug", handling="ai",
                  status="in_progress", title="Bump Tracelib")
    db.add(req)
    db.flush()
    return p, infra, app, req


def test_acquire_stamps_default_intent_and_chain(org_id, quiet):
    with SyncSession() as db:
        p, infra, app, req = _setup(db, org_id)
        db.commit()
        # fresh run without request intent -> the default push target
        run = dev_concurrency.acquire_slot(db, p, req)
        assert run.repo_id == infra.id
        # request intent wins over the default
        run.state = "failed"
        req2 = Request(project_id=p.id, type="feature", handling="ai",
                       status="open", title="App work", repo_id=app.id)
        db.add(req2)
        db.flush()
        run2 = dev_concurrency.acquire_slot(db, p, req2)
        assert run2.repo_id == app.id
        # a chain inherits its predecessor's pin VERBATIM - even after the
        # push target flips to the other repo
        run2.state = "failed"
        infra.is_push_target = False
        app.is_push_target = True
        db.flush()
        chained = dev_concurrency.acquire_slot(db, p, req, predecessor=run)
        assert chained.repo_id == infra.id
        db.rollback()


def test_dev_target_honors_the_bound_runs_pin(org_id, quiet):
    with SyncSession() as db:
        p, infra, app, req = _setup(db, org_id)
        db.flush()
        run = DevRun(project_id=p.id, request_id=req.id, state="awaiting_merge",
                     repo_id=infra.id, branch="feat/upgrade-tracelib-v4")
        db.add(run)
        db.flush()
        # the panel's radio moved to the app repo...
        infra.is_push_target = False
        app.is_push_target = True
        db.flush()
        # ...unbound resolution follows it
        dev_concurrency.bind_run(p, None)
        assert tasks._dev_target(db, p)["repo"] == "Storefront"
        # ...but the bound run stays on ITS repo
        dev_concurrency.bind_run(p, run)
        target = tasks._dev_target(db, p)
        assert target["repo"] == "acme-infrastructure"
        assert target["repo_id"] == infra.id
        db.rollback()


def test_dev_target_recovers_a_prebinding_chain_from_its_pr_url(org_id, quiet):
    """Legacy rows (repo_id null) recover their repo from the chain's stored
    PR URL - their own, else their predecessor's."""
    with SyncSession() as db:
        p, infra, app, req = _setup(db, org_id)
        db.flush()
        old = DevRun(project_id=p.id, request_id=req.id, state="superseded",
                     pr_url="https://github.com/acme/acme-infrastructure/pull/66",
                     pr_number=66)
        db.add(old)
        db.flush()
        revise = DevRun(project_id=p.id, request_id=req.id, state="running",
                        predecessor_id=old.id)
        db.add(revise)
        db.flush()
        infra.is_push_target = False
        app.is_push_target = True
        db.flush()
        dev_concurrency.bind_run(p, revise)
        assert tasks._dev_target(db, p)["repo"] == "acme-infrastructure"
        # no pin and no pointer anywhere -> live resolution (the new target)
        bare = DevRun(project_id=p.id, state="running")
        db.add(bare)
        db.flush()
        dev_concurrency.bind_run(p, bare)
        assert tasks._dev_target(db, p)["repo"] == "Storefront"
        db.rollback()


def test_run_branch_url_prefers_the_repo_pin(org_id):
    """A PR-less run's branch link follows its stamped repo, not the current
    push target."""
    from app.api.serializers import dev_run_out
    with SyncSession() as db:
        p, infra, app, req = _setup(db, org_id)
        db.flush()
        run = DevRun(project_id=p.id, request_id=req.id, state="awaiting_merge",
                     repo_id=infra.id, branch="feat/upgrade-tracelib-v4")
        db.add(run)
        db.flush()
        infra.is_push_target = False
        app.is_push_target = True
        db.flush()
        db.refresh(p, ["repos"])
        out = dev_run_out(run, p, legacy_feed_owner=None)
        assert out["branch_url"] == ("https://github.com/acme/"
                                     "acme-infrastructure/tree/feat/upgrade-tracelib-v4")
        assert out["repo_id"] == infra.id
        db.rollback()


# --------------------------------------------------------------- part B: intent

def test_repo_from_message_matches_a_single_connected_repo(org_id, quiet, monkeypatch):
    from app.agents import pipeline

    monkeypatch.setattr(pipeline, "infer_request_repo", lambda *a, **k: None)
    with SyncSession() as db:
        p, infra, app, _ = _setup(db, org_id)
        db.flush()
        assert tasks._repo_from_message(
            db, p, "Check https://github.com/acme/Storefront/pull/2453 please"
        ) == app.id
        # several repos referenced -> ambiguous -> no binding
        assert tasks._repo_from_message(
            db, p, "https://github.com/acme/Storefront and "
                   "https://github.com/acme/acme-infrastructure") is None
        # no URL, no name, LLM declines -> no binding
        assert tasks._repo_from_message(db, p, "add CSV export") is None
        db.rollback()


def test_repo_from_message_matches_a_repo_by_name(org_id, quiet, monkeypatch):
    """Prod regression: the request NAMED acme-infrastructure with no URL
    and bound nothing - a unique word-bounded name mention now binds."""
    from app.agents import pipeline

    monkeypatch.setattr(pipeline, "infer_request_repo", lambda *a, **k: None)
    with SyncSession() as db:
        p, infra, app, _ = _setup(db, org_id)
        db.flush()
        assert tasks._repo_from_message(
            db, p, "The fix belongs in acme-infrastructure (proxy stack)."
        ) == infra.id
        assert tasks._repo_from_message(
            db, p, "see acme/Storefront for the client code") == app.id
        # a name inside a LONGER name never false-binds (word boundary)
        assert tasks._repo_from_message(db, p, "storefront-v2 needs a fix") is None
        # both names -> ambiguous -> no binding
        assert tasks._repo_from_message(
            db, p, "Storefront reads what acme-infrastructure deploys") is None
        db.rollback()


def test_repo_from_message_llm_fallback_and_gate(org_id, quiet, monkeypatch):
    from app.agents import pipeline

    with SyncSession() as db:
        p, infra, app, _ = _setup(db, org_id)
        db.flush()
        calls = {"n": 0}

        def fake_infer(db_, project, repos, text):
            calls["n"] += 1
            assert {r["name"] for r in repos} == {
                "acme/acme-infrastructure", "acme/Storefront"}
            return app.id
        monkeypatch.setattr(pipeline, "infer_request_repo", fake_infer)
        assert tasks._repo_from_message(
            db, p, "upgrade the map rendering pipeline") == app.id
        assert calls["n"] == 1
        # the gate disables ONLY the LLM fallback
        monkeypatch.setattr(tasks.settings, "request_repo_infer_enabled", False)
        assert tasks._repo_from_message(
            db, p, "upgrade the map rendering pipeline") is None
        assert calls["n"] == 1
        db.rollback()


def test_infer_request_repo_validates_the_answer(monkeypatch):
    from app.agents import pipeline

    repos = [{"id": "r1", "name": "acme/app", "role": "primary", "push_target": True},
             {"id": "r2", "name": "acme/infra", "role": "secondary", "push_target": False}]
    monkeypatch.setattr(pipeline, "record_usage", lambda *a, **k: 0)

    class _P:
        id = "p1"
    monkeypatch.setattr(pipeline, "chat_json",
                        lambda *a, **k: ({"repo": "r2"}, {"model": "m", "input_tokens": 1,
                                                          "output_tokens": 1}))
    assert pipeline.infer_request_repo(None, _P(), repos, "fix the deploy") == "r2"
    monkeypatch.setattr(pipeline, "chat_json",
                        lambda *a, **k: ({"repo": "invented"}, {"model": "m", "input_tokens": 1,
                                                                "output_tokens": 1}))
    assert pipeline.infer_request_repo(None, _P(), repos, "fix the deploy") is None
    monkeypatch.setattr(pipeline, "chat_json",
                        lambda *a, **k: ({"repo": None}, {"model": "m", "input_tokens": 1,
                                                          "output_tokens": 1}))
    assert pipeline.infer_request_repo(None, _P(), repos, "anything") is None

    def boom(*a, **k):
        raise RuntimeError("llm down")
    monkeypatch.setattr(pipeline, "chat_json", boom)
    assert pipeline.infer_request_repo(None, _P(), repos, "anything") is None


def test_handle_request_backfills_the_repo_pin_at_dispatch(org_id, quiet, monkeypatch):
    with SyncSession() as db:
        p, infra, app, req = _setup(db, org_id)
        db.commit()
        pid, rid, iid = p.id, req.id, infra.id
    monkeypatch.setattr(tasks, "_repo_from_message",
                        lambda db_, project, text: iid)
    monkeypatch.setattr(tasks, "_dev_target", lambda db_, project: None)  # early exit
    tasks.handle_request(pid, rid, "m1")
    with SyncSession() as db:
        from app.models import Request as Req
        assert db.get(Req, rid).repo_id == iid


def test_pr_deliverable_predicate(org_id, quiet):
    with SyncSession() as db:
        p, infra, app, req = _setup(db, org_id)
        db.flush()
        run = DevRun(project_id=p.id, request_id=req.id, state="running",
                     repo_id=app.id)  # side repo (infra is the push target)
        db.add(run)
        db.flush()
        dev_concurrency.bind_run(p, run)
        assert tasks._pr_deliverable_run(db, p) is True
        run.repo_id = infra.id  # the default target -> normal demo path
        db.flush()
        assert tasks._pr_deliverable_run(db, p) is False
        dev_concurrency.bind_run(p, None)
        assert tasks._pr_deliverable_run(db, p) is False
        p.kind = "auto_dev"
        assert tasks._pr_deliverable_run(db, p) is True
        db.rollback()


def test_create_request_service_validates_and_stores_repo_intent(org_id, quiet):
    import asyncio

    from app.core.db import async_session, engine

    # Async-pool healing (the TestClient modules' pattern): the global engine
    # and the WS redis client may be bound to an earlier test's event loop.
    asyncio.run(engine.dispose(close=False))
    events._async_client = None
    from app.services import project_actions

    with SyncSession() as db:
        p, infra, app, _ = _setup(db, org_id)
        db.commit()
        pid, app_id = p.id, app.id

    async def _run():
        async with async_session() as adb:
            proj = await adb.get(Project, pid)
            req, _ = await project_actions.create_request(
                adb, proj, "customer", "feature", "ai", "Bump infra thing",
                repo_id=app_id)
            assert req.repo_id == app_id
            try:
                await project_actions.create_request(
                    adb, proj, "customer", "feature", "ai", "x", repo_id="nope")
                raise AssertionError("foreign repo_id must 404")
            except project_actions.ActionError as exc:
                assert exc.status == 404

    asyncio.run(_run())
