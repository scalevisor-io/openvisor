"""Deterministic failure triage - turns the eval scoreboard into a roadmap.

Every failing run is bucketed into a fixed taxonomy so residual failures can be read as
either MECHANICAL (bad edits/tools -> the ACI is the lever) or SEMANTIC (bad code ->
verification is the lever) or coordination (we have none). Deterministic gates cannot
see semantics, so the SEMANTIC residual falls into 'unresolved-needs-judge' - exactly
what the LLM judge + human review exist to split.
"""
from __future__ import annotations

from app.services.agent_eval.metrics import RunRecord, is_pass

FAILURE_CATEGORIES = (
    "contract-violation",
    "boot-failure",
    "security-or-leak-block",
    "cascading-edit-failure",
    "ran-out-of-iterations",
    "ran-out-of-time",
    "infra-error",
    "unresolved-needs-judge",   # the semantic residual: incorrect/incomplete impl
)


def classify(r: RunRecord) -> str | None:
    """None => the run passed. Otherwise one category. Order matters: a hard block
    (security/leak) is reported over a downstream symptom."""
    if is_pass(r):
        return None
    err = (r.error or "").lower()

    if r.leak_blocked or (r.security_ran and r.security_blocking > 0):
        return "security-or-leak-block"
    if r.final_state == "timeout" or "timeout" in err or "timed_out" in err:
        return "ran-out-of-time"
    if "iteration" in err or "max_iter" in err:
        return "ran-out-of-iterations"
    if any(k in err for k in ("patch", "apply_patch", "edit failed", "hunk", "no such file")):
        return "cascading-edit-failure"
    if r.contract_ok is False:
        return "contract-violation"
    if r.boot_result is False:
        return "boot-failure"
    if r.boot_result is None or r.leak_scanner_errored or "infra" in err or "deployer" in err:
        return "infra-error"
    # Gates that CAN run all came back clean but the build still isn't a pass, or a
    # review couldn't run - the semantic residual only a judge/human can adjudicate.
    return "unresolved-needs-judge"
