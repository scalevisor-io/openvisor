"""Openvisor OpenHands dev driver (SDK v1). Runs one headless build against the
mounted /workspace using the platform's OpenAI-compatible model, the default
tool preset, and the project's MCP servers (Context7 + browser + connected KBs).
Reads its inputs
from /workspace/.openvisor/."""
import json
import os
import shutil
import sys
import time
import uuid
from pathlib import Path

from openhands.sdk import LLM, Conversation
from openhands.sdk.context.condenser import LLMSummarizingCondenser
from openhands.sdk.tool import Tool
from openhands.tools.glob import GlobTool  # importing auto-registers the tool
from openhands.tools.grep import GrepTool  # importing auto-registers the tool
from openhands.tools.preset.default import get_default_agent
from openhands.tools.task import TaskToolSet  # importing auto-registers the tool

import live_events
import tool_args

OPENVISOR = Path("/workspace/.openvisor")

# §exploration budget: how many tool calls one agent step may run at once. The
# SDK ships 1 (strictly sequential), so every file read costs a full model
# round-trip: a metered production run spent 373 reads and 118 shell commands on
# 44 distinct files, and reads were ~30% of all its actions. Batching the
# independent ones collapses those round-trips - and the re-sent context each one
# would have carried. The SDK's caveat is real and is why this stays small and
# tunable: concurrent tools share the conversation, filesystem and working
# directory, so batched MUTATIONS can race. 1 restores the SDK default.
TOOL_CONCURRENCY = int(os.environ.get("DEV_TOOL_CONCURRENCY") or 3)


# §observation budget: how much text ONE tool result may keep in the conversation.
# The SDK clips at 50 000 chars (DEFAULT_TEXT_CONTENT_LIMIT) and throws the
# overflow away, which on a real run is a browser page snapshot: a metered
# production build carried three ~50 kB snapshots - ~37 k tokens - re-sent on 85
# subsequent calls, and with the widened condenser window they never aged out.
# Clipping smaller costs nothing here because the SDK was already truncating;
# what changes is that the FULL text is written to a file the agent is told about,
# so a clipped table stays greppable instead of being lost mid-page. The dir is
# per-container scratch, never the workspace: nothing here is a deliverable and
# nothing here may reach a commit. 0 restores the SDK default.
OBS_TEXT_LIMIT = int(os.environ.get("DEV_OBS_TEXT_LIMIT") or 20000)
OBS_OVERFLOW_DIR = "/tmp/tool_output"

# §14.5 per-command cap (seconds), set by the platform from DEV_CMD_TIMEOUT_S.
# Applied in _install_tool_call_repairs to every terminal call the model left
# unbounded - see tool_args.default_timeout for why the action, not the tool,
# is where SDK 1.8.0 keeps this. 0 disables (the SDK's 30 s idle return alone).
CMD_TIMEOUT_S = int(os.environ.get("DEV_CMD_TIMEOUT_S") or 600)


def _tuned_observation_limit() -> None:
    """Clip oversized tool results harder, but persist the full text and point
    the agent at it. Best-effort: on any SDK drift leave the stock behaviour."""
    if OBS_TEXT_LIMIT <= 0:
        return
    try:
        from openhands.sdk.llm import message as oh_message
        from openhands.sdk.utils import maybe_truncate

        limit = OBS_TEXT_LIMIT

        def _truncate(self, text: str) -> str:
            if not text or len(text) <= limit:
                return text
            return maybe_truncate(text, limit, save_dir=OBS_OVERFLOW_DIR,
                                  tool_prefix="tool")

        oh_message.Message._maybe_truncate_tool_text = _truncate
        print(f"driver: tool-result window {oh_message.DEFAULT_TEXT_CONTENT_LIMIT} "
              f"-> {limit} (overflow persisted to {OBS_OVERFLOW_DIR})")
    except Exception as exc:  # noqa: BLE001
        print(f"driver: tool-result window untouched: {exc}", file=sys.stderr)


# §delegated page reading: the sub-agent roster the Task tool delegates TO.
# `task_tool_set` ships with an EMPTY roster (`get_factory_info()` returns "No
# user-registered agents yet"), so enabling the tool alone gave the agent a
# delegation tool with nothing to delegate to. A third-party page is the read
# that pays for delegation: one pricing-page snapshot came back at ~50 kB and
# rode every later call of the run, for two numbers. The researcher browses in
# ITS OWN context and returns the facts. It gets the web-reading MCP servers and
# NO local tools - it cannot read, edit or run anything in the workspace, so a
# page it opened can never reach a commit. The SDK ships a worked example keyed
# on this exact name, which lands in the tool description for free.
WEB_AGENT = "web researcher"
WEB_MCP_PREFIXES = ("browser", "websearch")
WEB_AGENT_ITERATIONS = int(os.environ.get("DEV_WEB_AGENT_ITERATIONS") or 30)
WEB_AGENT_DESCRIPTION = (
    "Reads pages on the public web (browser + web search) and returns the facts "
    "asked for. Use it for every third-party page you need a value off - pricing "
    "tables, API docs, changelogs, status pages - so the page itself never enters "
    "this conversation. It has no access to the workspace, cannot run commands and "
    "cannot see the app you are building."
)
WEB_AGENT_PROMPT = (
    "You read pages so that someone else does not have to. Navigate to the page, "
    "find what was asked for, and answer with the values and the URL you read them "
    "off - a short list or table, never the page. Report a figure ONLY if you read "
    "it on the page: if the page did not load, hid the value behind JavaScript you "
    "could not reach, or shows something other than what was asked, say exactly "
    "that instead of filling the gap from memory or from a search-result snippet. "
    "Note anything adjacent that changes what the value means - a tier or threshold "
    "above the quoted rate, a different currency, a promotional or committed-use "
    "price next to the list price - because the caller cannot see the page."
)


def _register_web_researcher(mcp_cfg: dict) -> None:
    """Register the page-reading sub-agent with the web MCP servers this run
    actually has. Best-effort: on any SDK drift the main agent keeps browsing
    itself, exactly as before."""
    servers = {name: spec
               for name, spec in (mcp_cfg.get("mcpServers") or {}).items()
               if name.startswith(WEB_MCP_PREFIXES)}
    if not servers:
        return
    try:
        from openhands.sdk import agent_definition_to_factory, register_agent
        from openhands.sdk.subagent import AgentDefinition

        definition = AgentDefinition(
            name=WEB_AGENT,
            description=WEB_AGENT_DESCRIPTION,
            system_prompt=WEB_AGENT_PROMPT,
            mcp_servers=servers,
            max_iteration_per_run=WEB_AGENT_ITERATIONS,
        )
        register_agent(WEB_AGENT, agent_definition_to_factory(definition), definition)
        print(f"driver: sub-agent {WEB_AGENT!r} registered ({', '.join(sorted(servers))})")
    except Exception as exc:  # noqa: BLE001
        print(f"driver: web sub-agent not registered: {exc}", file=sys.stderr)


class RetryingCondenser(LLMSummarizingCondenser):
    """The condensation call is the ONE chat-completions request in a build (agent
    steps ride the Responses API for gpt-5-family models) and litellm never retries
    4xx - so a single transient provider rejection there (OpenAI has intermittently
    403'd gpt-5.6 chat completions) killed whole runs. Bounded retry; the last
    error still propagates."""

    def get_condensation(self, *args, **kwargs):
        for attempt in range(3):
            try:
                return super().get_condensation(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001
                if attempt == 2:
                    raise
                print(f"driver: condensation attempt {attempt + 1} failed "
                      f"({type(exc).__name__}); retrying", file=sys.stderr)
                time.sleep(10 * (attempt + 1))


# §working memory: how many conversation events the agent keeps before the
# condenser halves its history. The SDK preset's default (measured: 80) gives the
# agent a ~40-event working memory, which on a real build means it forgets what it read
# and reads it again: one metered production run condensed 68 times in 60 minutes
# and issued 373 file reads over 44 distinct files - an 8.5x re-read factor, with
# its own task file re-read 16 times, for a single edit. Every condensation also
# rewrites the prefix, so it discards the prompt cache the run had built (that run
# still held an 80% cache-read rate, which is what makes a WIDER window the cheap
# option: re-sending a cached prefix costs a tenth of re-deriving it, and costs no
# agent step at all). Widened, not removed - condensation is still the backstop
# against a context-window overflow, which fails the build outright.
CONDENSER_MAX_SIZE = int(os.environ.get("DEV_CONDENSER_MAX_SIZE") or 240)


def _tuned_condenser(agent):
    """Swap the preset condenser for the retrying one and widen its window,
    field-for-field. Best-effort: on any SDK drift keep the stock condenser rather
    than fail the build."""
    try:
        base = agent.condenser
        if not isinstance(base, LLMSummarizingCondenser):
            return agent
        fields = {f: getattr(base, f) for f in LLMSummarizingCondenser.model_fields}
        if "max_size" in fields and CONDENSER_MAX_SIZE > 0:
            print(f"driver: condenser window {fields['max_size']} -> {CONDENSER_MAX_SIZE}")
            fields["max_size"] = CONDENSER_MAX_SIZE
        wrapped = RetryingCondenser(**fields)
        return agent.model_copy(update={"condenser": wrapped})
    except Exception as exc:  # noqa: BLE001
        print(f"driver: condenser tuning skipped: {exc}", file=sys.stderr)
        return agent


ANTHROPIC_HOST = "api.anthropic.com"

# §anthropic caching: model ids the PINNED SDK's capability table predates.
# openhands-ai 1.8.0 stops at claude-opus-4-8 / claude-sonnet-4-6, so every newer
# Claude id reports supports_prompt_cache=False and the SDK emits NO cache_control
# breakpoints - measured: a Sonnet 5 build reached 595k input tokens at a 0.00%
# cache hit rate, where the same task through a caching path cached 86% of its
# input. At Anthropic's 0.1x cache-read rate that is most of the bill. The table
# is a plain module-level list and the lookup is not memoised, so appending to it
# at startup is enough; keep this list in sync when the SDK pin moves, and drop
# entries once the SDK ships them.
_CACHE_CAPABLE_ADDITIONS = ("claude-sonnet-5", "claude-opus-5", "claude-fable-5")


def _register_cache_capable_models() -> None:
    """Teach the pinned SDK that current Claude models support prompt caching.
    Best-effort: on any SDK drift the run proceeds uncached rather than dying."""
    try:
        import openhands.sdk.llm.utils.model_features as mf
        added = [m for m in _CACHE_CAPABLE_ADDITIONS if m not in mf.PROMPT_CACHE_MODELS]
        if not added:
            return
        mf.PROMPT_CACHE_MODELS.extend(added)
        for fn in (getattr(mf, "get_features", None),
                   getattr(mf, "_normalized_supported_openai_params", None)):
            if fn is not None and hasattr(fn, "cache_clear"):
                fn.cache_clear()
        print(f"driver: registered prompt-cache support for {', '.join(added)}")
    except Exception as exc:  # noqa: BLE001
        print(f"driver: could not extend the prompt-cache model table: {exc}",
              file=sys.stderr)


def _is_anthropic(base_url: str | None) -> bool:
    if not base_url:
        return False
    try:
        from urllib.parse import urlparse
        return (urlparse(base_url).hostname or "").lower() == ANTHROPIC_HOST
    except Exception:  # noqa: BLE001
        return False


def _model_for_litellm(model: str, base_url: str | None = None) -> str:
    """The LiteLLM routing name.

    Anthropic is special-cased because provider choice decides whether prompt
    caching can work at all. Anthropic serves an OpenAI-compatible surface, so
    the generic `openai/` route SUCCEEDS against it and silently drops the
    cache_control breakpoints the SDK emits - the request is OpenAI-shaped and
    those blocks are not part of that shape. Routing through LiteLLM's native
    anthropic provider is what makes them mean something. Everything else keeps
    the openai provider, which honours base_url for arbitrary gateways.
    """
    if "/" in model:
        return model  # already provider-qualified; the caller meant it
    if _is_anthropic(base_url):
        return f"anthropic/{model}"
    return f"openai/{model}"


# Providers where sending prompt_cache_key is known-safe: Mistral needs it to
# opt IN to prompt caching (cache reads then come back in usage and bill at the
# §18 cached rate); OpenAI accepts it as an official hit-rate hint. Everyone
# else: never send it - a strict OpenAI-compatible gateway may 400 on unknown
# params, which would fail every call of the build.
_CACHE_KEY_HOSTS = {"api.mistral.ai", "api.openai.com"}


def _cache_key_supported(base_url: str | None) -> bool:
    if not base_url:
        return True  # no override = LiteLLM's default OpenAI endpoint
    try:
        from urllib.parse import urlparse
        return (urlparse(base_url).hostname or "") in _CACHE_KEY_HOSTS
    except Exception:  # noqa: BLE001
        return False


def _dump_usage(llm: LLM, quiet: bool = False) -> None:
    """Report the run's accumulated LLM usage for the platform to bill
    (§14.6). Written incrementally during the run (§Phase 1) so a hard
    deployer timeout kill (SIGKILL, no finally) still leaves a recent report
    for _bill_dev_run to meter - closing the unbilled-COGS hole. Atomic
    (tmp + os.replace) so a kill mid-write leaves the prior complete file, never
    a torn one. Best-effort: never fail the build over metering."""
    try:
        tu = llm.metrics.accumulated_token_usage
        usage = {
            # the pricing table is keyed on the raw api_model, not the
            # LiteLLM-prefixed routing name
            "model": os.environ["LLM_MODEL"].split("/")[-1],
            "input_tokens": int(getattr(tu, "prompt_tokens", 0) or 0),
            "output_tokens": int(getattr(tu, "completion_tokens", 0) or 0),
            # prompt-cache READS (subset of prompt_tokens) - billed at the
            # model's cached rate (§18)
            "cached_input_tokens": int(getattr(tu, "cache_read_tokens", 0) or 0),
            # ...and WRITES, billed at the model's cache_write rate. The SDK gives
            # one write counter with no TTL split, so it meters as the 5-minute
            # tier - the cheaper of the two, and the one LiteLLM asks for.
            "cache_write_tokens": int(getattr(tu, "cache_write_tokens", 0) or 0),
        }
        path = OPENVISOR / "usage.json"
        tmp = path.with_name("usage.json.tmp")
        # The agent can delete .openvisor/ mid-run (it is gitignored, so a
        # `git clean -fdx` takes it out); recreate it or the run goes unbilled.
        OPENVISOR.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(usage))
        os.replace(tmp, path)  # atomic swap
        if not quiet:
            print(f"driver: usage {usage}")
    except Exception as exc:  # noqa: BLE001
        print(f"driver: usage dump failed: {exc}", file=sys.stderr)


def _dump_error(exc: Exception) -> None:
    """Leave a structured, secret-free crash report for the worker to surface in
    chat - a driver death used to reach the customer as an opaque 'Runner exited
    1' while the real cause (e.g. the endpoint 400-rejecting the model id) sat
    buried in the log. Best-effort; never masks the original failure."""
    try:
        text = f"{type(exc).__name__}: {exc}"
        for name in ("LLM_API_KEY",):
            val = os.environ.get(name) or ""
            if val:
                text = text.replace(val, "***")
        low = text.lower()
        if "invalid model" in low or "model_not_found" in low or "does not exist" in low:
            category = "llm_model"
            message = ("the model endpoint rejected the configured model "
                       f"({text[:200]})")
        elif "authentication" in low or "401" in low or "invalid api key" in low:
            category = "llm_auth"
            message = f"the model endpoint rejected the API key ({text[:200]})"
        elif "connection" in low or "timeout" in low or "unreachable" in low:
            category = "llm_unreachable"
            message = f"the model endpoint could not be reached ({text[:200]})"
        else:
            category = "agent_error"
            message = f"the build agent crashed ({text[:300]})"
        (OPENVISOR / "error.json").write_text(
            json.dumps({"category": category, "message": message}))
        print(f"driver: error report written ({category})", file=sys.stderr)
    except Exception as dump_exc:  # noqa: BLE001
        print(f"driver: error report failed: {dump_exc}", file=sys.stderr)


def _with_images(text: str):
    """Attach the worker-staged conversation screenshots (§chat images →
    sandbox: .openvisor/images.json, vision-gated worker-side) to a user
    message as inline data URIs, so "fix what this screenshot shows" reaches
    the model as pixels. Plain text when there is nothing staged or the SDK
    refuses the shape - an image must never fail a build."""
    manifest = OPENVISOR / "images.json"
    if not manifest.is_file():
        return text
    try:
        import base64
        from openhands.sdk.llm import ImageContent, Message, TextContent
        urls = []
        for entry in json.loads(manifest.read_text())[:4]:
            data = (Path("/workspace") / entry["path"]).read_bytes()
            ctype = entry.get("content_type", "image/png")
            urls.append(f"data:{ctype};base64,{base64.b64encode(data).decode()}")
        if not urls:
            return text
        print(f"driver: attaching {len(urls)} conversation screenshot(s)", flush=True)
        return Message(role="user",
                       content=[TextContent(text=text), ImageContent(image_urls=urls)])
    except Exception as exc:  # noqa: BLE001
        print(f"driver: screenshot attach skipped: {exc}", file=sys.stderr)
        return text


def _discard_token(_chunk) -> None:
    """§streaming: the SDK only streams when a token callback is present."""


def _install_tool_call_repairs(conversation) -> None:
    """§tool-call repairs: wrap the SDK's three seams between a parsed tool
    call and its validation with the `tool_args` rules - the argument fixer
    (stray keys, wrong literals), the MCP action builder (schema defaults,
    stray keys on MCP tools) and the name normalizer (prefixed names) - so a
    `terminal` call carrying a stray `description` runs instead of costing a
    round-trip. The roster the rules need comes off the agent's `tools_map`,
    which exists only once the SDK has initialized the agent - lazily, on the
    first message - so it is read on the first repaired call, not here; if it
    cannot be read, every seam is restored and the run validates exactly as
    the SDK shipped it."""
    try:
        from openhands.sdk.agent import agent as agent_mod
        from openhands.sdk.mcp import tool as mcp_tool_mod
        original_fix = agent_mod.fix_malformed_tool_arguments
        original_normalize = agent_mod.normalize_tool_call
        original_mcp_action = mcp_tool_mod.MCPToolDefinition.action_from_arguments
    except Exception as exc:  # noqa: BLE001
        print(f"driver: tool-call repairs not installed: {exc}", file=sys.stderr)
        return
    roster: set[str] | None = None

    def _log(text: str) -> None:
        print(f"driver: {text}", file=sys.stderr)

    def _stand_down(exc: Exception) -> None:
        _log(f"tool-call repairs stood down: {exc}")
        agent_mod.fix_malformed_tool_arguments = original_fix
        agent_mod.normalize_tool_call = original_normalize
        mcp_tool_mod.MCPToolDefinition.action_from_arguments = original_mcp_action

    def _roster() -> set[str]:
        nonlocal roster
        if roster is None:
            found: set[str] = set()
            for tool in conversation.agent.tools_map.values():
                mcp_tool = getattr(tool, "mcp_tool", None)
                if mcp_tool is not None:
                    found.update((getattr(mcp_tool, "inputSchema", None) or {})
                                 .get("properties", {}).keys())
                found.update(_action_fields(tool.action_type))
            roster = found
        return roster

    def _fixed(arguments, action_type):
        arguments = original_fix(arguments, action_type)
        try:
            fields = _roster()
        except Exception as exc:  # noqa: BLE001 - unknown roster: no repair at all
            _stand_down(exc)
            return arguments
        arguments, dropped = tool_args.repair(arguments, _action_fields(action_type),
                                              fields, action_type.model_validate)
        if dropped:
            _log(f"dropped stray argument(s) {dropped} from a {action_type.__name__} call")
        for name, allowed in _literal_fields(action_type).items():
            arguments, replacement = tool_args.repair_literal(
                arguments, name, allowed, action_type.model_validate)
            if replacement:
                _log(f"read {name}={replacement!r} on a {action_type.__name__} call")
        # §14.5 per-command cap: a terminal call with no timeout of its own gets
        # the platform's, so a foreground server can block one step, not the run.
        arguments, capped = tool_args.default_timeout(
            arguments, action_type.__name__, _action_fields(action_type), CMD_TIMEOUT_S)
        if capped:
            _log(f"bounded an unbounded {action_type.__name__} call at {CMD_TIMEOUT_S}s")
        return arguments

    def _mcp_action(self, arguments):
        try:
            fields = _roster()
        except Exception as exc:  # noqa: BLE001
            _stand_down(exc)
            return original_mcp_action(self, arguments)
        schema = getattr(self.mcp_tool, "inputSchema", None) or {}
        arguments, filled = tool_args.fill_schema_defaults(arguments, schema)
        if filled:
            _log(f"filled schema default(s) {filled} on a {self.name} call")

        def _validate(candidate):
            mcp_tool_mod._create_mcp_action_type(self.mcp_tool).model_validate(
                {k: v for k, v in candidate.items() if v is not None})
        arguments, dropped = tool_args.repair(
            arguments, set((schema.get("properties") or {}).keys()), fields, _validate)
        if dropped:
            _log(f"dropped stray argument(s) {dropped} from a {self.name} call")
        return original_mcp_action(self, arguments)

    def _normalized(tool_name, arguments, available_tools):
        fixed = tool_args.repair_tool_name(tool_name, available_tools)
        if fixed:
            _log(f"tool {tool_name!r} read as {fixed!r}")
            tool_name = fixed
        return original_normalize(tool_name, arguments, available_tools)

    agent_mod.fix_malformed_tool_arguments = _fixed
    agent_mod.normalize_tool_call = _normalized
    mcp_tool_mod.MCPToolDefinition.action_from_arguments = _mcp_action


def _keep_mcp_streams_alive() -> None:
    """§MCP stream keepalive: the MCP client's standalone GET (SSE) stream - the
    only path a server has to the client between calls - reads with a 300 s
    timeout and gives up after two reconnects, so an MCP server that stays quiet
    for ten minutes loses it for the rest of the run. Playwright is quiet until
    its backend exists, and creates the backend on the FIRST tool call: a run
    that explores for a while before its first browser call reaches that call
    with a dead stream, the server's `listRoots()` on the way in cannot reach
    the client (60 s stall: the server's request timeout), the heartbeat it then
    starts cannot either, and the session is closed 5 s later - every later
    browser call of the run dead. Both knobs are module constants read at use:
    an idle stream is not an error for as long as a run can live, and a dropped
    one keeps reconnecting (one attempt a second). Set BEFORE the agent builds
    its MCP client, i.e. before the Conversation exists."""
    try:
        from mcp.client import streamable_http
        from mcp.shared import _httpx_utils
        streamable_http.MAX_RECONNECTION_ATTEMPTS = 10_000
        _httpx_utils.MCP_DEFAULT_SSE_READ_TIMEOUT = 24 * 3600
    except Exception as exc:  # noqa: BLE001 - a client without the knobs keeps its defaults
        print(f"driver: MCP stream keepalive not applied: {exc}", file=sys.stderr)


# §MCP session recovery: how long the run waits before rebuilding its MCP client
# a second time - a server that is really down must not turn every call into a
# reconnect attempt.
MCP_RECONNECT_MIN_INTERVAL_S = 60.0


def _install_mcp_session_recovery(conversation) -> None:
    """§MCP session recovery: an MCP server that drops a session mid-run leaves
    every later call of that server failing in 0.1 s - "Session terminated"
    (the transport's 404 path), then "Unknown tool" once the client's proxy
    loses the mount - and neither the SDK nor its client ever reconnects: a
    production run burned 25 such calls and 15 minutes retrying. Playwright's
    heartbeat closes a session whose ping goes unanswered for 5 s (the browser
    sidecar now tolerates 120 s), a sidecar restart loses them all, and any
    server may drop one for its own reasons. So the MCP executor is wrapped:
    on that signature the SDK's MCP client is rebuilt from the agent's own
    `mcp_config`, every executor of the old client is re-pointed at the new one
    and the call is retried ONCE. What lived inside the old session (the page
    the agent had open) is gone - the model sees a fresh browser and
    re-navigates - the one-turn cost the run could not pay before."""
    try:
        from openhands.sdk.mcp import tool as mcp_tool_mod
        from openhands.sdk.mcp.utils import create_mcp_tools
        executor_cls = mcp_tool_mod.MCPToolExecutor
        original_call = executor_cls.__call__
    except Exception as exc:  # noqa: BLE001
        print(f"driver: MCP session recovery not installed: {exc}", file=sys.stderr)
        return
    last_reconnect = [0.0]

    def _lost(observation) -> bool:
        if not getattr(observation, "is_error", False):
            return False
        text = " ".join(getattr(c, "text", "") or ""
                        for c in (getattr(observation, "content", None) or []))
        return "Session terminated" in text or "Unknown tool" in text

    def _reconnect(old_client) -> bool:
        now = time.time()
        if now - last_reconnect[0] < MCP_RECONNECT_MIN_INTERVAL_S:
            return False
        last_reconnect[0] = now
        fresh = create_mcp_tools(conversation.agent.mcp_config, 30)
        moved = 0
        for tool in conversation.agent.tools_map.values():
            executor = getattr(tool, "executor", None)
            if isinstance(executor, executor_cls) and executor.client is old_client:
                executor.client = fresh
                moved += 1
        try:
            old_client.sync_close()
        except Exception:  # noqa: BLE001 - the old session is dead anyway
            pass
        print(f"driver: MCP session lost - client rebuilt, {moved} tool(s) re-pointed",
              file=sys.stderr)
        return moved > 0

    def _call(self, action, conversation_=None):
        observation = original_call(self, action, conversation_)
        if _lost(observation):
            try:
                if _reconnect(self.client):
                    observation = original_call(self, action, conversation_)
            except Exception as exc:  # noqa: BLE001 - the original error stands
                print(f"driver: MCP reconnect failed: {exc}", file=sys.stderr)
        return observation

    executor_cls.__call__ = _call


def _literal_fields(action_type) -> dict[str, set]:
    """The action's Literal-typed fields and their allowed values."""
    import types
    import typing
    out: dict[str, set] = {}
    for name, info in getattr(action_type, "model_fields", {}).items():
        ann = getattr(info, "annotation", None)
        if typing.get_origin(ann) is typing.Annotated:
            ann = typing.get_args(ann)[0]
        if typing.get_origin(ann) in (typing.Union, types.UnionType):
            members = [a for a in typing.get_args(ann) if a is not type(None)]
            ann = members[0] if len(members) == 1 else ann
        if typing.get_origin(ann) is typing.Literal:
            out[name] = set(typing.get_args(ann))
    return out


def _action_fields(action_type) -> set[str]:
    fields = set()
    for name, info in getattr(action_type, "model_fields", {}).items():
        fields.add(name)
        if getattr(info, "alias", None):
            fields.add(info.alias)
    return fields


def main() -> int:
    task = (OPENVISOR / "task.md").read_text()
    # A previous session's end-of-run marker must never drive this run's copy.
    (OPENVISOR / "exit_reason.json").unlink(missing_ok=True)
    # §effort: providers that don't know reasoning_effort must not fail the
    # build - LiteLLM drops unsupported params instead of erroring.
    try:
        import litellm
        litellm.drop_params = True
    except Exception as exc:  # noqa: BLE001
        print(f"driver: litellm drop_params unavailable: {exc}", file=sys.stderr)
    # §LLM transient errors: the SDK retries connection errors, 429, 500, 503
    # and timeouts - not litellm's BadGatewayError (a 502 from the gateway in
    # front of the model) nor its generic APIError (the 52x family a CDN edge
    # answers with), and one such blip used to end a whole build with the
    # work unpublished. The SDK's retry decorator reads a module constant on
    # every call, so widening it here gives those the same bounded backoff.
    try:
        import litellm
        import openhands.sdk.llm.llm as _sdk_llm
        extra = tuple(e for e in (getattr(litellm, "BadGatewayError", None),
                                  getattr(litellm, "APIError", None))
                      if isinstance(e, type) and e not in _sdk_llm.LLM_RETRY_EXCEPTIONS)
        _sdk_llm.LLM_RETRY_EXCEPTIONS = (*_sdk_llm.LLM_RETRY_EXCEPTIONS, *extra)
    except Exception as exc:  # noqa: BLE001 - an SDK without the constant: provider default
        print(f"driver: LLM retry set unchanged: {exc}", file=sys.stderr)
    # §streaming: every model call streams. A non-streaming answer is one silent
    # connection from the request until its last token, and any idle timeout on
    # the path cuts it while the model keeps generating for nobody - a load
    # balancer's 50 s default, a CDN's origin timeout, the SDK's own 300 s request
    # timeout - and the retry then adds a second stream beside the orphan. A
    # reasoning model answers a hard coding step in 100-570 s, so on such a path
    # nearly every real step failed (2026-08-27: two runs lost, the provider at 9
    # streams for 2 agents). Bytes that flow from the first token trip none of
    # those timers, and the SDK rebuilds the same response from the chunks, usage
    # included (it forces stream_options.include_usage). The SDK refuses to
    # stream without a token callback, and the condenser and the delegation
    # sub-agents call the LLM without one, so a no-op stands in for every path.
    try:
        for _name in ("completion", "acompletion", "responses", "aresponses"):
            _orig = getattr(LLM, _name)

            def _with_token_sink(self, *args, _orig=_orig, on_token=None, **kwargs):
                if on_token is None and self.stream:
                    on_token = _discard_token
                return _orig(self, *args, on_token=on_token, **kwargs)
            setattr(LLM, _name, _with_token_sink)
    except Exception as exc:  # noqa: BLE001 - an SDK without these methods
        print(f"driver: streaming shim not installed: {exc}", file=sys.stderr)
    _register_cache_capable_models()
    _base_url = os.environ.get("LLM_BASE_URL") or None
    llm_kwargs = dict(
        model=_model_for_litellm(os.environ["LLM_MODEL"], _base_url),
        api_key=os.environ["LLM_API_KEY"],
        base_url=_base_url,
        service_id="openvisor-agent",
        stream=True,
    )
    _anthropic = _is_anthropic(_base_url)
    if _anthropic:
        # The native provider owns its own endpoint and appends its own version
        # segment. Forwarding the platform's OpenAI-shaped base URL (which ends
        # in /v1, because that is what every other provider needs) would ask for
        # /v1/v1/messages. Same trap the claude_sdk driver hits, same answer.
        llm_kwargs["base_url"] = None
        print("driver: routing natively to Anthropic (prompt caching enabled)")
    effort = (os.environ.get("LLM_REASONING_EFFORT") or "").strip()
    if effort:
        llm_kwargs["reasoning_effort"] = effort
    # OpenAI rejects prompt_cache_key over 64 chars with a 400 - which would
    # fail EVERY call of the build. An overlong key degrades to truncation
    # (worst case a cache miss), never a dead run.
    cache_key = (os.environ.get("LLM_CACHE_KEY") or "").strip()[:64]
    # Test the ORIGINAL base URL, and never send this on the Anthropic route.
    # prompt_cache_key is an OpenAI/Mistral parameter; Anthropic rejects the whole
    # request with "extra_body: Extra inputs are not permitted", failing every call
    # of the build. Reading llm_kwargs["base_url"] here would see the None the
    # native route just set and conclude "LiteLLM's default OpenAI endpoint" -
    # which is exactly how this shipped broken.
    if cache_key and not _anthropic and _cache_key_supported(_base_url):
        llm_kwargs["litellm_extra_body"] = {"prompt_cache_key": cache_key}
    try:
        llm = LLM(**llm_kwargs)
    except Exception:  # noqa: BLE001 - SDK without the fields: run at provider default
        llm_kwargs.pop("reasoning_effort", None)
        llm_kwargs.pop("litellm_extra_body", None)
        llm_kwargs.pop("stream", None)
        llm = LLM(**llm_kwargs)
        print("driver: reasoning_effort/prompt_cache_key/stream not supported by this "
              "SDK; provider default", file=sys.stderr)
    # The Responses API (gpt-5 family) is a different streaming path in the SDK,
    # unverified here, and those calls reach OpenAI directly with no edge idle
    # timeout on the way: they keep the non-streaming path.
    try:
        if llm.stream and llm.uses_responses_api():
            llm = llm.model_copy(update={"stream": False})
            print("driver: Responses-API model; completions stay non-streaming",
                  file=sys.stderr)
    except Exception as exc:  # noqa: BLE001 - SDK without the switch: keep streaming
        print(f"driver: Responses-API check skipped: {exc}", file=sys.stderr)
    _keep_mcp_streams_alive()
    agent = get_default_agent(llm=llm, cli_mode=True)  # cli_mode: no browser GUI deps
    # §Phase 1: add purpose-built grep + glob so the agent navigates the repo with
    # real search tools instead of shelling out (the SWE-agent ACI thesis). They ship
    # with openhands-tools but aren't in the default preset; model_copy keeps the
    # preset's condenser/system-prompt intact and just extends the tool list.
    # §exploration budget: `task_tool_set` joins them so wide reading can be
    # DELEGATED. The preset leaves it off (`get_default_tools(enable_sub_agents=False)`),
    # which is why every file a run ever opened stayed in the one main context and was
    # re-uploaded on every later step - the dominant input-token line on a real build.
    # A subagent reads in its own context and returns a summary, so the transcript
    # carries the conclusion instead of the corpus. The tool is only as good as its
    # roster, which ships EMPTY - `_register_web_researcher` below fills it.
    agent = agent.model_copy(update={
        "tools": list(agent.tools) + [Tool(name=GrepTool.name), Tool(name=GlobTool.name),
                                      Tool(name=TaskToolSet.name)],
        "tool_concurrency_limit": max(1, TOOL_CONCURRENCY)})
    agent = _tuned_condenser(agent)
    _tuned_observation_limit()

    # Attach the project's MCP servers if present (best-effort - never fail the
    # build because an MCP server is unavailable).
    mcp_path = OPENVISOR / "mcp.json"
    if mcp_path.exists():
        try:
            cfg = json.loads(mcp_path.read_text())
            if cfg.get("mcpServers"):  # empty config makes the SDK raise
                agent = agent.model_copy(update={"mcp_config": cfg})
                # Before the Conversation: the Task tool renders its roster into
                # the tool description at creation time, so an agent registered
                # afterwards is invisible to the model.
                _register_web_researcher(cfg)
        except Exception as exc:  # noqa: BLE001
            print(f"driver: MCP config skipped: {exc}", file=sys.stderr)

    # Live build-panel feed (§14.8): sanitized event summaries + a throttled
    # token snapshot, streamed to the customer via the platform API. The SDK's
    # constructor signature drifts across versions, so fall back to a feed-less
    # run rather than fail the build.
    feed = live_events.LiveFeed(OPENVISOR, llm=llm,
                                model=os.environ["LLM_MODEL"].split("/")[-1],
                                on_snapshot=lambda: _dump_usage(llm, quiet=True))
    # Fail-safe token cap: bound the number of agent iterations so a stuck or
    # looping run can't burn the customer's budget. The hard wall-clock timeout
    # (enforced by the deployer) is the backstop; this is the finer guard. The
    # cap is a Conversation constructor arg (run() takes none), and the SDK
    # swallows unknown constructor kwargs - so verify it landed and warn loudly
    # when a future rename silently drops it.
    max_iters = int(os.environ.get("LLM_MAX_ITERATIONS") or 0)
    conv_kwargs = {"agent": agent, "workspace": "/workspace", "callbacks": [feed]}
    if max_iters > 0:
        conv_kwargs["max_iteration_per_run"] = max_iters

    def _construct(kwargs):
        try:
            return Conversation(**kwargs)
        except TypeError:
            print("driver: callbacks kwarg unsupported; running without the live feed",
                  file=sys.stderr)
            slim = {k: v for k, v in kwargs.items()
                    if k in ("agent", "workspace", "persistence_dir",
                             "conversation_id", "delete_on_close")}
            return Conversation(**slim)

    # §conversation resume: persist the session under the workspace, which a
    # resume chain reuses - so the next run of THIS chain rehydrates the full
    # agent conversation (messages, tool calls, its own reasoning) instead of
    # starting cold with only the artifact files. The worker deletes the state
    # for unchained runs, so a resume is the only way to land here with one.
    # Any persistence failure wipes the state and falls back to today's
    # ephemeral conversation: a broken resume must never kill a build.
    persist_dir = OPENVISOR / "conversation"
    cid_file = OPENVISOR / "conversation_id"
    conversation = None
    resumed = False
    try:
        if cid_file.exists():
            cid = uuid.UUID(cid_file.read_text().strip())
            resumed = persist_dir.is_dir() and any(persist_dir.iterdir())
        else:
            cid = uuid.uuid4()
            cid_file.write_text(str(cid))
        conversation = _construct({**conv_kwargs, "persistence_dir": str(persist_dir),
                                   "conversation_id": cid, "delete_on_close": False})
        if resumed:
            prior = len(getattr(conversation.state, "events", []) or [])
            if prior == 0:
                resumed = False          # state dir existed but nothing loaded
            else:
                print(f"driver: resumed previous session ({prior} events restored)")
    except Exception as exc:  # noqa: BLE001 - persistence must never fail the run
        print(f"driver: conversation persistence unavailable ({exc}); "
              "running a fresh session", file=sys.stderr)
        try:
            shutil.rmtree(persist_dir, ignore_errors=True)
            cid_file.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass
        conversation = None
        resumed = False
    if conversation is None:
        conversation = _construct(conv_kwargs)
    _install_tool_call_repairs(conversation)
    _install_mcp_session_recovery(conversation)
    if max_iters > 0 and getattr(conversation, "max_iteration_per_run", None) != max_iters:
        print("driver: iteration cap NOT applied by this SDK version "
              "(deployer timeout still applies)", file=sys.stderr)

    if resumed:
        # The restored history already contains the task; replaying it would
        # double the context. Send only what is NEW: the customer's steering
        # since the session ended (worker-written), else a plain continuation.
        steering_file = OPENVISOR / "steering.md"
        note = ""
        if steering_file.is_file():
            note = steering_file.read_text(errors="replace").strip()
        if note:
            message = ("Continuing the SAME task from your previous session - its "
                       "full history is restored above. New guidance from the "
                       "customer since that session ended:\n\n" + note)
        else:
            message = ("Continuing the SAME task from your previous session - its "
                       "full history is restored above. Pick up exactly where you "
                       "stopped and finish the task.")
        conversation.send_message(_with_images(message))
    else:
        conversation.send_message(_with_images(task))

    try:
        conversation.run()
    except Exception as exc:
        # §cache-key fallback (mirror of services/llm.py's strip-and-retry): a
        # provider rejecting the prompt_cache_key extra param kills the session
        # mid-call. Re-exec the driver ONCE without the key - the persisted
        # conversation resumes where it stopped, so the worst case is uncached
        # pricing, never a dead build. os.execve skips finally: bill first.
        if ("prompt_cache_key" in str(exc)
                and "litellm_extra_body" in llm_kwargs
                and not os.environ.get("OPENVISOR_CACHE_KEY_RETRIED")):
            print("driver: provider rejected prompt_cache_key - retrying the "
                  "session without it", file=sys.stderr)
            _dump_usage(llm, quiet=True)
            env = dict(os.environ, OPENVISOR_CACHE_KEY_RETRIED="1")
            env.pop("LLM_CACHE_KEY", None)
            os.execve(sys.executable,
                      [sys.executable, os.path.abspath(__file__)], env)
        _dump_error(exc)
        raise
    finally:
        # bill whatever was spent, even when the run errors or hits the cap
        _dump_usage(llm)
        try:
            feed.dump_progress(force=True)  # final live-counter snapshot
        except Exception:  # noqa: BLE001
            pass
    if feed.exit_reason == "max_iterations":
        # Structured, secret-free end-of-session marker (error.json parity): the
        # worker turns it into the customer-facing "iteration cap" copy instead
        # of the misleading generic "no changes to publish".
        try:
            (OPENVISOR / "exit_reason.json").write_text(
                json.dumps({"reason": "max_iterations", "limit": max_iters}))
            print(f"driver: session ended at the iteration cap ({max_iters})")
        except Exception as exc:  # noqa: BLE001 - copy plumbing never fails the run
            print(f"driver: exit-reason dump failed: {exc}", file=sys.stderr)
    print("driver: conversation finished")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 - setup failures (before the run loop)
        _dump_error(exc)
        raise
