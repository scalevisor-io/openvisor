import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { projectsApi, quotesApi, requestsApi } from "../lib/endpoints";
import { loadSpecialities, specialityLabel } from "../lib/meta";
import { useAuth } from "../lib/auth";
import { useToast } from "../lib/toast";
import { Alert, Badge, Loading, Pager, Spinner, formatCredits, formatCreditsExact, relTime, CollapsibleCard, readCardCollapsed, writeCardCollapsed } from "../components/ui";
import { DemoAccess, NowPanel, StatusTimeline, ThreadRail, projectNow } from "@shared-ui";
import type { NowActionId, SharedRequest } from "@shared-ui";
import Chat from "../components/Chat";
import MemoryTab from "../components/MemoryTab";
import McpTab from "../components/McpTab";
import UsageTab from "../components/UsageTab";
import ReposCard from "../components/ReposCard";
import QuotesTab from "../components/QuotesTab";
import RequestsTab from "../components/RequestsTab";
import AdminProjectControls from "../components/AdminProjectControls";
import BuildConsole, { CheckMark, RunConsole } from "../components/BuildConsole";
import KindChip from "../components/KindChip";
import RoutinesTab from "../components/RoutinesTab";
import ShareModal from "../components/ShareModal";
import type {
  DevRunState,
  Evaluation,
  IssueWatchEvent,
  IssueWatchEventPage,
  Project,
  ProjectStatus,
  Speciality,
  StatusChange,
} from "../types";

type Tab = "overview" | "quotes" | "requests" | "routines" | "memory" | "usage" | "mcp";

const TABS: Tab[] = ["overview", "requests", "quotes", "routines", "memory", "mcp", "usage"];

// Display labels where the default capitalized id isn't enough; the `memory`
// route id stays stable so existing links keep working.
const TAB_LABELS: Partial<Record<Tab, string>> = { memory: "Memory & files", mcp: "MCP" };
const CHAT_RAIL_KEY = "project:chat-rail";

function isTab(v: string | undefined): v is Tab {
  return TABS.includes(v as Tab);
}

const DEPLOYABLE_STATUSES: ProjectStatus[] = [
  "development",
  "awaiting_customer",
  "awaiting_admin",
  "finished",
];

// Mirrors the backend gate on PATCH /projects/{id}: the description locks once
// the project moves past review.
const DESCRIPTION_EDITABLE_STATUSES: ProjectStatus[] = ["draft", "awaiting_review"];

export default function ProjectDetail() {
  const { id = "", tab: tabParam, sub } = useParams();
  const navigate = useNavigate();
  const { isAdmin, config, settings } = useAuth();
  const consultant = settings?.consultant_first_name ?? "Consultant";
  const toast = useToast();

  const [project, setProject] = useState<Project | null>(null);
  const [history, setHistory] = useState<StatusChange[]>([]);
  const [specialities, setSpecialities] = useState<Speciality[]>([]);
  const [evaluation, setEvaluation] = useState<Evaluation | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busyAction, setBusyAction] = useState<NowActionId | null>(null);
  const [demoBusy, setDemoBusy] = useState(false);
  const [counts, setCounts] = useState<{ requests: number; quotes: number } | null>(null);
  // §threads: the orchestrator chat's rail of work threads (fed by the same
  // fetch as the tab counts).
  const [requests, setRequests] = useState<SharedRequest[]>([]);
  const [desc, setDesc] = useState("");
  const [descDirty, setDescDirty] = useState(false);
  const [descBusy, setDescBusy] = useState(false);
  const [setupOpen, setSetupOpen] = useState<boolean | null>(null);
  const [nameEditing, setNameEditing] = useState(false);
  const [nameDraft, setNameDraft] = useState("");
  const [nameBusy, setNameBusy] = useState(false);
  const [shareOpen, setShareOpen] = useState(false);
  // The rail starts open - it is the control surface - and remembers a retract.
  const [chatOpen, setChatOpen] = useState(() => readCardCollapsed(CHAT_RAIL_KEY) !== true);

  const reload = useCallback(() => {
    projectsApi
      .get(id)
      .then(setProject)
      .catch((err) => setError(err instanceof Error ? err.message : "Failed to load project."));
    projectsApi
      .statusHistory(id)
      .then(setHistory)
      .catch(() => {});
  }, [id]);

  useEffect(() => {
    reload();
    loadSpecialities().then(setSpecialities).catch(() => {});
  }, [reload]);

  // Tab affordances: Requests carries a count; Quotes hides on AI projects
  // with none (it stays for direct_quote, where quotes are the engagement).
  useEffect(() => {
    if (!project || project.kind === "chat") return;
    Promise.all([
      requestsApi.list(id).catch(() => []),
      quotesApi.list(id).catch(() => []),
    ]).then(([r, q]) => {
      setCounts({ requests: r.length, quotes: q.length });
      setRequests(r);
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id, project?.kind, project?.status, project?.dev_run_state]);

  // Evaluation feeds the draft-stage NowPanel (§project-page-now): fetch it
  // for AI drafts and poll while a run is pending so the verdict lands live.
  const isDraftAi = project?.kind === "ai" && project.status === "draft";
  useEffect(() => {
    if (!isDraftAi) return;
    let timer: number | undefined;
    let stop = false;
    const pull = () =>
      projectsApi
        .evaluation(id)
        .then((ev) => {
          if (stop) return;
          const changed = ev.state !== "pending";
          setEvaluation((prev) => {
            if (prev?.state === "pending" && changed) reload(); // verdict may retitle
            return ev;
          });
          if (ev.state === "pending") timer = window.setTimeout(pull, 3000);
        })
        .catch(() => {
          if (!stop) setEvaluation(null);
        });
    pull();
    return () => {
      stop = true;
      if (timer) window.clearTimeout(timer);
    };
  }, [id, isDraftAi, reload]);

  const tabs: Tab[] =
    project?.kind === "chat"
      ? ["overview", "memory", "usage"]
      : project?.kind === "ai" && counts != null && counts.quotes === 0
        ? ["overview", "requests", "routines", "memory", "mcp", "usage"]
        : TABS;
  const visibleTabs = settings?.routines_enabled === false
    ? tabs.filter((t) => t !== "routines")
    : tabs;
  const tab: Tab = isTab(tabParam) && visibleTabs.includes(tabParam) ? tabParam : "overview";
  const setTab = (t: Tab) =>
    navigate(t === "overview" ? `/projects/${id}` : `/projects/${id}/${t}`);
  // Returning to the overview after a tab where the socket wasn't mounted
  // (Requests, Memory…) may have missed pushes - refetch what happened there.
  const prevTab = useRef(tab);
  useEffect(() => {
    if (prevTab.current !== tab && tab === "overview") reload();
    prevTab.current = tab;
  }, [tab, reload]);

  // Keep the description field in sync with server state, but never clobber
  // an edit in progress (reload() runs on WS pushes and demo polling).
  useEffect(() => {
    if (project && !descDirty) setDesc(project.description);
  }, [project, descDirty]);

  // §milestones: a run witnessed landing `done` keeps the console mounted a
  // few seconds so the "Development complete" check can draw before the
  // console retracts to its compact row.
  const prevRunStateRef = useRef<DevRunState | null>(null);
  const [lingerDone, setLingerDone] = useState(false);
  useEffect(() => {
    const prev = prevRunStateRef.current;
    const next = project?.dev_run_state ?? null;
    prevRunStateRef.current = next;
    if (next === "done" && prev != null && prev !== "done" && prev !== "idle") {
      setLingerDone(true);
      const t = window.setTimeout(() => setLingerDone(false), 6000);
      return () => window.clearTimeout(t);
    }
    setLingerDone(false);
  }, [project?.dev_run_state]);

  const onStatusPush = useCallback(() => reload(), [reload]);
  const onDevPush = useCallback(
    (state: DevRunState) => {
      setProject((p) => (p ? { ...p, dev_run_state: state } : p));
      window.setTimeout(reload, 1000);
    },
    [reload],
  );

  async function act(actionId: NowActionId, run: () => Promise<unknown>, okMsg?: string) {
    setBusyAction(actionId);
    try {
      await run();
      if (okMsg) toast.push(okMsg, "ok");
      reload();
    } catch (err) {
      toast.push(err instanceof Error ? err.message : "Action failed", "err");
    } finally {
      setBusyAction(null);
    }
  }

  async function startStop(action: "start" | "stop") {
    setDemoBusy(true);
    const target = action === "start" ? "running" : "stopped";
    try {
      await (action === "start" ? projectsApi.demoStart(id) : projectsApi.demoStop(id));
      toast.push(action === "start" ? "Demo starting…" : "Demo stopping…", "ok");
      // The deploy/stop runs async in a worker; poll until the state settles
      // (a fresh DinD build can take up to a minute) so the UI reflects it.
      const deadline = Date.now() + 90_000;
      let settled = false;
      while (Date.now() < deadline) {
        await new Promise((r) => setTimeout(r, 2500));
        const p = await projectsApi.get(id);
        setProject(p);
        if (p.demo_state === target) {
          settled = true;
          break;
        }
      }
      reload();
      toast.push(
        settled
          ? action === "start"
            ? "Demo is live."
            : "Demo stopped."
          : "Still working… check back in a moment.",
        "ok",
      );
    } catch (err) {
      toast.push(err instanceof Error ? err.message : "Demo action failed", "err");
    } finally {
      setDemoBusy(false);
    }
  }

  async function saveDescription() {
    setDescBusy(true);
    try {
      const updated = await projectsApi.update(id, { description: desc.trim() });
      setProject(updated);
      setDescDirty(false);
      toast.push("Description saved.", "ok");
    } catch (err) {
      toast.push(err instanceof Error ? err.message : "Failed to save the description", "err");
    } finally {
      setDescBusy(false);
    }
  }

  async function saveName() {
    const name = nameDraft.trim();
    if (!name || name === project?.name) {
      setNameEditing(false);
      return;
    }
    setNameBusy(true);
    try {
      const updated = await projectsApi.update(id, { name });
      setProject(updated);
      setNameEditing(false);
      toast.push("Project renamed.", "ok");
    } catch (err) {
      toast.push(err instanceof Error ? err.message : "Failed to rename the project", "err");
    } finally {
      setNameBusy(false);
    }
  }

  if (error) return <Alert kind="error">{error}</Alert>;
  if (!project) return <Loading />;

  const timeout = config?.demo_timeout_minutes ?? 30;
  // §sharing: shared users see the page as-is; a read-only share also loses
  // every mutating control (the API 403s them anyway - don't show dead buttons).
  const access = project.access ?? "owner";
  const isOwner = access === "owner";
  const readOnly = access === "viewer";
  const isDirect = project.kind === "direct_quote";
  const isAuto = project.kind === "auto_dev";
  const isChat = project.kind === "chat";
  const deployable = project.demo_url != null || DEPLOYABLE_STATUSES.includes(project.status);
  const canEditDescription = isAuto || DESCRIPTION_EDITABLE_STATUSES.includes(project.status);
  // Setup is the work of the pre-review stage: open there, tucked away after -
  // unless the browser remembers an explicit choice from a previous visit.
  const storedSetupCollapsed = readCardCollapsed("project:setup");
  const isSetupOpen =
    setupOpen ?? (storedSetupCollapsed !== null ? !storedSetupCollapsed : canEditDescription);
  const adminHasBall = project.status === "awaiting_admin" && !isAdmin;
  const canResume = project.dev_can_resume && !adminHasBall;
  // §threads: the request the current/last run belongs to (Request #0 for MVP
  // runs) - names the console, links it into the thread, owns the in-thread view.
  const owningRequest = project.dev_request_id
    ? requests.find((r) => r.id === project.dev_request_id)
    : requests.find((r) => r.type === "mvp");
  const openOwningThread = () =>
    owningRequest
      ? navigate(`/projects/${id}/requests/${owningRequest.id}`)
      : setTab("requests");
  const resumeBlocker = adminHasBall
    ? `${consultant} has the project - it resumes after review`
    : project.dev_resume_blocker;
  const running = project.dev_run_state === "running" || project.dev_run_state === "deploying";
  // §parallel-builds MR4: the active run set (oldest started first). The
  // PRIMARY is the newest-started row - exactly what the dev_* mirror (and so
  // the full BuildConsole) tracks; every other active run stacks below it as
  // its own collapsed console. Older payloads without dev_runs -> single
  // console, zero visual change.
  const devRuns = project.dev_runs ?? [];
  const startedRuns = devRuns.filter((r) => r.started_at != null);
  const primaryRun = startedRuns.length > 0 ? startedRuns[startedRuns.length - 1] : null;
  const siblingRuns = devRuns.filter((r) => r !== primaryRun);
  const requestTitle = (rid: string | null) =>
    (rid
      ? requests.find((r) => r.id === rid)
      : requests.find((r) => r.type === "mvp")
    )?.title ?? null;
  // §parallel-builds MR4: every request with a run in flight - pulses its rail
  // chip and feeds the aggregate now-copy.
  const buildingRequestIds = devRuns
    .map((r) => r.request_id ?? requests.find((q) => q.type === "mvp")?.id ?? null)
    .filter((rid): rid is string => rid != null);
  const runCounts = {
    building: devRuns.filter((r) => ["queued", "running", "deploying"].includes(r.state)).length,
    merging: devRuns.filter((r) => r.state === "awaiting_merge").length,
  };

  // --- The page's spine: machine state -> narrative + one primary action ---
  const now = projectNow({
    status: project.status,
    kind: project.kind,
    devRunState: project.dev_run_state,
    demoUrl: project.demo_url,
    evaluationState: isDraftAi ? (evaluation?.state ?? null) : null,
    evaluationVerdict: evaluation?.feasibility?.verdict ?? null,
    estimateCredits: evaluation?.estimate?.credits ?? null,
    prNumber: project.dev_pr_number,
    consultant,
    runCounts,
  });
  const reviewFee =
    project.kind === "ai" && config?.review_request_credits
      ? `· ${formatCredits(config.review_request_credits)} credits`
      : undefined;
  const nowActions: Partial<Record<NowActionId, () => void>> = {
    evaluate: isDraftAi
      ? () =>
          act(
            "evaluate",
            async () => {
              await projectsApi.evaluate(id);
              setEvaluation({ state: "pending" });
            },
            "Evaluation started…",
          )
      : undefined,
    submit:
      project.status === "draft"
        ? () => act("submit", () => projectsApi.submit(id), "Submitted for review.")
        : undefined,
    fund: () => navigate("/billing"),
    resume:
      project.dev_run_state !== "idle"
        ? () => act("resume", () => projectsApi.retryBuild(id), "Resuming development…")
        : undefined,
    stop: running
      ? () => act("stop", () => projectsApi.stopBuild(id), "Stopping the build…")
      : undefined,
    "open-pr": project.dev_pr_url
      ? () => window.open(project.dev_pr_url ?? "", "_blank", "noopener")
      : undefined,
    approve:
      !isDirect && project.demo_url != null && project.status === "awaiting_customer"
        ? () =>
            act("approve", () => projectsApi.approveDelivery(id), "Delivery approved — thank you!")
        : undefined,
    "open-demo": project.demo_url
      ? () => window.open(project.demo_url ?? "", "_blank", "noopener")
      : undefined,
    "require-review": !isAdmin
      ? () =>
          act(
            "require-review",
            () => projectsApi.requireReview(id),
            `${consultant} has been notified.`,
          )
      : undefined,
  };

  // The rail follows the route: inside a request, it IS that request's thread.
  const railRequest = tab === "requests" && sub ? (requests.find((r) => r.id === sub) ?? null) : null;
  const railThread = railRequest ? `request:${railRequest.id}` : "main";

  return (
    <div className={`project-shell${chatOpen ? "" : " chat-retracted"}`}>
      <div className="project-main">
      <div className="page-head">
        <div>
          <Link to="/" className="tiny faint">
            ← Projects
          </Link>
          <div className="row gap-sm wrap" style={{ marginTop: "0.4rem" }}>
            {nameEditing ? (
              <form
                className="row gap-sm"
                onSubmit={(e) => {
                  e.preventDefault();
                  void saveName();
                }}
              >
                <input
                  type="text"
                  value={nameDraft}
                  maxLength={255}
                  autoFocus
                  aria-label="Project name"
                  onChange={(e) => setNameDraft(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Escape") setNameEditing(false);
                  }}
                />
                <button
                  type="submit"
                  className="btn btn-sm btn-primary"
                  disabled={nameBusy || !nameDraft.trim()}
                >
                  {nameBusy ? <Spinner /> : "Save"}
                </button>
                <button
                  type="button"
                  className="btn btn-sm btn-ghost"
                  disabled={nameBusy}
                  onClick={() => setNameEditing(false)}
                >
                  Cancel
                </button>
              </form>
            ) : (
              <>
                <h1 style={{ margin: 0 }}>{project.name}</h1>
                {!readOnly && (
                  <button
                    type="button"
                    className="btn btn-sm btn-ghost"
                    title="Rename project"
                    aria-label="Rename project"
                    onClick={() => {
                      setNameDraft(project.name);
                      setNameEditing(true);
                    }}
                  >
                    ✎
                  </button>
                )}
                <KindChip kind={project.kind} />
                {isOwner ? (
                  <button
                    type="button"
                    className="btn btn-sm"
                    title="Share this project with another user"
                    onClick={() => setShareOpen(true)}
                  >
                    Share
                  </button>
                ) : (
                  <span
                    className="badge"
                    title={
                      readOnly
                        ? "Shared with you read-only"
                        : "Shared with you as a contributor"
                    }
                  >
                    shared{readOnly ? " · read-only" : ""}
                  </span>
                )}
              </>
            )}
          </div>
          <div className="muted small" style={{ marginTop: "0.3rem" }}>
            {isDirect
              ? "Custom quote engagement"
              : isChat
                ? "Conversation"
                : `${specialityLabel(specialities, project.speciality)} · ${project.tier ?? "mvp"}`}
            {project.sovereign && " · sovereign"}
          </div>
        </div>
      </div>

      <div className="timeline-row">
        <div className="timeline-strip">
          <StatusTimeline
            status={project.status}
            kind={project.kind}
            demoExists={project.demo_url != null}
          />
        </div>
        {history.length > 0 && (
          <details className="history-inline">
            <summary>History</summary>
            <div className="history-pop card">
              {[...history].reverse().map((h, i) => (
                <div key={i} className="row gap-sm tiny">
                  <span className="faint nowrap">{relTime(h.at)}</span>
                  <Badge label={h.to} kind={h.to} />
                  <span className="muted">
                    by {h.actor}
                    {h.reason ? ` - ${h.reason}` : ""}
                  </span>
                </div>
              ))}
            </div>
          </details>
        )}
      </div>

      <NowPanel
        now={now}
        actions={readOnly ? {} : nowActions}
        busy={busyAction}
        consultant={consultant}
        meta={{
          resume: { disabled: !canResume, title: resumeBlocker ?? undefined },
          "require-review": {
            sublabel: reviewFee,
            title: reviewFee ? `Refundable by ${consultant}` : undefined,
          },
          fund: { title: "Opens billing - the build starts on its own once funded" },
        }}
      />

      <div className="tabs">
        {visibleTabs.map((t) => (
          <div key={t} className={`tab${tab === t ? " active" : ""}`} onClick={() => setTab(t)}>
            {TAB_LABELS[t] ?? t[0].toUpperCase() + t.slice(1)}
            {t === "requests" && counts != null && counts.requests > 0 && (
              <span className="tab-count">{counts.requests}</span>
            )}
            {t === "quotes" && counts != null && counts.quotes > 0 && (
              <span className="tab-count">{counts.quotes}</span>
            )}
          </div>
        ))}
      </div>

      {tab === "overview" && (
          <div className="stack">
            {/* chat projects set the flag by design (no build pipeline) - the
                admin-review warning would be meaningless noise there */}
            {project.block_auto_development && !isChat && (
              <Alert kind="warn">
                Automatic development is blocked pending admin authorization (manual review
                required).
              </Alert>
            )}

            {isAdmin && <AdminProjectControls project={project} onUpdated={setProject} />}

            {/* --- Activity: what's moving now (§14.8 console). The console
                appears while a run needs attention and retracts to a compact
                row once done - the full story stays in the request's thread. */}
            {!isDirect && project.dev_run_state !== "idle" &&
              (project.dev_run_state === "done" && !lingerDone ? (
                <button type="button" className="card dev-retracted" onClick={openOwningThread}>
                  <span className="dev-retracted-head">
                    <span className="section-title" style={{ margin: 0 }}>
                      Development
                    </span>
                    <CheckMark className="milestone-check run-check" />
                    {/* What the project has cost so far stays on screen once the
                        console retracts - it is the number customers look for. */}
                    {(project.tokens_consumed > 0 || project.cost_credits > 0) && (
                      <span
                        className="mono tiny dev-retracted-usage"
                        title={`${project.tokens_consumed.toLocaleString()} tokens billed on this project`}
                      >
                        {project.tokens_consumed.toLocaleString()} tok ·{" "}
                        <span className="grad-text">
                          {formatCreditsExact(project.cost_credits)} cr
                        </span>
                      </span>
                    )}
                  </span>
                  <span className="muted small">
                    Last build{owningRequest ? ` — “${owningRequest.title}”` : ""} finished
                    · open its thread →
                  </span>
                </button>
              ) : (
                <BuildConsole
                  key={project.id}
                  project={project}
                  canResume={canResume}
                  resumeBlocker={resumeBlocker ?? null}
                  retryBusy={busyAction === "resume"}
                  onRetry={() => nowActions.resume?.()}
                  onStale={reload}
                  consultant={consultant}
                  forRequest={
                    owningRequest
                      ? { title: owningRequest.title, onOpen: openOwningThread }
                      : null
                  }
                  readOnly={readOnly}
                  runId={primaryRun?.id}
                />
              ))}

            {/* §parallel-builds MR4: sibling runs stack under the primary
                console as their own collapsed consoles - request title, state,
                elapsed, tokens on the header; expanding polls that run's own
                feed; Stop is scoped to the run. */}
            {!isDirect &&
              siblingRuns.map((r) => (
                <RunConsole
                  key={r.id}
                  projectId={id}
                  run={r}
                  title={requestTitle(r.request_id)}
                  stoppable={!readOnly}
                  onOpenRequest={
                    r.request_id != null
                      ? () => navigate(`/projects/${id}/requests/${r.request_id}`)
                      : undefined
                  }
                />
              ))}

            {isDirect && (
              <CollapsibleCard title="Custom quote engagement" storageKey="project:direct-quote">
                <p className="muted small">
                  This is a human-quoted engagement. {consultant} reviews your request and sends a
                  tailored quote - there's no automated build or demo. Track progress and any quote
                  in the chat and Requests tabs.
                </p>
              </CollapsibleCard>
            )}

            {isChat && (
              <CollapsibleCard title="Conversation" storageKey="project:conversation">
                <p className="muted small">
                  Ask anything in the chat - answers draw on {consultant}'s experience knowledge
                  base and this project's Memory, billed per answer from your credits. Want a
                  human? "Ask for {consultant}'s review" hands the thread over.
                </p>
              </CollapsibleCard>
            )}

            {/* Demo - only once it exists; before that the NowPanel and the
                timeline already say a demo is coming. */}
            {!isDirect && !isChat && project.demo_url && (
              <CollapsibleCard title="Live demo" storageKey="project:demo">
                <DemoAccess
                  url={project.demo_url}
                  state={project.demo_state}
                  authUser={project.demo_basic_auth_user}
                  authPass={project.demo_basic_auth_pass}
                />
                {!readOnly && (
                  <div className="row gap-sm mt">
                    <button
                      className="btn btn-primary btn-sm"
                      disabled={!deployable || demoBusy || project.demo_state === "running"}
                      onClick={() => startStop("start")}
                    >
                      {demoBusy ? <Spinner /> : "Start"}
                    </button>
                    <button
                      className="btn btn-sm"
                      disabled={!deployable || demoBusy || project.demo_state === "stopped"}
                      onClick={() => startStop("stop")}
                    >
                      Stop
                    </button>
                  </div>
                )}
                <p className="tiny faint mt">
                  Demos auto-stop after {timeout} minutes (named volumes are kept, so restarts are
                  fast). Last started {relTime(project.demo_last_started_at)} · last stopped{" "}
                  {relTime(project.demo_last_stopped_at)}.
                </p>
              </CollapsibleCard>
            )}

            {/* --- Project setup: the pre-review workbench, tucked away after --- */}
            {!isChat && (
              <section className={`setup-group${isSetupOpen ? " open" : ""}`}>
                <button
                  type="button"
                  className="setup-toggle"
                  aria-expanded={isSetupOpen}
                  onClick={() => {
                    const next = !isSetupOpen;
                    setSetupOpen(next);
                    writeCardCollapsed("project:setup", !next);
                  }}
                >
                  <span className="section-title" style={{ margin: 0 }}>
                    Project setup
                  </span>
                  <span className="tiny faint">
                    {isAuto
                      ? "instructions · repository · git identity · filters"
                      : isDirect
                        ? "description"
                        : "description · repositories · git identity"}{" "}
                    {isSetupOpen ? "▾" : "▸"}
                  </span>
                </button>

                {isSetupOpen && (
                  <div className="stack">
                    <CollapsibleCard
                      title={isAuto ? "Development instructions" : "Description"}
                      storageKey="project:description"
                    >
                      <textarea
                        value={desc}
                        rows={6}
                        maxLength={40000}
                        disabled={!canEditDescription || descBusy || readOnly}
                        aria-label="Project description"
                        onChange={(e) => {
                          setDesc(e.target.value);
                          setDescDirty(true);
                        }}
                      />
                      {canEditDescription && !readOnly ? (
                        descDirty && (
                          <div className="row gap-sm mt">
                            <button
                              className="btn btn-primary btn-sm"
                              onClick={saveDescription}
                              disabled={descBusy || desc.trim().length === 0}
                            >
                              {descBusy ? <Spinner /> : "Save"}
                            </button>
                            <button
                              className="btn btn-sm"
                              onClick={() => {
                                setDesc(project.description);
                                setDescDirty(false);
                              }}
                              disabled={descBusy}
                            >
                              Cancel
                            </button>
                          </div>
                        )
                      ) : !canEditDescription ? (
                        <p className="tiny faint mt">
                          The description is locked while the project is{" "}
                          <strong>{project.status.replace(/_/g, " ")}</strong>. It can only be
                          edited in draft or while awaiting review.
                        </p>
                      ) : null}
                      {project.sovereign_comment && (
                        <p className="tiny faint">Sovereignty note: {project.sovereign_comment}</p>
                      )}
                    </CollapsibleCard>

                    {/* repo/issue-watch config is a workbench, not project
                        state - a read-only share doesn't get it at all */}
                    {!isDirect && !readOnly && (
                      <ReposCard
                        project={project}
                        onProjectChange={setProject}
                        onGoToMemory={() => setTab("memory")}
                        consultant={consultant}
                      />
                    )}

                    {!isDirect && !readOnly && (
                      <GitIdentityCard project={project} onUpdated={setProject} />
                    )}

                    {isAuto && !readOnly && (
                      <IssueWatchCard project={project} onUpdated={setProject} />
                    )}
                  </div>
                )}
              </section>
            )}

            {/* chat kind keeps its opening context readable */}
            {isChat && (
              <CollapsibleCard title="Opening message" storageKey="project:opening-message">
                <p className="muted small" style={{ whiteSpace: "pre-wrap" }}>
                  {project.description}
                </p>
              </CollapsibleCard>
            )}
          </div>
      )}

      {tab === "quotes" && <QuotesTab projectId={id} isAdmin={isAdmin} readOnly={readOnly} />}

      {tab === "requests" && (
        <RequestsTab
          projectId={id}
          isAdmin={isAdmin}
          requestId={sub}
          readOnly={readOnly}
          project={project}
          build={{
            canResume,
            resumeBlocker: resumeBlocker ?? null,
            retryBusy: busyAction === "resume",
            onRetry: () => nowActions.resume?.(),
            onStale: reload,
          }}
        />
      )}

      {tab === "routines" && <RoutinesTab projectId={id} readOnly={readOnly} />}

      {tab === "memory" && <MemoryTab projectId={id} readOnly={readOnly} isOwner={isOwner} />}

      {tab === "usage" && <UsageTab projectId={id} />}

      {tab === "mcp" && <McpTab projectId={id} readOnly={readOnly} />}

      {shareOpen && isOwner && (
        <ShareModal projectId={id} onClose={() => setShareOpen(false)} />
      )}
      </div>

      {/* The chat is the control surface, so it runs the full height of the page
          beside every tab rather than sitting inside the overview. It follows
          what you are looking at: the orchestrator's main thread by default, the
          open request's own thread while you are in it. Retracting it gives the
          work the whole width; the choice is remembered per browser. */}
      <aside
        className="project-chat-rail"
        aria-label={railRequest ? `Thread - ${railRequest.title}` : "Partner"}
      >
        <button
          type="button"
          className="chat-rail-toggle"
          onClick={() => {
            setChatOpen(!chatOpen);
            writeCardCollapsed(CHAT_RAIL_KEY, chatOpen);
          }}
          aria-expanded={chatOpen}
          title={
            (chatOpen ? "Retract " : "Open ") +
            (railRequest ? `the thread of "${railRequest.title}"` : "the partner")
          }
        >
          <span className="chat-rail-caret" aria-hidden="true">
            {chatOpen ? "›" : "‹"}
          </span>
          <span className="chat-rail-label">
            {/* The emoji is the at-a-glance tell: compass = the partner (the orchestrator),
                thread = one request's own conversation. */}
            <span className="chat-rail-emoji" aria-hidden="true">
              {railRequest ? "🧵" : "🧭"}
            </span>
            {railRequest ? railRequest.title : "Partner"}
          </span>
        </button>
        <div className="chat-rail-body" aria-hidden={!chatOpen}>
          {/* §threads: the chat orchestrates the work threads - the rail
              shows them and which one is building right now */}
          {!isChat && !railRequest && (
            <ThreadRail
              requests={requests}
              buildingId={
                ["running", "deploying", "awaiting_merge"].includes(project.dev_run_state)
                  ? (project.dev_request_id ??
                     requests.find((r) => r.type === "mvp")?.id ??
                     null)
                  : null
              }
              // §parallel-builds MR4: every building request's chip pulses,
              // not only the primary's ([] falls back to buildingId inside).
              buildingIds={buildingRequestIds.length > 0 ? buildingRequestIds : null}
              buildingKind={project.dev_run_state === "awaiting_merge" ? "merge" : "building"}
              onOpen={(rid) => navigate(`/projects/${id}/requests/${rid}`)}
              onAll={() => setTab("requests")}
            />
          )}
          <Chat
            // Remount on a thread switch so the new conversation loads clean.
            key={railThread}
            projectId={id}
            thread={railThread}
            canEmail={isAdmin}
            assistant={isChat && !railRequest}
            readOnly={readOnly}
            projectStatus={project.status}
            imageSupport={project.image_support}
            onStatus={onStatusPush}
            onDev={onDevPush}
            emptyHint={
              railRequest
                ? readOnly
                  ? "No messages in this request's thread yet."
                  : "Reply here and the agent folds it into this request's next run."
                : readOnly
                  ? "No messages yet - you're following this project read-only."
                  : isChat
                    ? "Ask anything - the assistant answers here."
                    : `Describe a change in plain words - the agent picks it up from here, and ${consultant} reads along.`
            }
            starters={
              isChat || railRequest
                ? undefined
                : ["What's the status?", "I'd like a change: ", "When can I see a demo?"]
            }
          />
        </div>
      </aside>
    </div>
  );
}

function IssueWatchCard({
  project,
  onUpdated,
}: {
  project: Project;
  onUpdated: (p: Project) => void;
}) {
  const toast = useToast();
  const watch = project.issue_watch ?? { labels: [], assignees: [], authors: [] };
  const [labels, setLabels] = useState(watch.labels.join(", "));
  const [assignees, setAssignees] = useState(watch.assignees.join(", "));
  const [authors, setAuthors] = useState(watch.authors.join(", "));
  const [dirty, setDirty] = useState(false);
  const [busy, setBusy] = useState(false);
  const csv = (v: string) => v.split(",").map((x) => x.trim()).filter(Boolean);

  async function save() {
    if (csv(labels).length === 0 && csv(assignees).length === 0) {
      toast.push("Set at least one label or assignee to watch.", "err");
      return;
    }
    setBusy(true);
    try {
      const updated = await projectsApi.update(project.id, {
        issue_watch: { labels: csv(labels), assignees: csv(assignees), authors: csv(authors) },
      });
      toast.push("Issue watch updated", "ok");
      onUpdated(updated);
      setDirty(false);
    } catch (err) {
      toast.push(err instanceof Error ? err.message : "Failed", "err");
    } finally {
      setBusy(false);
    }
  }

  const field = (
    label: string,
    value: string,
    set: (v: string) => void,
    placeholder: string,
  ) => (
    <div className="mt">
      <label>{label}</label>
      <input
        type="text"
        value={value}
        placeholder={placeholder}
        disabled={busy}
        onChange={(e) => {
          set(e.target.value);
          setDirty(true);
        }}
      />
    </div>
  );

  return (
    <CollapsibleCard title="Issue watch" storageKey="project:issue-watch">
      <p className="muted small">
        I poll the push repository's open issues about every minute. Labels and assignees are each
        "any of" lists - and an issue must satisfy BOTH lists when both are filled. Matching issues
        become build requests automatically; the optional author list restricts whose issues I act
        on.
      </p>
      {field("Labels (any of, comma-separated)", labels, setLabels, "ai, auto-dev")}
      {field("Assignees (any of, comma-separated)", assignees, setAssignees, "username")}
      {field("Only issues authored by (optional)", authors, setAuthors, "username")}
      {dirty && (
        <div className="row gap-sm mt">
          <button className="btn btn-primary btn-sm" onClick={save} disabled={busy}>
            {busy ? <Spinner /> : "Save"}
          </button>
        </div>
      )}
      <IssueWatchHistory projectId={project.id} />
    </CollapsibleCard>
  );
}

// §git identity: what the agent's commits in this project are authored as. The
// inputs start on the live values (the project's override, else the instance
// default), so saving an untouched form is a no-op.
function GitIdentityCard({
  project,
  onUpdated,
}: {
  project: Project;
  onUpdated: (p: Project) => void;
}) {
  const toast = useToast();
  const [name, setName] = useState(project.git_author_name_effective);
  const [email, setEmail] = useState(project.git_author_email_effective);
  const [busy, setBusy] = useState(false);
  const overridden = project.git_author_name !== null || project.git_author_email !== null;
  const dirty =
    name !== project.git_author_name_effective || email !== project.git_author_email_effective;

  async function save(next?: { name: string; email: string }) {
    const body = next ?? { name: name.trim(), email: email.trim() };
    setBusy(true);
    try {
      const updated = await projectsApi.update(project.id, {
        git_author_name: body.name,
        git_author_email: body.email,
      });
      toast.push("Git identity updated", "ok");
      setName(updated.git_author_name_effective);
      setEmail(updated.git_author_email_effective);
      onUpdated(updated);
    } catch (err) {
      toast.push(err instanceof Error ? err.message : "Failed", "err");
    } finally {
      setBusy(false);
    }
  }

  return (
    <CollapsibleCard title="Git identity" storageKey="project:git-identity">
      <p className="muted small">
        Who my commits in this project are authored as - the <code>user.name</code> and{" "}
        <code>user.email</code> git uses when I commit and push. Clearing a field restores the
        default.
      </p>
      <div className="grid-2">
        <div>
          <label htmlFor="git-author-name">Name</label>
          <input
            id="git-author-name"
            type="text"
            value={name}
            maxLength={120}
            disabled={busy}
            onChange={(e) => setName(e.target.value)}
          />
        </div>
        <div>
          <label htmlFor="git-author-email">Email</label>
          <input
            id="git-author-email"
            type="email"
            value={email}
            maxLength={254}
            disabled={busy}
            onChange={(e) => setEmail(e.target.value)}
          />
        </div>
      </div>
      <p className="tiny faint mt">
        {dirty
          ? "Not saved yet - Save to use this from the next build on."
          : overridden
            ? "Set for this project. Commits already pushed keep the identity they were made with."
            : "Currently the instance default."}
      </p>
      {(dirty || overridden) && (
        <div className="row gap-sm mt">
          {dirty && (
            <button
              className="btn btn-primary btn-sm"
              onClick={() => save()}
              disabled={busy || !name.trim() || !email.trim()}
            >
              {busy ? <Spinner /> : "Save"}
            </button>
          )}
          {overridden && (
            <button
              className="btn btn-sm"
              onClick={() => save({ name: "", email: "" })}
              disabled={busy}
            >
              Reset to default
            </button>
          )}
        </div>
      )}
    </CollapsibleCard>
  );
}

const WATCH_KIND_LABEL: Record<IssueWatchEvent["kind"], string> = {
  registered: "Issue registered",
  started: "Build started",
  deferred: "Deferred - daily cap reached",
  paused: "Paused - credit balance empty",
  unpollable: "Can't poll the repository",
  comment_failed: "Couldn't comment on the issue",
};

const WATCH_PAGE_SIZE = 10;

function IssueWatchHistory({ projectId }: { projectId: string }) {
  const [page, setPage] = useState(0);
  const [data, setData] = useState<IssueWatchEventPage | null>(null);

  // The sweep runs every minute; a matching refresh keeps the visible page live.
  useEffect(() => {
    let gone = false;
    const load = () =>
      projectsApi
        .issueEvents(projectId, page * WATCH_PAGE_SIZE, WATCH_PAGE_SIZE)
        .then((d) => {
          if (!gone) setData(d);
        })
        .catch(() => {});
    load();
    const t = setInterval(load, 60_000);
    return () => {
      gone = true;
      clearInterval(t);
    };
  }, [projectId, page]);

  if (data === null) return null;
  const pages = Math.max(1, Math.ceil(data.total / WATCH_PAGE_SIZE));
  return (
    <div className="mt">
      <label>History</label>
      {data.total === 0 ? (
        <p className="tiny faint">Nothing yet - issues the watch acts on will appear here.</p>
      ) : (
        data.events.map((e) => (
          <div key={e.id} className="tiny mt-xs">
            <span className="faint">{relTime(e.created_at)}</span>
            {" · "}
            <strong>{WATCH_KIND_LABEL[e.kind] ?? e.kind}</strong>
            {e.issue_url && (
              <>
                {" · "}
                <a href={e.issue_url} target="_blank" rel="noreferrer">
                  {e.issue_title || e.issue_url}
                </a>
              </>
            )}
            {e.request_id && (
              <>
                {" · "}
                <Link to={`/projects/${projectId}/requests/${e.request_id}`}>view request</Link>
              </>
            )}
            {e.detail && <div className="faint">{e.detail}</div>}
          </div>
        ))
      )}
      <Pager page={Math.min(page, pages - 1)} pages={pages} onPage={setPage} />
    </div>
  );
}
