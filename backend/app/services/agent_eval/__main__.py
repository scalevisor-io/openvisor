"""CLI: `python -m app.services.agent_eval <command>`

  version               print the current harness version + its config (no side effects)
  validate [dir]        load + validate the corpus, print the per-speciality stratification
  report <records.json> aggregate captured run records into a markdown report
  report --db [hv]      aggregate the DevRunRecords captured from live builds (optionally
                        scoped to one harness version hv - never mix versions)

Live builds populate DevRunRecords automatically (workers.tasks.run_development). Driving
the corpus as builds is a separate, stack-dependent concern.
"""
from __future__ import annotations

import json
import sys

from app.services.agent_eval.corpus import load_corpus, stratify
from app.services.agent_eval.harness_version import compute_harness_version, harness_config
from app.services.agent_eval.metrics import RunRecord
from app.services.agent_eval.report import aggregate, render_markdown


def _cmd_version() -> int:
    from app.core.config import settings
    print(compute_harness_version(settings))
    print(json.dumps(harness_config(settings), indent=2, sort_keys=True))
    return 0


def _cmd_validate(argv: list[str]) -> int:
    from pathlib import Path
    specs = load_corpus(Path(argv[0]) if argv else None)
    strat = stratify(specs)
    print(f"corpus OK: {len(specs)} specs across {len(strat)} specialities")
    for spec, items in sorted(strat.items()):
        print(f"  {spec:24s} {len(items):>2d}  ({', '.join(s.id for s in items)})")
    return 0


def _cmd_report(argv: list[str]) -> int:
    if argv and argv[0] == "--db":
        from app.core.db import SyncSession
        from app.services.agent_eval.collect import load_records
        hv = argv[1] if len(argv) > 1 else None
        with SyncSession() as db:
            records = load_records(db, source="live", harness_version=hv)
        print(render_markdown(aggregate(records)))
        return 0
    if not argv:
        print("usage: report <records.json> | report --db [harness_version]", file=sys.stderr)
        return 2
    raw = json.loads(open(argv[0]).read())
    records = [RunRecord.from_dict(d) for d in (raw.get("records", raw) if isinstance(raw, dict) else raw)]
    print(render_markdown(aggregate(records)))
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print(__doc__)
        return 0
    cmd, rest = argv[0], argv[1:]
    if cmd == "version":
        return _cmd_version()
    if cmd == "validate":
        return _cmd_validate(rest)
    if cmd == "report":
        return _cmd_report(rest)
    print(f"unknown command: {cmd}\n{__doc__}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
