import { useEffect, useMemo, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import Markdown from "../components/Markdown";
import { ProgramIcon, RunStateBadge } from "../components/programs";
import { Alert, Loading, Modal, Spinner, relTime } from "../components/ui";
import { programsApi } from "../lib/endpoints";
import { useToast } from "../lib/toast";
import type { Program, ProgramBrief, ProgramInstance } from "../types";

// Programs (§28): the org's installed instances + an app-store-style catalog.
// "Add" installs a program in one click (creates an instance and opens it);
// an already-installed program shows "Open" instead. Clicking a card opens
// the full description.
export default function Programs() {
  const navigate = useNavigate();
  const toast = useToast();
  const [instances, setInstances] = useState<ProgramInstance[] | null>(null);
  const [catalog, setCatalog] = useState<ProgramBrief[] | null>(null);
  const [q, setQ] = useState("");
  const [searching, setSearching] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const [selected, setSelected] = useState<Program | null>(null);
  const [label, setLabel] = useState("");
  const [adding, setAdding] = useState<string | null>(null); // program id being installed

  function loadInstances() {
    programsApi
      .instances()
      .then(setInstances)
      .catch((err) => setError(err instanceof Error ? err.message : "Failed to load programs."));
  }
  useEffect(loadInstances, []);

  // Server-side search over title + short description (debounced).
  useEffect(() => {
    setSearching(true);
    const t = setTimeout(() => {
      programsApi
        .catalog(q.trim() || undefined)
        .then(setCatalog)
        .catch(() => setCatalog([]))
        .finally(() => setSearching(false));
    }, 250);
    return () => clearTimeout(t);
  }, [q]);

  // program id → newest instance id (instances arrive newest first)
  const installed = useMemo(() => {
    const map = new Map<string, string>();
    for (const inst of instances ?? []) {
      if (!map.has(inst.program.id)) map.set(inst.program.id, inst.id);
    }
    return map;
  }, [instances]);

  function openProgram(id: string) {
    setLabel("");
    programsApi
      .get(id)
      .then(setSelected)
      .catch((err) => toast.push(err instanceof Error ? err.message : "Failed to load", "err"));
  }

  async function addInstance(programId: string, instanceLabel: string) {
    setAdding(programId);
    try {
      const inst = await programsApi.createInstance(programId, instanceLabel.trim());
      toast.push("Program added", "ok");
      navigate(`/programs/instances/${inst.id}`);
    } catch (err) {
      toast.push(err instanceof Error ? err.message : "Failed to add program", "err");
    } finally {
      setAdding(null);
    }
  }

  if (error) return <Alert kind="error">{error}</Alert>;
  if (!instances) return <Loading />;

  const count = catalog?.length ?? 0;

  return (
    <div>
      <div className="between mb">
        <h1 style={{ margin: 0 }}>Programs</h1>
        <Link to="/programs/docs" className="btn btn-sm btn-ghost">
          Documentation
        </Link>
      </div>

      <h2 className="small muted" style={{ marginTop: 0 }}>
        My program instances
      </h2>
      {instances.length === 0 ? (
        <div className="card center muted mb">
          Programs you add appear here - pick one from the catalog below.
        </div>
      ) : (
        <div className="table-wrap mb">
          <table className="data">
            <thead>
              <tr>
                <th>Program</th>
                <th>Label</th>
                <th>Last run</th>
                <th>Schedule</th>
                <th>Created</th>
              </tr>
            </thead>
            <tbody>
              {instances.map((inst) => (
                <tr
                  key={inst.id}
                  style={{ cursor: "pointer" }}
                  onClick={() => navigate(`/programs/instances/${inst.id}`)}
                >
                  <td>
                    <span className="row gap-sm" style={{ alignItems: "center" }}>
                      <ProgramIcon title={inst.program.title} seed={inst.program.id} size={32} />
                      {inst.program.title}
                    </span>
                  </td>
                  <td className="muted">{inst.label || "-"}</td>
                  <td>
                    {inst.latest_run ? (
                      <span className="row gap-sm">
                        <RunStateBadge state={inst.latest_run.state} />
                        <span className="faint tiny">{relTime(inst.latest_run.created_at)}</span>
                      </span>
                    ) : (
                      <span className="faint">never ran</span>
                    )}
                  </td>
                  <td className="muted tiny">
                    {inst.schedule_enabled ? (
                      <span className="mono">{inst.schedule_cron}</span>
                    ) : (
                      "off"
                    )}
                  </td>
                  <td className="faint tiny">{relTime(inst.created_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <div className="store-toolbar">
        <h2 className="small muted" style={{ margin: 0 }}>
          Catalog
        </h2>
        <div className="store-search">
          <svg
            className="store-search-glass"
            width="15"
            height="15"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="2.4"
            strokeLinecap="round"
            aria-hidden="true"
          >
            <circle cx="11" cy="11" r="7" />
            <line x1="21" y1="21" x2="16.2" y2="16.2" />
          </svg>
          <input
            type="text"
            placeholder="Search programs"
            aria-label="Search programs"
            value={q}
            onChange={(e) => setQ(e.target.value)}
            onKeyDown={(e) => e.key === "Escape" && setQ("")}
          />
          {searching ? (
            <span className="store-search-side">
              <Spinner />
            </span>
          ) : q ? (
            <button
              type="button"
              className="store-search-clear"
              aria-label="Clear search"
              onClick={() => setQ("")}
            >
              ✕
            </button>
          ) : null}
        </div>
        <span className="tiny faint store-count">
          {catalog === null ? "" : q.trim() ? `${count} result${count === 1 ? "" : "s"}` : `${count} program${count === 1 ? "" : "s"}`}
        </span>
      </div>

      {!catalog ? (
        <Loading />
      ) : catalog.length === 0 ? (
        <div className="card center muted">
          {q.trim() ? (
            <span>
              No programs match "{q.trim()}".{" "}
              <button type="button" className="btn btn-sm btn-ghost" onClick={() => setQ("")}>
                Clear search
              </button>
            </span>
          ) : (
            "No programs published yet."
          )}
        </div>
      ) : (
        <div className="store-grid">
          {catalog.map((p) => (
            <StoreCard
              key={p.id}
              program={p}
              installedInstance={installed.get(p.id)}
              busy={adding === p.id}
              onDetail={() => openProgram(p.id)}
              onAdd={() => addInstance(p.id, "")}
              onOpen={(instId) => navigate(`/programs/instances/${instId}`)}
            />
          ))}
        </div>
      )}

      {selected && (
        <Modal title="" onClose={() => setSelected(null)} wide>
          <div className="store-detail-head">
            <ProgramIcon title={selected.title} seed={selected.id} size={64} />
            <div className="store-card-titles">
              <h3 style={{ margin: 0 }}>{selected.title}</h3>
              <span className="store-eyebrow">
                {selected.schedulable ? "Runs on a schedule" : "Runs on demand"}
                {installed.has(selected.id) && " · installed"}
              </span>
            </div>
            <button
              type="button"
              className="btn-pill accent"
              disabled={adding === selected.id}
              onClick={() => addInstance(selected.id, label)}
            >
              {adding === selected.id ? <Spinner /> : installed.has(selected.id) ? "Add another" : "Add"}
            </button>
          </div>
          <p className="muted small" style={{ marginTop: "0.35rem" }}>
            {selected.short_description}
          </p>
          {selected.readme_md ? (
            <div className="card mb" style={{ maxHeight: 320, overflowY: "auto" }}>
              <Markdown>{selected.readme_md}</Markdown>
            </div>
          ) : null}
          <div className="field" style={{ marginBottom: 0 }}>
            <label>Instance label (optional) - tell several instances of this program apart</label>
            <input
              type="text"
              value={label}
              placeholder="e.g. production feed"
              onChange={(e) => setLabel(e.target.value)}
            />
          </div>
        </Modal>
      )}
    </div>
  );
}

function StoreCard({
  program,
  installedInstance,
  busy,
  onDetail,
  onAdd,
  onOpen,
}: {
  program: ProgramBrief;
  installedInstance: string | undefined;
  busy: boolean;
  onDetail: () => void;
  onAdd: () => void;
  onOpen: (instanceId: string) => void;
}) {
  return (
    <div
      className="store-card"
      role="button"
      tabIndex={0}
      onClick={onDetail}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          onDetail();
        }
      }}
    >
      <div className="store-card-head">
        <ProgramIcon title={program.title} seed={program.id} />
        <div className="store-card-titles">
          <h3>{program.title}</h3>
          <span className="store-eyebrow">
            {program.schedulable ? "Runs on a schedule" : "Runs on demand"}
          </span>
        </div>
        {installedInstance ? (
          <button
            type="button"
            className="btn-pill"
            onClick={(e) => {
              e.stopPropagation();
              onOpen(installedInstance);
            }}
          >
            Open
          </button>
        ) : (
          <button
            type="button"
            className="btn-pill accent"
            disabled={busy}
            onClick={(e) => {
              e.stopPropagation();
              onAdd();
            }}
          >
            {busy ? <Spinner /> : "Add"}
          </button>
        )}
      </div>
      <p className="store-desc">{program.short_description || "No description yet."}</p>
    </div>
  );
}
