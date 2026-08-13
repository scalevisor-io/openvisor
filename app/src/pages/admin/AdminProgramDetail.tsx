import { useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import Markdown from "../../components/Markdown";
import { RunLog, RunStateBadge, runActive, runDuration } from "../../components/programs";
import {
  Alert, Badge, Loading, Spinner, Toggle, formatCreditsExact, relTime, usePolling,
} from "../../components/ui";
import { adminProgramsApi, modelEndpointApi } from "../../lib/endpoints";
import { useToast } from "../../lib/toast";
import type { AdminProgram, ModelEndpoint, ProgramRun } from "../../types";

// Admin program detail (§28): everything but the repo path is editable, "Check
// Program run" dry-runs the repo (passes iff docker build + deploy succeed),
// and the runs table covers checks + customer instance runs.
export default function AdminProgramDetail() {
  const { id = "" } = useParams();
  const navigate = useNavigate();
  const toast = useToast();

  const [program, setProgram] = useState<AdminProgram | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  // form state (hydrated on each load - the admin page is single-writer)
  const [title, setTitle] = useState("");
  const [shortDesc, setShortDesc] = useState("");
  const [branch, setBranch] = useState("main");
  const [published, setPublished] = useState(false);
  const [schedulable, setSchedulable] = useState(false);
  const [endpointId, setEndpointId] = useState("");
  const [endpoints, setEndpoints] = useState<ModelEndpoint[]>([]);
  const [markup, setMarkup] = useState("");
  const [timeout_, setTimeout_] = useState("15");
  const [cpuReq, setCpuReq] = useState("0.5");
  const [cpuLim, setCpuLim] = useState("1");
  const [memReq, setMemReq] = useState("256m");
  const [memLim, setMemLim] = useState("1g");

  const [runs, setRuns] = useState<ProgramRun[]>([]);
  const [selectedRun, setSelectedRun] = useState<ProgramRun | null>(null);

  function hydrate(p: AdminProgram) {
    setProgram(p);
    setTitle(p.title);
    setShortDesc(p.short_description);
    setBranch(p.default_branch);
    setPublished(p.is_published);
    setSchedulable(p.schedulable);
    setEndpointId(p.model_endpoint_id ?? "");
    setMarkup(p.credit_markup == null ? "" : String(p.credit_markup));
    setTimeout_(String(p.timeout_minutes));
    setCpuReq(p.cpu_request);
    setCpuLim(p.cpu_limit);
    setMemReq(p.mem_request);
    setMemLim(p.mem_limit);
  }

  function load() {
    adminProgramsApi
      .get(id)
      .then(hydrate)
      .catch((err) => setError(err instanceof Error ? err.message : "Failed to load."));
    adminProgramsApi.runs(id).then(setRuns).catch(() => {});
  }
  useEffect(load, [id]);
  useEffect(() => {
    modelEndpointApi.list().then(setEndpoints).catch(() => {});
  }, []);
  const checkActive = runs.some((r) => r.kind === "check" && runActive(r));
  usePolling(load, 4000, runs.some(runActive));

  async function saveSettings() {
    setBusy(true);
    try {
      const p = await adminProgramsApi.update(id, {
        title: title.trim(),
        short_description: shortDesc.trim(),
        default_branch: branch.trim(),
        is_published: published,
        schedulable,
        model_endpoint_id: endpointId || null,
        ...(markup.trim() ? { credit_markup: Number(markup) } : {}),
        timeout_minutes: Number(timeout_) || 15,
        cpu_request: cpuReq.trim(),
        cpu_limit: cpuLim.trim(),
        mem_request: memReq.trim(),
        mem_limit: memLim.trim(),
      });
      hydrate(p);
      toast.push("Program saved", "ok");
    } catch (err) {
      toast.push(err instanceof Error ? err.message : "Save failed", "err");
    } finally {
      setBusy(false);
    }
  }

  async function refreshFromRepo() {
    setBusy(true);
    try {
      hydrate(await adminProgramsApi.refresh(id));
      toast.push("README and input template refreshed", "ok");
    } catch (err) {
      toast.push(err instanceof Error ? err.message : "Refresh failed", "err");
    } finally {
      setBusy(false);
    }
  }

  async function startCheck() {
    setBusy(true);
    try {
      const run = await adminProgramsApi.check(id);
      setSelectedRun(run);
      toast.push("Check run started", "ok");
      load();
    } catch (err) {
      toast.push(err instanceof Error ? err.message : "Check failed to start", "err");
    } finally {
      setBusy(false);
    }
  }

  async function removeProgram() {
    if (!window.confirm("Delete this program?")) return;
    try {
      await adminProgramsApi.remove(id);
      toast.push("Program deleted", "ok");
      navigate("/admin/programs");
    } catch (err) {
      toast.push(err instanceof Error ? err.message : "Delete failed", "err");
    }
  }

  if (error) return <Alert kind="error">{error}</Alert>;
  if (!program) return <Loading />;

  return (
    <div>
      <div className="between mb">
        <div className="row gap-sm">
          <Link to="/admin/programs" className="muted small">
            Programs
          </Link>
          <span className="faint">/</span>
          <h1 style={{ margin: 0 }}>{program.title}</h1>
          <Badge label={program.is_published ? "published" : "hidden"}
                 kind={program.is_published ? "finished" : "draft"} />
        </div>
        <div className="row gap-sm">
          <button className="btn btn-sm" disabled={busy} onClick={refreshFromRepo}>
            Refresh from repo
          </button>
          <button className="btn btn-primary btn-sm" disabled={busy || checkActive} onClick={startCheck}>
            {checkActive ? "Checking…" : "Check Program run"}
          </button>
          <button
            className="btn btn-sm btn-danger"
            disabled={(program.instances_count ?? 0) > 0}
            title={(program.instances_count ?? 0) > 0 ? "Customer instances exist - unpublish instead" : ""}
            onClick={removeProgram}
          >
            Delete
          </button>
        </div>
      </div>

      <div className="card mb">
        <div className="between mb">
          <h3 style={{ margin: 0 }}>Settings</h3>
          <button className="btn btn-primary btn-sm" disabled={busy} onClick={saveSettings}>
            {busy ? <Spinner /> : "Save"}
          </button>
        </div>
        <div className="grid-2">
          <div className="field">
            <label>Title</label>
            <input type="text" value={title} onChange={(e) => setTitle(e.target.value)} />
          </div>
          <div className="field">
            <label>Short description</label>
            <input type="text" value={shortDesc} onChange={(e) => setShortDesc(e.target.value)} />
          </div>
        </div>
        <div className="grid-2">
          <div className="field">
            <label>Repository (immutable)</label>
            <div className="row gap-sm" style={{ alignItems: "center" }}>
              <span className="mono tiny">{program.gitlab_repo_path}</span>
              {program.gitlab_web_url && (
                <a className="btn btn-sm btn-ghost" href={program.gitlab_web_url}
                   target="_blank" rel="noreferrer">
                  Open ↗
                </a>
              )}
            </div>
          </div>
          <div className="field">
            <label>Branch</label>
            <input type="text" value={branch} onChange={(e) => setBranch(e.target.value)} />
          </div>
        </div>
        <div className="row gap-sm mb" style={{ alignItems: "center", gap: "1.5rem" }}>
          <label className="row gap-sm" style={{ alignItems: "center" }}>
            <Toggle checked={published} onChange={setPublished} />
            <span className="small">Published (visible in the customer catalog)</span>
          </label>
          <label className="row gap-sm" style={{ alignItems: "center" }}>
            <Toggle checked={schedulable} onChange={setSchedulable} />
            <span className="small">Schedulable by customers</span>
          </label>
        </div>
        <div className="grid-2">
          <div className="field">
            <label>Credit markup (empty = platform default ×{program.credit_markup_effective})</label>
            <input type="number" step="0.05" min="0.05" value={markup}
                   placeholder={String(program.credit_markup_effective)}
                   onChange={(e) => setMarkup(e.target.value)} />
          </div>
          <div className="field">
            <label>Run timeout (minutes)</label>
            <input type="number" min="1" max="240" value={timeout_}
                   onChange={(e) => setTimeout_(e.target.value)} />
          </div>
        </div>
        <div className="grid-2">
          <div className="field">
            <label>vCPU request / limit (request applies on Kubernetes)</label>
            <div className="row gap-sm">
              <input type="text" value={cpuReq} onChange={(e) => setCpuReq(e.target.value)} />
              <input type="text" value={cpuLim} onChange={(e) => setCpuLim(e.target.value)} />
            </div>
          </div>
          <div className="field">
            <label>RAM request / limit (e.g. 256m / 1g)</label>
            <div className="row gap-sm">
              <input type="text" value={memReq} onChange={(e) => setMemReq(e.target.value)} />
              <input type="text" value={memLim} onChange={(e) => setMemLim(e.target.value)} />
            </div>
          </div>
        </div>
        <div className="field">
          <label>Model</label>
          <select value={endpointId} onChange={(e) => setEndpointId(e.target.value)}>
            <option value="">Global default (no override)</option>
            {endpoints.map((ep) => (
              <option key={ep.id} value={ep.id}>
                {ep.label} ({ep.provider}
                {ep.model_name ? ` · ${ep.model_name}` : ""})
              </option>
            ))}
          </select>
          <p className="tiny faint mt-xs">
            Runs use the selected endpoint's base URL, key and model (managed under{" "}
            <Link to="/admin/model-endpoints">Model configuration</Link> - rotating its key
            applies here too).
          </p>
          {program.has_legacy_model_config && (
            <Alert kind="info">
              This program still runs on an inline model config from before saved endpoints.
              Saving a selection here (including "Global default") replaces it.
            </Alert>
          )}
        </div>
      </div>

      <div className="card mb">
        <div className="between mb">
          <h3 style={{ margin: 0 }}>Runs</h3>
          <span className="row gap-sm">
            {program.last_check_state && (
              <span className="row gap-sm">
                <span className="tiny muted">last check:</span>
                <Badge label={program.last_check_state}
                       kind={program.last_check_state === "passed" ? "finished" : "canceled"} />
                <span className="faint tiny">{relTime(program.last_check_at)}</span>
              </span>
            )}
          </span>
        </div>
        {runs.length === 0 ? (
          <p className="muted small">No runs yet - hit "Check Program run".</p>
        ) : (
          <div className="table-wrap mb">
            <table className="data">
              <thead>
                <tr>
                  <th>State</th>
                  <th>Kind</th>
                  <th>Started</th>
                  <th>Duration</th>
                  <th>Credits</th>
                  <th>Error</th>
                </tr>
              </thead>
              <tbody>
                {runs.map((r) => (
                  <tr key={r.id} style={{ cursor: "pointer" }}
                      className={selectedRun?.id === r.id ? "active" : undefined}
                      onClick={() => setSelectedRun(r)}>
                    <td><RunStateBadge state={r.state} /></td>
                    <td className="muted">{r.kind}</td>
                    <td className="faint tiny">{relTime(r.started_at || r.created_at)}</td>
                    <td className="muted tiny">{runDuration(r)}</td>
                    <td className="muted tiny">{formatCreditsExact(r.cost_credits)}</td>
                    <td className="tiny danger-text">{r.error || ""}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        {selectedRun && (
          <div>
            <div className="between mb">
              <span className="row gap-sm">
                <RunStateBadge state={selectedRun.state} />
                <span className="muted tiny">{selectedRun.kind} run log</span>
              </span>
              <button className="btn btn-sm btn-ghost" onClick={() => setSelectedRun(null)}>
                Close
              </button>
            </div>
            <RunLog
              key={selectedRun.id}
              active={runActive(runs.find((r) => r.id === selectedRun.id) ?? selectedRun)}
              fetchChunk={(offset) => adminProgramsApi.runLog(id, selectedRun.id, offset)}
            />
          </div>
        )}
      </div>

      <div className="grid-2">
        <div className="card">
          <h3 style={{ marginTop: 0 }}>README.md (long description)</h3>
          <div style={{ maxHeight: 320, overflowY: "auto" }}>
            <Markdown>{program.readme_md || "No README fetched."}</Markdown>
          </div>
        </div>
        <div className="card">
          <h3 style={{ marginTop: 0 }}>Input template</h3>
          {program.input_template.length === 0 ? (
            <p className="muted small">No input.template.yml in the repo.</p>
          ) : (
            <div className="stack" style={{ gap: "0.4rem" }}>
              {program.input_template.map((f) => (
                <div key={f.name}>
                  <span className="row gap-sm">
                    <span className="mono tiny">{f.name}</span>
                    <span className="badge">{f.type}</span>
                    {f.required && <span className="badge">required</span>}
                    {f.secret && <span className="badge">secret</span>}
                  </span>
                  {f.description && <div className="tiny muted">{f.description}</div>}
                </div>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
