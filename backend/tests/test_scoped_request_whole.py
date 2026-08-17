"""§whole ask: the customer's request text reaches the dev agent COMPLETE.

The regression these pin is a real production one: a 6596-character routine
prompt was cut to 4000 mid-sentence, so the run built against 61% of its
requirement - and the agent that noticed ("the scoped request seems truncated")
went hunting through `.openvisor/task.md` for the rest, re-reading its own task
file 16 times. There was nothing to find: the task file IS the truncation. So
the bound is generous, and when it does bite it names the loss instead of
trailing off.
"""
from app.workers.tasks import ASK_MAX_CHARS, _ask_text


def test_a_realistic_routine_prompt_survives_whole():
    """The exact shape of the production failure: 6596 chars, formerly cut at 4000."""
    ask = "Providers price drift. " + ("x" * 6573)
    assert len(ask) == 6596
    assert _ask_text(ask) == ask


def test_text_within_the_bound_is_untouched():
    ask = "Add a /healthz endpoint."
    assert _ask_text(ask) == ask


def test_an_oversized_ask_keeps_its_head_and_declares_the_loss():
    ask = "A" * (ASK_MAX_CHARS + 2596)
    out = _ask_text(ask)
    assert out.startswith("A" * ASK_MAX_CHARS)
    assert "2596 more characters" in out


def test_the_truncation_notice_tells_the_agent_not_to_go_looking():
    """The hunting is the expensive failure, not the missing text - the notice has
    to close that door explicitly."""
    out = _ask_text("B" * (ASK_MAX_CHARS + 1))
    assert "NOT recoverable" in out
    assert "do not go looking" in out


def test_empty_and_missing_asks_are_safe():
    assert _ask_text("") == ""
    assert _ask_text(None) == ""
