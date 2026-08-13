import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { adminApi, billingApi, projectsApi, quotesApi, requestsApi } from "../lib/endpoints";
import { useAuth } from "../lib/auth";
import { useToast } from "../lib/toast";
import { Alert, Badge, Loading, Spinner, formatCredits } from "./ui";
import BuildConsole, { RunConsole } from "./BuildConsole";
import { QuoteCard } from "./QuotesTab";
import { PrChips, RequestList, validPrRefs } from "@shared-ui";
import type { DevRunSummary, Project, ProjectRequest, Quote, RequestEstimate, RequestHandling, RequestType, ServerRequestType } from "../types";

function formatHours(h: number): string {
  if (h < 1) return `${Math.max(1, Math.round(h * 60))} min`;
  return `${Math.round(h * 10) / 10} h`;
}

const TYPE_LABELS: Record<ServerRequestType, string> = {
  mvp: "Initial build",
  feature: "New feature",
  edit: "Edit",
  bug: "Bug fix",
  production_deploy: "Production deployment",
};

const STATUS_KIND: Record<string, string> = {
  proposed: "awaiting_customer",
  open: "development",
  quoted: "payment_due",
  in_progress: "development",
  done: "finished",
  rejected: "canceled",
};


export default function RequestsTab({
  projectId,
  isAdmin,
  requestId,
  readOnly = false,
  project,
  build,
}: {
  projectId: string;
  isAdmin: boolean;
  requestId?: string;
  // §sharing: a read-only share browses requests and threads but can't create,
  // start, or post.
  readOnly?: boolean;
  // §threads: the request that owns the current/last run shows its development
  // console beside the thread - the host passes the live project + the same
  // resume wiring the overview console uses.
  project?: Project | null;
  build?: {
    canResume: boolean;
    resumeBlocker: string | null;
    retryBusy: boolean;
    onRetry: () => void;
    onStale: () => void;
  };
}) {
  const toast = useToast();
  const navigate = useNavigate();
  const { settings } = useAuth();
  const consultant = settings?.consultant_first_name ?? "Consultant";
  const [requests, setRequests] = useState<ProjectRequest[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  // The open request lives in the URL (/projects/:id/requests/:requestId) so a
  // thread is deep-linkable and back/forward works.
  const selected = (requestId && requests?.find((r) => r.id === requestId)) || null;
  const [creating, setCreating] = useState(false);

  // create form
  const [type, setType] = useState<RequestType>("feature");
  const [handling, setHandling] = useState<RequestHandling>("ai");
  const [body, setBody] = useState("");
  // §repo binding: "" = the project's default push target.
  const [repoId, setRepoId] = useState("");
  const repos = project?.repos ?? [];
  const repoName = (uri: string) =>
    uri.replace(/\.git$/, "").split(/[/:]/).slice(-2).join("/");
  const [submitting, setSubmitting] = useState(false);
  const [estimate, setEstimate] = useState<RequestEstimate | null>(null);
  const [estimating, setEstimating] = useState(false);
  // Inline rename in the thread view (the title is machine-generated at creation).
  const [titleEditing, setTitleEditing] = useState(false);
  const [titleDraft, setTitleDraft] = useState("");
  const [titleBusy, setTitleBusy] = useState(false);
  const [starting, setStarting] = useState(false);
  // §threads: quotes attached to the open request render as cards in its
  // thread view (the only quote surface on projects whose Quotes tab hides).
  const [quotes, setQuotes] = useState<Quote[]>([]);
  const [balance, setBalance] = useState<number | null>(null);

  const selectedId = selected?.id ?? null;
  function loadQuotes() {
    if (!selectedId) return;
    quotesApi.list(projectId).then(setQuotes).catch(() => {});
    billingApi.balance().then((b) => setBalance(b.credit_balance)).catch(() => {});
  }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(loadQuotes, [projectId, selectedId]);

  // §threads: the open request's development history, one console per run.
  // Re-fetched when the live run changes state so a finishing build lands in
  // the history without a page reload.
  const [runs, setRuns] = useState<DevRunSummary[]>([]);
  // §parallel-builds MR4: a sibling run can change state without moving the
  // mirror scalar - stamp the active set so the history refetches on any of
  // them, not only the primary.
  const runsStamp = (project?.dev_runs ?? []).map((r) => `${r.id}:${r.state}`).join(",");
  useEffect(() => {
    if (!selectedId) {
      setRuns([]);
      return;
    }
    projectsApi.devRuns(projectId, selectedId).then(setRuns).catch(() => {});
  }, [projectId, selectedId, project?.dev_run_state, runsStamp]);
  const requestQuotes = selectedId ? quotes.filter((q) => q.request_id === selectedId) : [];

  const isProdDeploy = type === "production_deploy";
  // Estimation only makes sense for AI-handled work; manual requests are
  // priced by the consultant (quote flow).
  const estimatable = !isProdDeploy && handling === "ai";

  async function fetchEstimate() {
    setEstimating(true);
    setEstimate(null);
    try {
      const { task_id } = await requestsApi.estimate(projectId, {
        type,
        handling,
        body: body.trim(),
      });
      const deadline = Date.now() + 60_000;
      while (Date.now() < deadline) {
        await new Promise((r) => setTimeout(r, 1500));
        const res = await requestsApi.estimateResult(projectId, task_id);
        if (res.state === "done") {
          setEstimate(res.estimate ?? { available: false });
          return;
        }
        if (res.state === "failed") {
          setEstimate({ available: false });
          return;
        }
      }
      setEstimate({ available: false });
    } catch {
      setEstimate({ available: false });
    } finally {
      setEstimating(false);
    }
  }

  function load() {
    requestsApi
      .list(projectId)
      .then(setRequests)
      .catch((err) => setError(err instanceof Error ? err.message : "Failed to load requests."));
  }
  useEffect(load, [projectId]);

  // Deep link to a request that doesn't exist (deleted, wrong project): fall
  // back to the list URL instead of a dead detail view.
  useEffect(() => {
    if (requestId && requests && !requests.some((r) => r.id === requestId)) {
      navigate(`/projects/${projectId}/requests`, { replace: true });
    }
  }, [requestId, requests, navigate, projectId]);

  async function create(e: React.FormEvent) {
    e.preventDefault();
    if (!body.trim()) return;
    setSubmitting(true);
    try {
      await requestsApi.create(projectId, {
        type,
        handling: isProdDeploy ? "manual" : handling,
        body: body.trim(),
        ...(repoId ? { repo_id: repoId } : {}),
      });
      toast.push("Request created", "ok");
      setBody("");
      setCreating(false);
      load();
    } catch (err) {
      toast.push(err instanceof Error ? err.message : "Could not create request", "err");
    } finally {
      setSubmitting(false);
    }
  }

  async function saveTitle() {
    if (!selected) return;
    const t = titleDraft.trim();
    if (!t || t === selected.title) {
      setTitleEditing(false);
      return;
    }
    setTitleBusy(true);
    try {
      const updated = await requestsApi.update(projectId, selected.id, { title: t });
      setRequests((rs) => rs?.map((r) => (r.id === updated.id ? updated : r)) ?? rs);
      setTitleEditing(false);
      toast.push("Request renamed.", "ok");
      load();
    } catch (err) {
      toast.push(err instanceof Error ? err.message : "Failed to rename the request", "err");
    } finally {
      setTitleBusy(false);
    }
  }

  const [validating, setValidating] = useState(false);
  const [canceling, setCanceling] = useState(false);
  async function cancelRequest() {
    if (!selected) return;
    if (
      !window.confirm(
        "Cancel this request? It closes as rejected and its work stops being tracked - a pull request already open on your repository stays yours to close.",
      )
    )
      return;
    setCanceling(true);
    try {
      const updated = await requestsApi.cancel(projectId, selected.id);
      setRequests((rs) => rs?.map((r) => (r.id === updated.id ? updated : r)) ?? rs);
      toast.push("Request canceled.", "ok");
      load();
    } catch (err) {
      toast.push(err instanceof Error ? err.message : "Could not cancel the request", "err");
    } finally {
      setCanceling(false);
    }
  }
  async function validateRequest() {
    if (!selected) return;
    if (
      !window.confirm(
        "Mark this request as delivered? Its open pull request stops being watched - use this when the work already landed on your repository.",
      )
    )
      return;
    setValidating(true);
    try {
      const updated = await requestsApi.validate(projectId, selected.id);
      setRequests((rs) => rs?.map((r) => (r.id === updated.id ? updated : r)) ?? rs);
      toast.push("Request marked as delivered.", "ok");
      load();
    } catch (err) {
      toast.push(err instanceof Error ? err.message : "Could not validate the request", "err");
    } finally {
      setValidating(false);
    }
  }

  async function startBuild() {
    if (!selected) return;
    setStarting(true);
    try {
      const updated = await requestsApi.start(projectId, selected.id);
      setRequests((rs) => rs?.map((r) => (r.id === updated.id ? updated : r)) ?? rs);
      toast.push("Build started", "ok");
      load();
    } catch (err) {
      toast.push(err instanceof Error ? err.message : "Could not start the build", "err");
    } finally {
      setStarting(false);
    }
  }

  if (error) return <Alert kind="error">{error}</Alert>;
  if (!requests) return <Loading />;


  // Thread view for a single request. The request owning the current/last run
  // gets its development console beside the thread (§threads: the thread IS
  // the work unit - conversation and build side by side).
  if (selected) {
    const ownsRun = Boolean(
      project &&
        project.dev_run_state !== "idle" &&
        (project.dev_request_id === selected.id ||
          (!project.dev_request_id && selected.type === "mvp")),
    );
    // The scalar-mirrored current run renders as the full BuildConsole below;
    // every other run of this request keeps its own read-only console (newest
    // first, so runs[0] is the one the scalars mirror when ownsRun).
    const historyRuns = ownsRun ? runs.slice(1) : runs;
    return (
      <div>
        <button
          className="btn btn-sm mb"
          onClick={() => {
            setTitleEditing(false);
            navigate(`/projects/${projectId}/requests`);
            load();
          }}
        >
          ← Back to requests
        </button>
        <div className="card mb">
          <div className="between">
            <div>
              <div className="row gap-sm">
                <span className="badge">{TYPE_LABELS[selected.type]}</span>
                <span className="badge">{selected.handling === "ai" ? "AI" : "Manual"}</span>
                <Badge label={selected.status} kind={STATUS_KIND[selected.status] ?? "draft"} />
                {selected.repo_id &&
                  (() => {
                    const r = repos.find((x) => x.id === selected.repo_id);
                    return r ? (
                      <span className="badge mono" title="This request builds into this repository">
                        {repoName(r.ssh_uri)}
                      </span>
                    ) : null;
                  })()}
              </div>
              {titleEditing ? (
                <form
                  className="row gap-sm"
                  style={{ marginTop: "0.6rem" }}
                  onSubmit={(e) => {
                    e.preventDefault();
                    void saveTitle();
                  }}
                >
                  <input
                    type="text"
                    value={titleDraft}
                    maxLength={255}
                    autoFocus
                    aria-label="Request title"
                    onChange={(e) => setTitleDraft(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === "Escape") setTitleEditing(false);
                    }}
                  />
                  <button
                    type="submit"
                    className="btn btn-sm btn-primary"
                    disabled={titleBusy || !titleDraft.trim()}
                  >
                    {titleBusy ? <Spinner /> : "Save"}
                  </button>
                  <button
                    type="button"
                    className="btn btn-sm btn-ghost"
                    disabled={titleBusy}
                    onClick={() => setTitleEditing(false)}
                  >
                    Cancel
                  </button>
                </form>
              ) : (
                <div className="row gap-sm" style={{ marginTop: "0.6rem" }}>
                  <h3 style={{ margin: 0 }}>{selected.title}</h3>
                  {!readOnly && (
                    <button
                      type="button"
                      className="btn btn-sm btn-ghost"
                      title="Rename request"
                      aria-label="Rename request"
                      onClick={() => {
                        setTitleDraft(selected.title);
                        setTitleEditing(true);
                      }}
                    >
                      ✎
                    </button>
                  )}
                </div>
              )}
              {(selected.pr_urls?.length ?? 0) > 0 && (
                <div style={{ marginTop: "0.5rem" }}>
                  <PrChips refs={validPrRefs(selected.pr_urls)} />
                </div>
              )}
            </div>
            <div style={{ textAlign: "right" }}>
              {selected.price_credits != null && (
                <div className="grad-text" style={{ fontSize: "1.1rem" }}>
                  {formatCredits(selected.price_credits)} cr
                </div>
              )}
              {selected.tokens_consumed > 0 && (
                <div
                  className="tiny faint nowrap"
                  title={`≈ ${formatCredits(selected.cost_credits)} credits of AI usage on this request`}
                >
                  {selected.tokens_consumed.toLocaleString()} tokens
                </div>
              )}
            </div>
          </div>
          {selected.type === "mvp" && (
            <p className="tiny faint" style={{ margin: "0.5rem 0 0" }}>
              This thread follows your initial build - its development console appears right
              here while a run is active, and anything you write reaches the agent when the
              build resumes.
            </p>
          )}
          {selected.status === "proposed" && selected.handling === "ai" && !readOnly && (
            <div
              className="mt"
              style={{ borderTop: "1px solid var(--border)", paddingTop: "0.75rem" }}
            >
              <p className="tiny faint" style={{ margin: "0 0 0.5rem" }}>
                I read this from your chat and it's waiting for your go-ahead. Reply “go ahead”
                in the thread below, or start the build here.
              </p>
              <button
                className="btn btn-primary btn-sm"
                onClick={startBuild}
                disabled={starting}
              >
                {starting ? <Spinner /> : "Start build"}
              </button>
            </div>
          )}
          {/* §requests validate: the human's per-request "approve delivery" -
              the escape hatch when the work landed but the pipeline reported a
              failure (e.g. the push updated an already-open PR). Hidden while
              a build for it is running (stop first). */}
          {selected.handling === "ai" &&
            selected.type !== "production_deploy" &&
            selected.type !== "mvp" &&
            !["done", "rejected"].includes(selected.status) &&
            !readOnly && (
              <div
                className="mt"
                style={{ borderTop: "1px solid var(--border)", paddingTop: "0.75rem" }}
              >
                {selected.status !== "proposed" && (
                  <p className="tiny faint" style={{ margin: "0 0 0.5rem" }}>
                    Work already landed on your repository? You can close this request yourself -
                    its open pull request stops being watched.
                  </p>
                )}
                <div className="row gap-sm">
                  {selected.status !== "proposed" && (
                    <button
                      className="btn btn-sm"
                      onClick={validateRequest}
                      disabled={validating || canceling}
                    >
                      {validating ? <Spinner /> : "Mark as delivered"}
                    </button>
                  )}
                  <button
                    className="btn btn-sm btn-ghost"
                    onClick={cancelRequest}
                    disabled={canceling || validating}
                    title="Closes this request as rejected; a build in flight must be stopped first"
                  >
                    {canceling ? <Spinner /> : "Cancel request"}
                  </button>
                </div>
              </div>
            )}
          {isAdmin && (
            <AdminRequestControls request={selected} onDone={() => { load(); }} />
          )}
        </div>
        {requestQuotes.length > 0 && (
          <div className="mb">
            <p className="muted small" style={{ margin: "0 0 0.5rem" }}>
              {consultant} quoted this request:
            </p>
            <div className="stack">
              {requestQuotes.map((q) => (
                <QuoteCard
                  key={q.id}
                  projectId={projectId}
                  quote={q}
                  isAdmin={isAdmin}
                  balance={balance}
                  readOnly={readOnly}
                  onChanged={() => {
                    loadQuotes();
                    load();
                  }}
                />
              ))}
            </div>
          </div>
        )}
        {/* The thread itself lives in the page's chat rail while a request is
            open, so this column carries the request's WORK - its build console -
            at full width instead of a squeezed half. */}
        {ownsRun && project && build && (
          <BuildConsole
            key={project.id}
            project={project}
            canResume={build.canResume}
            resumeBlocker={build.resumeBlocker}
            retryBusy={build.retryBusy}
            onRetry={build.onRetry}
            onStale={build.onStale}
            consultant={consultant}
            readOnly={readOnly}
          />
        )}
        {historyRuns.map((r, i) => (
          <RunConsole
            key={r.id}
            projectId={projectId}
            run={r}
            defaultOpen={!ownsRun && i === 0}
            stoppable={!readOnly}
          />
        ))}
      </div>
    );
  }

  return (
    <div>
      <div className="between mb">
        <p className="muted small" style={{ margin: 0 }}>
          Request features, edits, bug fixes, or a production deployment. Each request has its own
          thread.
        </p>
        {!creating && !readOnly && (
          <button className="btn btn-primary btn-sm" onClick={() => setCreating(true)}>
            + New request
          </button>
        )}
      </div>

      {creating && (
        <form className="card mb" onSubmit={create}>
          <div className="grid-2">
            <div className="field">
              <label>Type</label>
              <select
                value={type}
                onChange={(e) => {
                  setType(e.target.value as RequestType);
                  setEstimate(null);
                }}
              >
                <option value="feature">New feature</option>
                <option value="edit">Edit</option>
                <option value="bug">Bug fix</option>
                <option value="production_deploy">Production deployment</option>
              </select>
            </div>
            {repos.length > 1 && !isProdDeploy && (
              <div className="field">
                <label>Target repository</label>
                <select value={repoId} onChange={(e) => setRepoId(e.target.value)}>
                  <option value="">
                    Default
                    {(() => {
                      const push = repos.find((r) => r.is_push_target);
                      return push ? ` (${repoName(push.ssh_uri)})` : "";
                    })()}
                  </option>
                  {repos.map((r) => (
                    <option key={r.id} value={r.id}>
                      {repoName(r.ssh_uri)}
                    </option>
                  ))}
                </select>
              </div>
            )}
            <div className="field">
              <label>Handling</label>
              <select
                value={isProdDeploy ? "manual" : handling}
                disabled={isProdDeploy}
                onChange={(e) => {
                  setHandling(e.target.value as RequestHandling);
                  setEstimate(null);
                }}
              >
                <option value="ai">AI-handled</option>
                <option value="manual">Manual ({consultant})</option>
              </select>
            </div>
          </div>
          {isProdDeploy && (
            <Alert kind="info">
              Production deployment is always handled manually. {consultant} reviews the request and sends
              a separate quote - real production has context-specific needs and is priced case by
              case, outside your prepaid credits.
            </Alert>
          )}
          <div className="field">
            <label>Details</label>
            <textarea
              value={body}
              placeholder="Describe what you need. This becomes the first message of the thread."
              onChange={(e) => {
                setBody(e.target.value);
                setEstimate(null);
              }}
            />
            <p className="tiny faint" style={{ margin: "0.4rem 0 0" }}>
              A title is generated from this description - you can rename the request from its
              thread view.
            </p>
          </div>
          {estimatable && (
            <div className="field">
              <div className="row gap-sm">
                <button
                  type="button"
                  className="btn btn-sm"
                  onClick={fetchEstimate}
                  disabled={estimating || !body.trim()}
                >
                  {estimating ? <Spinner /> : "Estimate cost & time"}
                </button>
                {estimate?.available && (
                  <span className="small">
                    ≈ <span className="grad-text">{formatCredits(estimate.cost_credits ?? 0)}</span>{" "}
                    credits · ≈ {formatHours(estimate.time_hours ?? 0)}
                  </span>
                )}
              </div>
              {estimating && (
                <p className="tiny faint" style={{ margin: "0.4rem 0 0" }}>
                  Asking the project's model, based on past similar work…
                </p>
              )}
              {estimate?.available && (
                <p className="tiny faint" style={{ margin: "0.4rem 0 0" }}>
                  {estimate.explanation} This is an automated estimation - it cannot be
                  considered a quote.
                </p>
              )}
              {estimate && !estimate.available && (
                <p className="tiny faint" style={{ margin: "0.4rem 0 0" }}>
                  No estimate can be made right now.
                </p>
              )}
            </div>
          )}
          <div className="wizard-actions" style={{ marginTop: 0 }}>
            <button type="button" className="btn" onClick={() => setCreating(false)}>
              Cancel
            </button>
            <button
              type="submit"
              className="btn btn-primary"
              disabled={submitting || !body.trim()}
            >
              {submitting ? <Spinner /> : "Create request"}
            </button>
          </div>
        </form>
      )}

      <RequestList
        requests={requests}
        onSelect={(id) => navigate(`/projects/${projectId}/requests/${id}`)}
        emptyLabel="No requests yet."
      />
    </div>
  );
}

function AdminRequestControls({
  request,
  onDone,
}: {
  request: ProjectRequest;
  onDone: () => void;
}) {
  const toast = useToast();
  const [price, setPrice] = useState(request.price_credits?.toString() ?? "");
  const [quoteAmount, setQuoteAmount] = useState("");
  const [busy, setBusy] = useState<"price" | "quote" | null>(null);

  async function setPriceCredits() {
    const val = Number(price);
    if (!Number.isFinite(val)) return;
    setBusy("price");
    try {
      await adminApi.priceRequest(request.id, val);
      toast.push("Price set", "ok");
      onDone();
    } catch (err) {
      toast.push(err instanceof Error ? err.message : "Failed", "err");
    } finally {
      setBusy(null);
    }
  }

  async function createQuote() {
    const val = Number(quoteAmount);
    if (!Number.isFinite(val) || val <= 0) return;
    setBusy("quote");
    try {
      const q = await adminApi.quoteRequest(request.id, val);
      toast.push(q.payment_link ? "Quote created with payment link" : "Quote created", "ok");
      onDone();
    } catch (err) {
      const msg = err instanceof Error ? err.message : "Failed";
      toast.push(msg.includes("Stripe") || msg.includes("503") ? "Stripe not configured" : msg, "err");
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="mt" style={{ borderTop: "1px solid var(--border)", paddingTop: "0.75rem" }}>
      <div className="section-title">Admin - pricing</div>
      <div className="grid-2">
        <div className="row gap-sm">
          <input
            type="number"
            placeholder="Credits"
            value={price}
            onChange={(e) => setPrice(e.target.value)}
          />
          <button className="btn btn-sm" onClick={setPriceCredits} disabled={busy === "price"}>
            {busy === "price" ? <Spinner /> : "Set price"}
          </button>
        </div>
        <div className="row gap-sm">
          <input
            type="number"
            placeholder="Quote €"
            value={quoteAmount}
            onChange={(e) => setQuoteAmount(e.target.value)}
          />
          <button className="btn btn-sm" onClick={createQuote} disabled={busy === "quote"}>
            {busy === "quote" ? <Spinner /> : "Create quote"}
          </button>
        </div>
      </div>
    </div>
  );
}
