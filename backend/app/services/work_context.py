"""§work answers: the evidence pack behind an agent answer about the work done.

The chat classifier can route a question ("what did you do?", "why is the build
stuck?", "how much has this cost?") to an `answer` intent; this module gathers
the facts that answer it, from the DB first (requests, connected repositories,
published run summaries, PR links, ledger totals) plus two best-effort extras
the caller passes in: the live build feed and the push repo's git facts.

Everything here is READ-ONLY and bounded - the result is pasted into one prompt,
so every list is capped and every free-text field truncated. It never includes
diff content or secret Memory values: an answer is public in an immutable chat
log forever.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.models import Message, Project, ProjectRepo, Request

MAX_REQUESTS = 20
MAX_FEED_EVENTS = 25
MAX_REPOS = 12
SUMMARY_CHARS = 2500
DESCRIPTION_CHARS = 1200


def _fmt_date(value) -> str:
    return value.strftime("%Y-%m-%d %H:%M UTC") if value else "unknown"


def _request_line(req: Request) -> str:
    prs = " ".join(p.get("url", "") for p in (req.pr_urls or []) if p.get("url"))
    bits = [f'- "{req.title}" ({req.type}, {req.handling}-handled, status {req.status},'
            f" opened {_fmt_date(req.created_at)})"]
    if prs:
        bits.append(f"  changes: {prs}")
    if req.cost_credits:
        bits.append(f"  billed: {req.tokens_consumed or 0} tokens, "
                    f"{round(req.cost_credits, 4)} credits")
    return "\n".join(bits)


def _project_block(project: Project) -> str:
    lines = [
        f"Project: {project.name} (kind {project.kind}, status {project.status})",
        f"Build state: {project.dev_run_state}"
        + (f", started {_fmt_date(project.dev_run_started_at)}"
           if project.dev_run_state in ("running", "deploying") else ""),
        f"Description (what the customer originally asked for): "
        f"{(project.description or '')[:DESCRIPTION_CHARS]}",
    ]
    if project.dev_branch:
        lines.append(f"Current work branch: {project.dev_branch}")
    if project.dev_pr_url:
        lines.append(f"Latest pull/merge request: {project.dev_pr_url}")
    if project.subdomain and project.demo_deployed_once:
        lines.append(f"Live demo: {project.subdomain} (state {project.demo_state})")
    if project.dev_run_error:
        lines.append(f"Last build error: {project.dev_run_error[:400]}")
    lines.append(f"Billed so far on this project: {project.tokens_consumed or 0} tokens, "
                 f"{round(project.cost_credits or 0.0, 4)} credits")
    return "\n".join(lines)


def _repos_block(db: Session, project: Project) -> str:
    """The repositories panel as the answer sees it: every connected repo and
    which one is the working repo (where the agent pushes its branch and opens
    the PR/MR - `is_push_target`, mirroring the workers' _push_repo/_dev_target
    resolution). Without this an answer could only infer the repo set from old
    PR URLs, so a freshly connected repo looked nonexistent to the agent
    (prod regression). SSH URIs carry no credentials and are shown to the same
    audience as the repositories panel itself."""
    rows = (db.query(ProjectRepo).filter_by(project_id=project.id)
            .order_by(ProjectRepo.role).limit(MAX_REPOS).all())
    if not rows:
        if project.gitlab_ssh_url:
            return ("CONNECTED REPOSITORIES:\n- the platform-managed GitLab "
                    "repository - working repo (changes are pushed there)")
        return ""
    pushed = next((r for r in rows if r.is_push_target), None)
    lines = [f"- {r.ssh_uri[:200]} ({r.provider}) - "
             + ("working repo: the agent pushes its branch and opens the "
                "pull/merge request here" if r is pushed
                else "read-only context, cloned into the agent's workspace")
             for r in rows]
    if pushed is None:
        lines.append("- the platform-managed GitLab repository - working repo "
                     "(none of the connected repos is marked as the push target)")
    return "CONNECTED REPOSITORIES:\n" + "\n".join(lines)


def _git_block(facts: dict | None) -> str:
    """Commit subjects + touched files from the push repo (never diff content)."""
    if not facts or not (facts.get("commits") or facts.get("files")):
        return ""
    lines = []
    if facts.get("commits"):
        lines.append("Commits on this change:")
        lines += [f"- {c}" for c in facts["commits"] if c]
    if facts.get("files"):
        lines.append("Files touched:")
        for f in facts["files"]:
            counts = ""
            if f.get("added") or f.get("removed"):
                counts = f" (+{f.get('added', 0)}/-{f.get('removed', 0)})"
            lines.append(f"- {f.get('path', '')} [{f.get('status', 'modified')}]{counts}")
    return "\n".join(lines)


def _feed_block(events: list[dict] | None) -> str:
    """The tail of the live build narration - what the agent is doing right now,
    or the last thing it did. Already redacted by devfeed."""
    if not events:
        return ""
    tail = events[-MAX_FEED_EVENTS:]
    lines = [f"- [{e.get('kind', 'info')}] {e.get('title', '')}"
             + (f" - {e['detail'][:200]}" if e.get("detail") else "")
             for e in tail]
    return "Recent build activity (oldest first):\n" + "\n".join(lines)


def build_context(db: Session, project: Project, req: Request | None = None,
                  git_facts: dict | None = None, feed_events: list[dict] | None = None,
                  memory_block: str = "") -> str:
    """The WORK CONTEXT block for the answer prompt. `req` scopes it to one
    request (a question asked inside that request's thread)."""
    parts = ["PROJECT STATE:\n" + _project_block(project)]

    repos = _repos_block(db, project)
    if repos:
        parts.append(repos)

    requests = (db.query(Request).filter_by(project_id=project.id)
                .order_by(Request.created_at.desc()).limit(MAX_REQUESTS).all())
    if requests:
        parts.append("WORK REQUESTS (newest first):\n"
                     + "\n".join(_request_line(r) for r in requests))

    if req is not None:
        detail = [f'THIS CONVERSATION IS ABOUT ONE REQUEST: "{req.title}"',
                  f"type {req.type}, status {req.status}, opened {_fmt_date(req.created_at)}"]
        if req.work_summary:
            detail.append("What the agent reported building for it:\n"
                          + req.work_summary[:SUMMARY_CHARS])
        parts.append("\n".join(detail))

    # The last published run's own summary - the fallback when the scoped request
    # has none of its own (pre-threads projects, MVP builds).
    if project.dev_summary and not (req is not None and req.work_summary):
        parts.append("WHAT THE LAST PUBLISHED BUILD REPORTED:\n"
                     + project.dev_summary[:SUMMARY_CHARS])

    git = _git_block(git_facts)
    if git:
        parts.append("GIT FACTS FOR THE LATEST CHANGE:\n" + git)

    feed = _feed_block(feed_events)
    if feed:
        parts.append(feed)

    if memory_block:
        parts.append("PROJECT MEMORY (configuration the build uses; secret values "
                     "withheld):\n" + memory_block)

    return "\n\n".join(parts)


def history(db: Session, project: Project, thread: str, limit: int) -> list[Message]:
    """The thread's last `limit` messages, oldest first."""
    rows = (db.query(Message).filter_by(project_id=project.id, thread=thread)
            .order_by(Message.created_at.desc()).limit(limit).all())
    return list(reversed(rows))
