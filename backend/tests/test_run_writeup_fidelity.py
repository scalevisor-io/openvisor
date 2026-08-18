"""§findings ledger: what a run OBSERVES has to survive to what it REPORTS.

A production price-drift run found a live billing exposure mid-session - a
provider tier above the single rate the catalog quotes - inside a line of
reasoning it then abandoned, and twenty steps later wrote "no tier boundary was
identified for any checked model". Nothing was wrong with the plumbing: the
write-up was composed from a 5.3M-token context at the very end, so an
observation made forty actions earlier simply was not there any more. Two other
things about the same run misrepresented it to the customer - the opening line
promised a pull request on a routine whose correct result was a report, and the
publish gate ended a perfectly correct session on a red error event.

These pin the fixes: findings are written when they are made and the write-up is
assembled from that file, figures come from a source opened in the session, the
opening line promises no change either way, and a DECLARED no-change outcome
reads as the finished answer it is.
"""
import re
from pathlib import Path

import pytest

from app.agents.pipeline import load_prompt
from app.workers import tasks

RUNNER_ENTRYPOINT = Path("/app/runner_src/entrypoint.sh")
RUNNER_DRIVER = Path("/app/runner_src/run_dev.py")


def _runner(path: Path) -> str:
    if not path.exists():
        pytest.skip("runner source not mounted at /app/runner_src")
    return path.read_text()


# ------------------------------------------------------------ the ledger

def test_findings_are_recorded_when_made_not_reconstructed_at_the_end():
    p = load_prompt("development_system.md")
    assert "`.openvisor/findings.md`" in p
    assert "Record a finding the moment you make it" in p
    # the two failure modes that produced the miss, both named
    assert "abandoning a line of reasoning" in p
    assert "never from memory" in p


def test_the_ledger_is_ordered_before_the_write_up_steps():
    """A ledger written after the report would be decoration."""
    p = load_prompt("development_system.md")
    assert p.index("findings.md`.**") < p.index("Write the pull-request description")
    assert p.index("findings.md`.**") < p.index('the honest answer is "nothing to change"')


def test_every_write_up_has_to_account_for_every_line_of_it():
    p = load_prompt("development_system.md")
    assert "either appear in the write-up or be explicitly dismissed" in p


def test_the_working_method_stays_consecutively_numbered():
    """The steps are referenced by number from tasks.py and CODE_MAP."""
    p = load_prompt("development_system.md")
    body = p[p.index("## Working method"):p.index("## Non-negotiable rules")]
    numbers = [int(m.group(1)) for m in
               (re.match(r"(\d+)\. \*\*", line) for line in body.splitlines()) if m]
    assert numbers == list(range(1, len(numbers) + 1))
    assert len(numbers) == 10


# ------------------------------------------------------------ reported figures

def test_a_reported_figure_has_to_come_from_a_source_opened_this_session():
    """Rule 6's twin: the run confirmed a provider's price by solving backwards
    from our own published price until the implied rate matched, then reported
    it as read from the provider's page."""
    p = load_prompt("development_system.md")
    assert "never state a\n   price, version, limit or status from memory" in p
    assert "working backwards from our own data" in p


def test_platform_run_instrumentation_is_not_task_material():
    p = load_prompt("development_system.md")
    for name in ("`run.log`", "`events.jsonl`", "`progress.json`"):
        assert name in p
    assert "only spends billed steps" in p


# ------------------------------------------------------------ the opening line

def test_the_opening_line_promises_no_change_either_way():
    src = Path(tasks.__file__).read_text()
    opening = src[src.index("On it - I\\'m working on"):]
    opening = opening[:opening.index("rid = run.id")]
    assert "If it needs a" in opening
    assert "If nothing needs changing" in opening
    # the old copy promised one unconditionally
    assert "You\\'ll get a" not in src


@pytest.mark.parametrize("provider,noun", [
    ("gitlab", "merge request"),
    ("github", "pull request"),
    ("other", "pull request"),
    (None, "pull request"),
])
def test_the_noun_follows_the_repo_the_run_pushes_to(provider, noun):
    assert tasks._change_noun({"provider": provider} if provider else None) == noun


# ------------------------------------------------------------ the publish gate

def test_a_declared_no_change_outcome_is_not_reported_as_an_error():
    src = _runner(RUNNER_ENTRYPOINT)
    gate = src[src.index("NO_CHANGES_TO_PUBLISH"):src.index("Hard exfiltration boundary")]
    assert 'emit_event finish "No repository change was needed"' in gate
    # the unexpected empty session still reads as the failure it is
    assert 'emit_event error "No changes produced - nothing to publish"' in gate
    # and the worker's contract - the exit code - is untouched either way
    assert gate.count("exit 5") == 1


@pytest.mark.parametrize("outcome,expected", [
    ('{"outcome": "no_change_needed", "summary": "checked, nothing drifted"}', "finish"),
    ('{"outcome":"no_change_needed","summary":"x"}', "finish"),
    ('{"outcome": "changed", "summary": "shipped it"}', "error"),
    ('{"outcome": "blocked", "summary": "no_change_needed was not my verdict"}', "error"),
    (None, "error"),
])
def test_the_gate_mechanism_reads_the_declaration(tmp_path, outcome, expected):
    """The entrypoint's own test, run against fixture declarations."""
    import subprocess
    if outcome is not None:
        (tmp_path / "outcome.json").write_text(outcome)
    probe = _runner(RUNNER_ENTRYPOINT)
    probe = probe[probe.index('if [[ -f "$OPENVISOR_DIR/outcome.json" ]]'):]
    probe = probe[:probe.index("      fi") + len("      fi")]
    probe = probe.replace("emit_event finish", "echo finish #").replace(
        "emit_event error", "echo error #")
    out = subprocess.run(["bash", "-c", f'OPENVISOR_DIR="{tmp_path}"\n{probe}'],
                         capture_output=True, text=True)
    assert out.stdout.split()[0] == expected, out


# ------------------------------------------------------------ between runs

def test_a_stale_ledger_never_survives_into_the_next_run():
    """Reporting an observation the session never made is the same defect."""
    src = Path(tasks.__file__).read_text()
    wipe = src[src.index('(openvisor_dir / "report.md").unlink'):]
    wipe = wipe[:wipe.index("if project.ssh_private_key_enc")]
    assert '(openvisor_dir / "findings.md").unlink(missing_ok=True)' in wipe


# ------------------------------------------------------------ the tool window

def test_an_oversized_tool_result_is_clipped_smaller_and_persisted():
    """The SDK already threw the overflow away at 50 kB; what is new is that the
    full text lands in a file the agent is told about."""
    src = _runner(RUNNER_DRIVER)
    assert 'DEV_OBS_TEXT_LIMIT' in src
    assert 'save_dir=OBS_OVERFLOW_DIR' in src
    # scratch, never the workspace: nothing there may reach a commit
    assert 'OBS_OVERFLOW_DIR = "/tmp/tool_output"' in src
    assert "/workspace" not in src[src.index("OBS_TEXT_LIMIT ="):
                                   src.index("class RetryingCondenser")]
    # a tuning that raises must never fail the build
    tuned = src[src.index("def _tuned_observation_limit"):src.index("def _model_for_litellm")]
    assert "except Exception" in tuned
    assert "_tuned_observation_limit()" in src[src.index("_tuned_condenser(agent)"):]


# ------------------------------------------------------------ delegated reading

def test_the_task_tool_has_an_agent_to_delegate_to():
    """`task_tool_set` ships with an EMPTY roster - get_factory_info() answers
    "No user-registered agents yet" - so enabling the tool alone (PR #39) gave
    the agent a delegation tool with nothing behind it."""
    src = _runner(RUNNER_DRIVER)
    reg = src[src.index("def _register_web_researcher"):src.index("class RetryingCondenser")]
    assert "register_agent(" in reg and "agent_definition_to_factory(" in reg
    # registered BEFORE the Conversation: the Task tool renders its roster into
    # the tool description at creation time
    assert src.index("_register_web_researcher(cfg)") < src.index('Conversation(')
    # ...and never fails the build
    assert "except Exception" in reg


def test_the_researcher_cannot_touch_the_workspace():
    """It reads public pages. No local tools means no file read, no edit, no
    command - so a page it opened can never reach a commit."""
    src = _runner(RUNNER_DRIVER)
    reg = src[src.index("def _register_web_researcher"):src.index("class RetryingCondenser")]
    assert "tools=" not in reg  # the AgentDefinition default is []
    assert "GrepTool" not in reg and "TerminalTool" not in reg


def test_only_the_web_servers_are_handed_down():
    """Context7 and the consultant's own MCP KBs stay with the main agent - the
    sub-agent is a page reader, not a second holder of internal knowledge."""
    src = _runner(RUNNER_DRIVER)
    assert 'WEB_MCP_PREFIXES = ("browser", "websearch")' in src
    reg = src[src.index("def _register_web_researcher"):src.index("class RetryingCondenser")]
    assert "name.startswith(WEB_MCP_PREFIXES)" in reg
    assert "if not servers:" in reg  # a run without them registers nothing


def test_the_researcher_reports_what_it_could_not_read():
    """The failure this exists to stop is a page that did not answer being
    filled in from memory or from a search-result snippet."""
    src = _runner(RUNNER_DRIVER)
    prompt = src[src.index("WEB_AGENT_PROMPT = "):src.index("def _register_web_researcher")]
    assert "ONLY if you read" in prompt
    assert "from memory or from a search-result snippet" in prompt
    # and the adjacent facts that change what a value means - the tier the prod
    # run found and dropped
    assert "tier or threshold" in prompt


def test_the_prompt_sends_third_party_pages_to_the_sub_agent():
    p = load_prompt("development_system.md")
    assert "`web researcher` sub-agent with the `task` tool" in p
    # the direct browser keeps its job: the app the run itself is running
    assert "is for the app YOU are running" in p
    # and rule 6 accepts a delegated read as a read, so the two do not fight
    assert "returns WITH the URL it read counts as read" in p
