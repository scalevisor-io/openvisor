import { useEffect, useMemo, useState } from "react";
import { adminApi } from "../../lib/endpoints";
import { useToast } from "../../lib/toast";
import { Alert, Badge, Loading, Pager, Spinner, formatCredits, relTime, usePager } from "../../components/ui";
import type { AdminUser } from "../../types";

interface OrgGroup {
  creditBalance: number;
  orgId: string;
  orgName: string;
  users: AdminUser[];
}

const PAGE_SIZE = 10;

export default function AdminUsers() {
  const toast = useToast();
  const [users, setUsers] = useState<AdminUser[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [blockBusyId, setBlockBusyId] = useState<string | null>(null);

  // §user blocking: flip the lockout; the API refuses admin targets (403).
  async function toggleBlocked(u: AdminUser) {
    setBlockBusyId(u.id);
    try {
      const res = await adminApi.patchUser(u.id, { blocked: !u.blocked });
      setUsers((prev) =>
        (prev ?? []).map((x) => (x.id === u.id ? { ...x, blocked: res.blocked } : x)),
      );
      toast.push(
        res.blocked ? `${u.email} blocked - they can no longer sign in` : `${u.email} unblocked`,
        "ok",
      );
    } catch (err) {
      toast.push(err instanceof Error ? err.message : "Failed", "err");
    } finally {
      setBlockBusyId(null);
    }
  }

  useEffect(() => {
    adminApi
      .users()
      .then(setUsers)
      .catch((err) => setError(err instanceof Error ? err.message : "Failed to load users."));
  }, []);

  const orgs = useMemo<OrgGroup[]>(() => {
    if (!users) return [];
    const map = new Map<string, OrgGroup>();
    for (const u of users) {
      const g = map.get(u.org_id) ?? { orgId: u.org_id, orgName: u.org_name, creditBalance: u.credit_balance, users: [] };
      g.users.push(u);
      map.set(u.org_id, g);
    }
    return [...map.values()];
  }, [users]);
  const { pageItems, page, pages, setPage } = usePager(orgs, PAGE_SIZE);

  if (error) return <Alert kind="error">{error}</Alert>;
  if (!users) return <Loading />;

  return (
    <div>
      <div className="page-head">
        <h1>Users & organizations</h1>
      </div>

      <div className="stack">
        {pageItems.map((org) => (
          <div key={org.orgId} className="card">
            <div className="between mb">
              <div>
                <h3 style={{ margin: 0 }}>{org.orgName}</h3>
                <span className="tiny faint mono">{org.orgId}</span>
                <div className="small" style={{ marginTop: "0.25rem" }}>
                  Balance: <strong>{formatCredits(org.creditBalance)} credits</strong>
                </div>
              </div>
              <CreditAdjust
                orgId={org.orgId}
                onDone={(bal) => {
                  setUsers((prev) =>
                    (prev ?? []).map((u) =>
                      u.org_id === org.orgId ? { ...u, credit_balance: bal } : u,
                    ),
                  );
                  toast.push(`Balance: ${formatCredits(bal)} credits`, "ok");
                }}
              />
            </div>
            <div className="table-wrap">
              <table className="data">
                <thead>
                  <tr>
                    <th>Email</th>
                    <th>Role</th>
                    <th>Verified</th>
                    <th>Access</th>
                    <th>Created</th>
                  </tr>
                </thead>
                <tbody>
                  {org.users.map((u) => (
                    <tr key={u.id}>
                      <td>{u.email}</td>
                      <td>
                        <span className="badge">{u.role}</span>
                      </td>
                      <td>
                        {u.email_verified ? (
                          <Badge label="verified" kind="finished" />
                        ) : (
                          <Badge label="unverified" kind="canceled" />
                        )}
                      </td>
                      <td>
                        {u.role === "admin" ? (
                          <span className="tiny faint">—</span>
                        ) : (
                          <div className="row gap-sm">
                            {u.blocked && <Badge label="blocked" kind="canceled" />}
                            <button
                              className="btn btn-sm"
                              disabled={blockBusyId === u.id}
                              title={u.blocked
                                ? "Let this user sign in again"
                                : "Refuse login and kill this user's sessions and API tokens"}
                              onClick={() => toggleBlocked(u)}
                            >
                              {blockBusyId === u.id ? <Spinner /> : u.blocked ? "Unblock" : "Block"}
                            </button>
                          </div>
                        )}
                      </td>
                      <td className="faint">{relTime(u.created_at)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        ))}
      </div>
      <Pager page={page} pages={pages} onPage={setPage} />
    </div>
  );
}

function CreditAdjust({ orgId, onDone }: { orgId: string; onDone: (balance: number) => void }) {
  const toast = useToast();
  const [amount, setAmount] = useState("");
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);

  async function adjust() {
    const val = Number(amount);
    if (!Number.isFinite(val) || val === 0) {
      toast.push("Enter a non-zero amount", "err");
      return;
    }
    setBusy(true);
    try {
      const res = await adminApi.adjustCredits(orgId, val, reason.trim() || "manual adjustment");
      onDone(res.credit_balance);
      setAmount("");
      setReason("");
    } catch (err) {
      toast.push(err instanceof Error ? err.message : "Failed", "err");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="row gap-sm">
      <input
        type="number"
        placeholder="± credits"
        value={amount}
        style={{ width: 110 }}
        onChange={(e) => setAmount(e.target.value)}
      />
      <input
        type="text"
        placeholder="reason"
        value={reason}
        style={{ width: 150 }}
        onChange={(e) => setReason(e.target.value)}
      />
      <button className="btn btn-sm" onClick={adjust} disabled={busy}>
        {busy ? <Spinner /> : "Adjust"}
      </button>
    </div>
  );
}
