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
         "cached_input_tokens": 400}))
    import app.services.pricing as pricing
    seen = {}

    def _fake(model, i, o, markup=None, cached_input_tokens=0, **kw):
        seen["cached"] = cached_input_tokens
        return 0.1234
    monkeypatch.setattr(pricing, "cost_credits", _fake)
    usage = devfeed.read_progress(p)
    assert usage["total_tokens"] == 1200 and usage["credits_estimate"] == 0.1234
    # the live estimate discounts prompt-cache reads exactly like billing does
    assert usage["cached_input_tokens"] == 400 and seen["cached"] == 400
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
                    "cached_input_tokens": 30, "updated_at": snap["updated_at"]}
