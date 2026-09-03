"""§delivery reconciler - what a request's change IS on its repository, observed
from the git provider on every tick, and what the platform should do about it.

Before this module the platform knew a delivery only through pointers it had
stamped on itself (`Project.dev_pr_number`, `DevRun.pr_number`, `Request.pr_urls`)
at the moment one of a run's exit paths executed, and the merge sweep watched
only runs parked in `awaiting_merge`. A merge that landed after the run had
parked, a pointer a Start fresh had cleared, a park on `ci_timeout` that dropped
the run out of the sweep's selection - each left a delivered change reported to
the customer as a failure, and the only affordance for "delivery did not
complete" was to run the agent again. (Prod, 2026-09-03: an MVP whose MR merged
on its own six minutes after the platform gave up on it was rebuilt twice.)

The reconciler asks the repository instead. `observe` gathers every change whose
source branch belongs to the request, in ANY state, plus whatever the pointers
still name, with the CI verdict and merge status of the open one; `decide` is a
pure table over that snapshot (unit-tested without a provider); the worker
applies the verdict (`tasks.reconcile_delivery`) through the same actuators a
run's own exit paths use. Level-triggered: it runs for every in-progress request
on every tick and before every Resume, and a verdict that was already acted on
is not re-announced. Nothing here waits or sleeps - one API round per request
per tick.
"""
from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Callable

from app.services import github, gitlab

log = logging.getLogger(__name__)

LIVE_RUN_STATES = ("queued", "running", "deploying")

# GitLab `detailed_merge_status` values, grouped by who can clear them. Anything
# not listed is treated as still settling (GitLab computes mergeability
# asynchronously; "checking"/"unchecked" are normal for a few seconds).
CONFLICT_STATUSES = frozenset({"conflict", "need_rebase"})
HUMAN_STATUSES = frozenset({
    "draft_status", "discussions_not_resolved", "not_approved", "blocked_status",
    "policies_denied", "external_status_checks", "requested_changes",
    "jira_association_missing",
})
# How long an open change may sit with no pipeline at all before the platform
# stops waiting for one and merges directly (auto_merge's own no-CI grace, made
# level-triggered). Measured from the change's last update.
NO_PIPELINE_GRACE_S = 180


@dataclass
class Change:
    """One PR/MR as the provider reports it."""
    number: int | None
    url: str | None
    state: str                      # open | merged | closed
    branch: str | None
    provider: str                   # gitlab | github
    head_sha: str | None = None
    merge_status: str | None = None  # GitLab detailed_merge_status (platform only)
    mwps: bool = False              # merge_when_pipeline_succeeds already armed
    ci: str | None = None           # none | pending | success | failure | None = unknown
    updated_at: datetime | None = None
    source: str = "branch"          # branch | pointer | ssh - how it was found

    def age_s(self, now: datetime | None = None) -> float | None:
        if self.updated_at is None:
            return None
        now = now or datetime.now(timezone.utc)
        return (now - self.updated_at).total_seconds()


@dataclass
class Snapshot:
    """Everything `decide` needs, gathered in one place."""
    provider: str | None            # gitlab | github | other | None (no target)
    platform: bool                  # the platform's own GitLab (auto-merge contract)
    target: dict | None = None      # the resolved dev target (tasks._dev_target)
    changes: list[Change] = field(default_factory=list)   # newest first
    live_run_state: str | None = None     # queued/running/deploying, else None
    latest_run_state: str | None = None   # newest row of the request (any state)
    delivered_numbers: frozenset = frozenset()  # change numbers a `done` row of the request carries
    ci_available: bool | None = None      # platform only; None = not checked
    branches: list[str] = field(default_factory=list)
    error: str | None = None              # an observation that failed (fail-soft)
    observed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def newest(self) -> Change | None:
        return self.changes[0] if self.changes else None

    @property
    def landed(self) -> Change | None:
        return next((c for c in self.changes if c.state == "merged"), None)

    @property
    def open(self) -> Change | None:
        return next((c for c in self.changes if c.state == "open"), None)


@dataclass
class Verdict:
    """The next action and why. `key` is the idempotence fingerprint: the same
    key on consecutive ticks means the same situation, already announced."""
    action: str          # wait | deploy | finalize | merge | arm | fix_ci | reject | park | idle
    change: Change | None
    note: str
    fault: str | None = None   # dev_faults.PLATFORM when the customer cannot act
    settle: bool = False       # the run should sit in awaiting_merge while waiting

    @property
    def key(self) -> str:
        n = self.change.number if self.change is not None else ""
        return f"{self.action}|{n}|{self.note}"


# ---------------------------------------------------------------- observe

def _iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None


def _gl_change(mr: dict, source: str) -> Change:
    state = {"merged": "merged", "closed": "closed"}.get(mr.get("state"), "open")
    return Change(number=mr.get("iid"), url=mr.get("web_url"), state=state,
                  branch=mr.get("source_branch"), provider="gitlab",
                  head_sha=mr.get("sha"),
                  merge_status=mr.get("detailed_merge_status") or mr.get("merge_status"),
                  mwps=bool(mr.get("merge_when_pipeline_succeeds")),
                  updated_at=_iso(mr.get("updated_at") or mr.get("created_at")),
                  source=source)


def _gh_change(pr: dict, source: str) -> Change:
    merged = bool(pr.get("merged") or pr.get("merged_at"))
    state = "merged" if merged else ("closed" if pr.get("state") == "closed" else "open")
    head = pr.get("head") or {}
    return Change(number=pr.get("number"), url=pr.get("html_url") or pr.get("url"),
                  state=state, branch=head.get("ref"), provider="github",
                  head_sha=head.get("sha"),
                  updated_at=_iso(pr.get("updated_at") or pr.get("created_at")),
                  source=source)


def _from_pointer(change: Change, number: int, url: str | None) -> Change:
    """A change fetched BY NUMBER is that number even when the payload omits
    it (a slim proxy, a fake in tests); the pointer's URL fills in likewise."""
    change.number = change.number or number
    change.url = change.url or url
    return change


def _gl_ci(pipe: dict | None) -> str:
    if not pipe:
        return "none"
    status = pipe.get("status")
    if status in ("failed", "canceled"):
        return "failure"
    return "success" if status == "success" else "pending"


def observe(*, target: dict | None, gitlab_project_id: int | None,
            branches: list[str], pointers: list[tuple[int, str | None]],
            token: str | None, live_run_state: str | None,
            latest_run_state: str | None,
            ssh_merged: Callable[[], bool | None] | None = None,
            base_branch: str = "main",
            delivered_numbers: frozenset = frozenset()) -> Snapshot:
    """Gather the request's changes from the provider. Fail-soft per call: a
    listing that errors is logged and the pointers still resolve; a pointer that
    errors is skipped. A request with a build in flight is not observed at all
    (nothing to reconcile until it ends)."""
    # No resolved target with a platform project id still means the platform
    # GitLab (a project provisioned before it had an SSH URL, or a test double)
    # - the same reading `_change_is_merged` always had.
    provider = target["provider"] if target else ("gitlab" if gitlab_project_id else None)
    platform = bool(gitlab_project_id) and (
        target is None or (provider == "gitlab" and not target.get("customer")))
    snap = Snapshot(provider=provider, platform=platform, target=target,
                    branches=list(branches), live_run_state=live_run_state,
                    latest_run_state=latest_run_state,
                    delivered_numbers=frozenset(delivered_numbers))
    if live_run_state in LIVE_RUN_STATES or (target is None and not platform):
        return snap
    found: dict[int, Change] = {}
    errors: list[str] = []

    def keep(change: Change) -> None:
        if change.number is not None and change.number not in found:
            found[change.number] = change

    try:
        if platform:
            if not gitlab_project_id:
                return snap
            for b in branches:
                try:
                    for mr in gitlab.list_mrs_for_branch(gitlab_project_id, b):
                        keep(_gl_change(mr, "branch"))
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"list {b}: {exc}")
            for n, _url in pointers:
                if n in found:
                    continue
                try:
                    keep(_from_pointer(_gl_change(gitlab.get_mr(gitlab_project_id, n),
                                                  "pointer"), n, _url))
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"mr !{n}: {exc}")
            for c in found.values():
                if c.state == "open" and c.head_sha:
                    try:
                        c.ci = _gl_ci(gitlab.pipeline_for_sha(gitlab_project_id, c.head_sha))
                    except Exception as exc:  # noqa: BLE001
                        errors.append(f"pipeline {c.head_sha[:8]}: {exc}")
                    if c.ci in ("none", "pending"):
                        try:
                            snap.ci_available = gitlab.ci_available(gitlab_project_id)
                        except Exception as exc:  # noqa: BLE001
                            errors.append(f"ci availability: {exc}")
        elif provider == "github" and token:
            owner, repo = target["owner"], target["repo"]
            for b in branches:
                try:
                    for pr in github.list_prs_for_branch(owner, repo, b, token=token):
                        keep(_gh_change(pr, "branch"))
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"list {b}: {exc}")
            for n, _url in pointers:
                if n in found:
                    continue
                try:
                    keep(_from_pointer(_gh_change(github.get_pr(owner, repo, n, token=token),
                                                  "pointer"), n, _url))
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"pr #{n}: {exc}")
            for c in found.values():
                if c.state == "closed" and c.head_sha:
                    # A PR closed with its commits already in the base landed
                    # (the customer merged locally and pushed base directly).
                    try:
                        if github.commits_contained_in(owner, repo, base_branch,
                                                       c.head_sha, token=token):
                            c.state = "merged"
                    except Exception as exc:  # noqa: BLE001
                        errors.append(f"containment #{c.number}: {exc}")
                if c.state == "open" and c.head_sha:
                    try:
                        c.ci = github.ci_status(owner, repo, c.head_sha, token=token)
                    except Exception as exc:  # noqa: BLE001
                        errors.append(f"ci #{c.number}: {exc}")
        elif provider == "gitlab" and token:
            base_url, path = target["base_url"], target["path"]
            for b in branches:
                try:
                    for mr in gitlab.customer_list_mrs_for_branch(base_url, token, path, b):
                        keep(_gl_change(mr, "branch"))
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"list {b}: {exc}")
            for n, _url in pointers:
                if n in found:
                    continue
                try:
                    keep(_from_pointer(_gl_change(gitlab.customer_get_mr(base_url, token,
                                                                         path, n), "pointer"),
                                       n, _url))
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"mr !{n}: {exc}")
            for c in found.values():
                if c.state == "open" and c.head_sha:
                    try:
                        c.ci = gitlab.customer_pipeline_status(base_url, token, path,
                                                               c.head_sha)
                    except Exception as exc:  # noqa: BLE001
                        errors.append(f"ci !{c.number}: {exc}")
        if not found and ssh_merged is not None:
            # No API access (no token, or an 'other' host): the deploy key can
            # still tell whether the agent's branch landed in the base.
            try:
                landed = ssh_merged()
            except Exception as exc:  # noqa: BLE001
                landed = None
                errors.append(f"ssh: {exc}")
            if landed:
                snap.changes.append(Change(number=None, url=None, state="merged",
                                           branch=branches[0] if branches else None,
                                           provider=provider or "other", source="ssh"))
    except Exception as exc:  # noqa: BLE001 - observation must never raise
        errors.append(str(exc))
    snap.changes = sorted(found.values(), key=lambda c: c.number or 0,
                          reverse=True) + snap.changes
    if errors:
        snap.error = "; ".join(errors)[:400]
        log.info("delivery observe: partial (%s)", snap.error)
    return snap


# ---------------------------------------------------------------- decide

def decide(snap: Snapshot, *, request_status: str | None,
           pr_deliverable: bool, project_status: str | None = None) -> Verdict:
    """The next action for this delivery. Pure: no I/O, no clock beyond the
    snapshot's. The NEWEST change decides - an older one, merged or closed, is
    history a previous tick already handled. While the consultant holds the
    project (`awaiting_admin`) nothing is merged, armed or fixed from here:
    the hold is theirs to lift (a Resume runs the same table with the hold
    gone); a change that already landed still deploys - delivering merged
    work is never wrong."""
    if snap.live_run_state in LIVE_RUN_STATES:
        return Verdict("wait", None, f"a build is in flight ({snap.live_run_state})")
    newest = snap.newest
    if newest is None:
        if snap.error:
            return Verdict("wait", None, "the repository could not be observed")
        if snap.latest_run_state == "awaiting_merge":
            return Verdict("wait", None, "pushed; waiting for the change to be merged")
        return Verdict("idle", None, "no change on the repository")
    if newest.state == "merged":
        if (request_status == "done" or snap.latest_run_state == "done"
                or newest.number in snap.delivered_numbers):
            # Request #0 stays in_progress until the project finishes, so a
            # `done` run carrying this change is the delivery marker for an
            # MVP - whichever row it is. Judging by the NEWEST row alone
            # re-deployed a merged MVP every minute in prod: the newest row was
            # a later Resume that died with its node, the `done` sat on the
            # older row that owns the change.
            return Verdict("idle", newest, "delivered")
        if snap.latest_run_state == "merged":
            return Verdict("idle", newest,
                           "merged; the demo deploy parked - restarting the demo retries it")
        return Verdict("finalize" if pr_deliverable else "deploy", newest, "merged")
    if newest.state == "closed":
        if snap.latest_run_state == "awaiting_merge":
            return Verdict("reject", newest, "closed without merging")
        return Verdict("idle", newest, "closed without merging")
    # open
    if project_status == "awaiting_admin":
        return Verdict("wait", newest, "open; held for the consultant's review")
    if newest.ci == "failure":
        return Verdict("fix_ci", newest, "pipeline failed", settle=True)
    if not snap.platform:
        # Customer repositories: the customer merges (or §14.7 auto-merge did
        # so inline after its security review). Watch, never merge from here.
        return Verdict("wait", newest, "open; waiting for the merge", settle=True)
    ms = newest.merge_status
    if ms in CONFLICT_STATUSES:
        return Verdict("park", newest, f"merge conflict ({ms})")
    if ms in HUMAN_STATUSES:
        return Verdict("park", newest, f"blocked on GitLab: {ms}", fault="platform")
    if newest.ci == "success" or ms == "mergeable":
        return Verdict("merge", newest, "mergeable")
    if newest.ci == "pending":
        if snap.ci_available is False:
            return Verdict("park", newest,
                           "its pipeline is stuck: no runner can pick up the job",
                           fault="platform")
        return Verdict("arm", newest, "pipeline running", settle=True)
    if newest.ci == "none":
        if ms == "ci_must_pass":
            if snap.ci_available is False:
                return Verdict("park", newest,
                               "the project requires a passing pipeline but no pipeline can run",
                               fault="platform")
            return Verdict("wait", newest, "waiting for a pipeline", settle=True)
        if snap.ci_available is False:
            return Verdict("merge", newest, "no CI configured")
        age = newest.age_s(snap.observed_at)
        if age is not None and age > NO_PIPELINE_GRACE_S:
            return Verdict("merge", newest, "no pipeline appeared")
        return Verdict("wait", newest, "waiting for a pipeline", settle=True)
    # ci unknown (lookup failed) and no decisive merge status: settle and retry
    return Verdict("wait", newest, "waiting for GitLab", settle=True)


# ---------------------------------------------------------------- record

def record(snap: Snapshot, verdict: Verdict, acted: bool) -> dict:
    """The JSON stored on `Request.delivery` - the observed state a reader (the
    API, an admin, the next tick's idempotence check) can trust without
    re-asking the provider."""
    change = None
    if verdict.change is not None:
        c = asdict(verdict.change)
        c["updated_at"] = verdict.change.updated_at.isoformat() if verdict.change.updated_at else None
        change = c
    return {
        "observed_at": snap.observed_at.isoformat(),
        "provider": snap.provider,
        "action": verdict.action,
        "note": verdict.note,
        "fault": verdict.fault,
        "key": verdict.key,
        "acted": acted,
        "change": change,
        "changes": [{"number": c.number, "state": c.state, "branch": c.branch}
                    for c in snap.changes[:6]],
        "error": snap.error,
    }
