"""Aggregate RunRecords into an eval report.

Headline metric is COST PER PASSING BUILD, not resolve rate: we bill actual tokens at a
1.3x markup against a pre-approved estimate, so a change that buys +3pp quality for +80%
tokens is a business LOSS even though it would be a publishable win. pass@1 is reported
SEPARATELY from pass@k because the 'Resume development' button lets a spec pass on a
later attempt - conflating them would let a change that makes the first attempt worse
but the retry cheaper look like an improvement (cost quietly shifted onto the customer).
"""
from __future__ import annotations

import statistics
from collections import Counter
from dataclasses import dataclass, field

from app.services.agent_eval.metrics import RunRecord, gate_failed_open, is_pass, total_tokens
from app.services.agent_eval.taxonomy import classify


def _rate(num: int, den: int) -> float | None:
    return round(num / den, 4) if den else None


@dataclass
class SpecialityStat:
    speciality: str
    specs: int
    attempts: int
    pass_at_1: float | None
    pass_at_k: float | None
    boot_pass_rate: float | None
    credits_per_passing_build: float | None


@dataclass
class EvalReport:
    harness_versions: list[str]
    models: list[str]
    n_specs: int
    n_attempts: int
    pass_at_1: float | None
    pass_at_k: float | None
    boot_pass_rate: float | None
    contract_pass_rate: float | None
    ci_green_rate: float | None
    security_clean_rate: float | None
    leak_clean_rate: float | None
    gate_failed_open_rate: float | None
    acceptance_pass_rate: float | None      # §Phase 1 #5: passed/total over runs that ran checks
    total_credits: float
    credits_per_passing_build: float | None      # over passing ATTEMPTS
    credits_per_passing_spec: float | None        # over specs that passed >= once
    median_wall_clock_s: float | None
    failure_histogram: dict
    by_speciality: list[SpecialityStat]
    warnings: list[str] = field(default_factory=list)


def _by_spec(records: list[RunRecord]) -> dict[str, list[RunRecord]]:
    out: dict[str, list[RunRecord]] = {}
    for r in records:
        out.setdefault(r.spec_id, []).append(r)
    return out


def _pass_at_1(specs: dict[str, list[RunRecord]]) -> tuple[int, int]:
    """(passed, evaluated) over specs that have a first attempt."""
    passed = evaluated = 0
    for attempts in specs.values():
        first = next((a for a in attempts if a.attempt == 1), None)
        if first is None:
            continue
        evaluated += 1
        passed += 1 if is_pass(first) else 0
    return passed, evaluated


def aggregate(records: list[RunRecord]) -> EvalReport:
    warnings: list[str] = []
    if not records:
        return EvalReport(
            harness_versions=[], models=[], n_specs=0, n_attempts=0, pass_at_1=None,
            pass_at_k=None, boot_pass_rate=None, contract_pass_rate=None, ci_green_rate=None,
            security_clean_rate=None, leak_clean_rate=None, gate_failed_open_rate=None,
            acceptance_pass_rate=None, total_credits=0.0, credits_per_passing_build=None,
            credits_per_passing_spec=None, median_wall_clock_s=None, failure_histogram={},
            by_speciality=[], warnings=["no records"])

    versions = sorted({r.harness_version for r in records if r.harness_version})
    if len(versions) > 1:
        warnings.append(
            f"records span {len(versions)} harness versions ({', '.join(versions)}) - "
            "NEVER compare across versions; re-run the corpus on one pinned harness.")

    by_spec = _by_spec(records)
    n_specs, n_attempts = len(by_spec), len(records)

    p1_num, p1_den = _pass_at_1(by_spec)
    pk_num = sum(1 for atts in by_spec.values() if any(is_pass(a) for a in atts))

    boot_ran = [r for r in records if r.boot_result is not None]
    ci_applicable = [r for r in records if r.ci_status is not None]
    passing = [r for r in records if is_pass(r)]
    passing_specs = {sid for sid, atts in by_spec.items() if any(is_pass(a) for a in atts)}
    total_credits = round(sum(r.credits for r in records), 4)

    fh = Counter(c for c in (classify(r) for r in records) if c)

    strat: dict[str, list[RunRecord]] = {}
    for r in records:
        strat.setdefault(r.speciality or "(unset)", []).append(r)
    by_spec_stats = []
    for spec, recs in sorted(strat.items()):
        sp = _by_spec(recs)
        sp1n, sp1d = _pass_at_1(sp)
        spk = sum(1 for a in sp.values() if any(is_pass(x) for x in a))
        sbr = [r for r in recs if r.boot_result is not None]
        spassing = [r for r in recs if is_pass(r)]
        by_spec_stats.append(SpecialityStat(
            speciality=spec, specs=len(sp), attempts=len(recs),
            pass_at_1=_rate(sp1n, sp1d), pass_at_k=_rate(spk, len(sp)),
            boot_pass_rate=_rate(sum(1 for r in sbr if r.boot_result), len(sbr)),
            credits_per_passing_build=(round(sum(r.credits for r in recs) / len(spassing), 4)
                                       if spassing else None),
        ))

    return EvalReport(
        harness_versions=versions,
        models=sorted({r.model for r in records if r.model}),
        n_specs=n_specs, n_attempts=n_attempts,
        pass_at_1=_rate(p1_num, p1_den), pass_at_k=_rate(pk_num, n_specs),
        boot_pass_rate=_rate(sum(1 for r in boot_ran if r.boot_result), len(boot_ran)),
        contract_pass_rate=_rate(sum(1 for r in records if r.contract_ok is True),
                                 sum(1 for r in records if r.contract_ok is not None)),
        ci_green_rate=_rate(sum(1 for r in ci_applicable if r.ci_status == "green"), len(ci_applicable)),
        security_clean_rate=_rate(sum(1 for r in records if r.security_ran and r.security_blocking == 0),
                                  sum(1 for r in records if r.security_ran)),
        leak_clean_rate=_rate(sum(1 for r in records if not r.leak_blocked), n_attempts),
        gate_failed_open_rate=_rate(sum(1 for r in records if gate_failed_open(r)), n_attempts),
        acceptance_pass_rate=_rate(sum(r.acceptance_passed or 0 for r in records if r.acceptance_total),
                                   sum(r.acceptance_total or 0 for r in records if r.acceptance_total)),
        total_credits=total_credits,
        credits_per_passing_build=(round(total_credits / len(passing), 4) if passing else None),
        credits_per_passing_spec=(round(total_credits / len(passing_specs), 4) if passing_specs else None),
        median_wall_clock_s=(round(statistics.median(r.wall_clock_s for r in records), 1)
                             if records else None),
        failure_histogram=dict(fh.most_common()),
        by_speciality=by_spec_stats,
        warnings=warnings,
    )


def render_markdown(rep: EvalReport) -> str:
    def pct(x): return "n/a" if x is None else f"{x * 100:.1f}%"
    def cr(x): return "n/a" if x is None else f"{x:.2f} cr"
    L = ["# Agent eval report", ""]
    if rep.warnings:
        L += ["> **⚠ " + "**  \n> **".join(rep.warnings) + "**", ""]
    L += [
        f"- harness version(s): `{', '.join(rep.harness_versions) or 'unset'}`  ·  model(s): "
        f"`{', '.join(rep.models) or 'unset'}`",
        f"- {rep.n_specs} specs · {rep.n_attempts} attempts",
        "",
        "## Outcome",
        f"| metric | value |",
        f"|---|---|",
        f"| **pass@1** | {pct(rep.pass_at_1)} |",
        f"| pass@k | {pct(rep.pass_at_k)} |",
        f"| boot-pass | {pct(rep.boot_pass_rate)} |",
        f"| contract-pass | {pct(rep.contract_pass_rate)} |",
        f"| CI-green | {pct(rep.ci_green_rate)} |",
        f"| security-clean | {pct(rep.security_clean_rate)} |",
        f"| leak-clean | {pct(rep.leak_clean_rate)} |",
        f"| **gate-failed-open** | {pct(rep.gate_failed_open_rate)} |",
        f"| acceptance-pass (spec conformance) | {pct(rep.acceptance_pass_rate)} |",
        "",
        "## Cost (the number that decides architecture)",
        f"- total credits: {rep.total_credits:.2f}",
        f"- **credits per passing build: {cr(rep.credits_per_passing_build)}**",
        f"- credits per passing spec: {cr(rep.credits_per_passing_spec)}",
        f"- median wall-clock: {'n/a' if rep.median_wall_clock_s is None else f'{rep.median_wall_clock_s:.0f}s'}",
        "",
        "## Failures",
    ]
    L += [f"- {k}: {v}" for k, v in rep.failure_histogram.items()] or ["- none"]
    L += ["", "## By speciality", "| speciality | specs | pass@1 | boot | cr/pass |", "|---|---|---|---|---|"]
    for s in rep.by_speciality:
        L.append(f"| {s.speciality} | {s.specs} | {pct(s.pass_at_1)} | {pct(s.boot_pass_rate)} "
                 f"| {cr(s.credits_per_passing_build)} |")
    return "\n".join(L) + "\n"
