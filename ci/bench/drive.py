#!/usr/bin/env python3
"""Drive the agent_eval corpus as REAL builds, once per harness, for a
harness-vs-harness comparison (§dev harness).

This is the piece agent_eval was missing. `python -m app.services.agent_eval`
already loads a frozen corpus, captures a DevRunRecord per dev-run outcome and
aggregates records into a report; what nobody could do was turn the corpus into
builds. Its own CLI says so: "Driving the corpus as builds is a separate,
stack-dependent concern."

It drives the CUSTOMER path over HTTP - signup, deposit, onboarding, evaluation,
submit, admin pricing, credit grant, build - because a benchmark that shortcuts
the intake measures a pipeline nobody runs. The per-project harness pin and model
endpoint are set BEFORE the build can dispatch, so each arm differs in exactly
one declared way.

Output is a manifest the reporter joins to the DevRunRecords the pipeline wrote
on its own; this script never computes a metric, so the benchmark and production
are scored by the same code.

Usage (from the worktree root, with the e2e gitserver up):

  python3 ci/bench/drive.py --harness openhands --harness claude_sdk \
      --specs webapp-url-shortener,webapp-feedback-board --reps 1 \
      --endpoint openhands="Benchmark: GPT-5.6 Terra" \
      --endpoint claude_sdk="Benchmark: Claude Sonnet 5" \
      --out /tmp/bench-manifest.json

Environment is e2e.py's (E2E_BASE, E2E_APP_HOST, E2E_MAIL_HOST, ADMIN_*,
E2E_REPO_SSH_URI, E2E_INSTALL_KEY_CMD).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "e2e"))
import e2e  # noqa: E402 - the walkthrough owns the HTTP actor, mail and hooks

CORPUS = Path(__file__).resolve().parents[2] / "backend/app/services/agent_eval/corpus"

# The plan gate's approve option, verbatim - workers/tasks.PLAN_APPROVE_LABEL.
# Matched case-insensitively by the deterministic branch in classify_chat_message.
PLAN_APPROVE = "Approve & build"


def load_specs(names: str) -> list[dict]:
    specs = [json.loads(p.read_text()) for p in sorted(CORPUS.glob("*.json"))]
    if names and names != "all":
        want = [n.strip() for n in names.split(",") if n.strip()]
        by_id = {s["id"]: s for s in specs}
        missing = [n for n in want if n not in by_id]
        if missing:
            raise SystemExit(f"unknown spec id(s): {', '.join(missing)}")
        specs = [by_id[n] for n in want]
    return specs


def admin_session() -> e2e.Actor:
    admin = e2e.Actor("admin")
    admin.login(os.environ["ADMIN_EMAIL"], os.environ["ADMIN_PASSWORD"])
    return admin


def enable_harness_selection(admin: e2e.Actor, harnesses: list[str]) -> None:
    """The pin is ignored while the instance flag is off, so a benchmark that
    forgot this would silently run every arm on the default driver and report a
    dead heat. Turn it on and allow exactly the arms under test."""
    admin.put("/api/admin/settings", {
        "dev_harness_selection_enabled": True,
        "dev_harness_allowed": sorted(set(harnesses) | {"openhands"}),
        "dev_harness_default": "openhands",
    })


def endpoint_ids(admin: e2e.Actor) -> dict[str, str]:
    return {e["label"]: e["id"] for e in admin.get("/api/admin/model-endpoints")}


def drive_one(spec: dict, harness: str, endpoint_id: str | None, rep: int,
              admin: e2e.Actor, build_timeout: float) -> dict:
    """One spec, one harness, one attempt. Returns a manifest row; never raises
    on a build failure - a failed build is a RESULT, and excluding it would
    inflate every rate in the report."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    email = f"bench+{harness}-{spec['id']}-{rep}-{stamp}@example.com"
    row = {"spec_id": spec["id"], "speciality": spec["speciality"], "harness": harness,
           "rep": rep, "email": email, "project_id": None, "error": ""}
    try:
        customer = e2e.Actor("customer")
        customer.refresh_csrf()
        signup = {"email": email, "password": e2e.CUSTOMER_PASSWORD,
                  "account_type": "individual", "full_name": "Bench Bot",
                  "accept_terms": True}
        if (a := customer.altcha()):
            signup["altcha"] = a
        customer.post("/api/auth/signup", signup)
        msg = e2e.wait_for("verification email", lambda: e2e.mailpit_find("Verify your email", email), 90)
        token = re.search(r"/verify-email\?token=([\w.\-]+)", e2e.mailpit_body(msg["ID"])).group(1)
        customer.post("/api/auth/verify-email", {"token": token})
        customer.login(email, e2e.CUSTOMER_PASSWORD)
        org_id = customer.get("/api/auth/me")["org"]["id"]

        project = customer.post("/api/projects", {
            "kind": "ai", "speciality": spec["speciality"],
            "from_scratch": False,
            "description": spec["description"],
            "repos": [{"ssh_uri": e2e.REPO_SSH_URI}],
        })
        pid = project["id"]
        row["project_id"] = pid
        e2e.run_hook("install key", e2e.INSTALL_KEY_CMD,
                     stdin=(project.get("ssh_public_key") or "") + "\n")
        repo = project["repos"][0]
        customer.post(f"/api/projects/{pid}/repos/{repo['id']}/verify-ssh")

        # The two knobs that define this arm, both set before anything can build.
        admin.patch(f"/api/admin/projects/{pid}", {"dev_harness": harness})
        if endpoint_id:
            admin.put(f"/api/admin/projects/{pid}/model-config", {"endpoint_id": endpoint_id})

        # The catalog's onboarding questions are the gate; a corpus spec's own
        # free-text answers ride in its description, which is the frozen part.
        answers = e2e.build_answers(customer.get("/api/meta/questions")["questions"])
        customer.post(f"/api/projects/{pid}/answers", {"answers": answers})
        customer.post(f"/api/projects/{pid}/evaluate")
        ev = e2e.wait_for("evaluation", lambda: (
            (x := customer.get(f"/api/projects/{pid}/evaluation")) and x.get("state") in ("done", "failed") and x), 180)
        estimate = float(((ev.get("estimate") or {}).get("credits")) or 0)
        customer.post(f"/api/projects/{pid}/submit")
        admin.post(f"/api/admin/projects/{pid}/status", {"status": "payment_due", "note": "bench"})
        admin.post(f"/api/admin/orgs/{org_id}/credits", {"amount": estimate + 200, "reason": "bench"})
        e2e.wait_for("development", lambda: (
            (x := customer.get(f"/api/projects/{pid}")) and x["status"] == "development" and x), 180)
        def _wait_terminal(label):
            """A run is over when it reaches a terminal run state OR the project
            leaves development. The second half matters because several paths hand
            the project back with the run row reset to idle, and waiting only on the
            run states blocks until the timeout on a run that already finished."""
            return e2e.wait_for(label, lambda: (
                (x := customer.get(f"/api/projects/{pid}")) and (
                    x.get("dev_run_state") in ("awaiting_merge", "failed", "done")
                    or x["status"] in ("finished", "canceled", "awaiting_customer",
                                       "awaiting_admin")) and x), build_timeout)

        final = _wait_terminal("first terminal state")

        # §working method plan gate: a fresh ai-kind MVP does NOT build on the first
        # dispatch. It runs a PLAN-ONLY pass, writes a plan and parks awaiting the
        # customer's approval - so a driver that stops at the first terminal state
        # measures planning, not building, and reports it as a build. (It did: an
        # entire 12-run sweep turned out to be plan passes, every one of them
        # scoring pass@1=0 because a proposed plan is correctly not a delivery.)
        # Approving is a plain chat message on main, handled by the deterministic
        # branch ahead of the classifier.
        row["crossed_plan_gate"] = False
        if (final.get("dev_plan_status") or "") == "proposed":
            customer.post(f"/api/projects/{pid}/messages",
                          {"thread": "main", "body": PLAN_APPROVE})
            row["crossed_plan_gate"] = True
            e2e.wait_for("build starts after plan approval", lambda: (
                (x := customer.get(f"/api/projects/{pid}")) and
                x["status"] == "development" and x), 300)
            final = _wait_terminal("build terminal state")
        row["final_state"] = final.get("dev_run_state")
        row["plan_status"] = final.get("dev_plan_status")
        row["pr_url"] = final.get("dev_pr_url")
        row["harness_version"] = final.get("dev_harness_version")
        row["status"] = final.get("status")
        gate = " (via plan gate)" if row.get("crossed_plan_gate") else ""
        print(f"  [{harness}/{spec['id']}#{rep}] {row['final_state']}{gate} "
              f"hv={row['harness_version']}")
    except Exception as exc:  # noqa: BLE001 - a failed arm is data, not a crash
        row["error"] = f"{type(exc).__name__}: {exc}"[:400]
        print(f"  [{harness}/{spec['id']}#{rep}] DRIVER ERROR {row['error']}", file=sys.stderr)
    return row


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--harness", action="append", required=True)
    ap.add_argument("--specs", default="all")
    ap.add_argument("--reps", type=int, default=1)
    ap.add_argument("--endpoint", action="append", default=[],
                    help="harness=<model endpoint label>")
    ap.add_argument("--build-timeout", type=float, default=2400)
    ap.add_argument("--out", default="bench-manifest.json")
    args = ap.parse_args()

    specs = load_specs(args.specs)
    admin = admin_session()
    enable_harness_selection(admin, args.harness)
    labels = endpoint_ids(admin)
    wanted = {}
    for pair in args.endpoint:
        h, _, label = pair.partition("=")
        if label not in labels:
            raise SystemExit(f"no model endpoint labelled {label!r} (have: {', '.join(labels)})")
        wanted[h] = labels[label]

    print(f"driving {len(specs)} spec(s) x {len(args.harness)} harness(es) x {args.reps} rep(s) "
          f"= {len(specs) * len(args.harness) * args.reps} builds")
    rows = []
    for rep in range(1, args.reps + 1):
        for spec in specs:
            for harness in args.harness:
                rows.append(drive_one(spec, harness, wanted.get(harness), rep, admin,
                                      args.build_timeout))
                Path(args.out).write_text(json.dumps(rows, indent=2))
    Path(args.out).write_text(json.dumps(rows, indent=2))
    print(f"manifest -> {args.out} ({len(rows)} rows)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
