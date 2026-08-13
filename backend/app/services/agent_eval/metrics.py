"""Deterministic run metrics - pure functions over a RunRecord (one build attempt).

No DB, no LLM. The eval driver populates RunRecords from what the pipeline already
emits (dev_run_state, the boot result, dev_security_review, the leak-scan outcome,
usage.json); these turn them into the gate metrics we own and the cost figures the
business actually bills on. A "pass" here means the deterministic gates cleared and the
build reached delivery - the closest analogue we have to SWE-bench's 'resolved'.
"""
from __future__ import annotations

from dataclasses import dataclass

# States in which a build that cleared the gates is considered delivered/deliverable.
_PASS_STATES = ("done", "awaiting_merge", "deploying")


@dataclass(frozen=True)
class RunRecord:
    spec_id: str
    speciality: str
    harness_version: str
    model: str
    attempt: int                 # 1-based; >1 = a Resume/retry (pass@1 vs pass@k)
    final_state: str             # dev_run_state: done|awaiting_merge|deploying|failed|timeout|...
    boot_result: bool | None     # True pass / False fail / None = gate unavailable (fail-open)
    contract_ok: bool | None     # compose.demo.yml + exactly one $PORT HTTP service
    ci_status: str | None        # "green" | "failed" | None (not applicable, e.g. GitHub path)
    security_blocking: int       # count of blocking (critical/high) findings
    security_ran: bool           # False => review could not run (fail-closed park)
    leak_blocked: bool           # pre-publish leak scan refused to publish
    leak_scanner_errored: bool   # scanner internal error (the fail-open path)
    input_tokens: int
    output_tokens: int
    credits: float
    wall_clock_s: float
    acceptance_passed: int | None = None  # §Phase 1 #5 advisory spec checks (None = not run)
    acceptance_total: int | None = None
    error: str = ""

    @staticmethod
    def from_dict(d: dict) -> "RunRecord":
        return RunRecord(
            spec_id=str(d["spec_id"]), speciality=str(d.get("speciality", "")),
            harness_version=str(d.get("harness_version", "")), model=str(d.get("model", "")),
            attempt=int(d.get("attempt", 1)), final_state=str(d.get("final_state", "")),
            boot_result=d.get("boot_result"), contract_ok=d.get("contract_ok"),
            ci_status=d.get("ci_status"), security_blocking=int(d.get("security_blocking", 0)),
            security_ran=bool(d.get("security_ran", True)),
            leak_blocked=bool(d.get("leak_blocked", False)),
            leak_scanner_errored=bool(d.get("leak_scanner_errored", False)),
            input_tokens=int(d.get("input_tokens", 0)), output_tokens=int(d.get("output_tokens", 0)),
            credits=float(d.get("credits", 0.0)), wall_clock_s=float(d.get("wall_clock_s", 0.0)),
            acceptance_passed=d.get("acceptance_passed"), acceptance_total=d.get("acceptance_total"),
            error=str(d.get("error", "")),
        )


def total_tokens(r: RunRecord) -> int:
    return r.input_tokens + r.output_tokens


def gate_failed_open(r: RunRecord) -> bool:
    """A gate could not run and we shipped anyway - our TRUE gate coverage is below
    nominal. Tracked as a first-class metric because both the boot gate and the leak
    scanner fail OPEN today and nobody measures how often."""
    return r.boot_result is None or r.leak_scanner_errored


def gates_clear(r: RunRecord) -> bool:
    """Every deterministic gate we own passed (a gate that could not run does NOT count
    as clear - fail-open ships the build but must not inflate the eval)."""
    return (
        r.boot_result is True
        and r.contract_ok is not False
        and r.security_ran and r.security_blocking == 0
        and not r.leak_blocked and not r.leak_scanner_errored
        and r.ci_status in (None, "green")
    )


def is_pass(r: RunRecord) -> bool:
    return gates_clear(r) and r.final_state in _PASS_STATES
