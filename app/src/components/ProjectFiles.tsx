import { useEffect, useRef, useState } from "react";
import { projectFilesApi } from "../lib/endpoints";
import { useToast } from "../lib/toast";
import { Alert, Loading, Spinner, relTime } from "./ui";
import { formatBytes } from "./programs";
import type { ProjectFileInfo } from "../types";

// The Files half of the Memory & files tab: import one or several files that get
// staged into every dev run's sandbox for the agent to use.
export default function ProjectFiles({
  projectId,
  readOnly = false,
}: {
  projectId: string;
  // §sharing: read-only shares browse and download files without import/delete.
  readOnly?: boolean;
}) {
  const toast = useToast();
  const [files, setFiles] = useState<ProjectFileInfo[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [uploading, setUploading] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  function load() {
    projectFilesApi
      .list(projectId)
      .then((rows) => {
        setFiles(rows);
        setError(null);
      })
      .catch((err) => setError(err instanceof Error ? err.message : "Failed to load files."));
  }
  useEffect(() => {
    setFiles(null);
    load();
  }, [projectId]);

  async function onPick(ev: React.ChangeEvent<HTMLInputElement>) {
    const picked = Array.from(ev.target.files ?? []);
    ev.target.value = ""; // allow re-importing the same file
    if (picked.length === 0) return;
    setUploading(true);
    try {
      await projectFilesApi.upload(projectId, picked);
      toast.push(picked.length > 1 ? `${picked.length} files imported` : "File imported", "ok");
      load();
    } catch (err) {
      toast.push(err instanceof Error ? err.message : "Import failed", "err");
    } finally {
      setUploading(false);
    }
  }

  async function remove(id: string) {
    try {
      await projectFilesApi.remove(projectId, id);
      toast.push("File deleted", "ok");
      load();
    } catch (err) {
      toast.push(err instanceof Error ? err.message : "Delete failed", "err");
    }
  }

  return (
    <div className="mt">
      <h3 style={{ margin: "2rem 0 0.75rem" }}>Files</h3>
      <div className="between mb">
        <p className="muted small" style={{ margin: 0, maxWidth: 640 }}>
          Import files the dev agent can use (specs, datasets, images, templates…). They are
          copied into its sandbox on every run and listed in its task. Re-importing a filename
          replaces it. Don't put secrets here - use a secret Memory entry instead.
        </p>
        {!readOnly && (
          <button
            className="btn btn-primary btn-sm"
            disabled={uploading}
            onClick={() => inputRef.current?.click()}
          >
            {uploading ? <Spinner /> : "+ Import files"}
          </button>
        )}
        <input
          ref={inputRef}
          type="file"
          multiple
          style={{ display: "none" }}
          onChange={onPick}
        />
      </div>

      {error ? (
        <Alert kind="error">{error}</Alert>
      ) : !files ? (
        <Loading />
      ) : files.length === 0 ? (
        <div className="card center muted">No files imported yet.</div>
      ) : (
        <div className="table-wrap mb">
          <table className="data">
            <thead>
              <tr>
                <th>File</th>
                <th>Size</th>
                <th>Author</th>
                <th>Updated</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {files.map((f) => (
                <tr key={f.id}>
                  <td className="mono">
                    <a href={projectFilesApi.downloadUrl(projectId, f.id)} download={f.filename}>
                      {f.filename}
                    </a>
                  </td>
                  <td className="faint">{formatBytes(f.size_bytes)}</td>
                  <td className="muted">{f.author}</td>
                  <td className="faint tiny">{relTime(f.updated_at)}</td>
                  <td>
                    {!readOnly && (
                      <button className="btn btn-sm btn-danger" onClick={() => remove(f.id)}>
                        Delete
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
