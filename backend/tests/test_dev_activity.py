"""Live build console (§14.8) pure-logic tests: the devfeed chunk reader
(offset semantics, torn/malformed lines, new-run reset, secret redaction,
PEM/KB withholding, progress snapshot + display-only estimate), the worker's
_save_run feed narration, and the runner-side event summarizer/LiveFeed
(loaded by file path from the compose.dev runner mount, leak-scan-test style).
"""
import importlib.util
import json
import pathlib
from types import SimpleNamespace

import pytest

from app.services import devfeed

RUNNER_LIVE_EVENTS = pathlib.Path("/app/runner_src/live_events.py")


def _project(tmp_path, state="running"):
    ws = tmp_path / "ws"
    (ws / ".openvisor").mkdir(parents=True, exist_ok=True)
    return SimpleNamespace(id="devfeed-test-project", workspace_path=str(ws),
                           dev_run_state=state, dev_run_started_at=None,
                           dev_run_log=None, dev_run_error=None,
                           ssh_private_key_enc=None)


def _write_feed(project, lines):
    devfeed.feed_path(project).write_text("\n".join(lines) + "\n")


# ---- devfeed.read_chunk ----

def test_read_chunk_consumes_complete_lines_and_advances_offset(tmp_path):
    p = _project(tmp_path)
    _write_feed(p, [json.dumps({"ts": 1, "kind": "phase", "title": "one"}),
                    json.dumps({"ts": 2, "kind": "command", "title": "two"})])
    c1 = devfeed.read_chunk(p, 0)
    assert [e["title"] for e in c1["events"]] == ["one", "two"]
    assert c1["live"] is True and c1["reset"] is False
    # polling again from next_offset returns nothing new
    c2 = devfeed.read_chunk(p, c1["next_offset"])
    assert c2["events"] == [] and c2["next_offset"] == c1["next_offset"]
    # a new complete line then appears in the next chunk
    with devfeed.feed_path(p).open("a") as f:
        f.write(json.dumps({"ts": 3, "kind": "git", "title": "three"}) + "\n")
    c3 = devfeed.read_chunk(p, c2["next_offset"])
    assert [e["title"] for e in c3["events"]] == ["three"]


def test_read_chunk_partial_tail_line_waits_for_next_poll(tmp_path):
    p = _project(tmp_path)
    full = json.dumps({"ts": 1, "kind": "phase", "title": "done-line"})
    with devfeed.feed_path(p).open("w") as f:
        f.write(full + "\n" + '{"ts": 2, "kind": "phase", "ti')  # torn mid-write
    c = devfeed.read_chunk(p, 0)
    assert [e["title"] for e in c["events"]] == ["done-line"]
    assert c["next_offset"] == len(full) + 1  # the torn tail is NOT consumed


def test_read_chunk_skips_malformed_lines(tmp_path):
    p = _project(tmp_path)
    _write_feed(p, ["not json at all", json.dumps({"kind": "phase", "title": "ok"}),
                    json.dumps(["a", "list"])])
    c = devfeed.read_chunk(p, 0)
    assert [e["title"] for e in c["events"]] == ["ok"]


def test_read_chunk_missing_file_and_new_run_reset(tmp_path):
    p = _project(tmp_path)
    c = devfeed.read_chunk(p, 0)
    assert c["events"] == [] and c["next_offset"] == 0
    # a client holding a stale (larger) offset from the previous run is reset
    _write_feed(p, [json.dumps({"kind": "phase", "title": "fresh"})])
    c2 = devfeed.read_chunk(p, 10_000)
    assert c2["reset"] is True
    assert [e["title"] for e in c2["events"]] == ["fresh"]


def test_read_chunk_redacts_platform_secrets(tmp_path, monkeypatch):
    p = _project(tmp_path)
    monkeypatch.setattr("app.services.leakscan.platform_secret_values",
                        lambda extra_values=None, ssh_private_keys=None:
                        ["sk-super-secret-value-123"])
    _write_feed(p, [json.dumps({"kind": "command",
                                "title": "curl -H 'Bearer sk-super-secret-value-123'",
                                "detail": "using sk-super-secret-value-123 again"})])
    c = devfeed.read_chunk(p, 0)
    assert "sk-super-secret-value-123" not in json.dumps(c)
    assert "[redacted]" in c["events"][0]["title"]
    assert "[redacted]" in c["events"][0]["detail"]


def test_read_chunk_withholds_pem_and_kb_verbatim_lines(tmp_path):
    p = _project(tmp_path)
    kb_span = "the consultant's confidential methodology paragraph"
    (pathlib.Path(p.workspace_path) / ".openvisor" / "leak_kb.json").write_text(
        json.dumps([kb_span]))
    _write_feed(p, [
        json.dumps({"kind": "think", "title": "-----BEGIN RSA PRIVATE KEY----- oops"}),
        json.dumps({"kind": "think",
                    "title": f"Quoting: {kb_span.upper()} verbatim"}),
        json.dumps({"kind": "phase", "title": "clean line"}),
    ])
    c = devfeed.read_chunk(p, 0)
    assert c["events"][0]["title"] == devfeed.WITHHELD
    assert c["events"][1]["title"] == devfeed.WITHHELD  # whitespace/case-normalized match
    assert c["events"][2]["title"] == "clean line"


# ---- devfeed.read_progress ----

def test_read_progress_estimates_cost_and_survives_unknown_model(tmp_path, monkeypatch):
    p = _project(tmp_path)
    devfeed.progress_path(p).write_text(json.dumps(
        {"model": "test-model", "input_tokens": 1000, "output_tokens": 200,
         "cached_input_tokens": 400, "cache_write_tokens": 150,
         "cache_write_1h_tokens": 50}))
    import app.services.pricing as pricing
    seen = {}

    def _fake(model, i, o, markup=None, cached_input_tokens=0, cache_write_tokens=0,
              cache_write_1h_tokens=0, **kw):
        seen["cached"] = cached_input_tokens
        seen["write"] = cache_write_tokens
        seen["write_1h"] = cache_write_1h_tokens
        return 0.1234
    monkeypatch.setattr(pricing, "cost_credits", _fake)
    usage = devfeed.read_progress(p)
    assert usage["total_tokens"] == 1200 and usage["credits_estimate"] == 0.1234
    # the live estimate prices every cache tier exactly like billing does, so the
    # number on screen tracks the invoice instead of drifting under it
    assert usage["cached_input_tokens"] == 400 and seen["cached"] == 400
    assert seen["write"] == 150 and seen["write_1h"] == 50
    # the console names the model this run executes on (routing can change
    # between runs)
    assert usage["model"] == "test-model"

    def _boom(model, i, o, markup=None, **kw):
        raise pricing.UnknownModelError(model)
    monkeypatch.setattr(pricing, "cost_credits", _boom)
    usage = devfeed.read_progress(p)
    assert usage["total_tokens"] == 1200 and usage["credits_estimate"] is None
    assert devfeed.read_progress(_project(tmp_path / "other")) is None


def test_read_progress_prices_an_endpoint_only_model(tmp_path):
    """A model priced ONLY by its admin endpoint (absent from the static table)
    is metered fine by the worker, so the console must price it the same way -
    it used to raise UnknownModelError and show no estimate at all while the
    same run billed correctly."""
    from app.core.config import settings
    p = _project(tmp_path)
    devfeed.progress_path(p).write_text(json.dumps(
        {"model": "endpoint-only-model", "input_tokens": 1_000_000,
         "output_tokens": 1_000_000, "cached_input_tokens": 500_000}))
    # no endpoint prices = the static table alone = unpriced = no estimate
    assert devfeed.read_progress(p)["credits_estimate"] is None
    usage = devfeed.read_progress(p, {"endpoint-only-model": (2.0, 12.0, 0.2)})
    expected = round((500_000 * 2.0 + 500_000 * 0.2 + 1_000_000 * 12.0) / 1_000_000
                     * settings.credit_markup, 4)
    assert usage["credits_estimate"] == expected
    # another model's price is never borrowed
    assert devfeed.read_progress(
        p, {"other-model": (2.0, 12.0, 0.2)})["credits_estimate"] is None


# ---- worker narration (_save_run) ----

def test_save_run_narrates_state_changes_once(tmp_path, monkeypatch):
    from app.services import events
    from app.workers.tasks import _save_run
    monkeypatch.setattr(events, "publish_sync", lambda pid, ev: None)
    p = _project(tmp_path, state="running")
    _save_run(p, "failed", error="boom")
    _save_run(p, "failed", error="boom")  # same state again: no duplicate event
    lines = devfeed.feed_path(p).read_text().strip().splitlines()
    assert len(lines) == 1
    ev = json.loads(lines[0])
    assert ev["kind"] == "error" and ev["detail"] == "boom"
    assert p.dev_run_state == "failed" and p.dev_run_error == "boom"


def test_devfeed_reset_clears_previous_run(tmp_path):
    p = _project(tmp_path)
    _write_feed(p, [json.dumps({"kind": "phase", "title": "old"})])
    devfeed.progress_path(p).write_text("{}")
    devfeed.reset(p)
    assert not devfeed.feed_path(p).exists()
    assert not devfeed.progress_path(p).exists()


# ---- runner/live_events.py (loaded by file path, skip when not mounted) ----

@pytest.fixture(scope="module")
def live_events():
    if not RUNNER_LIVE_EVENTS.exists():
        pytest.skip("runner source not mounted at /app/runner_src")
    spec = importlib.util.spec_from_file_location("live_events_under_test",
                                                  RUNNER_LIVE_EVENTS)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _obj(cls_name, **attrs):
    return type(cls_name, (), attrs)()


def test_summarizer_maps_actions_and_silences_output(live_events):
    ev = _obj("ActionEvent", action=_obj("TerminalAction", command="npm install"),
              thought=[_obj("TextContent", text="need deps first")])
    out = live_events.summarize_event(ev)
    assert out == {"kind": "command", "title": "npm install", "detail": "need deps first"}
    # a file view is a read, any other editor sub-command an edit
    view = _obj("ActionEvent", action=_obj("FileEditorAction", command="view", path="app/main.py"))
    assert live_events.summarize_event(view)["kind"] == "read"
    edit = _obj("ActionEvent", action=_obj("FileEditorAction", command="str_replace", path="app/main.py"))
    assert live_events.summarize_event(edit) == {"kind": "edit", "title": "app/main.py"}
    # MCP calls name their tool (from the EVENT's tool_name - the action only
    # carries the raw args dict, which never reaches the feed)
    mcp = _obj("ActionEvent", tool_name="context7.get-library-docs",
               action=_obj("MCPToolAction", data={"q": "SECRET"}))
    assert live_events.summarize_event(mcp) == {
        "kind": "action", "title": "MCP tool: context7.get-library-docs"}
    anon = _obj("ActionEvent", action=_obj("MCPToolAction", data={}))
    assert live_events.summarize_event(anon)["title"] == "Using an MCP tool"
    # tool OUTPUT (can echo secrets/KB) and the system prompt never reach the feed
    assert live_events.summarize_event(_obj("ObservationEvent", observation=_obj("X"))) is None
    assert live_events.summarize_event(_obj("SystemPromptEvent")) is None


def test_summarizer_messages_and_errors(live_events):
    task = _obj("MessageEvent", source="user",
                llm_message=_obj("Message", content=[_obj("TextContent", text="SECRET TASK")]))
    assert live_events.summarize_event(task) is None  # the task carries RAG/system text
    reply = _obj("MessageEvent", source="agent",
                 llm_message=_obj("Message", content=[_obj("TextContent", text="Building the API now")]))
    assert live_events.summarize_event(reply) == {"kind": "think", "title": "Building the API now"}
    err = live_events.summarize_event(_obj("AgentErrorEvent", error="x" * 500))
    assert err["kind"] == "error" and len(err["detail"]) <= live_events.DETAIL_MAX


def test_summarizer_clips_and_strips_ansi(live_events):
    long_cmd = "\x1b[31mecho\x1b[0m " + "a" * 400
    out = live_events.summarize_event(
        _obj("ActionEvent", action=_obj("TerminalAction", command=long_cmd)))
    assert "\x1b" not in out["title"] and len(out["title"]) <= live_events.TITLE_MAX


def test_max_iterations_event_gets_clear_copy_and_flags_exit(live_events, tmp_path):
    ev = _obj("ConversationErrorEvent", code="MaxIterationsReached",
              detail="Agent reached maximum iterations limit (40).")
    out = live_events.summarize_event(ev)
    assert "iteration cap" in out["title"] and "recovering" not in out["title"]
    feed = live_events.LiveFeed(tmp_path)
    feed(ev)
    assert feed.exit_reason == "max_iterations"
    # generic errors keep the recovering copy and never set the flag
    generic = live_events.summarize_event(_obj("AgentErrorEvent", error="boom"))
    assert "recovering" in generic["title"]


def test_livefeed_appends_events_and_snapshots_progress(live_events, tmp_path):
    llm = SimpleNamespace(metrics=SimpleNamespace(
        accumulated_token_usage=SimpleNamespace(prompt_tokens=42, completion_tokens=7,
                                                cache_read_tokens=30)))
    feed = live_events.LiveFeed(tmp_path, llm=llm, model="test-model")
    feed(_obj("ActionEvent", action=_obj("TerminalAction", command="ls")))
    feed(_obj("BrokenEvent", action=property(lambda self: 1 / 0)))  # never raises
    lines = (tmp_path / "events.jsonl").read_text().strip().splitlines()
    assert json.loads(lines[0])["title"] == "ls"
    feed.dump_progress(force=True)
    snap = json.loads((tmp_path / "progress.json").read_text())
    assert snap == {"model": "test-model", "input_tokens": 42, "output_tokens": 7,
                    "cached_input_tokens": 30, "cache_write_tokens": 0,
                    "cache_write_1h_tokens": 0, "updated_at": snap["updated_at"]}


# ---- customer-secret redaction (the glpat-in-the-console incident) ----

def test_read_chunk_redacts_memory_secrets_and_token_shapes(tmp_path):
    """A customer Memory secret the agent inlined into a command is redacted via
    extra_secrets, and a credential-SHAPED string is redacted with no stored
    copy at all - which is also what retroactively cleans a feed written before
    this guard existed."""
    p = _project(tmp_path)
    _write_feed(p, [
        json.dumps({"kind": "command",
                    "title": 'export GITLAB_TOKEN="glpat-hy-Faketoken_12345"',
                    "detail": 'curl --header "PRIVATE-TOKEN: glpat-hy-Faketoken_12345"'}),
        json.dumps({"kind": "command",
                    "title": "deploy using customer-memory-secret-99"}),
    ])
    c = devfeed.read_chunk(p, 0, None, ["customer-memory-secret-99"])
    dumped = json.dumps(c)
    assert "glpat-" not in dumped
    assert "customer-memory-secret-99" not in dumped
    assert "[redacted]" in c["events"][0]["title"]
    assert "[redacted]" in c["events"][0]["detail"]
    assert "[redacted]" in c["events"][1]["title"]


def test_secret_values_collects_project_and_org_memory():
    import asyncio

    from sqlalchemy.ext.asyncio import (AsyncSession, async_sessionmaker,
                                        create_async_engine)
    from sqlalchemy.pool import NullPool

    from app.core.config import settings
    from app.core.db import SyncSession
    from app.core.encryption import encrypt
    from app.models import Organization, OrgMemory, Project, ProjectMemory

    with SyncSession() as db:
        o = Organization(name="devfeed-secrets org", credit_balance=0)
        db.add(o)
        db.flush()
        p = Project(org_id=o.id, name="P", description="d", kind="ai",
                    status="development")
        db.add(p)
        db.flush()
        db.add(ProjectMemory(project_id=p.id, author="customer", key="GITLAB_TOKEN",
                             value_enc=encrypt("project-secret-value-1"), is_secret=True))
        db.add(ProjectMemory(project_id=p.id, author="customer", key="PLAIN",
                             value_enc=encrypt("not-a-secret-value"), is_secret=False))
        db.add(OrgMemory(org_id=o.id, author="customer", key="ORG_TOKEN",
                         value_enc=encrypt("org-secret-value-22"), is_secret=True))
        db.commit()
        oid, pid = o.id, p.id

    async def _run():
        # A private engine on THIS loop: the app's pooled async engine holds
        # connections bound to other tests' loops and asyncio.run opens a new one.
        engine = create_async_engine(settings.database_url, poolclass=NullPool)
        try:
            maker = async_sessionmaker(engine, class_=AsyncSession,
                                       expire_on_commit=False)
            async with maker() as adb:
                proj = await adb.get(Project, pid)
                return await devfeed.secret_values(adb, proj)
        finally:
            await engine.dispose()

    try:
        vals = asyncio.run(_run())
        assert "project-secret-value-1" in vals
        assert "org-secret-value-22" in vals
        assert "not-a-secret-value" not in vals
    finally:
        with SyncSession() as db:
            db.query(ProjectMemory).filter_by(project_id=pid).delete()
            db.query(OrgMemory).filter_by(org_id=oid).delete()
            db.query(Project).filter_by(id=pid).delete()
            db.query(Organization).filter_by(id=oid).delete()
            db.commit()


def test_livefeed_redacts_secret_values_and_token_shapes(live_events, tmp_path, monkeypatch):
    """Runner-side choke point: a secret value from the sandbox environment and a
    credential-shaped string never reach the on-disk feed, for BOTH writers (the
    SDK callback and a driver's own append)."""
    monkeypatch.setenv("GITLAB_TOKEN", "env-memory-secret-token-1")
    feed = live_events.LiveFeed(tmp_path)
    feed(_obj("ActionEvent", action=_obj(
        "TerminalAction",
        command='export GITLAB_TOKEN="env-memory-secret-token-1" && '
                'curl --header "PRIVATE-TOKEN: glpat-hy-Faketoken_12345"')))
    feed.append({"kind": "phase", "title": "pushing with glpat-hy-Faketoken_12345"})
    raw = feed.feed_path.read_text()
    assert "env-memory-secret-token-1" not in raw
    assert "glpat-" not in raw
    assert raw.count("[redacted]") >= 3


def test_summarizer_surfaces_a_timed_out_command_and_nothing_else_from_output(live_events):
    """§14.5 per-command cap. Tool output stays silent, except the one fact an
    admin needs: a step hit its timeout. SDK 1.8.0 carries that in
    CmdOutputMetadata.suffix (probed in the runner image - .text is only the raw
    output), and the feed line is fixed copy: none of the output reaches it."""
    suffix = ("\n[The command timed out after 600.0 seconds. You may wait longer to see "
              "additional output by sending empty command '' ...]")
    timed_out = _obj("ObservationEvent", observation=_obj(
        "TerminalObservation", text="tick\ntick\nSECRET=hunter2",
        metadata=_obj("CmdOutputMetadata", exit_code=-1, suffix=suffix)))
    out = live_events.summarize_event(timed_out)
    assert out == {"kind": "command",
                   "title": "Command still running after its timeout - the session moved on"}
    assert "hunter2" not in json.dumps(out)
    # a normal observation - same shape, empty suffix - is still silenced
    quiet = _obj("ObservationEvent", observation=_obj(
        "TerminalObservation", text="ok", metadata=_obj("CmdOutputMetadata", exit_code=0, suffix="")))
    assert live_events.summarize_event(quiet) is None
    # a driver that renders the marker inline (no metadata) is caught by the text fallback
    inline = _obj("ObservationEvent", observation=_obj(
        "BashObservation", content=[_obj("TextContent", text="... [The command timed out after 5 seconds]")]))
    assert live_events.summarize_event(inline)["kind"] == "command"
