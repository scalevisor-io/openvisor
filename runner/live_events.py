"""Live build-activity feed for the customer build panel (§14.8). Registered as
an OpenHands Conversation callback: maps SDK events to small sanitized JSON
lines appended to /workspace/.openvisor/events.jsonl (offset-polled by the
platform API) and throttle-snapshots the accumulated token usage to
progress.json (display only - usage.json, written at exit, stays the billing
artifact).

Deliberately defensive: duck-typed against the SDK's event/action classes
(their names and fields drift across releases), every hook wrapped so the feed
can NEVER fail the build, and only summaries composed HERE are written - never
raw model output, task/system text, or command/tool output, which can carry
confidential KB/RAG content or secret values (leak_scan.py threat model). The
platform API applies a second redaction/withhold pass before serving."""
import json
import os
import re
import time
from pathlib import Path

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

MAX_FEED_BYTES = 2_000_000  # runaway-loop backstop; the build itself continues
PROGRESS_EVERY_S = 2.0
TITLE_MAX = 160
DETAIL_MAX = 280

# Event classes that must never reach the customer feed: tool/command OUTPUT
# (echoes files, env, KB text), the system prompt, and low-level plumbing.
_SILENT_NAMES = ("Observation", "SystemPrompt", "Token", "StreamingDelta",
                 "LLMCompletionLog", "ConversationStateUpdate", "Condensation",
                 "Pause", "Interrupt", "HookExecution")


def _clip(text, limit: int) -> str:
    text = _ANSI_RE.sub("", str(text or ""))
    text = re.sub(r"\s+", " ", text).strip()
    return text[: limit - 1] + "…" if len(text) > limit else text


def _thought_text(event) -> str:
    parts = []
    for item in getattr(event, "thought", None) or []:
        parts.append(item if isinstance(item, str) else getattr(item, "text", "") or "")
    return " ".join(p for p in parts if p).strip()


def _message_text(message) -> str:
    content = getattr(message, "content", None) or []
    parts = [c if isinstance(c, str) else getattr(c, "text", "") or "" for c in content]
    return " ".join(p for p in parts if p).strip()


def _summarize_action(event, action) -> dict:
    aname = type(action).__name__
    command = getattr(action, "command", None)
    path = (getattr(action, "path", None) or getattr(action, "file_path", None)
            or getattr(action, "dir_path", None))
    url = getattr(action, "url", None)
    kind, title = "action", _clip(re.sub(r"Action$", "", aname), TITLE_MAX) or "Working"
    if "Terminal" in aname or "Bash" in aname or "Cmd" in aname:
        kind, title = "command", _clip(command, TITLE_MAX) or "Running a command"
    elif "FileEditor" in aname:
        # FileEditorAction multiplexes view/create/str_replace/insert on its own
        # `command` field - a view is a read, everything else an edit.
        if str(command or "") == "view":
            kind, title = "read", _clip(path, TITLE_MAX) or "Reading a file"
        else:
            kind, title = "edit", _clip(path, TITLE_MAX) or "Editing a file"
    elif "Write" in aname or "Edit" in aname:
        kind, title = "edit", _clip(path, TITLE_MAX) or "Editing a file"
    elif ("Read" in aname or "Glob" in aname or "Grep" in aname
          or "ListDirectory" in aname):
        target = path or getattr(action, "pattern", None)
        kind, title = "read", _clip(target, TITLE_MAX) or "Exploring the code"
    elif "MCP" in aname:
        # The SDK keeps the called tool's name on the EVENT (ActionEvent.
        # tool_name); the action itself only carries the raw args dict. Args
        # never reach the feed (they can embed KB/RAG text - leak_scan threat
        # model); the tool NAME alone is platform-configured and safe.
        tool = (getattr(event, "tool_name", None)
                or getattr(action, "tool_name", None) or getattr(action, "name", None))
        kind, title = "action", (_clip(f"MCP tool: {tool}", TITLE_MAX)
                                 if tool else "Using an MCP tool")
    elif "Browser" in aname or url:
        kind, title = "browse", _clip(url, TITLE_MAX) or "Using the browser"
    elif "TaskTracker" in aname or "Plan" in aname:
        kind, title = "plan", "Updating the work plan"
    elif "Finish" in aname:
        kind, title = "finish", "Wrapping up the session"
    elif "Think" in aname:
        kind, title = "think", _clip(_thought_text(event), DETAIL_MAX) or "Thinking"
    out = {"kind": kind, "title": title}
    thought = _clip(_thought_text(event), DETAIL_MAX)
    if thought and thought != title:
        out["detail"] = thought
    return out


def summarize_event(event):
    """One sanitized feed dict for an SDK event, or None for events that must
    not reach the customer (tool output, the task/system messages, plumbing)."""
    name = type(event).__name__
    if any(s in name for s in _SILENT_NAMES):
        return None
    if "Error" in name:
        # The SDK's end-of-loop cap event deserves its own copy: "recovering" is
        # wrong (the session is over) and the cap is the one error customers act on.
        if getattr(event, "code", "") == "MaxIterationsReached":
            return {"kind": "error", "title": "Agent hit its iteration cap - session over, progress kept",
                    "detail": _clip(getattr(event, "detail", ""), DETAIL_MAX)}
        return {"kind": "error", "title": "The agent hit an error and is recovering",
                "detail": _clip(getattr(event, "error", ""), DETAIL_MAX)}
    action = getattr(event, "action", None)
    if action is not None:
        return _summarize_action(event, action)
    if "Message" in name:
        if str(getattr(event, "source", "") or "") != "agent":
            return None  # the user message IS the assembled task (system prompt + RAG)
        text = _message_text(getattr(event, "llm_message", None))
        return {"kind": "think", "title": _clip(text, DETAIL_MAX)} if text else None
    return None


class LiveFeed:
    """The Conversation callback object: append sanitized events, snapshot
    token usage at most every PROGRESS_EVERY_S."""

    def __init__(self, openvisor_dir, llm=None, model: str | None = None,
                 on_snapshot=None):
        base = Path(openvisor_dir)
        self.feed_path = base / "events.jsonl"
        self.progress_path = base / "progress.json"
        self.llm = llm
        self.model = model
        # Called (best-effort) whenever a throttled snapshot actually fires, so a
        # billing artifact (usage.json) can be refreshed on the same cadence as the
        # display snapshot - see run_dev._dump_usage / §Phase 1 incremental metering.
        self.on_snapshot = on_snapshot
        self._last_progress = 0.0
        self._truncated = False
        # Set when the SDK reports the session ended at the iteration cap; the
        # driver persists it as .openvisor/exit_reason.json for the worker's copy.
        self.exit_reason: str | None = None

    def __call__(self, event) -> None:
        if getattr(event, "code", "") == "MaxIterationsReached":
            self.exit_reason = "max_iterations"
        try:
            ev = summarize_event(event)
            if ev:
                self._append(ev)
        except Exception:  # noqa: BLE001 - the feed must never fail the build
            pass
        try:
            self.dump_progress()
        except Exception:  # noqa: BLE001
            pass

    def _append(self, ev: dict) -> None:
        if self._truncated:
            return
        try:
            if self.feed_path.exists() and self.feed_path.stat().st_size > MAX_FEED_BYTES:
                self._truncated = True
                ev = {"kind": "phase", "title": "Activity feed truncated - the build continues"}
        except OSError:
            pass
        line = json.dumps({"ts": round(time.time(), 3), **ev}, ensure_ascii=False)
        # The agent can delete .openvisor/ mid-run (gitignored → `git clean -fdx`
        # takes it out); recreate it so the feed self-heals.
        self.feed_path.parent.mkdir(parents=True, exist_ok=True)
        with self.feed_path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    def dump_progress(self, force: bool = False) -> None:
        """Atomic write (tmp + rename) so the polling API never reads a torn
        JSON. Named progress.json, NOT usage.json - the worker unlinks the
        latter after billing and must never bill this display snapshot."""
        now = time.time()
        if not force and now - self._last_progress < PROGRESS_EVERY_S:
            return
        self._last_progress = now
        tu = getattr(getattr(self.llm, "metrics", None), "accumulated_token_usage", None)
        snap = {"model": self.model,
                "input_tokens": int(getattr(tu, "prompt_tokens", 0) or 0),
                "output_tokens": int(getattr(tu, "completion_tokens", 0) or 0),
                "cached_input_tokens": int(getattr(tu, "cache_read_tokens", 0) or 0),
                "updated_at": round(now, 3)}
        tmp = self.progress_path.with_name(self.progress_path.name + ".tmp")
        self.progress_path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(snap))
        os.replace(tmp, self.progress_path)
        if self.on_snapshot is not None:
            try:
                self.on_snapshot()
            except Exception:  # noqa: BLE001 - metering must never fail the build
                pass
