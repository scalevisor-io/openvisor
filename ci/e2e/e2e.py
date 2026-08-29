#!/usr/bin/env python3
"""Headless end-to-end check of the customer <-> consultant workflow (CI e2e).

Replays `.claude/commands/e2e-check.md` over the HTTP API against a running
compose stack reached through Traefik with Host headers, acting as both the
customer and the admin: signup -> email verification -> project deposit ->
evaluation -> submit -> memory / request / human-answer -> admin pricing ->
credits -> auto-advance to development -> the §14 pipeline pushes the build
branch to the customer repo -> the customer merges -> the demo deploys ->
delivery approval. No browser, no LLM tokens (`OPENHANDS_ENABLED=0` drives the
deterministic scaffold through the exact same push -> merge -> deploy path).

Configuration is by environment (see `ci/e2e/README.md`); the two hooks that
touch the customer repository - installing the project's deploy key and
merging the build branch - are shell commands, so the same script runs against
the CI git server and against a real GitHub repository.

Exit status is non-zero on the first failed step; every step prints PASS/FAIL
with its evidence so the CI log reads like the manual report.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Any

import requests

# --- configuration -----------------------------------------------------------

BASE = os.environ.get("E2E_BASE", "http://127.0.0.1:8090").rstrip("/")
APP_HOST = os.environ.get("E2E_APP_HOST", "app.openvisor2.local")
MAIL_HOST = os.environ.get("E2E_MAIL_HOST", "mail.openvisor2.local")
ADMIN_EMAIL = os.environ["ADMIN_EMAIL"]
ADMIN_PASSWORD = os.environ["ADMIN_PASSWORD"]
REPO_SSH_URI = os.environ["E2E_REPO_SSH_URI"]
# Shell hooks. The deploy key hook receives the project's public key on stdin;
# the merge hook receives the build branch name as $E2E_BRANCH and must merge it
# into the repository's default branch so the platform's merge sweep sees it.
INSTALL_KEY_CMD = os.environ.get("E2E_INSTALL_KEY_CMD", "")
MERGE_CMD = os.environ.get("E2E_MERGE_CMD", "")
ALTCHA_ENABLED = os.environ.get("E2E_ALTCHA", "0") == "1"
TIMEOUT_BUILD = int(os.environ.get("E2E_BUILD_TIMEOUT", "900"))
TIMEOUT_DEMO = int(os.environ.get("E2E_DEMO_TIMEOUT", "300"))
CUSTOMER_PASSWORD = "OpenvisorE2E!2026"

# --- reporting ---------------------------------------------------------------


class StepFailed(Exception):
    pass


@dataclass
class Report:
    steps: list[tuple[str, str, str]] = field(default_factory=list)

    def ok(self, name: str, evidence: str = "") -> None:
        self.steps.append((name, "PASS", evidence))
        print(f"PASS  {name}" + (f"  [{evidence}]" if evidence else ""), flush=True)

    def fail(self, name: str, evidence: str) -> None:
        self.steps.append((name, "FAIL", evidence))
        print(f"FAIL  {name}  [{evidence}]", flush=True)
        raise StepFailed(name)


report = Report()


def check(cond: bool, name: str, evidence: str) -> None:
    if cond:
        report.ok(name, evidence)
    else:
        report.fail(name, evidence)


def _brief(value: Any) -> str:
    """The evidence worth printing for a polled value: the state-bearing fields
    of a payload, or a short repr."""
    if isinstance(value, dict):
        keys = ("status", "dev_run_state", "demo_state", "state", "Subject")
        picked = {k: value[k] for k in keys if k in value}
        return ", ".join(f"{k}={v}" for k, v in picked.items()) or json.dumps(value)[:120]
    return repr(value)[:120]


def wait_for(name: str, fn, timeout: float, every: float = 3.0):
    """Poll `fn` until it returns a truthy value (reported as the step's
    evidence); fail the step at the deadline."""
    deadline = time.monotonic() + timeout
    t0 = time.monotonic()
    last: Any = None
    while time.monotonic() < deadline:
        last = fn()
        if last:
            report.ok(name, f"{_brief(last)} after {time.monotonic() - t0:.0f}s")
            return last
        time.sleep(every)
    report.fail(name, f"timed out after {timeout:.0f}s, last={_brief(last)}")


# --- HTTP actors -------------------------------------------------------------


class Actor:
    """One authenticated session: cookie jar + the CSRF token the SPA sends."""

    def __init__(self, label: str) -> None:
        self.label = label
        self.s = requests.Session()
        self.s.headers["Host"] = APP_HOST
        self.csrf = ""

    def refresh_csrf(self) -> None:
        r = self.s.get(f"{BASE}/api/auth/csrf", timeout=30)
        r.raise_for_status()
        self.csrf = r.json()["csrf_token"]

    def call(self, method: str, path: str, ok: tuple[int, ...] = (200, 201), **kw) -> requests.Response:
        headers = kw.pop("headers", {})
        if method != "GET":
            if not self.csrf:
                self.refresh_csrf()
            headers["X-CSRF-Token"] = self.csrf
        r = self.s.request(method, f"{BASE}{path}", headers=headers, timeout=kw.pop("timeout", 60), **kw)
        if r.status_code not in ok:
            body = r.text[:400].replace("\n", " ")
            report.fail(f"{self.label} {method} {path}", f"HTTP {r.status_code}: {body}")
        return r

    def get(self, path: str, **kw) -> Any:
        return self.call("GET", path, **kw).json()

    def post(self, path: str, body: dict | None = None, **kw) -> Any:
        return self.call("POST", path, json=body if body is not None else {}, **kw).json()

    def put(self, path: str, body: dict | None = None, **kw) -> Any:
        return self.call("PUT", path, json=body if body is not None else {}, **kw).json()

    def patch(self, path: str, body: dict | None = None, **kw) -> Any:
        return self.call("PATCH", path, json=body if body is not None else {}, **kw).json()

    def altcha(self) -> str | None:
        """Solve one proof-of-work challenge (single use: one per signup/login)."""
        if not ALTCHA_ENABLED:
            return None
        c = self.get("/api/auth/altcha")
        for n in range(c["maxnumber"] + 1):
            if hashlib.sha256(f"{c['salt']}{n}".encode()).hexdigest() == c["challenge"]:
                payload = {"algorithm": c["algorithm"], "challenge": c["challenge"], "number": n,
                           "salt": c["salt"], "signature": c["signature"]}
                return base64.b64encode(json.dumps(payload).encode()).decode()
        raise RuntimeError("altcha challenge not solvable")

    def login(self, email: str, password: str) -> dict:
        self.refresh_csrf()
        body = {"email": email, "password": password}
        if (a := self.altcha()):
            body["altcha"] = a
        me = self.post("/api/auth/login", body)
        self.refresh_csrf()
        return me


def mailpit_messages() -> list[dict]:
    r = requests.get(f"{BASE}/api/v1/messages", headers={"Host": MAIL_HOST}, timeout=30)
    r.raise_for_status()
    return r.json().get("messages", [])


def mailpit_find(subject_fragment: str, to: str | None = None) -> dict | None:
    for m in mailpit_messages():
        if subject_fragment in m.get("Subject", ""):
            if to is None or any(to == a.get("Address") for a in m.get("To", [])):
                return m
    return None


def mailpit_body(msg_id: str) -> str:
    r = requests.get(f"{BASE}/api/v1/message/{msg_id}", headers={"Host": MAIL_HOST}, timeout=30)
    r.raise_for_status()
    j = r.json()
    return (j.get("Text") or "") + "\n" + (j.get("HTML") or "")


def run_hook(name: str, cmd: str, stdin: str = "", env: dict | None = None) -> None:
    if not cmd:
        report.fail(name, "no hook command configured")
    proc = subprocess.run(cmd, shell=True, input=stdin, text=True, capture_output=True,
                          env={**os.environ, **(env or {})}, timeout=300)
    tail = (proc.stdout + proc.stderr)[-600:].replace("\n", " | ")
    check(proc.returncode == 0, name, f"exit {proc.returncode}: {tail}")


# --- the walkthrough ---------------------------------------------------------


def build_answers(questions: list[dict]) -> list[dict]:
    """The plainest MVP's onboarding answers.

    These option ids stay clear of the catalog's deterministic review triggers:
    sensitive / classified / air-gapped work is flagged for the consultant, which
    is a different path than the one under test. Shared with the harness benchmark
    (ci/bench/drive.py) so a corpus build and the walkthrough enter the pipeline
    through the same door - a benchmark whose projects get routed to review
    measures nothing.
    """
    preferred = {"project_type": "web_app", "audience": "internal_team",
                 "data_sensitivity": "public_non_sensitive",
                 "hosting_target": "no_preference", "auth_needs": "email_password",
                 "expected_scale": "prototype_demo", "timeline": "asap_quick_mvp"}
    trigger = re.compile(r"classified|defen[cs]e|air.?gap|on.?prem|regulated|health|payment|personal", re.I)
    answers = []
    for q in questions:
        opts = q.get("options") or []
        if not opts or q.get("show_if"):
            continue  # conditional questions only show for other answers
        ids = [o["id"] for o in opts]
        pick = preferred.get(q["id"]) if preferred.get(q["id"]) in ids else next(
            (o["id"] for o in opts if not trigger.search(o["id"] + " " + o.get("label", ""))), ids[0])
        answers.append({"question_id": q["id"], "option_ids": [pick]})
    return answers


def main() -> int:
    t0 = time.monotonic()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    email = f"e2e+{stamp}@example.com"
    customer = Actor("customer")
    admin = Actor("admin")

    # 0. Prep: the stack answers.
    r = requests.get(f"{BASE}/api/health", headers={"Host": APP_HOST}, timeout=30)
    check(r.status_code == 200, "0 stack healthy", f"GET /api/health -> {r.status_code}")

    # 1. Customer signup + email verification + login.
    customer.refresh_csrf()
    body = {"email": email, "password": CUSTOMER_PASSWORD, "account_type": "individual",
            "full_name": "E2E Bot", "accept_terms": True}
    if (a := customer.altcha()):
        body["altcha"] = a
    customer.post("/api/auth/signup", body)
    msg = wait_for("1 verification email in mailpit", lambda: mailpit_find("Verify your email", email), 90)
    m = re.search(r"/verify-email\?token=([\w.\-]+)", mailpit_body(msg["ID"]))
    check(m is not None, "1 verification link found", msg["Subject"])
    customer.post("/api/auth/verify-email", {"token": m.group(1)})
    me = customer.login(email, CUSTOMER_PASSWORD)
    check(me["user"]["email_verified"] is True, "1 customer signup + verify + login", email)
    org_id = customer.get("/api/auth/me")["org"]["id"]

    # 2. Project deposit with an existing repository, onboarding answers,
    #    evaluation and submission.
    specialities = customer.get("/api/meta/specialities")
    spec = next((s["id"] for s in specialities if s["id"] == "general-webapp"), specialities[0]["id"])
    project = customer.post("/api/projects", {
        "kind": "ai", "speciality": spec, "from_scratch": False,
        "description": f"TodoApp E2E {stamp}: a small todo web application with a list, add, "
                       "complete and delete actions, persisted in the browser.",
        "repos": [{"ssh_uri": REPO_SSH_URI}],
    })
    pid = project["id"]
    pubkey = project.get("ssh_public_key") or ""
    check(pubkey.startswith("ssh-"), "2 project created with deploy key", f"id={pid} key={pubkey[:20]}...")
    repo = project["repos"][0]
    check(repo.get("is_push_target") is True, "2 connected repo is the push target", repo.get("ssh_uri", ""))
    run_hook("2 deploy key installed on the repository", INSTALL_KEY_CMD, stdin=pubkey + "\n")
    verify = customer.post(f"/api/projects/{pid}/repos/{repo['id']}/verify-ssh")
    check(verify.get("ok") is True, "2 verify SSH (read + push preflight)", json.dumps(verify)[:200])

    answers = build_answers(customer.get("/api/meta/questions")["questions"])
    customer.post(f"/api/projects/{pid}/answers", {"answers": answers})
    customer.post(f"/api/projects/{pid}/evaluate")
    ev = wait_for("2 evaluation finished",
                  lambda: (e := customer.get(f"/api/projects/{pid}/evaluation")) and e.get("state") in ("done", "failed") and e,
                  180)
    verdict = (ev.get("feasibility") or {}).get("verdict")
    estimate = float(((ev.get("estimate") or {}).get("credits")) or 0)
    check(ev["state"] == "done" and verdict in ("pass", "review_required") and estimate > 0,
          "2 evaluation done", f"verdict={verdict} estimate={estimate} credits")
    p = customer.post(f"/api/projects/{pid}/submit")
    check(p["status"] == "awaiting_review", "2 submitted for review", f"status={p['status']}")

    # 3. Memory (plain + secret), a feature request, a chat message, and the
    #    free human-answer escalation.
    customer.call("PUT", f"/api/projects/{pid}/memory",
                  json={"key": "SUPABASE_URL", "value": "https://example.supabase.co", "is_secret": False, "description": ""})
    customer.call("PUT", f"/api/projects/{pid}/memory",
                  json={"key": "SUPABASE_ANON_KEY", "value": "anon-e2e-secret", "is_secret": True, "description": ""})
    mem = {m["key"]: m for m in customer.get(f"/api/projects/{pid}/memory")}
    check(mem["SUPABASE_ANON_KEY"]["is_secret"] is True and mem["SUPABASE_URL"]["is_secret"] is False,
          "3 memory plain + secret", ",".join(sorted(mem)))
    req = customer.post(f"/api/projects/{pid}/requests", {"type": "feature", "handling": "ai", "body": "Add a dark-mode toggle."})
    check(req.get("status") in ("open", "proposed", "quoted"), "3 feature request created", f"status={req.get('status')}")
    customer.post(f"/api/projects/{pid}/messages", {"thread": "main", "body": "Hello, when does the build start?"})
    customer.post(f"/api/projects/{pid}/request-human-answer", {"thread": "main"})
    p = customer.get(f"/api/projects/{pid}")
    check(p["status"] == "awaiting_admin", "3 request human answer -> awaiting_admin", f"status={p['status']}")
    wait_for("3 admin notified by email", lambda: mailpit_find("", ADMIN_EMAIL), 90)

    # 4. Consultant review: clear any review block, price it, customer emailed.
    admin.login(ADMIN_EMAIL, ADMIN_PASSWORD)
    overview = admin.get("/api/admin/overview")
    row = next((x for x in overview["projects"] if x["id"] == pid), None)
    check(row is not None and row.get("org_id") == org_id, "4 admin sees the project", f"org_id={org_id}")
    admin.call("PATCH", f"/api/admin/projects/{pid}", json={"block_auto_development": False})
    p = admin.post(f"/api/admin/projects/{pid}/status", {"status": "payment_due", "note": "E2E: ready for payment"})
    check(p["status"] == "payment_due", "4 admin -> payment_due", f"status={p['status']}")
    wait_for("4 customer payment_due email", lambda: mailpit_find("payment_due", email), 90)

    # 5. Payment: the documented local fallback (a credit grant auto-advances
    #    payment_due -> development once the wallet covers the estimate).
    grant = admin.post(f"/api/admin/orgs/{org_id}/credits", {"amount": estimate + 50, "reason": "E2E topup"})
    check(float(grant.get("credit_balance", 0)) >= estimate, "5 credits granted", f"balance={grant.get('credit_balance')}")
    p = wait_for("5 auto-advance payment_due -> development",
                 lambda: (x := customer.get(f"/api/projects/{pid}")) and x["status"] == "development" and x, 120)

    # 6. Dev pipeline: the run pushes the build branch and parks awaiting merge.
    p = wait_for("6 build pushed, awaiting merge",
                 lambda: (x := customer.get(f"/api/projects/{pid}")) and (
                     x.get("dev_run_state") in ("awaiting_merge", "failed") or x["status"] in ("finished", "canceled")) and x,
                 TIMEOUT_BUILD, every=5)
    branch = p.get("dev_branch") or ""
    check(p.get("dev_run_state") == "awaiting_merge" and bool(branch),
          "6 dev_run_state=awaiting_merge", f"branch={branch} error={p.get('dev_run_error')}")
    feed = customer.get(f"/api/projects/{pid}/dev-activity?offset=0")
    check(len(feed.get("events", [])) > 0, "6 build console has events", f"{len(feed.get('events', []))} events, state={feed.get('state')}")

    # 7. The customer merges the build branch; the merge sweep deploys the demo.
    run_hook("7 build branch merged into the default branch", MERGE_CMD, env={"E2E_BRANCH": branch})
    p = wait_for("7 demo deployed after merge",
                 lambda: (x := customer.get(f"/api/projects/{pid}")) and x.get("demo_state") == "running" and x.get("demo_url") and x,
                 TIMEOUT_DEMO, every=5)
    demo_url = p["demo_url"]
    host = re.sub(r"^https?://", "", demo_url).split("/")[0].split(":")[0]
    dr = requests.get(f"{BASE}/", headers={"Host": host}, auth=(p["demo_basic_auth_user"], p["demo_basic_auth_pass"]),
                      timeout=60, allow_redirects=True)
    check(dr.status_code < 400, "7 demo answers over Traefik with basic auth", f"{demo_url} -> HTTP {dr.status_code}")
    # demo_start finalizes the owning run and hands the project back to the
    # customer; Request #0 (the MVP) only closes with the terminal transition.
    p = wait_for("7 run finalized, project handed back",
                 lambda: (x := customer.get(f"/api/projects/{pid}")) and x.get("dev_run_state") == "done"
                 and x["status"] == "awaiting_customer" and x, 60)

    # 8. Fail-safe surface: the run ledger and status history are populated.
    runs = customer.get(f"/api/projects/{pid}/dev-runs")
    hist = customer.get(f"/api/projects/{pid}/status-history")
    check(len(runs) >= 1 and any(h["to"] == "development" for h in hist),
          "8 dev-run ledger + status history", f"runs={len(runs)} history={[h['to'] for h in hist]}")

    # 9. Delivery approval.
    p = customer.post(f"/api/projects/{pid}/approve-delivery")
    check(p["status"] == "finished", "9 approve delivery -> finished", f"status={p['status']}")
    reqs = customer.get(f"/api/projects/{pid}/requests")
    mvp = next((r for r in reqs if r.get("type") == "mvp"), None)
    check(mvp is not None and mvp.get("status") == "done", "9 initial build request closed", f"mvp={mvp and mvp.get('status')}")
    wait_for("9 customer finished email", lambda: mailpit_find("finished", email), 90)

    print(f"\nALL STEPS PASSED in {time.monotonic() - t0:.0f}s  project={pid} branch={branch} demo={demo_url}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except StepFailed as exc:
        print(f"\nE2E FAILED at step: {exc}", file=sys.stderr)
        sys.exit(1)
