"""Openvisor Claude Agent SDK dev driver (§dev harness, SDK v1).

The second driver for the sandboxed runner: same /workspace, same
/workspace/.openvisor/ contract as run_dev.py, a different agent loop. The
entrypoint picks between them on DEV_HARNESS, and everything around this file -
checkout, deploy key, secret export, leak scan, publish, exit codes - stays the
entrypoint's job. So this module owns exactly three things: run the task, keep
the build console fed, and leave the artifacts the worker reads back.

Those artifacts are the contract, and the worker cannot tell which harness wrote
them:
  usage.json        atomic {model, input_tokens, output_tokens, cached_input_tokens}
  error.json        {category, message} when the driver dies
  exit_reason.json  {reason: "max_iterations", limit} when the turn cap ended it
  events.jsonl      the live build panel (live_events.LiveFeed, shared with run_dev)
  progress.json     the throttled token snapshot the SPA polls
  plan.md           written by the AGENT under PLAN_ONLY - task.md asks for it and
                    the entrypoint discards the working tree afterwards either way

The Agent SDK runs the `claude` CLI as a subprocess, so the image needs BOTH the
npm CLI and this Python package (see runner/Dockerfile).
"""
import asyncio
import json
import os
import sys
import time
from pathlib import Path

import live_events

OPENVISOR = Path("/workspace/.openvisor")

# Last lines of the `claude` CLI's stderr. The SDK raises ProcessError with the
# text "Check stderr output for details" and no stderr attached, so without this
# a harness misconfiguration reaches the build panel as an exit code and nothing
# else - which is exactly how the root/--dangerously-skip-permissions refusal
# below cost a debugging round to find.
_STDERR_TAIL: list[str] = []
_STDERR_KEEP = 40


def _capture_stderr(line: str) -> None:
    text = (line or "").rstrip()
    if not text:
        return
    _STDERR_TAIL.append(text)
    del _STDERR_TAIL[:-_STDERR_KEEP]


class _Usage:
    """The token counters, in the shape live_events.LiveFeed already reads off
    the OpenHands LLM object (`llm.metrics.accumulated_token_usage`). Wearing
    that shape lets the Claude driver reuse the feed unmodified rather than fork
    a second progress writer that could drift from the first."""

    def __init__(self):
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.cache_read_tokens = 0
        self.cache_write_tokens = 0
        self.provider_cost_usd = 0.0


class _Metrics:
    def __init__(self, usage):
        self.accumulated_token_usage = usage


class _LLMShim:
    """Duck-types the one attribute LiveFeed touches."""

    def __init__(self, usage):
        self.metrics = _Metrics(usage)


def _model() -> str:
    """The billing key: the raw api_model, not a provider-prefixed routing name.
    Identical normalisation to run_dev.py so one price row serves both drivers."""
    return os.environ["LLM_MODEL"].split("/")[-1]


def _dump_usage(usage: _Usage, quiet: bool = False) -> None:
    """Write the run's token totals for _bill_dev_run to meter. Atomic
    (tmp + os.replace) so a kill mid-write leaves the previous complete file
    rather than a torn one, and best-effort: metering never fails a build.

    §cache accounting: Anthropic reports three disjoint input counters -
    `input_tokens` (fresh), `cache_creation_input_tokens` (written to cache, billed
    at 1.25x base) and `cache_read_input_tokens` (billed at 0.1x). The price table
    has columns for only two rates, so cache CREATION is folded into plain input
    and bills at 1.0x instead of 1.25x. That under-bills, by a bounded and known
    amount, and the price table's owner_todo carries the fix (a cache_write
    column). Reporting it any other way would over-bill instead, which the §spend
    floor makes worse than under-billing.
    """
    try:
        report = {
            "model": _model(),
            "input_tokens": int(usage.prompt_tokens),
            "output_tokens": int(usage.completion_tokens),
            "cached_input_tokens": int(usage.cache_read_tokens),
            # Anthropic's own costing for the run, cache-accurate and independent
            # of our price table. The worker ignores keys it does not know; the
            # benchmark reads it as the ground truth the table is checked against.
            "provider_cost_usd": round(float(usage.provider_cost_usd), 6),
        }
        path = OPENVISOR / "usage.json"
        tmp = path.with_name("usage.json.tmp")
        # The agent can delete .openvisor/ mid-run (it is gitignored, so a
        # `git clean -fdx` takes it out); recreate it or the run goes unbilled.
        OPENVISOR.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(report))
        os.replace(tmp, path)
        if not quiet:
            print(f"driver: usage {report}")
    except Exception as exc:  # noqa: BLE001
        print(f"driver: usage dump failed: {exc}", file=sys.stderr)


def _dump_error(exc: Exception) -> None:
    """Structured, secret-free crash report, categorised exactly like run_dev's so
    the worker's customer-facing copy does not have to know which harness ran."""
    try:
        text = f"{type(exc).__name__}: {exc}"
        key = os.environ.get("LLM_API_KEY") or ""
        if key:
            text = text.replace(key, "***")
        low = text.lower()
        if "authentication" in low or "401" in low or "invalid api key" in low:
            category, message = "llm_auth", f"the model endpoint rejected the API key ({text[:200]})"
        elif "connection" in low or "timeout" in low or "unreachable" in low:
            category, message = "llm_unreachable", f"the model endpoint could not be reached ({text[:200]})"
        elif "not found" in low and "claude" in low:
            # The CLI half of the SDK missing from the image is a harness bug, not
            # a customer-visible model problem - name it as one.
            category, message = "agent_error", f"the claude CLI is missing from the runner image ({text[:200]})"
        else:
            category, message = "agent_error", f"the build agent crashed ({text[:300]})"
        if _STDERR_TAIL:
            # The CLI's own words beat our category guess; the worker redacts and
            # the leak scan still gates anything that reaches a customer.
            tail = " | ".join(_STDERR_TAIL[-6:])
            if key:
                tail = tail.replace(key, "***")
            message = f"{message} [cli: {tail[:400]}]"
        OPENVISOR.mkdir(parents=True, exist_ok=True)
        (OPENVISOR / "error.json").write_text(
            json.dumps({"category": category, "message": message}))
        print(f"driver: error report written ({category})", file=sys.stderr)
    except Exception as dump_exc:  # noqa: BLE001
        print(f"driver: error report failed: {dump_exc}", file=sys.stderr)


def _transport(name: str, cfg) -> dict | None:
    """One staged MCP server in the Agent SDK's dialect, or None if it is unusable.

    The worker stages ONE mcp.json for every harness, in the spelling the OpenHands
    driver reads: `{"url": ..., "headers": {...}}`, with no transport field. The
    Claude CLI's config is a DISCRIMINATED union - an entry with a `url` and no
    `type` is not an HTTP server missing a hint, it is a stdio server missing its
    `command`, and the CLI SKIPS it. Silently, from the build's side: the session
    opens with an empty tool list and the agent works on without the browser,
    Context7, the connected KBs or the §Tools action servers. Verified against
    claude-code 2.1.251, which answers an untyped entry with `url_missing_type` and
    an empty `mcp_servers`, and registers the identical entry once `type` is there.

    Every server the platform stages today is HTTP; `command` is mapped anyway so a
    stdio server added later does not repeat this bug from the other direction.
    """
    if not isinstance(cfg, dict):
        return None
    if cfg.get("type"):
        return cfg
    if cfg.get("url"):
        return {**cfg, "type": "http"}
    if cfg.get("command"):
        return {**cfg, "type": "stdio"}
    print(f"driver: mcp server {name} has neither url nor command; skipped", file=sys.stderr)
    return None


def _mcp_servers() -> dict:
    """The project's MCP servers (Context7 + browser + connected KBs), as the
    worker staged them. Tolerates both the bare {name: cfg} map and the
    {"mcpServers": {...}} wrapper. A malformed file costs the agent its tools,
    never the build."""
    path = OPENVISOR / "mcp.json"
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text())
        servers = data.get("mcpServers", data) if isinstance(data, dict) else {}
        if not isinstance(servers, dict):
            return {}
        typed = {name: _transport(name, cfg) for name, cfg in servers.items()}
        return {name: cfg for name, cfg in typed.items() if cfg is not None}
    except Exception as exc:  # noqa: BLE001
        print(f"driver: mcp config unreadable ({exc}); running without MCP", file=sys.stderr)
        return {}


# What this agent loop is, in the agent's own terms. The CLI's default system
# prompt is written for an INTERACTIVE Claude Code session, where a background
# task notifies its user and the turn resumes. Nothing here does: `query()` ends
# when the agent stops acting, and the entrypoint publishes whatever is on disk.
# Production, 2026-08-30: the agent started `make dev` in the background, reached
# for ScheduleWakeup (refused - "/loop mode only"), then said "I'll wait for the
# background build to notify me" four times and ENDED THE SESSION, shipping a
# branch it had never verified.
_HEADLESS_NOTE = """\
Operating environment: this is a single headless session. Nothing will wake you \
up - there are no notifications, no scheduled resumes, and no turn after the one \
you are in. If you start work in the background, poll it yourself until it \
finishes. The session ends the moment you stop taking actions, and whatever is in \
the workspace at that point is what gets published, so finish your verification \
before you stop.

"""


def _prompt() -> str:
    """The task, plus any steering the worker left for a resumed run.

    §resume gap: run_dev.py rehydrates the previous SDK conversation so a resume
    costs only the new instruction. This driver does NOT yet restore an Agent SDK
    session, so a resumed run replays the whole task with the steering appended.
    That is correct but more expensive than the OpenHands path, and it is a real
    difference between the harnesses - do not read a resume-heavy cost comparison
    as a like-for-like result until sessions are wired up.
    """
    task = _HEADLESS_NOTE + (OPENVISOR / "task.md").read_text()
    steering = OPENVISOR / "steering.md"
    if steering.is_file():
        note = steering.read_text(errors="replace").strip()
        if note:
            return (task + "\n\n---\n\nNew guidance from the customer since the "
                    "previous session ended:\n\n" + note)
    return task


def _counter(obj, *names) -> int:
    """Read a usage counter that may be an attribute or a dict key, under any of
    several spellings. The SDK's ResultMessage shape is not pinned by our tests,
    so a field rename must degrade to 0 (an under-report the worker's
    'session not metered' warning surfaces) rather than crash a finished build."""
    for name in names:
        if isinstance(obj, dict):
            if obj.get(name) is not None:
                return int(obj[name] or 0)
        elif getattr(obj, name, None) is not None:
            return int(getattr(obj, name) or 0)
    return 0


def _absorb_turn(usage: _Usage, message) -> None:
    """Fold ONE assistant turn's usage into the running counters.

    ResultMessage carries the session TOTAL and arrives only at the very end, so
    until this existed the counters were zero for the whole run. Two consequences,
    both live in production on 2026-08-30: the build console showed 0 output
    tokens and ~0 credits while the agent worked, and - the serious one - a run
    stopped by the customer or killed by the deployer's wall clock never reached
    the ResultMessage, so the incremental usage.json snapshot the worker meters
    from reported zeros and `_bill_dev_run` bailed on `not (input_tokens or
    output_tokens)`. A 4M-token build would have billed NOTHING.

    Each API response's usage is what that call was charged, so summing across
    turns is the run's real total; `_absorb_usage` then OVERWRITES with the
    provider's own session figure, which is authoritative and covers turns this
    loop never sees (a subagent's, say).
    """
    raw = getattr(message, "usage", None)
    if not raw:
        return
    fresh = _counter(raw, "input_tokens", "inputTokens")
    created = _counter(raw, "cache_creation_input_tokens", "cacheCreationInputTokens")
    read = _counter(raw, "cache_read_input_tokens", "cacheReadInputTokens")
    usage.prompt_tokens += fresh + created + read
    usage.completion_tokens += _counter(raw, "output_tokens", "outputTokens")
    usage.cache_read_tokens += read
    usage.cache_write_tokens += created


def _absorb_usage(usage: _Usage, result) -> None:
    """Fold a ResultMessage's totals into the running counters. Assignment, not
    addition: this is the session total, and it REPLACES whatever `_absorb_turn`
    accumulated on the way."""
    raw = getattr(result, "usage", None)
    if raw is None:
        return
    fresh = _counter(raw, "input_tokens", "inputTokens")
    created = _counter(raw, "cache_creation_input_tokens", "cacheCreationInputTokens")
    read = _counter(raw, "cache_read_input_tokens", "cacheReadInputTokens")
    # See _dump_usage: every input token the provider charges for, however it is
    # tiered, has to reach the meter or the run bills short.
    usage.prompt_tokens = fresh + created + read
    usage.completion_tokens = _counter(raw, "output_tokens", "outputTokens")
    usage.cache_read_tokens = read
    usage.cache_write_tokens = created
    cost = getattr(result, "total_cost_usd", None)
    if cost is not None:
        usage.provider_cost_usd = float(cost)


def _report_session(feed, message) -> None:
    """Say what the session actually got, in the run log and the build console.

    The CLI drops an MCP server it cannot parse or reach and opens the session
    anyway with a smaller tool list, so a build that lost every tool is otherwise
    indistinguishable from one that had them - which is how an untyped mcp.json
    went unnoticed through the harness's first production run. The names are the
    platform's own (browser, context7, a KB's slug); the CLI's message text is
    ours too, but it goes to the log rather than the customer-visible feed.
    """
    data = getattr(message, "data", None)
    if not isinstance(data, dict):
        return
    ok = [s.get("name") for s in (data.get("mcp_servers") or [])
          if isinstance(s, dict) and s.get("status") == "connected"]
    bad = [s.get("name") for s in (data.get("mcp_servers") or [])
           if isinstance(s, dict) and s.get("status") != "connected"]
    skipped = [e.get("name") for e in (data.get("mcp_server_errors") or [])
               if isinstance(e, dict)]
    print(f"driver: mcp connected={ok} not_connected={bad} skipped={skipped}")
    for err in (data.get("mcp_server_errors") or []):
        if isinstance(err, dict):
            print(f"driver: mcp {err.get('name')}: {err.get('message')}", file=sys.stderr)
    lost = [n for n in (bad + skipped) if n]
    if lost:
        feed.append({"kind": "error",
                     "title": f"Tool server unavailable: {', '.join(lost[:6])}"})


# The build console's vocabulary, shared with the OpenHands driver's summarizer
# (live_events._summarize_action). The FEED KINDS ARE A CONTRACT: shared-ui's
# BuildFeed renders `title` and picks its icon off `kind`, so an event carrying
# `text`, or a kind outside this set, renders as a BLANK ROW with the fallback
# icon - which is exactly what the first Claude build looked like, every thought
# an empty line and every tool call an identical clock.
_TOOL_KINDS: tuple[tuple[tuple[str, ...], str, str], ...] = (
    (("Bash", "BashOutput", "KillShell"), "command", "Running a command"),
    (("Write", "Edit", "MultiEdit", "NotebookEdit"), "edit", "Editing a file"),
    (("Read", "Glob", "Grep", "LS"), "read", "Exploring the code"),
    (("WebFetch",), "browse", "Reading a page"),
    (("WebSearch",), "browse", "Searching the web"),
    (("TodoWrite", "ExitPlanMode"), "plan", "Updating the work plan"),
    (("Task",), "action", "Delegating to a subagent"),
)

# Tool inputs that name a target the customer can read without leaking anything a
# path or a command does not already leak - the OpenHands summarizer puts exactly
# these in its titles, and devfeed's redaction/withhold pass covers both drivers.
# A model-composed free-text argument (a web-search query, an MCP tool's args) is
# NOT here: it can carry knowledge-base text, and the leak-scan threat model keeps
# it out of the feed.
_TOOL_TARGETS = ("command", "file_path", "path", "pattern", "url", "notebook_path")


def _tool_event(name: str, args) -> dict:
    """One feed line for a tool call: the same kind/title vocabulary the other
    driver produces, so one console reads the same whichever harness ran."""
    if name.startswith("mcp__"):
        # Name only, never args: an MCP tool's arguments can embed KB/RAG text.
        parts = name.split("__")
        label = "/".join(p for p in parts[1:] if p) or name
        return {"kind": "action", "title": f"MCP tool: {label}"[:live_events.TITLE_MAX]}
    kind, fallback = "action", f"Using {name}"
    for names, k, default in _TOOL_KINDS:
        if name in names:
            kind, fallback = k, default
            break
    target = ""
    if isinstance(args, dict) and name != "WebSearch":
        for key in _TOOL_TARGETS:
            if args.get(key):
                target = live_events._clip(args[key], live_events.TITLE_MAX)
                break
    return {"kind": kind, "title": target or fallback}


def _events_for(message) -> list[dict]:
    """The sanitized build-console lines for one SDK message. Deliberately coarse:
    the feed is customer-visible and passes through devfeed's secret/KB stripping,
    so it carries what the agent DID, never raw tool payloads.

    Every block is reported, not just the first: an assistant turn that thinks and
    then calls a tool is two lines, and returning one used to drop the other.
    """
    kind = type(message).__name__
    if kind == "AssistantMessage":
        out = []
        for block in getattr(message, "content", []) or []:
            name = getattr(block, "name", None)
            if name:  # a tool use block
                out.append(_tool_event(name, getattr(block, "input", None)))
                continue
            text = getattr(block, "text", None)
            if text:
                out.append({"kind": "think",
                            "title": live_events._clip(text, live_events.DETAIL_MAX)})
        return out
    if kind == "ResultMessage":
        return [{"kind": "phase", "title": "Agent session finished"}]
    return []


async def _run(feed, usage: _Usage) -> tuple[bool, int | None]:
    """Drive the agent loop. Returns (hit_turn_cap, turn_limit)."""
    from claude_agent_sdk import ClaudeAgentOptions, PermissionResultAllow, query

    async def _approve(tool_name, tool_input, context):  # noqa: ANN001 - SDK callback
        """Approve every tool call, programmatically.

        The container IS the boundary here (leak scan, egress lockdown, no host
        mounts, a throwaway workspace), so there is nothing for a prompt to
        protect - and a prompt in a headless build just hangs until the deployer
        kills the run. The obvious way to say that, permission_mode
        "bypassPermissions", is NOT usable: it maps to the CLI's
        --dangerously-skip-permissions, which refuses to run as root, and the
        runner is root (it drives apt, git and an inner dockerd). This callback
        reaches the same place without the flag, and gives a future policy a real
        seam - deny by returning PermissionResultDeny instead.
        """
        return PermissionResultAllow()

    max_turns = int(os.environ.get("LLM_MAX_ITERATIONS") or 0)
    budget = float(os.environ.get("DEV_RUN_MAX_USD") or 0)
    # §dev harness: the endpoint's configured reasoning effort, which this driver
    # used to drop on the floor - an admin who set an endpoint to "xhigh" got the
    # provider default on every Claude build. Whitelisted rather than forwarded:
    # a value the SDK's literal does not accept would fail the session, and the
    # platform's own column is free text.
    effort = (os.environ.get("LLM_REASONING_EFFORT") or "").strip().lower()
    options = ClaudeAgentOptions(
        model=_model(),
        cwd="/workspace",
        effort=effort if effort in ("low", "medium", "high", "xhigh", "max") else None,
        # Tools that only mean something inside an interactive Claude Code
        # session: they either promise a wake-up that cannot happen here (see
        # _HEADLESS_NOTE), address agents and people this run has no channel to,
        # or - Workflow - fan out to a swarm of agents on a customer's bill.
        disallowed_tools=["ScheduleWakeup", "CronCreate", "CronDelete", "CronList",
                          "PushNotification", "SendMessage", "ListAgents",
                          "DesignSync", "Workflow"],
        # Edits are settled by the mode so they never round-trip through the
        # callback; everything else (Bash above all) lands on _approve.
        permission_mode="acceptEdits",
        can_use_tool=_approve,
        # §spend floor, per run: a hard provider-side ceiling, complementing the
        # iteration cap and the deployer's wall clock. 0 = unset (the platform's
        # existing caps still bound the run).
        max_budget_usd=budget or None,
        stderr=_capture_stderr,
        max_turns=max_turns or None,
        mcp_servers=_mcp_servers(),
        # §customer settings are NOT ours to load. /workspace is the customer's
        # repository: letting the SDK read its .claude/ would let a customer repo
        # inject hooks, skills and commands into a build running with our
        # credentials. Explicit empty list, never the ambient default.
        setting_sources=[],
    )
    hit_cap = False
    async for message in query(prompt=_prompt(), options=options):
        if (type(message).__name__ == "SystemMessage"
                and str(getattr(message, "subtype", "")) == "init"):
            _report_session(feed, message)
        if type(message).__name__ == "AssistantMessage":
            _absorb_turn(usage, message)
        for ev in _events_for(message):
            feed.append(ev)
        feed.dump_progress()
        if type(message).__name__ == "ResultMessage":
            _absorb_usage(usage, message)
            subtype = str(getattr(message, "subtype", "") or "")
            if "max_turn" in subtype or "max_iteration" in subtype:
                hit_cap = True
    return hit_cap, (max_turns or None)


def _apply_base_url(raw: str | None) -> None:
    """Map the platform's LLM_BASE_URL onto ANTHROPIC_BASE_URL, or leave the SDK's
    default alone.

    Two traps, both silent. The platform stores an OpenAI-shaped base URL ending
    in `/v1` (that is what every other harness needs), but the Anthropic client
    appends its own version segment - forwarding it verbatim asks for
    `/v1/v1/messages` and 404s every call. And pointing this driver at the
    canonical Anthropic host is a no-op worth skipping entirely, so a stray
    trailing slash or a `/v1` in the admin's endpoint row cannot break a build
    that would otherwise work. Anything else is a genuine gateway and is passed
    through with only the version segment trimmed.
    """
    base = (raw or "").strip().rstrip("/")
    if not base:
        return
    if base.endswith("/v1"):
        base = base[: -len("/v1")]
    from urllib.parse import urlparse
    if (urlparse(base).hostname or "").lower() == "api.anthropic.com":
        return  # the SDK default already points here
    os.environ["ANTHROPIC_BASE_URL"] = base
    print(f"driver: routing through {base}")


def main() -> int:
    # A previous session's marker must never describe this run.
    (OPENVISOR / "exit_reason.json").unlink(missing_ok=True)
    # The SDK authenticates from the environment; the platform passes the key as
    # LLM_API_KEY for every harness. Set it here rather than in the entrypoint so
    # the variable exists for exactly the process that needs it.
    os.environ["ANTHROPIC_API_KEY"] = os.environ["LLM_API_KEY"]
    _apply_base_url(os.environ.get("LLM_BASE_URL"))

    usage = _Usage()
    feed = live_events.LiveFeed(OPENVISOR, llm=_LLMShim(usage), model=_model(),
                                on_snapshot=lambda: _dump_usage(usage, quiet=True))
    print(f"driver: starting claude-agent-sdk session (model={_model()})")
    started = time.time()
    hit_cap, limit = False, None
    try:
        hit_cap, limit = asyncio.run(_run(feed, usage))
    finally:
        # Bill whatever was spent, even when the run errors or hits the cap.
        _dump_usage(usage)
        try:
            feed.dump_progress(force=True)
        except Exception:  # noqa: BLE001
            pass
    if hit_cap:
        try:
            (OPENVISOR / "exit_reason.json").write_text(
                json.dumps({"reason": "max_iterations", "limit": limit}))
            print(f"driver: session ended at the turn cap ({limit})")
        except Exception as exc:  # noqa: BLE001 - copy plumbing never fails the run
            print(f"driver: exit-reason dump failed: {exc}", file=sys.stderr)
    print(f"driver: conversation finished in {round(time.time() - started)}s")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 - setup failures before the run loop
        _dump_error(exc)
        raise
