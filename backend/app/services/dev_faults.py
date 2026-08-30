"""§request help: whose fault a parked dev run was.

A failed build is not one thing. Most failures are the build's own: the agent
produced nothing publishable, the demo would not boot, the run hit its step cap,
the leak scan refused what it wrote. Those are the customer's to steer - Resume
with a note, or Start fresh - and the pipeline's copy already says so.

A few are ours. The agent driver crashed, the model endpoint refused the model
the project is configured with, the sandbox could not reach the git remote, the
worker running the build died. Nothing the customer types changes any of them,
and telling someone to "hit Resume" over a platform fault is how a customer
spends a second sandbox to learn the same thing.

This module names that distinction, so the rest of the platform can key on it
instead of pattern-matching failure copy. `PLATFORM` is stamped on the run
(`DevRun.run_fault`, mirrored to `Project.dev_run_fault`) by the park that knows
which path it took, and it is the ONE thing that offers the free Request-help
button (`serializers.dev_help_capability` → `project_actions.request_help`).
Never derive it later from `dev_run_error`: that string is customer copy and
gets reworded, and a classifier reading it would go quietly wrong on the next
rewording.

Deliberately two-valued. "Platform" earns a free escalation; everything else is
an ordinary build outcome, and a third category would only be a slower way of
writing one of those two.
"""

PLATFORM = "platform"

# The runner drivers' own error categories (runner/run_dev.py, run_claude.py
# write them to .openvisor/error.json). Every one of them describes the build's
# machinery rather than its task: the agent process died, the endpoint refused
# the model we configured, it rejected our key, it could not be reached. The
# model the project runs on is chosen by the admin, never by the customer, so
# `llm_model` belongs here too - it is the exact shape of the production failure
# this feature was built for.
_RUNNER_PLATFORM_CATEGORIES = {"agent_error", "llm_auth", "llm_model", "llm_unreachable"}


def from_runner_category(category: str | None) -> str | None:
    """The fault a driver's error.json category implies, or None to leave the
    park unstamped. An unknown category is not a platform fault: a driver that
    grows a category we have never seen must not silently start handing out free
    consultant time."""
    return PLATFORM if (category or "") in _RUNNER_PLATFORM_CATEGORIES else None
