import { Fragment, useEffect, useState, type ReactNode } from "react";
import { metaApi, type MemoryInput } from "../lib/endpoints";
import { useToast } from "../lib/toast";
import { Alert, CopyButton, Loading, Modal, Spinner, relTime } from "./ui";
import type { MemoryEntry, MemoryPlaceholder } from "../types";

export interface MemoryStore {
  list: () => Promise<MemoryEntry[]>;
  upsert: (body: MemoryInput) => Promise<MemoryEntry>;
  remove: (entryId: string) => Promise<unknown>;
}

// The shared Memory table + CRUD modal + suggested-keys picker. Used by both the
// per-project Memory tab and the global (org-scoped) Memory page; the only
// difference between the two is the `store` it talks to and the `intro`/`top` slots.
export default function MemoryManager({
  store,
  scopeKey,
  intro,
  top,
  readOnly = false,
}: {
  store: MemoryStore;
  scopeKey: string; // changes when the underlying scope (project/org) changes → reload
  intro: ReactNode;
  top?: ReactNode;
  // §sharing: read-only shares browse entries without add/edit/delete.
  readOnly?: boolean;
}) {
  const toast = useToast();
  const [entries, setEntries] = useState<MemoryEntry[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const [key, setKey] = useState("");
  const [value, setValue] = useState("");
  const [isSecret, setIsSecret] = useState(false);
  const [description, setDescription] = useState("");
  const [editing, setEditing] = useState<string | null>(null);
  const [modalOpen, setModalOpen] = useState(false);
  const [saving, setSaving] = useState(false);

  // Collapsible descriptions in the list.
  const [expanded, setExpanded] = useState<Set<string>>(new Set());

  // Placeholder catalog (conventional keys the customer can pre-fill).
  const [placeholders, setPlaceholders] = useState<MemoryPlaceholder[]>([]);
  const [phQuery, setPhQuery] = useState("");

  function load() {
    store
      .list()
      .then((rows) => {
        setEntries(rows);
        setError(null);
      })
      .catch((err) => setError(err instanceof Error ? err.message : "Failed to load memory."));
  }
  // Reset to the loading state when the scope changes so one scope's entries never
  // flash under another; a plain reload (after save/delete) keeps the table visible.
  useEffect(() => {
    setEntries(null);
    load();
  }, [scopeKey]);
  useEffect(() => {
    metaApi.memoryPlaceholders().then(setPlaceholders).catch(() => {});
  }, []);

  function openNew() {
    setEditing(null);
    setKey("");
    setValue("");
    setIsSecret(false);
    setDescription("");
    setPhQuery("");
    setModalOpen(true);
  }

  function openEdit(e: MemoryEntry) {
    setEditing(e.id);
    setKey(e.key);
    setValue(e.value);
    setIsSecret(e.is_secret);
    setDescription(e.description);
    setModalOpen(true);
  }

  function closeModal() {
    setModalOpen(false);
    setEditing(null);
  }

  function usePlaceholder(p: MemoryPlaceholder) {
    setKey(p.key);
    setValue("");
    setIsSecret(p.is_secret);
    setDescription(p.description);
  }

  function toggleExpanded(id: string) {
    setExpanded((prev) => {
      const next = new Set(prev);
      next.has(id) ? next.delete(id) : next.add(id);
      return next;
    });
  }

  async function save(ev: React.FormEvent) {
    ev.preventDefault();
    if (!key.trim()) return;
    setSaving(true);
    try {
      await store.upsert({
        key: key.trim(),
        value,
        is_secret: isSecret,
        description: description.trim(),
      });
      toast.push("Memory saved", "ok");
      closeModal();
      load();
    } catch (err) {
      toast.push(err instanceof Error ? err.message : "Save failed", "err");
    } finally {
      setSaving(false);
    }
  }

  async function remove(id: string) {
    try {
      await store.remove(id);
      toast.push("Entry deleted", "ok");
      load();
    } catch (err) {
      toast.push(err instanceof Error ? err.message : "Delete failed", "err");
    }
  }

  const q = phQuery.trim().toLowerCase();
  const existingKeys = new Set((entries ?? []).map((e) => e.key));
  const phMatches = placeholders
    .filter((p) => !q || `${p.key} ${p.category} ${p.description}`.toLowerCase().includes(q))
    .slice(0, 8);

  return (
    <div>
      {top}

      <div className="between mb">
        <p className="muted small" style={{ margin: 0, maxWidth: 640 }}>
          {intro}
        </p>
        {!readOnly && (
          <button className="btn btn-primary btn-sm" onClick={openNew}>
            + New memory
          </button>
        )}
      </div>

      {error ? (
        <Alert kind="error">{error}</Alert>
      ) : !entries ? (
        <Loading />
      ) : entries.length === 0 ? (
        <div className="card center muted">No memory entries yet.</div>
      ) : (
        <div className="table-wrap mb">
          <table className="data">
            <thead>
              <tr>
                <th>Key</th>
                <th>Value</th>
                <th>Author</th>
                <th>Updated</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {entries.map((e) => {
                const hasDesc = !!e.description?.trim();
                const isOpen = expanded.has(e.id);
                return (
                  <Fragment key={e.id}>
                    <tr>
                      <td className="mono">
                        <span className="row gap-sm">
                          {hasDesc && (
                            <button
                              className="btn btn-sm btn-ghost"
                              style={{ padding: "0 0.3rem" }}
                              title={isOpen ? "Hide description" : "Show description"}
                              onClick={() => toggleExpanded(e.id)}
                            >
                              {isOpen ? "▾" : "▸"}
                            </button>
                          )}
                          {e.key}
                        </span>
                      </td>
                      <td className="mono">
                        <span className="row gap-sm">
                          {e.is_secret ? (
                            <span className="faint" title="Hidden in display only - copy works">
                              ••••••
                            </span>
                          ) : (
                            e.value
                          )}
                          <CopyButton value={e.value} />
                        </span>
                      </td>
                      <td className="muted">{e.author}</td>
                      <td className="faint tiny">{relTime(e.updated_at)}</td>
                      <td>
                        {!readOnly && (
                          <div className="row gap-sm">
                            <button className="btn btn-sm btn-ghost" onClick={() => openEdit(e)}>
                              Edit
                            </button>
                            <button className="btn btn-sm btn-danger" onClick={() => remove(e.id)}>
                              Delete
                            </button>
                          </div>
                        )}
                      </td>
                    </tr>
                    {hasDesc && isOpen && (
                      <tr>
                        <td colSpan={5} className="tiny muted" style={{ paddingTop: 0 }}>
                          {e.description}
                        </td>
                      </tr>
                    )}
                  </Fragment>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      {modalOpen && (
        <Modal title={editing ? "Edit memory entry" : "New memory entry"} onClose={closeModal} wide>
          <form onSubmit={save}>
            {/* Conventional-key suggestions, shown by default when adding */}
            {!editing && (
              <div className="card mb" style={{ padding: "0.75rem" }}>
                <label>Suggested keys</label>
                <input
                  type="text"
                  placeholder="Search (e.g. github token, aws, gcp, supabase)…"
                  value={phQuery}
                  onChange={(e) => setPhQuery(e.target.value)}
                />
                <div
                  className="stack mt"
                  style={{ gap: "0.4rem", maxHeight: 240, overflowY: "auto" }}
                >
                  {phMatches.length === 0 ? (
                    <p className="muted small" style={{ margin: 0 }}>
                      No suggestion matches your search - fill the fields below manually.
                    </p>
                  ) : (
                    phMatches.map((p) => {
                      const picked = key === p.key;
                      return (
                        <div
                          key={p.key}
                          className="between"
                          style={{ alignItems: "flex-start", gap: "0.6rem" }}
                        >
                          <div>
                            <div className="row gap-sm">
                              <span className="mono">{p.key}</span>
                              <span className="badge">{p.category}</span>
                              {p.is_secret && <span className="badge">secret</span>}
                              {existingKeys.has(p.key) && (
                                <span className="tiny faint">already added</span>
                              )}
                            </div>
                            <div className="tiny muted">{p.description}</div>
                          </div>
                          <button
                            type="button"
                            className="btn btn-sm"
                            disabled={picked}
                            onClick={() => usePlaceholder(p)}
                          >
                            {picked ? "Selected" : "Use"}
                          </button>
                        </div>
                      );
                    })
                  )}
                </div>
              </div>
            )}

            <div className="grid-2">
              <div className="field">
                <label>Key</label>
                <input
                  type="text"
                  value={key}
                  disabled={!!editing}
                  placeholder="e.g. SCW_ACCESS_KEY"
                  onChange={(e) => setKey(e.target.value)}
                />
              </div>
              <div className="field">
                <label>Value</label>
                <input
                  type={isSecret ? "password" : "text"}
                  value={value}
                  onChange={(e) => setValue(e.target.value)}
                />
              </div>
            </div>
            <div className="field">
              <label>Description (optional) - helps the AI understand what this value is for</label>
              <textarea
                value={description}
                placeholder="e.g. GitHub token with repo scope, used to open pull requests"
                style={{ minHeight: 60 }}
                onChange={(e) => setDescription(e.target.value)}
              />
            </div>
            <div className="between">
              <label className="checkbox-row">
                <input
                  type="checkbox"
                  checked={isSecret}
                  onChange={(e) => setIsSecret(e.target.checked)}
                />
                Secret (hidden)
              </label>
              <div className="row gap-sm">
                <button type="button" className="btn btn-sm" onClick={closeModal}>
                  Cancel
                </button>
                <button
                  type="submit"
                  className="btn btn-primary btn-sm"
                  disabled={saving || !key.trim()}
                >
                  {saving ? <Spinner /> : editing ? "Save changes" : "Add entry"}
                </button>
              </div>
            </div>
          </form>
        </Modal>
      )}
    </div>
  );
}
