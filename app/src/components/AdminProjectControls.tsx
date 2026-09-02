import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { adminApi, kbApi, modelEndpointApi, toolsApi, type ProjectTool } from "../lib/endpoints";
import { useToast } from "../lib/toast";
import { CollapsibleCard, Modal, Spinner, Toggle, formatCredits } from "./ui";
import type {
  AdminSettings,
  KnowledgeBase,
  ModelEndpoint,
  Project,
  ProjectSpend,
  ProjectStatus,
} from "../types";

const SPEND_LABELS: Record<string, string> = {
  consumption: "AI usage",
  review_request: "review fees",
  refund: "refunds",
  adjustment: "adjustments",
};

const STATUSES: ProjectStatus[] = [
  "draft",
  "awaiting_review",
  "payment_due",
  "development",
  "awaiting_customer",
  "awaiting_admin",
  "finished",
  "canceled",
];

// Docker-style resource quantity, mirroring the backend's _RESOURCE_RE.
const RESOURCE_RE = /^[0-9]+(\.[0-9]+)?[bkmgBKMG]?$/;

/** Field label with the explanation in an ⓘ tooltip (dense admin form). */
// A blank cap field inherits the instance default, and naming the NUMBER is the
// whole point: "instance default" on its own told the admin nothing they could
// weigh against the value they were about to type - they had to go read the
// deployment's env to find out what they were overriding. Falls back to the bare
// wording only while the settings payload is still in flight.
function inheritPlaceholder(value: number | undefined): string {
  return value == null ? "instance default" : `instance default (${value})`;
}

function LabelHint({ label, hint }: { label: string; hint: string }) {
  return (
    <span>
      {label}{" "}
      <span title={hint} aria-label={hint} style={{ cursor: "help", opacity: 0.7 }}>
        ⓘ
      </span>
    </span>
  );
}

export default function AdminProjectControls({
  project,
  onUpdated,
}: {
  project: Project;
  onUpdated: (p: Project) => void;
}) {
  const toast = useToast();

  const [status, setStatus] = useState<ProjectStatus>(project.status);
  const [note, setNote] = useState("");
  const [tier, setTier] = useState(project.tier ?? "mvp");
  const [subdomain, setSubdomain] = useState(project.subdomain ?? "");
  const [blockAuto, setBlockAuto] = useState(project.block_auto_development);
  const [maxIter, setMaxIter] = useState(
    project.dev_max_iterations == null ? "" : String(project.dev_max_iterations),
  );
  const [runTimeout, setRunTimeout] = useState(
    project.dev_run_timeout_minutes == null ? "" : String(project.dev_run_timeout_minutes),
  );
  const [parallelLimit, setParallelLimit] = useState(
    project.dev_parallel_limit == null ? "" : String(project.dev_parallel_limit),
  );
  const [devCpu, setDevCpu] = useState(project.dev_cpu_request ?? "");
  const [devMem, setDevMem] = useState(project.dev_mem_request ?? "");
  // §dev harness: "" = inherit the instance default. The picker only appears once
  // the admin has enabled selection instance-wide (adminSettings carries both the
  // flag and the allowed catalog).
  const [devHarness, setDevHarness] = useState(project.dev_harness ?? "");
  const [adminSettings, setAdminSettings] = useState<AdminSettings | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [modelOpen, setModelOpen] = useState(false);
  const [kbOpen, setKbOpen] = useState(false);
  const [toolsOpen, setToolsOpen] = useState(false);
  const [quoteAmount, setQuoteAmount] = useState("");
  const [spend, setSpend] = useState<ProjectSpend | null>(null);

  useEffect(() => {
    adminApi.projectSpend(project.id).then(setSpend).catch(() => {});
  }, [project.id]);

  useEffect(() => {
    adminApi.settings().then(setAdminSettings).catch(() => {});
  }, []);

  // §dev harness: the picker exists only while the instance offers selection, and
  // lists only what the admin allowed.
  const harnessPickable = !!adminSettings?.dev_harness_selection_enabled;
  const harnessOptions = (adminSettings?.dev_harnesses ?? []).filter((h) =>
    (adminSettings?.dev_harness_allowed ?? []).includes(h.id),
  );
  const defaultHarnessLabel =
    (adminSettings?.dev_harnesses ?? []).find((h) => h.id === adminSettings?.dev_harness_default)
      ?.label ?? "instance default";
  // What the picked (or inherited) harness actually is. Shown under the select
  // because a harness can carry a constraint the label cannot - the Claude driver
  // runs Anthropic models only, and pinning it on a project pointed elsewhere is
  // refused by the API rather than discovered inside the sandbox.
  const selectedHarness = (adminSettings?.dev_harnesses ?? []).find(
    (h) => h.id === (devHarness || adminSettings?.dev_harness_default),
  );

  async function saveStatus() {
    setBusy("status");
    try {
      const updated = await adminApi.setStatus(project.id, status, note.trim() || undefined);
      toast.push("Status updated", "ok");
      setNote("");
      onUpdated(updated);
    } catch (err) {
      toast.push(err instanceof Error ? err.message : "Failed", "err");
    } finally {
      setBusy(null);
    }
  }

  async function saveSettings() {
    setBusy("settings");
    try {
      const timeoutTrim = runTimeout.trim();
      const timeoutVal = timeoutTrim === "" ? null : parseInt(timeoutTrim, 10);
      if (
        timeoutVal !== null &&
        (!Number.isInteger(timeoutVal) || timeoutVal < 5 || timeoutVal > 240)
      ) {
        toast.push("Run timeout must be 5-240 minutes (or empty for the instance default)", "err");
        setBusy(null);
        return;
      }
      const iterTrim = maxIter.trim();
      const iterVal = iterTrim === "" ? null : parseInt(iterTrim, 10);
      if (iterVal !== null && (!Number.isInteger(iterVal) || iterVal < 1 || iterVal > 500)) {
        toast.push("Agent iterations must be 1-500 (or empty for the instance default)", "err");
        setBusy(null);
        return;
      }
      const plTrim = parallelLimit.trim();
      const plVal = plTrim === "" ? null : parseInt(plTrim, 10);
      if (plVal !== null && (!Number.isInteger(plVal) || plVal < 1 || plVal > 16)) {
        toast.push("Parallel builds must be 1-16 (or empty for the instance default)", "err");
        setBusy(null);
        return;
      }
      const cpuVal = devCpu.trim() === "" ? null : devCpu.trim();
      const memVal = devMem.trim() === "" ? null : devMem.trim();
      if ((cpuVal !== null && !RESOURCE_RE.test(cpuVal)) ||
          (memVal !== null && !RESOURCE_RE.test(memVal))) {
        toast.push("Dev pod resources look like 0.5, 512m or 4g (or empty for the instance default)", "err");
        setBusy(null);
        return;
      }
      const updated = await adminApi.patchProject(project.id, {
        tier,
        subdomain: subdomain.trim(),
        block_auto_development: blockAuto,
        dev_max_iterations: iterVal,
        dev_run_timeout_minutes: timeoutVal,
        dev_parallel_limit: plVal,
        dev_cpu_request: cpuVal,
        dev_mem_request: memVal,
        // Only sent when the instance offers the choice: the API refuses a pin
        // while selection is disabled, and an unsent field is left untouched.
        ...(harnessPickable ? { dev_harness: devHarness === "" ? null : devHarness } : {}),
      });
      toast.push("Project settings updated", "ok");
      onUpdated(updated);
    } catch (err) {
      toast.push(err instanceof Error ? err.message : "Failed", "err");
    } finally {
      setBusy(null);
    }
  }

  async function sendProjectQuote() {
    const amount = parseFloat(quoteAmount);
    if (!(amount > 0)) {
      toast.push("Enter a quote amount", "err");
      return;
    }
    setBusy("quote");
    try {
      const q = await adminApi.quoteProject(project.id, amount);
      toast.push(
        q.payment_link ? "Quote sent with payment link" : "Quote saved (Stripe not configured)",
        "ok",
      );
      setQuoteAmount("");
    } catch (err) {
      toast.push(err instanceof Error ? err.message : "Failed", "err");
    } finally {
      setBusy(null);
    }
  }

  async function refundReview() {
    setBusy("refund");
    try {
      const r = await adminApi.refundReview(project.id);
      toast.push(`Refunded ${formatCredits(r.refunded)} credits`, "ok");
    } catch (err) {
      toast.push(err instanceof Error ? err.message : "Nothing to refund", "err");
    } finally {
      setBusy(null);
    }
  }

  const isDirect = project.kind === "direct_quote";

  return (
    <CollapsibleCard title="Admin controls" className="admin-card">
      <div className="grid-2">
        {/* Status editor */}
        <div>
          <label>Status</label>
          <div className="stack" style={{ gap: "0.5rem" }}>
            <select value={status} onChange={(e) => setStatus(e.target.value as ProjectStatus)}>
              {STATUSES.map((s) => (
                <option key={s} value={s}>
                  {s.replace(/_/g, " ")}
                </option>
              ))}
            </select>
            <input
              type="text"
              placeholder="Note (posted to chat)"
              value={note}
              onChange={(e) => setNote(e.target.value)}
            />
            <button className="btn btn-sm" onClick={saveStatus} disabled={busy === "status"}>
              {busy === "status" ? <Spinner /> : "Update status"}
            </button>
          </div>
        </div>

        {/* Tier / subdomain / block */}
        <div>
          <label>Tier</label>
          <div className="stack" style={{ gap: "0.5rem" }}>
            <select value={tier} onChange={(e) => setTier(e.target.value as "mvp" | "production")}>
              <option value="mvp">mvp</option>
              <option value="production">production</option>
            </select>
            <input
              type="text"
              placeholder="subdomain"
              value={subdomain}
              onChange={(e) => setSubdomain(e.target.value)}
            />
            <label className="checkbox-row">
              <Toggle checked={blockAuto} onChange={setBlockAuto} />
              Block auto development
            </label>
            <label className="field">
              <LabelHint
                label="Agent iterations per run"
                hint="Empty = the instance default. Raise only for projects whose model needs more steps - the run wall-clock still bounds every build."
              />
              <input
                type="number"
                min={1}
                max={500}
                value={maxIter}
                onChange={(e) => setMaxIter(e.target.value)}
                placeholder={inheritPlaceholder(adminSettings?.dev_max_iterations_default)}
              />
            </label>
            <label className="field">
              <LabelHint
                label="Run timeout (minutes)"
                hint="Wall-clock ceiling on ONE build. The sandbox is force-killed past it and the run parks resumable, so raise it for projects whose builds legitimately sit on a long step - and remember it is a spend fail-safe: a stuck run bills for every minute you give it."
              />
              <input
                type="number"
                min={5}
                max={240}
                value={runTimeout}
                onChange={(e) => setRunTimeout(e.target.value)}
                placeholder={inheritPlaceholder(adminSettings?.dev_run_timeout_minutes_default)}
              />
            </label>
            {harnessPickable && (
              <label className="field">
                <LabelHint
                  label="Dev harness"
                  hint="Which agent driver this project's builds run on. Inherit = the instance default. Changing it changes the build's cost and behavior, and runs on different harnesses are never compared with each other."
                />
                <select value={devHarness} onChange={(e) => setDevHarness(e.target.value)}>
                  <option value="">inherit ({defaultHarnessLabel})</option>
                  {harnessOptions.map((h) => (
                    <option key={h.id} value={h.id}>
                      {h.label}
                    </option>
                  ))}
                </select>
                {selectedHarness?.description && (
                  <span className="tiny faint">{selectedHarness.description}</span>
                )}
              </label>
            )}
            <label className="field">
              <LabelHint
                label="Parallel builds (per project)"
                hint="Concurrency entitlement: 1 = one build at a time (today's behavior); the instance ceiling and any future plan/license cap still apply."
              />
              <input
                type="number"
                min={1}
                max={16}
                value={parallelLimit}
                onChange={(e) => setParallelLimit(e.target.value)}
                placeholder="instance default (1)"
              />
            </label>
            <label className="field">
              <LabelHint
                label="Dev pod CPU request"
                hint="Requested CPUs for this project's dev-run pods, e.g. 0.5 or 2. Empty = the instance default. On Kubernetes this is the scheduler reservation - a request above the instance limit raises the run's limit to match; compose dev can't honor a CPU request."
              />
              <input
                type="text"
                value={devCpu}
                onChange={(e) => setDevCpu(e.target.value)}
                placeholder="instance default"
              />
            </label>
            <label className="field">
              <LabelHint
                label="Dev pod memory request"
                hint="Requested memory for this project's dev-run pods, e.g. 512m or 4g. Empty = the instance default. On Kubernetes this is the scheduler reservation (a request above the instance limit raises the limit); compose applies it as the container's memory reservation."
              />
              <input
                type="text"
                value={devMem}
                onChange={(e) => setDevMem(e.target.value)}
                placeholder="instance default"
              />
            </label>
            <button className="btn btn-sm" onClick={saveSettings} disabled={busy === "settings"}>
              {busy === "settings" ? <Spinner /> : "Save settings"}
            </button>
          </div>
        </div>
      </div>

      {spend && (
        <div className="mt">
          <label>Customer spend on this project</label>
          <div className="row gap-sm" style={{ alignItems: "baseline" }}>
            <span className="grad-text" style={{ fontSize: "1.2rem", fontWeight: 600 }}>
              {formatCredits(spend.total_spent)} credits
            </span>
            <span className="tiny faint">
              {[
                // Charges are negative in the ledger; show them as positive
                // spend and returned credits (refunds) with a leading "+".
                ...Object.entries(spend.by_kind).map(
                  ([kind, amount]) =>
                    `${SPEND_LABELS[kind] ?? kind} ${
                      amount < 0 ? formatCredits(-amount) : `+${formatCredits(amount)}`
                    }`,
                ),
                ...(spend.quotes_paid > 0
                  ? [`quotes paid ${spend.quotes_paid.toLocaleString()} EUR`]
                  : []),
              ].join(" · ") || "no charges yet"}
            </span>
          </div>
        </div>
      )}

      <div className="row row-wrap gap-sm mt">
        {!isDirect && (
          <button className="btn btn-sm" onClick={() => setModelOpen(true)}>
            Model config…
          </button>
        )}
        {!isDirect && (
          <button className="btn btn-sm" onClick={() => setKbOpen(true)}>
            Knowledge bases…
            <span className="tiny faint">
              {" "}
              ({(project.kb_ids ?? []).length} selected)
            </span>
          </button>
        )}
        {!isDirect && (
          <button className="btn btn-sm" onClick={() => setToolsOpen(true)}>
            Tools…
          </button>
        )}
        {!isDirect && (
          <button className="btn btn-sm" onClick={refundReview} disabled={busy === "refund"}>
            {busy === "refund" ? <Spinner /> : "Refund review fee"}
          </button>
        )}
      </div>

      {isDirect && (
        <div className="mt">
          <label>Send a custom quote</label>
          <div className="row gap-sm">
            <input
              type="number"
              min={0}
              step="any"
              placeholder={`Amount (${project.tier ? "" : ""}credits/EUR)`}
              value={quoteAmount}
              onChange={(e) => setQuoteAmount(e.target.value)}
            />
            <button className="btn btn-sm btn-primary" onClick={sendProjectQuote} disabled={busy === "quote"}>
              {busy === "quote" ? <Spinner /> : "Send quote"}
            </button>
          </div>
          <p className="tiny faint mt">
            Creates a Stripe payment link for the whole engagement and records it under the
            project's quotes.
          </p>
        </div>
      )}

      {modelOpen && (
        <ModelConfigModal projectId={project.id} onClose={() => setModelOpen(false)} />
      )}
      {kbOpen && (
        <KbSelectModal project={project} onUpdated={onUpdated} onClose={() => setKbOpen(false)} />
      )}
      {toolsOpen && (
        <ProjectToolsModal projectId={project.id} onClose={() => setToolsOpen(false)} />
      )}
    </CollapsibleCard>
  );
}

function KbSelectModal({
  project,
  onUpdated,
  onClose,
}: {
  project: Project;
  onUpdated: (p: Project) => void;
  onClose: () => void;
}) {
  const toast = useToast();
  const [kbs, setKbs] = useState<KnowledgeBase[] | null>(null);
  // Off = NO knowledge bases ([]). A legacy null reads as none everywhere the
  // backend scopes retrieval, so it renders as none here too.
  const [custom, setCustom] = useState((project.kb_ids ?? []).length > 0);
  const [selected, setSelected] = useState<Set<string>>(new Set(project.kb_ids ?? []));
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    kbApi
      .list()
      .then(setKbs)
      .catch((err) => {
        setKbs([]);
        toast.push(err instanceof Error ? err.message : "Could not load knowledge bases", "err");
      });
  }, [toast]);

  function toggle(id: string) {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  async function save() {
    setBusy(true);
    try {
      const updated = await adminApi.patchProject(project.id, {
        kb_ids: custom ? Array.from(selected) : [],
      });
      toast.push(
        custom ? "Knowledge-base selection saved" : "Knowledge bases disabled for this project",
        "ok",
      );
      onUpdated(updated);
      onClose();
    } catch (err) {
      toast.push(err instanceof Error ? err.message : "Failed", "err");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal title="Per-project knowledge bases" onClose={onClose}>
      <p className="muted small">
        Choose which knowledge bases feed this project's dev runs (MCP servers and RAG
        retrieval). Knowledge bases are opt-in per project: off means this project
        uses none. The selection only narrows the global set: a KB disabled under{" "}
        <Link to="/admin/knowledge-bases">Knowledge bases</Link> stays off here too.
      </p>
      <label className="checkbox-row">
        <Toggle checked={custom} onChange={setCustom} />
        Select specific knowledge bases (off = no knowledge bases)
      </label>
      {custom && (
        <div className="stack mt" style={{ gap: "0.35rem" }}>
          {kbs === null && <Spinner />}
          {(kbs ?? []).map((kb) => (
            <label key={kb.id} className="checkbox-row">
              <input
                type="checkbox"
                checked={selected.has(kb.id)}
                onChange={() => toggle(kb.id)}
              />
              {kb.name}
              <span className="tiny faint">
                {" "}
                ({kb.kind}
                {!kb.enabled ? " · disabled globally" : ""}
                {kb.kind === "git" && !kb.verified ? " · unverified" : ""})
              </span>
            </label>
          ))}
          {kbs !== null && kbs.length === 0 && (
            <p className="tiny faint">No knowledge bases configured.</p>
          )}
        </div>
      )}
      <div className="wizard-actions" style={{ marginTop: 0 }}>
        <button className="btn" onClick={onClose}>
          Cancel
        </button>
        <button className="btn btn-primary" onClick={save} disabled={busy || kbs === null}>
          {busy ? <Spinner /> : "Save"}
        </button>
      </div>
    </Modal>
  );
}

function ModelConfigModal({ projectId, onClose }: { projectId: string; onClose: () => void }) {
  const toast = useToast();
  const [endpoints, setEndpoints] = useState<ModelEndpoint[] | null>(null);
  const [endpointId, setEndpointId] = useState<string>("");
  const [busy, setBusy] = useState(false);

  // Load the saved endpoints and this project's current selection on open.
  useEffect(() => {
    Promise.all([modelEndpointApi.list(), adminApi.getModelConfig(projectId)])
      .then(([eps, cfg]) => {
        setEndpoints(eps);
        setEndpointId(cfg.endpoint_id ?? "");
      })
      .catch((err) => {
        setEndpoints([]);
        toast.push(err instanceof Error ? err.message : "Could not load model config", "err");
      });
  }, [projectId, toast]);

  const selected = (endpoints ?? []).find((e) => e.id === endpointId) ?? null;

  async function save() {
    setBusy(true);
    try {
      await adminApi.putModelConfig(projectId, { endpoint_id: endpointId || null });
      toast.push(
        endpointId ? "Model config saved" : "Model config cleared - following this kind's default",
        "ok",
      );
      onClose();
    } catch (err) {
      toast.push(err instanceof Error ? err.message : "Failed", "err");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal title="Per-project model config" onClose={onClose}>
      <p className="muted small">
        Route this project's builds to a saved endpoint (the model and credentials come with it).
        Leave it unset and the project follows the default for its kind (
        <Link to="/admin/settings">Settings</Link>), else the instance default. Manage endpoints
        under <Link to="/admin/model-endpoints">Model configuration</Link>.
      </p>
      <div className="field">
        <label>Endpoint</label>
        <select value={endpointId} onChange={(e) => setEndpointId(e.target.value)}>
          <option value="">Default for this project kind (no override)</option>
          {(endpoints ?? []).map((ep) => (
            <option key={ep.id} value={ep.id}>
              {ep.label} ({ep.provider}
              {ep.model_name ? ` · ${ep.model_name}` : ""})
            </option>
          ))}
        </select>
        {selected && (
          <p className="tiny faint mt-xs">
            Builds will use <code>{selected.model_name}</code> via {selected.base_url}.
          </p>
        )}
      </div>
      <div className="wizard-actions" style={{ marginTop: 0 }}>
        <button className="btn" onClick={onClose}>
          Cancel
        </button>
        <button
          className="btn btn-primary"
          onClick={save}
          disabled={busy || endpoints === null}
        >
          {busy ? <Spinner /> : "Save"}
        </button>
      </div>
    </Modal>
  );
}

function ProjectToolsModal({ projectId, onClose }: { projectId: string; onClose: () => void }) {
  const toast = useToast();
  const [tools, setTools] = useState<ProjectTool[] | null>(null);
  const [urls, setUrls] = useState<Record<string, string>>({});
  const [keys, setKeys] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState<string | null>(null);

  const load = () =>
    toolsApi
      .projectList(projectId)
      .then((t) => {
        setTools(t);
        setUrls(Object.fromEntries(t.map((x) => [x.id, x.override_url ?? ""])));
      })
      .catch((err) =>
        toast.push(err instanceof Error ? err.message : "Could not load tools", "err"));

  useEffect(() => {
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [projectId]);

  async function save(t: ProjectTool, enabled: boolean | null) {
    setBusy(t.id);
    try {
      await toolsApi.projectPut(projectId, t.id, {
        enabled,
        url: urls[t.id] ?? "",
        ...((keys[t.id] ?? "").trim() ? { api_key: keys[t.id].trim() } : {}),
      });
      setKeys((k) => ({ ...k, [t.id]: "" }));
      toast.push(`${t.name} saved for this project.`, "ok");
      await load();
    } catch (err) {
      toast.push(err instanceof Error ? err.message : "Save failed", "err");
    } finally {
      setBusy(null);
    }
  }

  return (
    <Modal title="Per-project tools" onClose={onClose}>
      <p className="muted small">
        Override the global <Link to="/admin/tools">Tools</Link> per project: enable state,
        endpoint URL (e.g. this customer's own GitLab instance:{" "}
        <code>https://&lt;host&gt;/api/v4/mcp</code>) and key. Builds resolve the key as
        project override → the project's Memory secret (GITHUB_TOKEN/GITLAB_TOKEN) → the
        global tool key.
      </p>
      {tools === null && <Spinner />}
      {(tools ?? []).map((t) => (
        <div key={t.id} className="mt" style={{ borderTop: "1px solid var(--border)", paddingTop: "0.6rem" }}>
          <div className="between">
            <strong>{t.name}</strong>
            <span className="tiny faint">
              globally {t.enabled ? "enabled" : "disabled"} · effective:{" "}
              {t.effective_enabled ? "on" : "off"}
            </span>
          </div>
          <div className="row gap-sm mt">
            <select
              value={t.override_enabled === null ? "inherit" : t.override_enabled ? "on" : "off"}
              onChange={(e) => {
                const v = e.target.value;
                void save(t, v === "inherit" ? null : v === "on");
              }}
              disabled={busy === t.id}
            >
              <option value="inherit">Inherit global</option>
              <option value="on">Enabled for this project</option>
              <option value="off">Disabled for this project</option>
            </select>
          </div>
          <div className="row gap-sm mt">
            <input
              value={urls[t.id] ?? ""}
              onChange={(e) => setUrls((u) => ({ ...u, [t.id]: e.target.value }))}
              placeholder={`URL override (default: ${t.url})`}
              style={{ flex: 1 }}
            />
          </div>
          <div className="row gap-sm mt">
            <input
              type="password"
              value={keys[t.id] ?? ""}
              onChange={(e) => setKeys((k) => ({ ...k, [t.id]: e.target.value }))}
              placeholder={t.override_has_api_key ? "•••••••• (override set)" : "Key override (optional)"}
              autoComplete="new-password"
              style={{ flex: 1 }}
            />
            <button
              className="btn btn-sm"
              disabled={busy === t.id}
              onClick={() => save(t, t.override_enabled)}
            >
              {busy === t.id ? <Spinner /> : "Save"}
            </button>
          </div>
        </div>
      ))}
    </Modal>
  );
}
