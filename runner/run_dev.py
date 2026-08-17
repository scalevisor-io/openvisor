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


def _model_for_litellm(model: str) -> str:
    # Route any custom OpenAI-compatible endpoint through LiteLLM's openai
    # provider (honours base_url); leave already-prefixed models untouched.
    return model if "/" in model else f"openai/{model}"


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
    llm_kwargs = dict(
        model=_model_for_litellm(os.environ["LLM_MODEL"]),
        api_key=os.environ["LLM_API_KEY"],
        base_url=os.environ.get("LLM_BASE_URL") or None,
        service_id="openvisor-agent",
    )
    effort = (os.environ.get("LLM_REASONING_EFFORT") or "").strip()
    if effort:
        llm_kwargs["reasoning_effort"] = effort
    # OpenAI rejects prompt_cache_key over 64 chars with a 400 - which would
    # fail EVERY call of the build. An overlong key degrades to truncation
    # (worst case a cache miss), never a dead run.
    cache_key = (os.environ.get("LLM_CACHE_KEY") or "").strip()[:64]
    if cache_key and _cache_key_supported(llm_kwargs["base_url"]):
        llm_kwargs["litellm_extra_body"] = {"prompt_cache_key": cache_key}
    try:
        llm = LLM(**llm_kwargs)
    except Exception:  # noqa: BLE001 - SDK without the fields: run at provider default
        llm_kwargs.pop("reasoning_effort", None)
        llm_kwargs.pop("litellm_extra_body", None)
        llm = LLM(**llm_kwargs)
        if effort or cache_key:
            print("driver: reasoning_effort/prompt_cache_key not supported by this SDK; "
                  "provider default", file=sys.stderr)
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
    # carries the conclusion instead of the corpus.
    agent = agent.model_copy(update={
        "tools": list(agent.tools) + [Tool(name=GrepTool.name), Tool(name=GlobTool.name),
                                      Tool(name=TaskToolSet.name)],
        "tool_concurrency_limit": max(1, TOOL_CONCURRENCY)})
    agent = _tuned_condenser(agent)

    # Attach the project's MCP servers if present (best-effort - never fail the
    # build because an MCP server is unavailable).
    mcp_path = OPENVISOR / "mcp.json"
    if mcp_path.exists():
        try:
            cfg = json.loads(mcp_path.read_text())
            if cfg.get("mcpServers"):  # empty config makes the SDK raise
                agent = agent.model_copy(update={"mcp_config": cfg})
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
