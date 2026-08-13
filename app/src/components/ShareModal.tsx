import { useCallback, useEffect, useState } from "react";
import type { FormEvent } from "react";
import { projectsApi } from "../lib/endpoints";
import { useToast } from "../lib/toast";
import { Modal, Spinner } from "./ui";
import type { ShareEntry } from "../types";

type ShareRole = ShareEntry["role"];

const ROLE_LABEL: Record<ShareRole, string> = {
  contributor: "Contributor",
  viewer: "Read-only",
};

// §sharing: owner-org management of who else sees the project. Adding an email
// grants access instantly (registered users only, no invitation email); picking
// a new role for an existing entry re-posts the same email with the new role.
export default function ShareModal({
  projectId,
  onClose,
}: {
  projectId: string;
  onClose: () => void;
}) {
  const toast = useToast();
  const [shares, setShares] = useState<ShareEntry[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [email, setEmail] = useState("");
  const [role, setRole] = useState<ShareRole>("contributor");
  const [busy, setBusy] = useState(false);

  const load = useCallback(() => {
    projectsApi
      .shares(projectId)
      .then((rows) => {
        setShares(rows);
        setError(null);
      })
      .catch((err) => setError(err instanceof Error ? err.message : "Failed to load shares."));
  }, [projectId]);
  useEffect(load, [load]);

  async function add(e: FormEvent) {
    e.preventDefault();
    const target = email.trim();
    if (!target) return;
    setBusy(true);
    try {
      await projectsApi.addShare(projectId, target, role);
      setEmail("");
      toast.push(`Shared with ${target}.`, "ok");
      load();
    } catch (err) {
      toast.push(err instanceof Error ? err.message : "Could not share the project", "err");
    } finally {
      setBusy(false);
    }
  }

  async function changeRole(s: ShareEntry, newRole: ShareRole) {
    if (newRole === s.role) return;
    setBusy(true);
    try {
      await projectsApi.addShare(projectId, s.email, newRole);
      toast.push(`${s.email} is now ${ROLE_LABEL[newRole].toLowerCase()}.`, "ok");
      load();
    } catch (err) {
      toast.push(err instanceof Error ? err.message : "Could not change the role", "err");
    } finally {
      setBusy(false);
    }
  }

  async function remove(s: ShareEntry) {
    setBusy(true);
    try {
      await projectsApi.removeShare(projectId, s.id);
      toast.push(`Removed ${s.email}.`, "ok");
      load();
    } catch (err) {
      toast.push(err instanceof Error ? err.message : "Could not remove the share", "err");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal title="Share project" onClose={onClose}>
      <p className="muted small">
        Give another registered user access by email - they see the project immediately, no
        invitation to accept. Contributors act on the project like you (chat, requests, builds,
        billed to this project's organization); read-only users see everything and change nothing.
      </p>
      <form className="row gap-sm mt" onSubmit={add}>
        <input
          type="email"
          value={email}
          placeholder="colleague@company.com"
          aria-label="Email of the user to share with"
          style={{ flex: "1 1 10rem", minWidth: 0 }}
          onChange={(e) => setEmail(e.target.value)}
        />
        <select
          value={role}
          aria-label="Access level"
          style={{ width: "auto", flex: "0 0 auto" }}
          onChange={(e) => setRole(e.target.value as ShareRole)}
        >
          <option value="contributor">Contributor</option>
          <option value="viewer">Read-only</option>
        </select>
        <button type="submit" className="btn btn-primary btn-sm" disabled={busy || !email.trim()}>
          {busy ? <Spinner /> : "Share"}
        </button>
      </form>
      {error && <p className="tiny faint mt">{error}</p>}
      {shares == null ? (
        <p className="tiny faint mt">Loading…</p>
      ) : shares.length === 0 ? (
        <p className="tiny faint mt">Not shared with anyone yet.</p>
      ) : (
        <div className="stack mt">
          {shares.map((s) => (
            <div key={s.id} className="row gap-sm">
              <span className="small" style={{ flex: "1 1 0", minWidth: 0, overflowWrap: "anywhere" }}>
                {s.email}
                {s.full_name && <span className="faint"> · {s.full_name}</span>}
              </span>
              <select
                value={s.role}
                aria-label={`Access level for ${s.email}`}
                disabled={busy}
                style={{ width: "auto", flex: "0 0 auto" }}
                onChange={(e) => changeRole(s, e.target.value as ShareRole)}
              >
                <option value="contributor">Contributor</option>
                <option value="viewer">Read-only</option>
              </select>
              <button
                type="button"
                className="btn btn-sm btn-ghost"
                disabled={busy}
                onClick={() => remove(s)}
              >
                Remove
              </button>
            </div>
          ))}
        </div>
      )}
    </Modal>
  );
}
