import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { memoryApi } from "../lib/endpoints";
import { useAuth } from "../lib/auth";
import { useToast } from "../lib/toast";
import { Spinner, Toggle } from "./ui";
import type { MemorySettings } from "../types";
import MemoryManager, { type MemoryStore } from "./MemoryManager";
import ProjectFiles from "./ProjectFiles";

// Top-of-tab control: does this project pick up the org's global Memory? Shows the
// effective state, honors the org default when unset, and links to the global page.
function GlobalMemoryControl({ projectId }: { projectId: string }) {
  const toast = useToast();
  const [settings, setSettings] = useState<MemorySettings | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    memoryApi.settings(projectId).then(setSettings).catch(() => {});
  }, [projectId]);

  async function set(use: boolean | null) {
    if (!settings) return;
    const previous = settings;
    setBusy(true);
    setSettings({ ...settings, use_global_memory: use, effective: use ?? settings.org_default });
    try {
      setSettings(await memoryApi.updateSettings(projectId, { use_global_memory: use }));
    } catch (err) {
      setSettings(previous);
      toast.push(err instanceof Error ? err.message : "Could not save.", "err");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="card mb" style={{ padding: "0.85rem 1rem" }}>
      <div className="between" style={{ alignItems: "center", gap: "1rem" }}>
        <div>
          <strong>Use global memory</strong>
          <div className="muted small" style={{ maxWidth: 560 }}>
            Also feed your organization's global memory to this project's dev runs. A
            project entry below overrides a global entry with the same key.
          </div>
        </div>
        <div className="row gap-sm" style={{ alignItems: "center" }}>
          <Link to="/memory" className="btn btn-sm">
            Manage
          </Link>
          {settings ? (
            <Toggle
              checked={settings.effective}
              disabled={busy}
              onChange={(v) => set(v)}
            />
          ) : (
            <Spinner />
          )}
        </div>
      </div>
      {settings && (
        <div className="tiny faint" style={{ marginTop: "0.5rem" }}>
          {settings.use_global_memory === null ? (
            <>Following the organization default ({settings.org_default ? "on" : "off"}).</>
          ) : (
            <>
              Overridden for this project.{" "}
              <button
                type="button"
                className="btn btn-sm btn-ghost"
                style={{ padding: 0, height: "auto" }}
                disabled={busy}
                onClick={() => set(null)}
              >
                Use organization default
              </button>
            </>
          )}
        </div>
      )}
    </div>
  );
}

export default function MemoryTab({
  projectId,
  readOnly = false,
  isOwner = true,
}: {
  projectId: string;
  // §sharing: read-only shares browse entries without the add/edit/delete
  // affordances; the global-memory control is owner-org only either way (it
  // toggles the OWNING org's global memory, and its Manage link points at the
  // caller's own org page).
  readOnly?: boolean;
  isOwner?: boolean;
}) {
  const { settings } = useAuth();
  const consultant = settings?.consultant_first_name ?? "Consultant";

  const store = useMemo<MemoryStore>(
    () => ({
      list: () => memoryApi.list(projectId),
      upsert: (body) => memoryApi.upsert(projectId, body),
      remove: (id) => memoryApi.remove(projectId, id),
    }),
    [projectId],
  );

  return (
    <>
      <MemoryManager
        store={store}
        scopeKey={projectId}
        readOnly={readOnly}
        top={isOwner ? <GlobalMemoryControl projectId={projectId} /> : undefined}
        intro={
          <>
            Provide project details or credentials (e.g. AWS/GCP keys). Add a description so
            the dev agent knows what each value is for - key, description, and value are all passed
            to it. Mark entries as secret to hide them in the interface; values stay encrypted at
            rest and remain copiable by you, {consultant}, and the agent (exported to its sandbox as
            environment variables).
          </>
        }
      />
      <ProjectFiles projectId={projectId} readOnly={readOnly} />
    </>
  );
}
