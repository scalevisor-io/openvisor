"""§conversation resume: the worker's half of session persistence.

The runner persists the agent conversation under the workspace and rehydrates
it on the next run of the SAME chain (probed against the pinned SDK). What the
worker owns is the two lifetime rules pinned here:

- the steering note is ALSO written alone to steering.md, because a rehydrated
  session must receive only what is new - the restored history already contains
  the task, and replaying it would double the context;
- conversation state belongs to ONE chain: an unchained run (a new request, a
  Start fresh, the first run of any unit) deletes it, since in the legacy
  single-checkout mode the workspace - and therefore .openvisor/ - is reused
  across DIFFERENT requests, and request B must never resume request A's
  session. A fix dispatch is NOT such a run: it is the same DevRun row taking
  another pass, so it keeps the session and routes its fix text through the
  resume channel.
"""
from types import SimpleNamespace

import pytest

from app.core.db import SyncSession
from app.workers import tasks


@pytest.fixture
def db():
    with SyncSession() as s:
        yield s
        s.rollback()


def _project(tmp_path):
    return SimpleNamespace(workspace_path=str(tmp_path), ssh_private_key_enc=None,
                           id="no-such-project", org_id="no-such-org",
                           use_global_memory=False, kb_ids=None)


def _prep(db, project, **kw):
    return tasks._prepare_runner_inputs(db, project, **kw)


@pytest.fixture(autouse=True)
def _stub_task(monkeypatch):
    monkeypatch.setattr(tasks, "_build_task_file", lambda *a, **k: ("task", []))


def test_steering_is_written_alone_and_unlinked_when_absent(db, tmp_path):
    project = _project(tmp_path)
    _prep(db, project, steering_note="Focus on the failing tests first.")
    steering = tmp_path / ".openvisor" / "steering.md"
    assert steering.read_text() == "Focus on the failing tests first."
    # the next dispatch without a note must not leave the old one behind
    _prep(db, project)
    assert not steering.exists()


def test_an_unchained_run_discards_any_previous_conversation(db, tmp_path):
    """Legacy single-checkout mode reuses .openvisor/ across requests - request
    B must never rehydrate request A's session."""
    project = _project(tmp_path)
    conv = tmp_path / ".openvisor" / "conversation"
    conv.mkdir(parents=True)
    (conv / "state.json").write_text("{}")
    (tmp_path / ".openvisor" / "conversation_id").write_text("abc")

    project._dev_run = None                    # no bound run: unchained
    _prep(db, project)
    assert not conv.exists()
    assert not (tmp_path / ".openvisor" / "conversation_id").exists()


def test_a_fresh_run_row_without_predecessor_also_discards(db, tmp_path):
    project = _project(tmp_path)
    conv = tmp_path / ".openvisor" / "conversation"
    conv.mkdir(parents=True)
    (conv / "state.json").write_text("{}")

    project._dev_run = SimpleNamespace(predecessor_id=None, workspace_dir="")
    _prep(db, project)
    assert not conv.exists()


def test_a_fix_dispatch_keeps_the_conversation_on_the_same_row(db, tmp_path):
    """A boot/CI/security fix is the SAME run taking another pass, not a new
    chain: wiping there made every retry re-explore the repository from cold."""
    project = _project(tmp_path)
    conv = tmp_path / ".openvisor" / "conversation"
    conv.mkdir(parents=True)
    (conv / "state.json").write_text("{}")
    cid = tmp_path / ".openvisor" / "conversation_id"
    cid.write_text("abc")

    project._dev_run = SimpleNamespace(predecessor_id=None, workspace_dir="")
    _prep(db, project, fix_instruction="compose up failed: port 8080 in use")
    assert conv.exists()
    assert cid.read_text() == "abc"


def test_a_fix_dispatch_puts_its_instruction_in_the_resume_channel(db, tmp_path):
    """The driver's resumed branch sends steering.md INSTEAD of task.md, so a fix
    that lived only in the task would never reach a rehydrated agent."""
    project = _project(tmp_path)
    project._dev_run = SimpleNamespace(predecessor_id=None, workspace_dir="")
    _prep(db, project, fix_instruction="boot check failed: exit 1")
    assert "boot check failed: exit 1" in (
        tmp_path / ".openvisor" / "steering.md").read_text()


def test_steering_and_fix_text_both_reach_a_resumed_session(db, tmp_path):
    project = _project(tmp_path)
    project._dev_run = SimpleNamespace(predecessor_id="prior-run", workspace_dir="")
    _prep(db, project, steering_note="use the staging bucket",
          fix_instruction="tests failed: 3 assertions")
    note = (tmp_path / ".openvisor" / "steering.md").read_text()
    assert "use the staging bucket" in note and "tests failed: 3 assertions" in note


def test_a_chained_resume_keeps_the_conversation(db, tmp_path):
    project = _project(tmp_path)
    conv = tmp_path / ".openvisor" / "conversation"
    conv.mkdir(parents=True)
    (conv / "state.json").write_text("{}")
    cid = tmp_path / ".openvisor" / "conversation_id"
    cid.write_text("abc")

    project._dev_run = SimpleNamespace(predecessor_id="prior-run", workspace_dir="")
    _prep(db, project, steering_note="keep going")
    assert conv.exists()                       # the whole point of the chain
    assert cid.read_text() == "abc"
