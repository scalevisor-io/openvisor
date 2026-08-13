import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { Alert, Badge, Loading, Modal, Spinner, relTime } from "../../components/ui";
import { adminProgramsApi, metaApi } from "../../lib/endpoints";
import { useToast } from "../../lib/toast";
import type { AdminProgram } from "../../types";

// Admin Programs (§28): catalog management. Creation binds a program to its
// GitLab repo (immutable afterwards); everything else is edited on the detail page.
export default function AdminPrograms() {
  const navigate = useNavigate();
  const toast = useToast();
  const [programs, setPrograms] = useState<AdminProgram[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [gitlabUrl, setGitlabUrl] = useState("");

  const [modalOpen, setModalOpen] = useState(false);
  const [title, setTitle] = useState("");
  const [shortDesc, setShortDesc] = useState("");
  const [repoPath, setRepoPath] = useState("");
  const [creating, setCreating] = useState(false);

  useEffect(() => {
    adminProgramsApi
      .list()
      .then(setPrograms)
      .catch((err) => setError(err instanceof Error ? err.message : "Failed to load programs."));
    metaApi
      .config()
      .then((c) => setGitlabUrl(c.gitlab_url || ""))
      .catch(() => {});
  }, []);

  async function create(ev: React.FormEvent) {
    ev.preventDefault();
    setCreating(true);
    try {
      const program = await adminProgramsApi.create({
        title: title.trim(),
        short_description: shortDesc.trim(),
        gitlab_repo_path: repoPath.trim().replace(/^\/+|\/+$/g, ""),
      });
      toast.push("Program created", "ok");
      navigate(`/admin/programs/${program.id}`);
    } catch (err) {
      toast.push(err instanceof Error ? err.message : "Creation failed", "err");
    } finally {
      setCreating(false);
    }
  }

  if (error) return <Alert kind="error">{error}</Alert>;
  if (!programs) return <Loading />;

  return (
    <div>
      <div className="between mb">
        <h1 style={{ margin: 0 }}>Programs</h1>
        <button className="btn btn-primary btn-sm" onClick={() => setModalOpen(true)}>
          + New program
        </button>
      </div>

      {programs.length === 0 ? (
        <div className="card center muted">No programs yet.</div>
      ) : (
        <div className="table-wrap">
          <table className="data">
            <thead>
              <tr>
                <th>Title</th>
                <th>Repository</th>
                <th>Published</th>
                <th>Instances</th>
                <th>Last check</th>
                <th>Created</th>
              </tr>
            </thead>
            <tbody>
              {programs.map((p) => (
                <tr
                  key={p.id}
                  style={{ cursor: "pointer" }}
                  onClick={() => navigate(`/admin/programs/${p.id}`)}
                >
                  <td>{p.title}</td>
                  <td className="mono tiny">{p.gitlab_repo_path}</td>
                  <td>
                    <Badge label={p.is_published ? "published" : "hidden"}
                           kind={p.is_published ? "finished" : "draft"} />
                  </td>
                  <td className="muted">{p.instances_count ?? 0}</td>
                  <td>
                    {p.last_check_state ? (
                      <Badge label={p.last_check_state}
                             kind={p.last_check_state === "passed" ? "finished" : "canceled"} />
                    ) : (
                      <span className="faint tiny">never</span>
                    )}
                  </td>
                  <td className="faint tiny">{relTime(p.created_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {modalOpen && (
        <Modal title="New program" onClose={() => setModalOpen(false)} wide>
          <form onSubmit={create}>
            <div className="field">
              <label>Title</label>
              <input type="text" value={title} onChange={(e) => setTitle(e.target.value)} />
            </div>
            <div className="field">
              <label>Short description (catalog card)</label>
              <input type="text" value={shortDesc} onChange={(e) => setShortDesc(e.target.value)} />
            </div>
            <div className="field">
              <label>GitLab repository (immutable after creation)</label>
              <div className="row gap-sm" style={{ alignItems: "center" }}>
                {gitlabUrl && <span className="mono tiny muted">{gitlabUrl}/</span>}
                <input
                  type="text"
                  value={repoPath}
                  placeholder="openvisor/program-dummy"
                  style={{ flex: 1 }}
                  onChange={(e) => setRepoPath(e.target.value)}
                />
              </div>
              <div className="tiny muted">
                The repo must follow the program-template contract (compose.yml with a `program`
                service, /input.template.yml, output/output.txt). README.md becomes the long
                description.
              </div>
            </div>
            <div className="between">
              <button type="button" className="btn btn-sm" onClick={() => setModalOpen(false)}>
                Cancel
              </button>
              <button
                type="submit"
                className="btn btn-primary btn-sm"
                disabled={creating || !title.trim() || !repoPath.trim()}
              >
                {creating ? <Spinner /> : "Create program"}
              </button>
            </div>
          </form>
        </Modal>
      )}
    </div>
  );
}
