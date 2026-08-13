import { useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { projectsApi } from "../lib/endpoints";
import { loadSpecialities, specialityLabel } from "../lib/meta";
import { Alert, Badge, Loading, Pager, Spinner, relTime, usePager } from "../components/ui";
import KindChip from "../components/KindChip";
import { StatusTimeline } from "@shared-ui";
import type { ProjectSummary, Speciality } from "../types";

const PAGE_SIZE = 6;
const SEARCH_DEBOUNCE_MS = 500;

export default function Projects() {
  const [projects, setProjects] = useState<ProjectSummary[] | null>(null);
  const [specialities, setSpecialities] = useState<Speciality[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [q, setQ] = useState("");
  // The server ranking, kept keyed by the query it answers - a response that
  // lands after the customer has typed on must never be shown for the new text.
  const [ranked, setRanked] = useState<{ q: string; ids: string[]; ai: boolean } | null>(null);
  const [searching, setSearching] = useState(false);
  const seq = useRef(0);

  useEffect(() => {
    Promise.all([projectsApi.list(), loadSpecialities()])
      .then(([p, s]) => {
        setProjects(p);
        setSpecialities(s);
      })
      .catch((err) => setError(err instanceof Error ? err.message : "Failed to load projects."));
  }, []);

  const query = q.trim();

  // Instant, free filtering over the text on the cards; the debounced AI rerank
  // replaces this ordering once it answers. So typing never waits on the network,
  // and the box still filters when the model (or the API) is unavailable.
  const local = useMemo(() => {
    const needle = query.toLowerCase();
    if (!projects) return [];
    if (!needle) return projects;
    return projects.filter((p) =>
      [p.name, specialityLabel(specialities, p.speciality), p.status.replace(/_/g, " "), p.kind]
        .join(" ")
        .toLowerCase()
        .includes(needle),
    );
  }, [projects, specialities, query]);

  useEffect(() => {
    if (!query) {
      setRanked(null);
      setSearching(false);
      return;
    }
    setSearching(true);
    const mine = ++seq.current;
    const t = setTimeout(() => {
      projectsApi
        .search(query)
        .then((r) => {
          if (mine !== seq.current) return; // a newer keystroke owns the box now
          setRanked({ q: query, ids: r.results.map((p) => p.id), ai: r.ai });
        })
        .catch(() => {
          if (mine === seq.current) setRanked(null); // keep the local filter
        })
        .finally(() => {
          if (mine === seq.current) setSearching(false);
        });
    }, SEARCH_DEBOUNCE_MS);
    return () => clearTimeout(t);
  }, [query]);

  const shown = useMemo(() => {
    if (!projects) return [];
    if (!query) return projects;
    if (ranked && ranked.q === query) {
      const byId = new Map(projects.map((p) => [p.id, p]));
      return ranked.ids.map((id) => byId.get(id)).filter((p): p is ProjectSummary => !!p);
    }
    return local;
  }, [projects, local, ranked, query]);

  const aiRanked = !!ranked && ranked.q === query && ranked.ai;
  const { pageItems, page, pages, setPage } = usePager(shown, PAGE_SIZE);

  // A new search starts at page 1 - staying on page 3 of the previous result set
  // reads as an empty search.
  useEffect(() => setPage(0), [query, setPage]);

  if (error) return <Alert kind="error">{error}</Alert>;
  if (!projects) return <Loading />;

  return (
    <div>
      <div className="page-head">
        <div>
          <h1>Projects</h1>
          <p className="muted">Your MVPs, demos, and requests.</p>
        </div>
        <Link to="/projects/new" className="btn btn-primary">
          + New project
        </Link>
      </div>

      {projects.length === 0 ? (
        <div className="card center">
          <p className="muted">No projects yet.</p>
          <Link to="/projects/new" className="btn btn-primary mt">
            Create your first project
          </Link>
        </div>
      ) : (
        <>
          <div className="store-toolbar">
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
                placeholder="Search your projects"
                aria-label="Search your projects"
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
              {query
                ? `${shown.length} result${shown.length === 1 ? "" : "s"}${
                    aiRanked ? " - AI-ranked" : ""
                  }`
                : `${projects.length} project${projects.length === 1 ? "" : "s"}`}
            </span>
          </div>

          {shown.length === 0 ? (
            <div className="card center">
              <p className="muted">No projects match "{query}".</p>
              <button className="btn btn-sm mt" onClick={() => setQ("")}>
                Clear search
              </button>
            </div>
          ) : (
            <>
              <div className="cards-grid">
                {pageItems.map((p) => (
                  <Link key={p.id} to={`/projects/${p.id}`} className="card project-card">
                    <div className="between project-card-head">
                      <h3 style={{ margin: 0 }}>{p.name}</h3>
                      <Badge
                        label={p.status}
                        kind={p.status}
                        live={
                          p.status === "development" &&
                          (p.dev_run_state === "running" || p.dev_run_state === "deploying")
                        }
                      />
                    </div>
                    <div className="muted small">
                      {p.kind === "direct_quote"
                        ? "Custom quote engagement"
                        : specialityLabel(specialities, p.speciality)}
                    </div>
                    <StatusTimeline
                      status={p.status}
                      kind={p.kind}
                      demoExists={p.demo_state === "running"}
                      compact
                    />
                    <div className="row row-wrap gap-sm" style={{ marginTop: "0.75rem" }}>
                      <KindChip kind={p.kind} />
                      {p.kind === "ai" && (
                        <>
                          {p.tier && <span className="badge">{p.tier}</span>}
                          <Badge label={p.demo_state} kind={p.demo_state} />
                        </>
                      )}
                      {p.access !== "owner" && (
                        <span
                          className="badge"
                          title={
                            p.access === "viewer"
                              ? "Shared with you read-only"
                              : "Shared with you as a contributor"
                          }
                        >
                          shared{p.access === "viewer" ? " · read-only" : ""}
                        </span>
                      )}
                    </div>
                    <div className="tiny faint" style={{ marginTop: "0.75rem" }}>
                      Created {relTime(p.created_at)}
                    </div>
                  </Link>
                ))}
              </div>
              <Pager page={page} pages={pages} onPage={setPage} />
            </>
          )}
        </>
      )}
    </div>
  );
}
