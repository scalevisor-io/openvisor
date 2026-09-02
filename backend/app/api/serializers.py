"""Model → JSON dict serializers shared by routers and WS."""
import re
from types import SimpleNamespace
from urllib.parse import quote

from app.core.config import settings
from app.core.encryption import decrypt
from app.models import (
    DevRun, Message, Organization, Project, ProjectShare, Quote, Request, User,
)
from app.services import brand, countries, dev_faults
from app.services import repos as repolib


def _parallel_effective(p: Project) -> int:
    """The resolved per-project parallel limit (§parallel-builds MR3): the
    entitlement hook returns None today, so this is computable without a
    session; 1 = serialized."""
    return max(1, min(p.dev_parallel_limit or settings.dev_parallel_runs_default,
                      settings.dev_parallel_runs_max))


def _resume_closed_blocker(p: Project) -> str | None:
    if p.kind == "direct_quote":
        return "Managed engagement - no automated builds"
    if p.status == "canceled":
        return "The project is canceled"
    if p.status == "finished":
        return "The project is finished - submit a new request for further changes"
    return None


def _resume_setup_blocker(p: Project) -> str | None:
    if p.block_auto_development:
        return "Automatic development is blocked pending admin authorization"
    if not (p.gitlab_project_id or len(p.repos) > 0):
        return "No repository to build into yet"
    return None


def dev_resume_capability(p: Project) -> tuple[bool, str | None]:
    """State-level capability behind the Resume-development button: (enabled,
    why-disabled). Enabled only when a failed run actually exists and no run is
    in flight - shared by the serializer (button state + tooltip) and the
    retry-build endpoint so the UI and the API can't drift. `p.repos` must be
    loaded."""
    blocker = _resume_closed_blocker(p)
    if blocker:
        return False, blocker
    if p.dev_run_state in ("running", "deploying"):
        return False, "A build is already in progress"
    if p.dev_run_state == "awaiting_merge":
        return False, "Waiting for you to merge the pull request"
    blocker = _resume_setup_blocker(p)
    if blocker:
        return False, blocker
    if p.dev_run_state == "merged":
        # delivered work whose demo deploy parked: rebuilding is pointless -
        # restarting the demo retries the deploy and finalizes the run
        return False, "The change is merged - restart the demo to redeploy it"
    if p.dev_run_state != "failed":
        return False, "Nothing to resume - the last build completed"
    return True, None


def dev_run_resume_capability(p: Project, run: DevRun, *, inflight_request_ids: set,
                              latest_failed_ids: set) -> tuple[bool, str | None]:
    """§parallel-builds: the Resume behind ONE run's console - the request
    thread's history rows. The project-level rules that do not depend on which
    run is meant apply unchanged; then the row must be its request's newest
    failed run and, in parallel mode, its request must have no run in flight: a
    sibling request's live build is no reason to keep a parked one waiting,
    which the project-level verdict ("A build is already in progress") would
    do. At limit 1 that verdict stands whole - one workspace, one run.
    `inflight_request_ids` are the request ids of the ACTIVE rows and
    `latest_failed_ids` the newest failed row per request; the endpoints and
    the action gather both (`project_actions.run_resume_sets`)."""
    if run.state != "failed":
        return False, "Nothing to resume - this run did not fail"
    if _parallel_effective(p) <= 1:
        return dev_resume_capability(p)
    blocker = _resume_closed_blocker(p) or _resume_setup_blocker(p)
    if blocker:
        return False, blocker
    if run.id not in latest_failed_ids:
        return False, "A later build of this request took over - resume that one"
    if run.request_id in inflight_request_ids:
        return False, "This request already has a build in flight"
    return True, None


def dev_help_capability(p: Project) -> tuple[bool, str | None]:
    """§request help: whether the free "Request help" button is offered.

    Offered ONLY over a platform fault (services/dev_faults.py) - the failures
    the customer cannot act on, stamped by the park that knows which path it
    took. Every other failed build keeps Resume and Start fresh as its answer,
    which is what they are for; handing those to the consultant free would
    price consulting at zero for work the pipeline is built to do.

    One more rule, and it is the reason the action needs no idempotency column:
    a project already sitting in awaiting_admin IS the escalation, so the button
    goes away rather than filing a second one. Shared by the serializer (button
    + tooltip) and `project_actions.request_help` so the two cannot drift."""
    if p.dev_run_fault != dev_faults.PLATFORM:
        return False, ("This build failed on its own terms - Resume with a note, "
                       "or Start fresh")
    if p.status == "awaiting_admin":
        return False, (f"{brand.consultant_first_name()} already has this project - "
                       "the answer comes back in chat")
    blocker = _resume_closed_blocker(p)
    if blocker:
        return False, blocker
    return True, None


def dev_revise_capability(p: Project) -> tuple[bool, str | None]:
    """§revise: whether the agent can take another pass at work that is already
    pushed and waiting on a merge - "actually, make it X" while the pull request
    is open. Distinct from resume (which continues a FAILED run): nothing failed
    here, the run finished and its PR is open, so the revision continues the same
    branch and the same PR picks up the new commits. `p.repos` must be loaded."""
    if p.kind == "direct_quote":
        return False, "Managed engagement - no automated builds"
    if p.status in ("canceled", "finished"):
        return False, "The project is closed - submit a new request for further changes"
    if p.block_auto_development:
        return False, "Automatic development is blocked pending admin authorization"
    if not (p.gitlab_project_id or len(p.repos) > 0):
        return False, "No repository to build into yet"
    if p.dev_run_state != "awaiting_merge":
        return False, "No pull request is waiting for a merge"
    return True, None


def _ssh_web_base(ssh_uri: str) -> str | None:
    """https web base for a repo's ssh uri: `git@host:path.git` and
    `ssh://git@host[:port]/path.git` forms; None when it doesn't parse."""
    m = (re.match(r"^(?:ssh://)?git@([^:/]+)(?::\d+)?[:/](.+?)(?:\.git)?/?$", ssh_uri or "")
         or re.match(r"^https?://([^/]+)/(.+?)(?:\.git)?/?$", ssh_uri or ""))
    if not m:
        return None
    return f"https://{m.group(1)}/{m.group(2)}"


def _change_web_base(url: str | None) -> tuple[str, str] | None:
    """(repo web base, tree path prefix) recovered from a PR/MR web URL - the
    repo the change actually lives on. Pins a branch link to ITS repo, immune
    to a later push-target switch (prod regression: switching the working repo
    re-linked every historical run's branch to the NEW repo)."""
    if not url:
        return None
    if "/pull/" in url:  # github
        return url.rsplit("/pull/", 1)[0], "tree"
    if "/-/merge_requests/" in url:  # gitlab
        return url.rsplit("/-/merge_requests/", 1)[0], "-/tree"
    if "/merge_requests/" in url:  # older gitlab web urls without the /-/ scope
        return url.rsplit("/merge_requests/", 1)[0], "-/tree"
    return None



def _branch_path(branch: str) -> str:
    """Percent-encode a branch name for a /tree/ URL. `/` stays literal (GitHub
    and GitLab both take multi-segment branch paths), but `#` and friends must
    encode - a KB convention like `f/#67-…` otherwise truncates at the browser's
    fragment delimiter and 404s (prod regression)."""
    return quote(branch, safe="/")


BRANCH_LINK_STATES = ("awaiting_merge", "superseded")


def branch_url(p: Project) -> str | None:
    """Web URL of the run's branch (§build panel branch chip). Linked ONLY while
    the branch verifiably exists on the remote - awaiting_merge (pushed, waiting)
    and superseded (§revise: its open PR keeps the branch alive). Everywhere
    else the chip renders unlinked: mid-run the branch usually isn't pushed yet,
    and after a merge GitLab deletes it - a dead `/tree/` link 404'd from the
    console (prod regression); the PR/MR chip stays the durable link. The stored
    PR/MR web URL wins when present - it names the repo the change actually
    lives on, so history keeps linking to ITS repo after the push target
    changes; else the CURRENT push repo: github `/tree/`, gitlab `/-/tree/` off
    its ssh uri, platform GitLab off gitlab_web_url; None for `other` hosts (the
    SPA shows the name unlinked). `p.repos` must be loaded."""
    if not p.dev_branch:
        return None
    if getattr(p, "dev_run_state", None) not in BRANCH_LINK_STATES:
        return None
    pinned = _change_web_base(getattr(p, "dev_pr_url", None))
    if pinned is not None:
        return f"{pinned[0]}/{pinned[1]}/{_branch_path(p.dev_branch)}"
    push = next((r for r in p.repos if r.is_push_target), None)
    if push is None:
        return (f"{p.gitlab_web_url}/-/tree/{_branch_path(p.dev_branch)}"
                if p.gitlab_web_url else None)
    base = _ssh_web_base(push.ssh_uri)
    if base is None:
        return None
    if push.provider == "github":
        return f"{base}/tree/{_branch_path(p.dev_branch)}"
    if push.provider == "gitlab":
        return f"{base}/-/tree/{_branch_path(p.dev_branch)}"
    return None


def quote_out(q: Quote) -> dict:
    """Full quote view for the Quotes tab; `attachments` must be loaded."""
    return {"id": q.id, "project_id": q.project_id, "request_id": q.request_id,
            "title": q.title, "details": q.details, "amount": q.amount,
            "currency": q.currency, "price_credits": q.price_credits,
            "status": q.status, "payment_link": q.stripe_payment_link,
            "decision_comment": q.decision_comment, "decided_at": q.decided_at,
            "refunded_credits": q.refunded_credits, "created_at": q.created_at,
            "attachments": [{"id": a.id, "filename": a.filename,
                             "content_type": a.content_type, "size_bytes": a.size_bytes}
                            for a in q.attachments]}


def user_out(u: User) -> dict:
    return {"id": u.id, "email": u.email, "full_name": u.full_name, "role": u.role,
            "email_verified": u.email_verified, "created_at": u.created_at}


def org_out(o: Organization) -> dict:
    # `billing_address_missing` is the one field the customer cannot work out
    # for themselves: an address that is only PARTIALLY filled in renders as a
    # complete-looking block on screen while Stripe receives nothing at all (it
    # is withheld whole rather than sent incomplete, because a partial address
    # resolves to the wrong tax rate instead of to an error). Nothing said so
    # until an invoice came out addressed to nobody.
    return {"id": o.id, "name": o.name, "type": o.type, "company_name": o.company_name,
            "vat_id": o.vat_id, "address_line1": o.address_line1,
            "address_line2": o.address_line2, "postal_code": o.postal_code,
            "city": o.city, "country": o.country, "province": o.province,
            "billing_address_missing": countries.missing_address_fields(o),
            "stripe_customer": bool(o.stripe_customer_id),
            "credit_balance": round(o.credit_balance or 0.0, 4)}


def project_summary(p: Project) -> dict:
    # `access` (§sharing) is stamped on the instance by the access checks
    # (deps.get_project_for_user / the list routes): owner|contributor|viewer.
    # `dev_run_state` lets the dashboard show a live "agent working" chip.
    return {"id": p.id, "name": p.name, "kind": p.kind, "speciality": p.speciality,
            "status": p.status, "tier": p.tier, "demo_state": p.demo_state,
            "dev_run_state": p.dev_run_state,
            "access": getattr(p, "access_role", "owner"),
            "created_at": p.created_at}


def share_out(s: ProjectShare, u: User) -> dict:
    """A project share (§sharing) with its target user's identity."""
    return {"id": s.id, "user_id": s.user_id, "email": u.email, "full_name": u.full_name,
            "role": s.role, "created_at": s.created_at}


def project_out(p: Project, *, include_secrets: bool = True) -> dict:
    demo_url = None
    if p.subdomain and p.demo_deployed_once:
        demo_url = (f"{settings.http_scheme}://{p.subdomain}.{settings.deploy_domain}"
                    f"{settings.public_port_suffix}")
    demo_pass = None
    if include_secrets and p.demo_basic_auth_pass_enc:
        demo_pass = decrypt(p.demo_basic_auth_pass_enc)
    can_resume, resume_blocker = dev_resume_capability(p)
    can_ask_help, help_blocker = dev_help_capability(p)
    git_name, git_email = repolib.git_identity(p)
    return {
        **project_summary(p),
        "description": p.description,
        "from_scratch": p.from_scratch,
        "sovereign": p.sovereign,
        "sovereign_comment": p.sovereign_comment,
        "block_auto_development": p.block_auto_development,
        # Per-project KB selection (§KB): raw KnowledgeBase ids only (null = all,
        # [] = none) - names/kinds stay behind the admin KB API.
        "kb_ids": p.kb_ids,
        "dev_max_iterations": p.dev_max_iterations,
        # §14.5 run wall-clock override (null = the instance default).
        "dev_run_timeout_minutes": p.dev_run_timeout_minutes,
        # §dev harness: the raw pin only (null = inheriting the instance default).
        # What it resolves to depends on instance state the admin Settings payload
        # already carries, so the effective value is composed client-side.
        "dev_harness": p.dev_harness,
        "dev_cpu_request": p.dev_cpu_request,
        "dev_mem_request": p.dev_mem_request,
        "issue_watch": p.issue_watch,
        # §git identity: the raw override (null = inheriting) plus what the agent
        # will actually commit as, so the form can prefill the live value and still
        # say whether it is this project's or the instance's.
        "git_author_name": p.git_author_name,
        "git_author_email": p.git_author_email,
        "git_author_name_effective": git_name,
        "git_author_email_effective": git_email,
        "gitlab_url": p.gitlab_web_url,
        "subdomain": p.subdomain,
        "demo_url": demo_url,
        "demo_basic_auth_user": p.demo_basic_auth_user,
        "demo_basic_auth_pass": demo_pass,
        "demo_state": p.demo_state,
        "demo_last_started_at": p.demo_last_started_at,
        "demo_last_stopped_at": p.demo_last_stopped_at,
        "demo_timeout_minutes": settings.demo_timeout_minutes,
        "ssh_public_key": p.ssh_public_key,
        # Connected customer repos. `is_push_target` marks the one the AI pushes
        # into (at most one across repos + the platform repo below); `can_auto_merge`
        # is whether §14.7 auto-merge is offerable (github/gitlab only, not 'other').
        "repos": [{"id": r.id, "ssh_uri": r.ssh_uri, "role": r.role, "provider": r.provider,
                   "is_push_target": r.is_push_target, "auto_merge": r.auto_merge,
                   "squash_on_merge": r.squash_on_merge,
                   "summarize_to_issue": r.summarize_to_issue,
                   "can_auto_merge": r.provider in repolib.AUTO_MERGE_PROVIDERS}
                  for r in p.repos],
        # The platform-auto-generated GitLab repo (ai projects). It is the push
        # target when no connected repo is; its auto-merge is the always-on green-CI
        # path (no per-repo toggle). null for direct-quote projects.
        "platform_repo": ({
            "web_url": p.gitlab_web_url, "ssh_url": p.gitlab_ssh_url,
            "provisioned": p.gitlab_project_id is not None,
            "is_push_target": not any(r.is_push_target for r in p.repos),
        } if p.kind == "ai" else None),
        # §chat images: {enabled, reason, model} - stamped by the route (like
        # `access`); absent on payloads built without a session.
        "image_support": getattr(p, "image_support", None),
        "tokens_consumed": p.tokens_consumed,
        "cost_credits": round(p.cost_credits or 0.0, 4),
        "quick_devs_enabled": False,
        "dev_run_state": p.dev_run_state,
        "dev_plan_status": p.dev_plan_status,
        # §plan visibility: the FULL plan. The approval message carries only an
        # excerpt (immutable chat), so without this the customer was asked to
        # approve a plan they could read maybe a third of. Null on every project
        # that never ran a plan pass, which is most of them.
        "dev_plan": p.dev_plan,
        # §threads: the request the in-flight/last run is scoped to (null = the
        # MVP build) - lets the SPA mark which thread is building.
        "dev_request_id": p.dev_request_id,
        # §parallel-builds MR3: raw per-project override + the resolved
        # effective limit (entitlement hook returns None today, so this is
        # computable without a session; 1 = serialized).
        "dev_parallel_limit": p.dev_parallel_limit,
        "dev_parallel_effective": _parallel_effective(p),
        # §build panel branch chip: the run's branch + its push-repo web URL.
        "dev_branch": p.dev_branch,
        # §repo binding: the detail route stamps the primary run's own pinned
        # link (dev_branch_url_pinned) so the live chip follows the RUN's repo;
        # payloads without the stamp keep the mirror derivation.
        "dev_branch_url": getattr(p, "dev_branch_url_pinned", None) or branch_url(p),
        "dev_run_error": p.dev_run_error,
        # §request help: the fault class behind a failed run ("platform" = ours),
        # and the free-escalation affordance it gates.
        "dev_run_fault": p.dev_run_fault,
        "dev_can_request_help": can_ask_help,
        "dev_help_blocker": help_blocker,
        "dev_run_started_at": p.dev_run_started_at,
        "dev_harness_version": p.dev_harness_version,
        "dev_pr_number": p.dev_pr_number,
        "dev_pr_url": p.dev_pr_url,
        "dev_can_resume": can_resume,
        "dev_resume_blocker": resume_blocker,
        "dev_security_review": p.dev_security_review,
        # §parallel-builds MR4: the active DevRun rows behind the stacked
        # consoles (dev_run_out shape, oldest started first), stamped by the
        # detail route like image_support - [] on payloads built without it.
        # Every dev_* scalar above keeps its mirror semantics untouched (§8).
        "dev_runs": getattr(p, "dev_runs_payload", []),
    }


def message_out(m: Message) -> dict:
    return {"id": m.id, "thread": m.thread, "author": m.author, "body": m.body,
            "meta": m.meta, "emailed": m.emailed, "created_at": m.created_at}


def _run_branch_url(r: DevRun, p: Project) -> str | None:
    """§repo binding: a run row's branch link, pinned to ITS repo - the stored
    PR/MR web URL first, then the repo_id stamp, and only a pin-less legacy
    row falls back to the project's current push target. Linked only in
    BRANCH_LINK_STATES (see branch_url) - anywhere else the branch either isn't
    pushed yet or was deleted by its merge, and the chip renders unlinked."""
    if not r.branch or r.state not in BRANCH_LINK_STATES:
        return None
    pinned = _change_web_base(r.pr_url)
    if pinned is not None:
        return f"{pinned[0]}/{pinned[1]}/{_branch_path(r.branch)}"
    if r.repo_id:
        row = next((x for x in p.repos if x.id == r.repo_id), None)
        if row is not None:
            base = _ssh_web_base(row.ssh_uri)
            if base and row.provider == "github":
                return f"{base}/tree/{_branch_path(r.branch)}"
            if base and row.provider == "gitlab":
                return f"{base}/-/tree/{_branch_path(r.branch)}"
            return None
    shim = SimpleNamespace(dev_branch=r.branch, dev_pr_url=r.pr_url, repos=p.repos,
                           gitlab_web_url=p.gitlab_web_url,
                           dev_run_state=r.state)
    return branch_url(shim)


def dev_run_out(r: DevRun, p: Project, legacy_feed_owner: str | None,
                resume_ctx: tuple[set, set] | None = None) -> dict:
    """One run-history row for the request-thread consoles. `p.repos` must be
    loaded (branch link derivation); `legacy_feed_owner` is the id of the one
    legacy row whose feed still lives at the shared workspace path;
    `resume_ctx` is `project_actions.run_resume_sets` (the row's own Resume
    verdict - without it the row is not resumable, never guessed)."""
    can_resume, resume_blocker = False, None
    if resume_ctx is not None:
        can_resume, resume_blocker = dev_run_resume_capability(
            p, r, inflight_request_ids=resume_ctx[0], latest_failed_ids=resume_ctx[1])
    return {"id": r.id, "request_id": r.request_id, "state": r.state,
            "can_resume": can_resume, "resume_blocker": resume_blocker,
            "created_at": r.created_at, "started_at": r.started_at,
            "repo_id": r.repo_id,
            "branch": r.branch, "branch_url": _run_branch_url(r, p),
            "pr_number": r.pr_number, "pr_url": r.pr_url,
            "run_error": r.run_error, "security_review": r.security_review,
            "tokens_consumed": r.tokens_consumed or 0,
            "cost_credits": round(r.cost_credits or 0.0, 4),
            "has_feed": bool(r.workspace_dir) or r.id == legacy_feed_owner}


def request_out(r: Request) -> dict:
    return {"id": r.id, "project_id": r.project_id, "type": r.type, "handling": r.handling,
            "status": r.status, "title": r.title, "price_credits": r.price_credits,
            "repo_id": r.repo_id,
            "tokens_consumed": r.tokens_consumed or 0,
            "cost_credits": round(r.cost_credits or 0.0, 4),
            "source_issue_url": r.source_issue_url,
            "pr_urls": r.pr_urls or [],
            "created_at": r.created_at}
