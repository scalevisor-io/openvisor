import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { adminApi } from "../../lib/endpoints";
import { Alert, Badge, Loading, Pager, relTime, usePager } from "../../components/ui";
import type { AdminProjectSummary } from "../../types";

const PAGE_SIZE = 15;

export default function AdminOverview() {
  const [projects, setProjects] = useState<AdminProjectSummary[] | null>(null);
  const [counts, setCounts] = useState<Record<string, number>>({});
  const [error, setError] = useState<string | null>(null);
  const { pageItems, page, pages, setPage } = usePager(projects ?? [], PAGE_SIZE);

  useEffect(() => {
    adminApi
      .overview()
      .then((d) => {
        setProjects(d.projects);
        setCounts(d.counts.by_status);
      })
      .catch((err) => setError(err instanceof Error ? err.message : "Failed to load overview."));
  }, []);

  if (error) return <Alert kind="error">{error}</Alert>;
  if (!projects) return <Loading />;

  return (
    <div>
      <div className="page-head">
        <h1>Admin overview</h1>
      </div>

      <div className="row row-wrap gap-sm mb">
        {Object.entries(counts).map(([status, n]) => (
          <span key={status} className="row gap-sm">
            <Badge label={status} kind={status} />
            <strong>{n}</strong>
          </span>
        ))}
      </div>

      <div className="table-wrap">
        <table className="data">
          <thead>
            <tr>
              <th>Project</th>
              <th>Organization</th>
              <th>Speciality</th>
              <th>Status</th>
              <th>Demo</th>
              <th>Created</th>
            </tr>
          </thead>
          <tbody>
            {pageItems.map((p) => (
              <tr key={p.id}>
                <td>
                  <Link to={`/projects/${p.id}`}>{p.name}</Link>
                </td>
                <td className="muted">{p.org_name}</td>
                <td className="tiny muted">{p.speciality}</td>
                <td>
                  <Badge label={p.status} kind={p.status} />
                </td>
                <td>
                  <Badge label={p.demo_state} kind={p.demo_state} />
                </td>
                <td className="faint">{relTime(p.created_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <Pager page={page} pages={pages} onPage={setPage} />
    </div>
  );
}
