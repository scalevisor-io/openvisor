// Live build console (§14.8): the Development card of the project overview.
// While a run is in flight it offset-polls GET /projects/{id}/dev-activity and
// renders the agent's sanitized activity feed (commands, edits, thoughts,
// worker phases) with a ticking elapsed clock and an animated token/cost
// counter; after the run the feed stays as the build's story. Also renders the
// AI security review verdict and keeps the raw log tail behind "View raw logs"
// (disabled while a build is live - the tail is only captured at session end).
import { useEffect, useRef, useState } from "react";
import { projectsApi } from "../lib/endpoints";
import type {
  DevActivityChunk,
  DevActivityUsage,
  DevEvent,
  DevLogs,
  DevRunSummary,
  Project,
  SecurityReview,
} from "../types";
import {
  Alert,
  Badge,
  CopyButton,
  Spinner,
  formatCreditsExact,
  relTime,
  useCountUp,
  usePolling,
} from "./ui";
import { BranchChip, BuildFeed } from "@shared-ui";

const MAX_FEED_ITEMS = 500;

function fmtElapsed(ms: number): string {
  const s = Math.max(0, Math.floor(ms / 1000));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  return h > 0
    ? `${h}:${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}`
    : `${m}:${String(sec).padStart(2, "0")}`;
}


// §milestones: the green completion check, drawn by CSS when animated.
// pathLength normalizes the tick's stroke so the dash keyframe is exact.
export function CheckMark({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 52 52" aria-hidden="true">
      <circle className="check-ring" cx="26" cy="26" r="23" fill="none" />
      <path className="check-mark" fill="none" pathLength={40} d="M15 27.5l7.5 7.5L37 19" />
    </svg>
  );
}

function MilestoneBanner({ kind, animate }: { kind: "merged" | "done"; animate: boolean }) {
  return (
    <div className={`build-milestone${animate ? " is-animated" : ""}`} role="status">
      <CheckMark className="milestone-check" />
      <div>
        <strong>{kind === "merged" ? "Merge complete" : "Development complete"}</strong>
        <span className="milestone-sub">
          {kind === "merged"
            ? "The pull request is in - your change is merged."
            : "The build finished successfully."}
        </span>
      </div>
    </div>
  );
}

function SecurityReviewPanel({ review }: { review: SecurityReview }) {
  const ok = review.verdict === "approve";
  const findings = review.findings ?? [];
  return (
    <div className="sec-review">
      <div className="between">
        <div className="section-title" style={{ margin: 0 }}>
          AI security review
        </div>
        <Badge label={review.verdict.replace(/_/g, " ")} kind={ok ? "finished" : "awaiting_admin"} />
      </div>
      {review.error ? (
        <p className="tiny faint mt">Review unavailable: {review.error}</p>
      ) : findings.length === 0 ? (
        <p className="muted small mt">No security findings - the change was cleared for merge.</p>
      ) : (
        <div className="mt">
          {findings.map((f, i) => (
            <div key={i} className="sec-finding">
              <span className={`sev-chip sev-${f.severity}`}>{f.severity}</span>
              <span>
                {f.issue}
                {f.file && (
                  <span className="mono tiny faint">
                    {" "}
                    - {f.file}
                    {f.line != null ? `:${f.line}` : ""}
                  </span>
                )}
              </span>
            </div>
          ))}
        </div>
      )}
      <p className="tiny faint mt">
        {review.attempts != null && review.attempts > 1 ? `${review.attempts} review rounds · ` : ""}
        {review.merged != null ? (review.merged ? "merged automatically · " : "merge held · ") : ""}
        {review.reviewed_at ? `reviewed ${relTime(review.reviewed_at)}` : ""}
      </p>
    </div>
  );
}

export default function BuildConsole({
  project,
  canResume,
  resumeBlocker,
  retryBusy,
  onRetry,
  onStartFresh,
  onStale,
  consultant,
  forRequest = null,
  readOnly = false,
  runId,
}: {
  project: Project;
  canResume: boolean;
  resumeBlocker: string | null;
  retryBusy: boolean;
  onRetry: () => void;
  onStartFresh?: () => void;
  onStale: () => void;
  consultant: string;
  // §threads: the request this build belongs to - names the console and links
  // into the request's thread (null for pre-threads projects).
  forRequest?: { title: string; onOpen: () => void } | null;
  // §sharing: a read-only share watches the build without the stop/resume controls.
  readOnly?: boolean;
  // §parallel-builds MR4: the primary run's id - scopes Stop to THIS run so a
  // sibling can never be killed by the primary console (absent = legacy stop).
  runId?: string;
}) {
  const [events, setEvents] = useState<DevEvent[]>([]);
  const [usage, setUsage] = useState<DevActivityUsage | null>(null);
  const [devLogs, setDevLogs] = useState<DevLogs | null>(null);
  const [showLogs, setShowLogs] = useState(false);
  const [stopBusy, setStopBusy] = useState(false);
  const [now, setNow] = useState(() => Date.now());
  const offsetRef = useRef(0);
  const stateRef = useRef(project.dev_run_state);
  const feedRef = useRef<HTMLDivElement>(null);
  const pinnedRef = useRef(true); // autoscroll unless the user scrolled up

  const live = project.dev_run_state === "running" || project.dev_run_state === "deploying";
  const watching = live || project.dev_run_state === "awaiting_merge";
  stateRef.current = project.dev_run_state;

  function applyChunk(c: DevActivityChunk) {
    // A shrunken/replaced feed file (new run) restarts the buffer.
    if (c.reset || c.next_offset < offsetRef.current) {
      setEvents([]);
      offsetRef.current = 0;
    }
    if (c.next_offset > offsetRef.current && c.events.length) {
      setEvents((prev) => [...prev, ...c.events].slice(-MAX_FEED_ITEMS));
    }
    offsetRef.current = c.next_offset;
    if (c.usage) setUsage(c.usage);
    else if (c.reset) setUsage(null); // new run: don't show the old session's counter
    // The worker moved the run along (e.g. running → deploying → done):
    // refresh the project so the whole page follows.
    if (c.state !== stateRef.current) onStale();
  }

  usePolling(
    () => {
      projectsApi.devActivity(project.id, offsetRef.current).then(applyChunk).catch(() => {});
    },
    live ? 2000 : 12000,
    watching,
  );

  // Not in flight: fetch once so the last run's story (and final counters)
  // still shows, including one drain after a watched run just ended.
  const wasWatching = useRef(watching);
  useEffect(() => {
    if (!watching && (wasWatching.current || events.length === 0)) {
      projectsApi.devActivity(project.id, offsetRef.current).then(applyChunk).catch(() => {});
    }
    wasWatching.current = watching;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [watching, project.id]);

  // Elapsed clock ticks while the run is live.
  useEffect(() => {
    if (!live) return;
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, [live]);

  // Follow the tail like a terminal, but never fight the user's scrollback.
  useEffect(() => {
    const el = feedRef.current;
    if (el && pinnedRef.current) el.scrollTop = el.scrollHeight;
  }, [events]);

  // Raw logs are captured when a session ends, so mid-build they'd show the
  // PREVIOUS run - close and disable them while a build is in flight.
  useEffect(() => {
    if (live) setShowLogs(false);
    else setStopBusy(false);
  }, [live]);

  // §milestones: "Merge complete" when the run leaves awaiting_merge, then
  // "Development complete" when it lands done. A transition witnessed live
  // animates the check; a page opened after the fact shows the static row.
  const prevStateRef = useRef(project.dev_run_state);
  const [celebrated, setCelebrated] = useState<"merged" | "done" | null>(null);
  useEffect(() => {
    const prev = prevStateRef.current;
    const next = project.dev_run_state;
    if (prev === next) return;
    prevStateRef.current = next;
    if (prev === "awaiting_merge" && (next === "deploying" || next === "done")) {
      setCelebrated("merged");
      if (next === "done") {
        // both milestones landed in one poll: let the merge check draw first
        const t = setTimeout(() => setCelebrated("done"), 2400);
        return () => clearTimeout(t);
      }
    } else if (next === "done") {
      setCelebrated("done");
    } else {
      setCelebrated(null);
    }
  }, [project.dev_run_state]);
  const milestone =
    celebrated ??
    (project.dev_run_state === "done"
      ? "done"
      : project.dev_run_state === "deploying" && project.dev_pr_url
        ? "merged"
        : null);

  async function stopBuild() {
    if (
      !window.confirm(
        "Stop the current build? It parks as resumable - progress already pushed to the branch is kept.",
      )
    )
      return;
    setStopBusy(true);
    try {
      await projectsApi.stopBuild(project.id, runId);
    } catch {
      setStopBusy(false);
    }
  }

  async function toggleLogs() {
    if (showLogs) {
      setShowLogs(false);
      return;
    }
    try {
      setDevLogs(await projectsApi.devLogs(project.id));
    } catch {
      /* ignore */
    }
    setShowLogs(true);
  }

  const animTokens = useCountUp(usage?.output_tokens ?? 0);
  const animCost = useCountUp(usage?.credits_estimate ?? 0);
  const started = project.dev_run_started_at ? Date.parse(project.dev_run_started_at) : null;

  return (
    <div className="card build-console">
      <div className="between">
        <div className="row gap-sm">
          <div className="section-title" style={{ margin: 0 }}>
            Development
          </div>
          {forRequest && (
            <button
              type="button"
              className="btn btn-ghost btn-sm dev-for-request"
              onClick={forRequest.onOpen}
              title="Open this build's request thread"
            >
              for “{forRequest.title}” →
            </button>
          )}
          {live && (
            <span className="live-indicator">
              <span className="live-dot" />
              live
            </span>
          )}
        </div>
        <Badge label={project.dev_run_state.replace(/_/g, " ")} kind={project.status} />
      </div>

      {project.dev_run_state === "running" && (
        <p className="muted small mt">
          The agent is building your MVP in a sandbox - you're watching its moves as they happen.
          The run is bounded by a safety timeout so it can never run away with your budget.
        </p>
      )}
      {project.dev_run_state === "awaiting_merge" && (
        <p className="small mt">
          Your MVP is ready as a pull request. <strong>Review and merge it</strong> to deploy your
          live demo - deployment starts automatically after the merge.
        </p>
      )}
      {milestone && (
        // key restarts the CSS draw when the merge check hands over to done
        <MilestoneBanner
          key={`${milestone}${celebrated ? "-live" : ""}`}
          kind={milestone}
          animate={celebrated != null}
        />
      )}

      {/* Per-run stats only - project-lifetime metering lives on the Usage tab,
          not on every thread's console. */}
      {(events.length > 0 || live || usage != null) && (
        <>
          <div className={`build-stats${live ? " is-live" : ""}`}>
            {live && started != null && (
              <div className="stat">
                <label>elapsed</label>
                <span className="mono">{fmtElapsed(now - started)}</span>
              </div>
            )}
            {/* Per-run figures need a run that reported something; the lifetime
                pair below always stands on its own. Output is what the agent
                actually wrote - the input half is mostly re-sent context, so the
                split hides behind hover/focus. */}
            {usage != null && (
              <>
                <div className="stat stat-tokens" tabIndex={0}>
                  <div className="tok-face tok-face-default">
                    <label>output tokens</label>
                    <span className="mono">{Math.round(animTokens).toLocaleString()}</span>
                  </div>
                  <div className="tok-face tok-face-split">
                    <label>tokens in · out</label>
                    <span className="mono">
                      {usage.input_tokens.toLocaleString()} ·{" "}
                      {usage.output_tokens.toLocaleString()}
                    </span>
                  </div>
                </div>
                <div className="stat">
                  <label>est. cost</label>
                  <span className="mono grad-text">
                    {usage.credits_estimate != null
                      ? `~${formatCreditsExact(Math.round(animCost * 10000) / 10000)} credits`
                      : "—"}
                  </span>
                </div>
              </>
            )}
            {events.length > 0 && (
              <div className="stat">
                <label>actions</label>
                <span className="mono">{events.length.toLocaleString()}</span>
              </div>
            )}
            {/* The model THIS run executes on - it can change between runs
                (admin reroutes, endpoint switches), so the console names it
                per run instead of leaving the reader to guess from cost. */}
            {usage?.model && (
              <div className="stat stat-model">
                <label>model</label>
                <span className="mono" title="The model this run is billed on">{usage.model}</span>
              </div>
            )}
          </div>

          <BuildFeed
            events={events}
            live={live}
            scrollRef={feedRef}
            onScroll={() => {
              const el = feedRef.current;
              if (el) pinnedRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 60;
            }}
          />
        </>
      )}

      {project.dev_branch && (
        // key remounts on a rename so the fade-in replays for the new branch
        <div className="field mt build-chip-in" key={project.dev_branch}>
          <label>Branch</label>
          <div className="row gap-sm">
            <BranchChip name={project.dev_branch} url={project.dev_branch_url} />
            <CopyButton value={project.dev_branch} />
          </div>
        </div>
      )}
      {project.dev_pr_url && (
        <div className="field mt build-chip-in" key={project.dev_pr_url}>
          <label>Pull request</label>
          <div className="row gap-sm">
            <a href={project.dev_pr_url} target="_blank" rel="noreferrer">
              #{project.dev_pr_number}
            </a>
            <CopyButton value={project.dev_pr_url} />
          </div>
        </div>
      )}
      {project.dev_run_error && (
        <Alert kind="warn">
          Last run: {project.dev_run_error}. Use <strong>Resume development</strong> to try again,
          or request {consultant}'s review.
        </Alert>
      )}

      {project.dev_security_review && <SecurityReviewPanel review={project.dev_security_review} />}

      <div className="row gap-sm mt">
        {project.dev_run_state === "running" && !readOnly && (
          <button
            className="btn btn-sm btn-danger"
            onClick={stopBuild}
            disabled={stopBusy}
            title="Parks the build as resumable; progress already pushed to the branch is kept"
          >
            {stopBusy ? "Stopping…" : "Stop build"}
          </button>
        )}
        {!readOnly && (
          <button
            className="btn btn-sm"
            onClick={onRetry}
            disabled={retryBusy || !canResume}
            title={resumeBlocker ?? "Continues the failed build: same branch, same files, your notes folded in"}
          >
            {retryBusy ? <Spinner /> : "Resume development"}
          </button>
        )}
        {!readOnly && onStartFresh && canResume && (
          <button
            className="btn btn-sm"
            onClick={() => {
              // Abandoning a chain is not undoable - the discarded runs never
              // resume - so it earns a confirm where plain Resume does not.
              if (
                window.confirm(
                  "Start over from scratch? The failed build's work in progress is set aside and a new build starts clean on a new branch.",
                )
              )
                onStartFresh();
            }}
            disabled={retryBusy}
            title="Discards the failed build's work in progress and starts a clean build"
          >
            Start fresh
          </button>
        )}
        <button
          className="btn btn-sm"
          onClick={toggleLogs}
          disabled={live}
          title={
            live
              ? "Raw logs are captured when a session ends - available once the current build finishes"
              : "Raw output from the most recent completed build session"
          }
        >
          {showLogs ? "Hide raw logs" : "View raw logs"}
        </button>
      </div>
      {showLogs && (
        <pre className="log-pane mt">{devLogs?.log?.trim() || "No logs captured for the last run."}</pre>
      )}
    </div>
  );
}

const RUN_BADGE_KIND: Record<string, string> = {
  queued: "draft",
  running: "development",
  awaiting_merge: "awaiting_customer",
  deploying: "development",
  merged: "finished",
  failed: "canceled",
  done: "finished",
  idle: "draft",
  superseded: "draft",
};

// §threads: one run-history console in the request thread view - the run's
// state, links, security review and (while still addressable) its activity
// feed, read-only. The scalar-mirrored current run keeps the full BuildConsole;
// these are the request's OTHER runs.
export function RunConsole({
  projectId,
  run,
  defaultOpen = false,
  title = null,
  stoppable = false,
  resumable = false,
  onResumed,
  onOpenRequest,
}: {
  projectId: string;
  run: DevRunSummary;
  defaultOpen?: boolean;
  // §parallel-builds MR4: the owning request's title - names a sibling console
  // stacked on the overview (the request view already names the page itself).
  title?: string | null;
  // §parallel-builds MR4: offer a Stop scoped to THIS run (running + not a
  // read-only share). The worker validates the row before killing anything.
  stoppable?: boolean;
  // §parallel-builds: offer Resume / Start fresh scoped to THIS run when it
  // failed (not a read-only share); the row's own can_resume gates the button
  // and resume_blocker is its tooltip, so a sibling's live build no longer
  // hides the way back into a parked request.
  resumable?: boolean;
  onResumed?: () => void;
  onOpenRequest?: () => void;
}) {
  const [open, setOpen] = useState(defaultOpen);
  const [events, setEvents] = useState<DevEvent[]>([]);
  const [usage, setUsage] = useState<DevActivityUsage | null>(null);
  const [devLogs, setDevLogs] = useState<DevLogs | null>(null);
  const [showLogs, setShowLogs] = useState(false);
  const [stopBusy, setStopBusy] = useState(false);
  const [resumeBusy, setResumeBusy] = useState(false);
  const [resumeError, setResumeError] = useState<string | null>(null);
  const offsetRef = useRef(0);
  const feedRef = useRef<HTMLDivElement>(null);
  const pinnedRef = useRef(true);
  const live = run.state === "queued" || run.state === "running" || run.state === "deploying";

  async function stopRun() {
    if (
      !window.confirm(
        "Stop this build? It parks as resumable - progress already pushed to the branch is kept.",
      )
    )
      return;
    setStopBusy(true);
    try {
      await projectsApi.stopBuild(projectId, run.id);
    } catch {
      setStopBusy(false);
    }
  }

  async function resumeRun(fresh: boolean) {
    if (
      fresh &&
      !window.confirm(
        "Start over from scratch? The failed build's work in progress is set aside and a new build starts clean on a new branch.",
      )
    )
      return;
    setResumeBusy(true);
    setResumeError(null);
    try {
      await projectsApi.retryBuild(projectId, fresh, run.id);
      onResumed?.();
    } catch (e) {
      setResumeError((e as Error).message || "Resume failed");
    }
    setResumeBusy(false);
  }

  function pull() {
    projectsApi
      .devActivity(projectId, offsetRef.current, run.id)
      .then((c) => {
        if (c.reset || c.next_offset < offsetRef.current) {
          setEvents([]);
          offsetRef.current = 0;
        }
        if (c.next_offset > offsetRef.current && c.events.length) {
          setEvents((prev) => [...prev, ...c.events].slice(-MAX_FEED_ITEMS));
        }
        offsetRef.current = c.next_offset;
        if (c.usage) setUsage(c.usage);
      })
      .catch(() => {});
  }

  // A live sibling run keeps polling; a finished run's feed is pulled once
  // when its console is expanded.
  usePolling(pull, 3000, open && run.has_feed && live);
  useEffect(() => {
    if (open && run.has_feed && !live && offsetRef.current === 0) pull();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  useEffect(() => {
    const el = feedRef.current;
    if (el && pinnedRef.current) el.scrollTop = el.scrollHeight;
  }, [events]);

  async function toggleLogs() {
    if (showLogs) {
      setShowLogs(false);
      return;
    }
    try {
      setDevLogs(await projectsApi.devLogs(projectId, run.id));
    } catch {
      /* ignore */
    }
    setShowLogs(true);
  }

  return (
    <div className="card run-console">
      <div className="between">
        <div className="row gap-sm">
          <div className="section-title" style={{ margin: 0 }}>
            Development
          </div>
          {title != null &&
            (onOpenRequest ? (
              <button
                type="button"
                className="btn btn-ghost btn-sm dev-for-request"
                onClick={onOpenRequest}
                title="Open this build's request thread"
              >
                for “{title}” →
              </button>
            ) : (
              <span className="tiny faint">for “{title}”</span>
            ))}
          {run.state === "done" && <CheckMark className="milestone-check run-check" />}
          {live && (
            <span className="live-indicator">
              <span className="live-dot" />
              live
            </span>
          )}
          <span className="tiny faint">
            {run.started_at
              ? `started ${relTime(run.started_at)}`
              : `created ${relTime(run.created_at)}`}
          </span>
        </div>
        <div className="row gap-sm">
          {run.tokens_consumed > 0 && (
            <span
              className="tiny faint nowrap"
              title={`≈ ${formatCreditsExact(run.cost_credits)} credits billed on this run`}
            >
              {run.tokens_consumed.toLocaleString()} tok
            </span>
          )}
          <Badge label={run.state.replace(/_/g, " ")} kind={RUN_BADGE_KIND[run.state] ?? "draft"} />
          {stoppable && run.state === "running" && (
            <button
              type="button"
              className="btn btn-sm btn-danger"
              onClick={stopRun}
              disabled={stopBusy}
              title="Parks this build as resumable; progress already pushed to its branch is kept"
            >
              {stopBusy ? "Stopping…" : "Stop"}
            </button>
          )}
          {resumable && run.state === "failed" && (
            <>
              <button
                type="button"
                className="btn btn-sm"
                onClick={() => resumeRun(false)}
                disabled={resumeBusy || !run.can_resume}
                title={
                  run.resume_blocker ??
                  "Continues this failed build: same branch, same files, your notes folded in"
                }
              >
                {resumeBusy ? "Resuming…" : "Resume"}
              </button>
              {run.can_resume && (
                <button
                  type="button"
                  className="btn btn-sm"
                  onClick={() => resumeRun(true)}
                  disabled={resumeBusy}
                  title="Discards this build's work in progress and starts a clean build"
                >
                  Start fresh
                </button>
              )}
            </>
          )}
          <button type="button" className="btn btn-ghost btn-sm" onClick={() => setOpen(!open)}>
            {open ? "Hide console" : "Show console"}
          </button>
        </div>
      </div>

      {(run.branch || run.pr_url) && (
        <div className="row row-wrap gap-sm mt">
          {run.branch &&
            (run.branch_url ? (
              <a className="mono tiny" href={run.branch_url} target="_blank" rel="noreferrer">
                {run.branch}
              </a>
            ) : (
              <span className="mono tiny">{run.branch}</span>
            ))}
          {run.pr_url && (
            <a className="tiny" href={run.pr_url} target="_blank" rel="noreferrer">
              PR #{run.pr_number}
            </a>
          )}
        </div>
      )}
      {run.run_error && <Alert kind="warn">{run.run_error}</Alert>}
      {resumeError && <Alert kind="warn">{resumeError}</Alert>}

      {open && (
        <>
          {events.length > 0 ? (
            <>
              {usage && (
                <div className="build-stats">
                  <div className="stat">
                    <label>output tokens</label>
                    <span className="mono">{usage.output_tokens.toLocaleString()}</span>
                  </div>
                  {usage.credits_estimate != null && (
                    <div className="stat">
                      <label>est. cost</label>
                      <span className="mono grad-text">
                        ~{formatCreditsExact(usage.credits_estimate)} credits
                      </span>
                    </div>
                  )}
                </div>
              )}
              <BuildFeed
                events={events}
                live={live}
                scrollRef={feedRef}
                onScroll={() => {
                  const el = feedRef.current;
                  if (el)
                    pinnedRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 60;
                }}
              />
            </>
          ) : (
            <p className="tiny faint mt">
              {run.has_feed
                ? "No captured activity for this run."
                : "This run's live feed is no longer available - a later run reused its console."}
            </p>
          )}
          {run.security_review && <SecurityReviewPanel review={run.security_review} />}
          <div className="row gap-sm mt">
            <button
              className="btn btn-sm"
              onClick={toggleLogs}
              disabled={live}
              title={
                live
                  ? "Raw logs are captured when a session ends"
                  : "Raw output captured at the end of this run"
              }
            >
              {showLogs ? "Hide raw logs" : "View raw logs"}
            </button>
          </div>
          {showLogs && (
            <pre className="log-pane mt">
              {devLogs?.log?.trim() || "No logs captured for this run."}
            </pre>
          )}
        </>
      )}
    </div>
  );
}
