import { useEffect, useMemo, useState } from "react";
import { orgMemoryApi } from "../lib/endpoints";
import { useToast } from "../lib/toast";
import { Alert, Spinner, Toggle } from "../components/ui";
import MemoryManager, { type MemoryStore } from "../components/MemoryManager";

// Global (organization-scoped) Memory shared across every project. The switch here
// is the default for projects: a project follows it unless it overrides the choice
// in its own Memory tab.
export default function GlobalMemory() {
  const toast = useToast();
  const [enabledDefault, setEnabledDefault] = useState<boolean | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    orgMemoryApi.settings().then((s) => setEnabledDefault(s.enabled_default)).catch(() => {});
  }, []);

  async function setDefault(v: boolean) {
    const previous = enabledDefault;
    setBusy(true);
    setEnabledDefault(v);
    try {
      const s = await orgMemoryApi.updateSettings({ enabled_default: v });
      setEnabledDefault(s.enabled_default);
      toast.push("Setting saved", "ok");
    } catch (err) {
      setEnabledDefault(previous);
      toast.push(err instanceof Error ? err.message : "Could not save.", "err");
    } finally {
      setBusy(false);
    }
  }

  const store = useMemo<MemoryStore>(
    () => ({
      list: () => orgMemoryApi.list(),
      upsert: (body) => orgMemoryApi.upsert(body),
      remove: (id) => orgMemoryApi.remove(id),
    }),
    [],
  );

  return (
    <div>
      <div className="page-head">
        <div>
          <h1>Global memory</h1>
          <p className="muted">
            Shared across all your projects. Set details or credentials once here instead of
            repeating them in every project.
          </p>
        </div>
      </div>

      <div className="card mb" style={{ padding: "0.85rem 1rem", maxWidth: 720 }}>
        <div className="between" style={{ alignItems: "center", gap: "1rem" }}>
          <div>
            <strong>Enabled by default</strong>
            <div className="muted small" style={{ maxWidth: 560 }}>
              New and existing projects use global memory unless they override it in their own
              Memory & files tab. A project entry always overrides a global entry with the same key.
            </div>
          </div>
          {enabledDefault === null ? (
            <Spinner />
          ) : (
            <Toggle checked={enabledDefault} disabled={busy} onChange={setDefault} />
          )}
        </div>
      </div>

      <Alert kind="info">
        Global memory reaches a project's dev runs exactly like project memory: non-secret values
        are shown to the agent, secret values are exported to its sandbox as environment variables
        and stay encrypted at rest.
      </Alert>

      <div className="mt">
        <MemoryManager
          store={store}
          scopeKey="org"
          intro={
            <>
              These entries are available to every project that has global memory enabled. Add a
              description so the dev agent knows what each value is for - key, description, and value
              are all passed to it. Mark entries as secret to hide them in the interface.
            </>
          }
        />
      </div>
    </div>
  );
}
