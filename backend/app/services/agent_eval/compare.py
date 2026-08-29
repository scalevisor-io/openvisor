"""Harness-vs-harness comparison over captured DevRunRecords (§dev harness).

report.aggregate already scores a set of runs; this joins a benchmark manifest
(ci/bench/drive.py) to the records the pipeline captured on its own, groups them
by harness, and puts the two columns side by side.

Two rules the renderer enforces rather than assumes:

- A harness arm whose runs carry more than one `harness_version` is NOT a single
  configuration, and its column is flagged. That is the whole reason the
  fingerprint exists - a prompt edit or a cap change mid-benchmark silently makes
  the two halves incomparable.
- The headline is credits per passing build, not pass rate. A harness that buys
  +3pp quality for +80% tokens is a business loss, and reading pass rate first is
  how that gets shipped anyway.
"""
from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy.orm import Session

from app.models import DevRunRecord
from app.services.agent_eval.metrics import RunRecord
from app.services.agent_eval.report import aggregate


def _to_run_record(row: DevRunRecord, spec_id: str) -> RunRecord:
    return RunRecord(
        spec_id=spec_id, speciality=row.speciality or "",
        harness_version=row.harness_version or "", model=row.model or "",
        attempt=row.attempt or 1, final_state=row.final_state or "",
        boot_result=row.boot_result, contract_ok=row.contract_ok,
        ci_status=row.ci_status, security_blocking=row.security_blocking or 0,
        security_ran=bool(row.security_ran), leak_blocked=bool(row.leak_blocked),
        leak_scanner_errored=bool(row.leak_scanner_errored),
        input_tokens=row.input_tokens or 0, output_tokens=row.output_tokens or 0,
        credits=row.credits or 0.0, wall_clock_s=row.wall_clock_s or 0.0,
        acceptance_passed=row.acceptance_passed, acceptance_total=row.acceptance_total,
        error=row.error or "",
    )


def load_manifest(db: Session, manifest_path: str | Path) -> dict[str, list[RunRecord]]:
    """{harness: [RunRecord]} for the builds a benchmark run drove.

    The manifest carries the corpus spec id; DevRunRecord.spec_id holds the
    project id for a live run, so the join relabels it - without that, pass@k
    cannot see two attempts at the same spec as the same spec.
    """
    manifest = json.loads(Path(manifest_path).read_text())
    by_harness: dict[str, list[RunRecord]] = {}
    for entry in manifest:
        pid = entry.get("project_id")
        if not pid:
            continue
        rows = (db.query(DevRunRecord)
                .filter(DevRunRecord.project_id == pid)
                .order_by(DevRunRecord.attempt).all())
        for row in rows:
            by_harness.setdefault(entry["harness"], []).append(
                _to_run_record(row, entry["spec_id"]))
    return by_harness


def _fmt(value, suffix: str = "") -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.3f}{suffix}" if abs(value) < 100 else f"{value:.0f}{suffix}"
    return f"{value}{suffix}"


def render_comparison(by_harness: dict[str, list[RunRecord]]) -> str:
    """A markdown table, one column per harness, cost first."""
    names = sorted(by_harness)
    if not names:
        return "No records matched the manifest - did any build reach a terminal state?"
    reports = {n: aggregate(by_harness[n]) for n in names}

    lines = ["# Harness comparison", ""]
    for n in names:
        versions = sorted({r.harness_version for r in by_harness[n] if r.harness_version})
        models = sorted({r.model for r in by_harness[n] if r.model})
        lines.append(f"- **{n}**: {len(by_harness[n])} attempt(s), model(s) {', '.join(models) or '?'}, "
                     f"harness_version {', '.join(versions) or '?'}")
        if len(versions) > 1:
            lines.append(f"  - **NOT ONE CONFIGURATION**: {len(versions)} harness versions in this arm. "
                         "Its numbers are not comparable with anything, including itself.")
    lines.append("")

    rows = [
        ("credits per passing build", lambda r: r.credits_per_passing_build),
        ("credits per passing spec", lambda r: r.credits_per_passing_spec),
        ("total credits", lambda r: r.total_credits),
        ("pass@1", lambda r: r.pass_at_1),
        ("pass@k", lambda r: r.pass_at_k),
        ("boot pass rate", lambda r: r.boot_pass_rate),
        ("security clean rate", lambda r: r.security_clean_rate),
        ("leak clean rate", lambda r: r.leak_clean_rate),
        ("gate failed open rate", lambda r: r.gate_failed_open_rate),
        ("acceptance pass rate", lambda r: r.acceptance_pass_rate),
        ("median wall clock (s)", lambda r: r.median_wall_clock_s),
        ("attempts", lambda r: r.n_attempts),
        ("specs", lambda r: r.n_specs),
    ]
    lines.append("| metric | " + " | ".join(names) + " |")
    lines.append("|---|" + "|".join(["---"] * len(names)) + "|")
    for label, pick in rows:
        lines.append(f"| {label} | " + " | ".join(_fmt(pick(reports[n])) for n in names) + " |")

    lines += ["", "## Failure histogram", ""]
    for n in names:
        hist = reports[n].failure_histogram or {}
        detail = ", ".join(f"{k}={v}" for k, v in sorted(hist.items())) or "none"
        lines.append(f"- **{n}**: {detail}")

    warnings = {n: reports[n].warnings for n in names if reports[n].warnings}
    if warnings:
        lines += ["", "## Warnings", ""]
        for n, ws in warnings.items():
            for w in ws:
                lines.append(f"- **{n}**: {w}")
    return "\n".join(lines)
