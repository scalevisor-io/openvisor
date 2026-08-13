"""Worker-side git transport to a tailnet-only git host (GIT_EXTRA_HOST) and the
demo-start canonical-checkout sync. Regression for prod regression: the k8s
worker pod has no route to the platform GitLab's SSH port (only runner sandboxes
get the deployer's hostAliases), so every post-merge `_refresh_root_workspace`
timed out and parked a DELIVERED run as failed - and the park copy hid the real
ssh error because CalledProcessError's str() drops captured stderr. Manual demo
starts also skipped the sync entirely and served a stale root checkout.
"""
import subprocess
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, select

from app.core.config import settings
from app.core.db import SyncSession
from app.models import (
    DeploymentEvent, DevRun, Message, Organization, Project, Request, StatusChange,
)
from app.services import events
from app.workers import tasks


# ---------------------------------------------------------------- _git_host_rewrite

def test_rewrite_maps_matching_ssh_host(monkeypatch):
    monkeypatch.setattr(settings, "git_extra_host",
                        "git.example.com:gitlab-ssh.ns.svc.cluster.local")
    args = tasks._git_host_rewrite("ssh://git@git.example.com:10022/grp/repo.git")
    assert args == ["-c", ("url.ssh://git@gitlab-ssh.ns.svc.cluster.local:10022/"
                           ".insteadOf=ssh://git@git.example.com:10022/")]


def test_rewrite_noop_cases(monkeypatch):
    monkeypatch.setattr(settings, "git_extra_host",
                        "git.example.com:gitlab-ssh.ns.svc.cluster.local")
    # different host, non-ssh scheme, scp-style shorthand: all untouched
    assert tasks._git_host_rewrite("ssh://git@github.com/org/repo.git") == []
    assert tasks._git_host_rewrite("https://git.example.com/grp/repo.git") == []
    assert tasks._git_host_rewrite("git@git.example.com:grp/repo.git") == []
    monkeypatch.setattr(settings, "git_extra_host", "")
    assert tasks._git_host_rewrite("ssh://git@git.example.com:10022/g/r.git") == []
    monkeypatch.setattr(settings, "git_extra_host", "hostwithouttarget")
    assert tasks._git_host_rewrite("ssh://git@hostwithouttarget/g/r.git") == []


# ---------------------------------------------------------- _refresh_root_workspace

def _target(remote="ssh://git@git.example.com:10022/grp/repo.git"):
    return {"remote": remote, "base_branch": "main", "provider": "gitlab"}


def test_refresh_surfaces_git_stderr_and_applies_rewrite(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir(parents=True)
    project = SimpleNamespace(workspace_path=str(tmp_path), ssh_private_key_enc=None)
    monkeypatch.setattr(tasks, "_dev_target", lambda db, p: _target())
    monkeypatch.setattr(settings, "git_extra_host",
                        "git.example.com:gitlab-ssh.ns.svc.cluster.local")
    calls: list[list[str]] = []

    def fake_run(argv, **kw):
        calls.append(argv)
        return subprocess.CompletedProcess(
            argv, 128, stdout="",
            stderr="ssh: connect to host git.example.com port 10022: "
                   "Connection timed out\nfatal: Could not read from remote repository.")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(RuntimeError) as exc:
        tasks._refresh_root_workspace(None, project)
    assert "Connection timed out" in str(exc.value)      # stderr surfaced, not exit code alone
    assert calls and calls[0][1] == "-c"                 # transport rewrite rides every git call
    assert "gitlab-ssh.ns.svc.cluster.local" in calls[0][2]


def test_refresh_lands_on_base_branch(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir(parents=True)
    project = SimpleNamespace(workspace_path=str(tmp_path), ssh_private_key_enc=None)
    monkeypatch.setattr(tasks, "_dev_target", lambda db, p: _target())
    monkeypatch.setattr(settings, "git_extra_host", "")
    calls: list[list[str]] = []

    def fake_run(argv, **kw):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    tasks._refresh_root_workspace(None, project)
    verbs = [a for call in calls for a in call if a in ("fetch", "reset", "checkout")]
    assert verbs == ["fetch", "reset", "checkout"]       # sync then land ON the base branch
    assert calls[-1][-2:] == ["main", "origin/main"]


# ------------------------------------------------------------- demo-start sync gate

@pytest.fixture
def org_id():
    with SyncSession() as db:
        org = Organization(name="Transport Test Org", credit_balance=100.0)
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
                db.execute(delete(DeploymentEvent).where(DeploymentEvent.project_id.in_(pids)))
                db.execute(delete(DevRun).where(DevRun.project_id.in_(pids)))
                db.execute(delete(Request).where(Request.project_id.in_(pids)))
            db.execute(delete(Project).where(Project.org_id == oid))
            db.execute(delete(Organization).where(Organization.id == oid))
            db.commit()


@pytest.fixture
def quiet(monkeypatch):
    started: list = []
    monkeypatch.setattr(events, "publish_sync", lambda pid, ev: None)
    monkeypatch.setattr(tasks.celery, "send_task", lambda *a, **k: None)
    monkeypatch.setattr(tasks, "_find_demo_dir", lambda w: ".")
    monkeypatch.setattr(tasks, "_allocate_port", lambda db, p: 20002)
    monkeypatch.setattr(tasks, "_htpasswd", lambda p: "")
    monkeypatch.setattr(tasks.deployer_client, "start_demo",
                        lambda *a, **k: started.append(a))
    return started


def _project(oid, **kw):
    kw.setdefault("name", "P")
    kw.setdefault("description", "d")
    kw.setdefault("kind", "ai")
    kw.setdefault("status", "finished")
    kw.setdefault("workspace_path", "/tmp/ws")
    kw.setdefault("subdomain", "sub-" + uuid.uuid4().hex[:8])
    with SyncSession() as db:
        p = Project(org_id=oid, **kw)
        db.add(p)
        db.commit()
        return p.id


def _last_message(pid):
    with SyncSession() as db:
        return (db.execute(select(Message).where(Message.project_id == pid)
                           .order_by(Message.created_at.desc())).scalars().first())


def test_manual_start_refuses_stale_tree_on_sync_failure(quiet, org_id, monkeypatch):
    pid = _project(org_id, dev_run_state="done")
    monkeypatch.setattr(tasks, "_dev_target", lambda db, p: _target())
    monkeypatch.setattr(tasks, "_refresh_root_workspace",
                        lambda db, p: (_ for _ in ()).throw(RuntimeError("sync broke")))
    tasks.demo_start(pid, "start")
    assert quiet == []                                   # never deployed a stale tree
    msg = _last_message(pid)
    assert msg is not None and "not deploying a stale tree" in msg.body
    with SyncSession() as db:
        assert db.get(Project, pid).demo_state != "running"


def test_manual_start_syncs_git_backed_project(quiet, org_id, monkeypatch):
    pid = _project(org_id, dev_run_state="done")
    synced: list = []
    monkeypatch.setattr(tasks, "_dev_target", lambda db, p: _target())
    monkeypatch.setattr(tasks, "_refresh_root_workspace",
                        lambda db, p: synced.append(True))
    tasks.demo_start(pid, "start")
    assert synced == [True]
    assert len(quiet) == 1


def test_manual_start_skips_sync_without_git_target(quiet, org_id, monkeypatch):
    pid = _project(org_id, dev_run_state="done")
    monkeypatch.setattr(tasks, "_dev_target", lambda db, p: None)
    monkeypatch.setattr(tasks, "_refresh_root_workspace",
                        lambda db, p: pytest.fail("refresh must not run without a target"))
    tasks.demo_start(pid, "start")
    assert len(quiet) == 1                               # scaffold projects still deploy


def test_manual_start_skips_sync_under_live_legacy_build(quiet, org_id, monkeypatch):
    pid = _project(org_id, dev_run_state="running")
    with SyncSession() as db:
        db.add(DevRun(project_id=pid, state="running", workspace_dir=""))
        db.commit()
    monkeypatch.setattr(tasks, "_dev_target", lambda db, p: _target())
    monkeypatch.setattr(tasks, "_refresh_root_workspace",
                        lambda db, p: pytest.fail("refresh must not reset a live legacy build"))
    tasks.demo_start(pid, "start")
    assert len(quiet) == 1                               # legacy semantics: deploy the tree as-is
