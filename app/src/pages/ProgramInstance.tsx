import { useEffect, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import Markdown from "../components/Markdown";
import {
  InputsForm, RunLog, RunStateBadge, formatBytes, runActive, runDuration,
} from "../components/programs";
import {
  Alert, CopyField, Loading, Modal, Spinner, Toggle, formatCreditsExact, relTime, usePolling,
} from "../components/ui";
import { ApiError } from "../lib/api";
import { useAuth } from "../lib/auth";
import { programsApi } from "../lib/endpoints";
import { useToast } from "../lib/toast";
import type {
  ProgramInstance as Instance, ProgramModelOption, ProgramRun, ProgramRunFull,
} from "../types";

const CRON_PRESETS: { label: string; cron: string }[] = [
  { label: "Every 15 minutes", cron: "*/15 * * * *" },
  { label: "Every hour", cron: "0 * * * *" },
  { label: "Every day at 07:00", cron: "0 7 * * *" },
  { label: "Every Monday at 07:00", cron: "0 7 * * 1" },
  { label: "Monthly (1st, 07:00)", cron: "0 7 1 * *" },
];

// One program instance (§28): overview tab (inputs form, webhook, schedule,
// SSH key, run/delete) + runs tab (history, live/past logs, output viewer).
export default function ProgramInstance() {
  const { id = "", tab: tabParam, sub: runId } = useParams();
  const tab = tabParam === "runs" ? "runs" : "overview";
  const navigate = useNavigate();
  const toast = useToast();
  const { settings } = useAuth();
  const brandSlug = settings?.brand_slug ?? "openvisor";

  const [inst, setInst] = useState<Instance | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [showReadme, setShowReadme] = useState(false);
  const [busy, setBusy] = useState(false);

  // Form state hydrated once from the API (later loads must not clobber edits).
  const hydrated = useRef(false);
  const [values, setValues] = useState<Record<string, string>>({});
  const [inputErrors, setInputErrors] = useState<Record<string, string>>({});
  const [label, setLabel] = useState("");
  const [webhook, setWebhook] = useState("");
  // §28 per-instance model: "" = run on the program's default model.
  const [modelEndpoint, setModelEndpoint] = useState("");
  const [modelOptions, setModelOptions] = useState<ProgramModelOption[]>([]);
  const [schedOn, setSchedOn] = useState(false);
  const [cron, setCron] = useState("");
  const [hookOn, setHookOn] = useState(false);
  const [hookActions, setHookActions] = useState("");
  const [hookLabels, setHookLabels] = useState("");
  const [hookAssignees, setHookAssignees] = useState("");
  const [hookAuthors, setHookAuthors] = useState("");

  function hydrate(data: Instance) {
    setInst(data);
    if (!hydrated.current) {
      hydrated.current = true;
      const vals: Record<string, string> = {};
      for (const [k, v] of Object.entries(data.inputs || {})) vals[k] = String(v);
      setValues(vals);
      setLabel(data.label);
      setWebhook(data.webhook_url);
      setModelEndpoint(data.model_endpoint_id ?? "");
      setSchedOn(data.schedule_enabled);
      setCron(data.schedule_cron);
      setHookOn(data.hook_enabled);
      setHookActions((data.hook_filters?.actions ?? []).join(", "));
      setHookLabels((data.hook_filters?.labels ?? []).join(", "));
      setHookAssignees((data.hook_filters?.assignees ?? []).join(", "));
      setHookAuthors((data.hook_filters?.authors ?? []).join(", "));
    }
  }

  const csv = (v: string) => v.split(",").map((x) => x.trim()).filter(Boolean);

  function load() {
    programsApi
      .instance(id)
      .then(hydrate)
      .catch((err) => setError(err instanceof Error ? err.message : "Failed to load."));
  }
  useEffect(() => {
    hydrated.current = false;
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id]);
  // The models this instance may be pinned to; an empty list just leaves the
  // picker on "Program default".
  useEffect(() => {
    programsApi.modelEndpoints().then(setModelOptions).catch(() => setModelOptions([]));
  }, []);
  // Keep the header state fresh while a run is in flight.
  usePolling(load, 4000, runActive(inst?.latest_run));

  async function save(body: Parameters<typeof programsApi.updateInstance>[1], okMsg: string) {
    setBusy(true);
    setInputErrors({});
    try {
      const data = await programsApi.updateInstance(id, body);
      hydrated.current = false;
      hydrate(data);
      toast.push(okMsg, "ok");
      return true;
    } catch (err) {
      if (err instanceof ApiError && err.data && typeof err.data === "object") {
        const errors = (err.data as { errors?: Record<string, string> }).errors;
        if (errors) {
          setInputErrors(errors);
          toast.push("Some inputs are invalid", "err");
          return false;
        }
      }
      toast.push(err instanceof Error ? err.message : "Save failed", "err");
      return false;
    } finally {
      setBusy(false);
    }
  }

  async function triggerRun() {
    setBusy(true);
    try {
      const run = await programsApi.run(id);
      toast.push("Run started", "ok");
      navigate(`/programs/instances/${id}/runs/${run.id}`);
    } catch (err) {
      if (err instanceof ApiError && err.data && typeof err.data === "object") {
        const errors = (err.data as { errors?: Record<string, string> }).errors;
        if (errors) {
          setInputErrors(errors);
          toast.push("Fix the inputs before running", "err");
          return;
        }
      }
      toast.push(err instanceof Error ? err.message : "Run failed to start", "err");
    } finally {
      setBusy(false);
    }
  }

  async function removeInstance() {
    if (!window.confirm("Delete this program instance and its run history?")) return;
    try {
      await programsApi.deleteInstance(id);
      toast.push("Instance deleted", "ok");
      navigate("/programs");
    } catch (err) {
      toast.push(err instanceof Error ? err.message : "Delete failed", "err");
    }
  }

  if (error) return <Alert kind="error">{error}</Alert>;
  if (!inst) return <Loading />;

  const active = runActive(inst.latest_run);
  const program = inst.program;

  return (
    <div>
      <div className="between mb">
        <div>
          <div className="row gap-sm">
            <Link to="/programs" className="muted small">
              Programs
            </Link>
            <span className="faint">/</span>
            <h1 style={{ margin: 0 }}>{program.title}</h1>
            {inst.label && <span className="badge">{inst.label}</span>}
          </div>
          <button
            type="button"
            className="btn btn-sm btn-ghost"
            style={{ paddingLeft: 0 }}
            onClick={() => setShowReadme(true)}
          >
            About this program
          </button>
        </div>
        <div className="row gap-sm">
          {inst.latest_run && <RunStateBadge state={inst.latest_run.state} />}
          <button
            className="btn btn-primary btn-sm"
            disabled={busy || active}
            title={active ? "A run is already in progress" : "Run the program now"}
            onClick={triggerRun}
          >
            {active ? "Running…" : "Run now"}
          </button>
          <button className="btn btn-sm btn-danger" disabled={active} onClick={removeInstance}>
            Delete
          </button>
        </div>
      </div>

      <div className="tabs mb">
        <Link className={`tab${tab === "overview" ? " active" : ""}`} to={`/programs/instances/${id}`}>
          Overview
        </Link>
        <Link className={`tab${tab === "runs" ? " active" : ""}`} to={`/programs/instances/${id}/runs`}>
          Runs
        </Link>
      </div>

      {tab === "overview" ? (
        <div className="stack" style={{ gap: "1rem" }}>
          <div className="card">
            <div className="between mb">
              <h3 style={{ margin: 0 }}>Inputs</h3>
              <button
                className="btn btn-primary btn-sm"
                disabled={busy}
                onClick={() => save({ inputs: values }, "Inputs saved")}
              >
                {busy ? <Spinner /> : "Save inputs"}
              </button>
            </div>
            <InputsForm
              fields={program.input_template}
              values={values}
              errors={inputErrors}
              onChange={(name, v) => setValues((prev) => ({ ...prev, [name]: v }))}
            />
          </div>

          <div className="card">
            <div className="between mb">
              <h3 style={{ margin: 0 }}>Instance settings</h3>
              <button
                className="btn btn-sm"
                disabled={busy}
                onClick={() =>
                  save(
                    {
                      label: label.trim(),
                      webhook_url: webhook.trim(),
                      model_endpoint_id: modelEndpoint,
                    },
                    "Settings saved",
                  )
                }
              >
                Save settings
              </button>
            </div>
            <div className="grid-2">
              <div className="field">
                <label>Label</label>
                <input type="text" value={label} onChange={(e) => setLabel(e.target.value)} />
              </div>
              <div className="field">
                <label>Webhook URL - receives the output (or the error) after every run</label>
                <input
                  type="url"
                  value={webhook}
                  placeholder={`https://example.com/hooks/${brandSlug}`}
                  onChange={(e) => setWebhook(e.target.value)}
                />
              </div>
              <div className="field">
                <label>Model - which model this instance runs on</label>
                <select value={modelEndpoint} onChange={(e) => setModelEndpoint(e.target.value)}>
                  <option value="">Program default</option>
                  {modelOptions.map((m) => (
                    <option key={m.id} value={m.id}>
                      {m.label} - {m.model_name}
                    </option>
                  ))}
                </select>
                <div className="hint">Runs are billed per token at the selected model's rate.</div>
              </div>
            </div>
          </div>

          {program.schedulable && (
            <div className="card">
              <div className="between mb">
                <h3 style={{ margin: 0 }}>Schedule</h3>
                <button
                  className="btn btn-sm"
                  disabled={busy}
                  onClick={() => save({ schedule_enabled: schedOn, schedule_cron: cron.trim() }, "Schedule saved")}
                >
                  Save schedule
                </button>
              </div>
              <div className="row gap-sm mb" style={{ alignItems: "center" }}>
                <Toggle checked={schedOn} onChange={setSchedOn} />
                <span className="small">{schedOn ? "Enabled" : "Disabled"}</span>
                {inst.schedule_enabled && inst.next_run_at && (
                  <span className="tiny muted">next run: {relTime(inst.next_run_at)}</span>
                )}
              </div>
              <div className="grid-2">
                <div className="field">
                  <label>Preset</label>
                  <select value="" onChange={(e) => e.target.value && setCron(e.target.value)}>
                    <option value="">Pick a preset…</option>
                    {CRON_PRESETS.map((p) => (
                      <option key={p.cron} value={p.cron}>
                        {p.label}
                      </option>
                    ))}
                  </select>
                </div>
                <div className="field">
                  <label>Cron expression (UTC, 15 min minimum interval)</label>
                  <input
                    type="text"
                    className="mono"
                    value={cron}
                    placeholder="*/30 * * * *"
                    onChange={(e) => setCron(e.target.value)}
                  />
                </div>
              </div>
            </div>
          )}

          <div className="card">
            <div className="between mb">
              <h3 style={{ margin: 0 }}>Trigger hook</h3>
              <button
                className="btn btn-sm"
                disabled={busy}
                onClick={() =>
                  save(
                    {
                      hook_enabled: hookOn,
                      hook_filters: {
                        actions: csv(hookActions),
                        labels: csv(hookLabels),
                        assignees: csv(hookAssignees),
                        authors: csv(hookAuthors),
                      },
                    },
                    "Trigger hook saved",
                  )
                }
              >
                Save hook
              </button>
            </div>
            <p className="muted small">
              A signed GitHub/GitLab webhook can start a run in reaction to a repository issue
              event. Paste the URL and secret into your repo's webhook settings (GitHub: content
              type JSON, secret = this secret; GitLab: secret token = this secret). Filters are
              allowlists - leave one empty for no constraint on that dimension.
            </p>
            <div className="row gap-sm mb" style={{ alignItems: "center" }}>
              <Toggle checked={hookOn} onChange={setHookOn} />
              <span className="small">{hookOn ? "Enabled" : "Disabled"}</span>
            </div>
            <div className="field">
              <label>Hook URL</label>
              <CopyField value={inst.hook_url} block />
            </div>
            {inst.hook_secret && (
              <div className="field">
                <label>Secret</label>
                <CopyField value={inst.hook_secret} block masked />
                <button
                  className="btn btn-sm mt"
                  disabled={busy}
                  onClick={async () => {
                    try {
                      await programsApi.rotateHookSecret(id);
                      hydrated.current = false;
                      load();
                      toast.push("Hook secret rotated - update your repo webhook", "ok");
                    } catch (err) {
                      toast.push(err instanceof Error ? err.message : "Rotation failed", "err");
                    }
                  }}
                >
                  Rotate secret
                </button>
              </div>
            )}
            <div className="grid-2">
              <div className="field">
                <label>Actions (e.g. opened, labeled, assigned)</label>
                <input type="text" value={hookActions} placeholder="any"
                       onChange={(e) => setHookActions(e.target.value)} />
              </div>
              <div className="field">
                <label>Labels</label>
                <input type="text" value={hookLabels} placeholder="any"
                       onChange={(e) => setHookLabels(e.target.value)} />
              </div>
              <div className="field">
                <label>Assignees</label>
                <input type="text" value={hookAssignees} placeholder="any"
                       onChange={(e) => setHookAssignees(e.target.value)} />
              </div>
              <div className="field">
                <label>Authors</label>
                <input type="text" value={hookAuthors} placeholder="any"
                       onChange={(e) => setHookAuthors(e.target.value)} />
              </div>
            </div>
          </div>

          <div className="card">
            <h3 style={{ marginTop: 0 }}>SSH public key</h3>
            <p className="muted small">
              This instance has its own keypair; the private key is mounted into the program
              container as a secret. Add this public key wherever the program must connect
              (deploy key, authorized_keys…).
            </p>
            <CopyField value={inst.ssh_public_key} block />
          </div>
        </div>
      ) : runId ? (
        <RunView instanceId={id} runId={runId} />
      ) : (
        <RunsTable instanceId={id} />
      )}

      {showReadme && (
        <Modal title={program.title} onClose={() => setShowReadme(false)} wide>
          <p className="muted small" style={{ marginTop: 0 }}>
            {program.short_description}
          </p>
          <div style={{ maxHeight: 420, overflowY: "auto" }}>
            <Markdown>{program.readme_md || "No description."}</Markdown>
          </div>
        </Modal>
      )}
    </div>
  );
}

function RunsTable({ instanceId }: { instanceId: string }) {
  const navigate = useNavigate();
  const [runs, setRuns] = useState<ProgramRun[] | null>(null);

  function load() {
    programsApi.runs(instanceId).then(setRuns).catch(() => {});
  }
  useEffect(load, [instanceId]);
  usePolling(load, 4000, !!runs?.some(runActive));

  if (!runs) return <Loading />;
  if (runs.length === 0) return <div className="card center muted">No runs yet.</div>;
  return (
    <div className="table-wrap">
      <table className="data">
        <thead>
          <tr>
            <th>State</th>
            <th>Trigger</th>
            <th>Started</th>
            <th>Duration</th>
            <th>Credits</th>
            <th>Webhook</th>
          </tr>
        </thead>
        <tbody>
          {runs.map((r) => (
            <tr
              key={r.id}
              style={{ cursor: "pointer" }}
              onClick={() => navigate(`/programs/instances/${instanceId}/runs/${r.id}`)}
            >
              <td>
                <RunStateBadge state={r.state} />
              </td>
              <td className="muted">{r.kind}</td>
              <td className="faint tiny">{relTime(r.started_at || r.created_at)}</td>
              <td className="muted tiny">{runDuration(r)}</td>
              <td className="muted tiny">{formatCreditsExact(r.cost_credits)}</td>
              <td className="muted tiny">{r.webhook_status || "-"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function RunView({ instanceId, runId }: { instanceId: string; runId: string }) {
  const [run, setRun] = useState<ProgramRunFull | null>(null);

  function load() {
    programsApi.runDetail(instanceId, runId).then(setRun).catch(() => {});
  }
  useEffect(load, [instanceId, runId]);
  usePolling(load, 3000, runActive(run));

  if (!run) return <Loading />;
  const finished = !runActive(run);
  return (
    <div className="stack" style={{ gap: "1rem" }}>
      <div className="card">
        <div className="between mb">
          <div className="row gap-sm">
            <Link className="btn btn-sm btn-ghost" to={`/programs/instances/${instanceId}/runs`}>
              ← All runs
            </Link>
            <RunStateBadge state={run.state} />
            <span className="muted tiny">{run.kind}</span>
          </div>
          <span className="muted tiny">
            {relTime(run.started_at || run.created_at)} · {runDuration(run)}
            {run.exit_code != null && ` · exit ${run.exit_code}`}
            {run.cost_credits > 0 && ` · ${formatCreditsExact(run.cost_credits)} credits`}
          </span>
        </div>
        {run.error && <Alert kind={run.state === "blocked" ? "warn" : "error"}>{run.error}</Alert>}
        <RunLog
          key={run.id}
          active={!finished}
          fetchChunk={(offset) => programsApi.runLog(instanceId, runId, offset)}
        />
      </div>

      {finished && run.state !== "blocked" && (
        <div className="grid-2">
          <div className="card">
            <h3 style={{ marginTop: 0 }}>output.txt</h3>
            {run.output_text ? (
              <pre className="output-pane">{run.output_text}</pre>
            ) : (
              <p className="muted small">The run produced no output.txt.</p>
            )}
          </div>
          <div className="card">
            <h3 style={{ marginTop: 0 }}>Output files</h3>
            {run.output_files.length === 0 ? (
              <p className="muted small">No files in output/.</p>
            ) : (
              <div className="stack" style={{ gap: "0.3rem" }}>
                {run.output_files.map((f) => (
                  <div key={f.path} className="between">
                    <span className="mono tiny">{f.path}</span>
                    <span className="row gap-sm">
                      <span className="faint tiny">{formatBytes(f.size)}</span>
                      <a
                        className="btn btn-sm btn-ghost"
                        href={programsApi.fileUrl(instanceId, runId, f.path)}
                        download
                      >
                        Download
                      </a>
                    </span>
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
