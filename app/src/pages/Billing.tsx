import { useEffect, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { billingApi } from "../lib/endpoints";
import { ApiError } from "../lib/api";
import { useAuth } from "../lib/auth";
import { useToast } from "../lib/toast";
import { Alert, Loading, Pager, Spinner, formatCredits, formatCreditsExact, relTime, usePager } from "../components/ui";
import TopupThanks from "../components/TopupThanks";
import type { Transaction } from "../types";

// A topup counts as "just paid" when it landed within this window; Stripe's
// redirect beats the webhook, so the success flow also polls a few times.
const TOPUP_RECENT_MS = 15 * 60 * 1000;
const TOPUP_POLL_MS = 2500;
const TOPUP_POLL_MAX = 6;
const TXN_PAGE_SIZE = 12;

function recentTopup(txns: Transaction[]): Transaction | undefined {
  // Transactions come newest-first from the API.
  return txns.find(
    (t) =>
      t.kind === "topup" &&
      t.amount > 0 &&
      Date.now() - new Date(t.created_at).getTime() < TOPUP_RECENT_MS,
  );
}

export default function Billing() {
  const toast = useToast();
  const { settings } = useAuth();
  const consultant = settings?.consultant_first_name ?? "Consultant";
  const [params, setParams] = useSearchParams();
  const checkoutStatus = useRef(params.get("status")).current;
  const [balance, setBalance] = useState<{
    credit_balance: number;
    currency: string;
    min_topup: number;
  } | null>(null);
  const [txns, setTxns] = useState<Transaction[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [amount, setAmount] = useState("50");
  const [busy, setBusy] = useState(false);
  const [stripeDown, setStripeDown] = useState(false);
  const [thanks, setThanks] = useState<{
    amount: number;
    balance: number;
    reference: string;
  } | null>(null);
  // Transactions come newest-first; paginate client-side over the single fetch.
  const { pageItems, page, pages, setPage } = usePager(txns ?? [], TXN_PAGE_SIZE);

  useEffect(() => {
    Promise.all([billingApi.balance(), billingApi.transactions()])
      .then(([b, t]) => {
        setBalance(b);
        setTxns(t);
      })
      .catch((err) => setError(err instanceof Error ? err.message : "Failed to load billing."));
  }, []);

  // Stripe redirects back with ?status=success|canceled. Strip the param so a
  // refresh doesn't replay the flow, then confirm the topup actually exists
  // before celebrating (the webhook may land a few seconds after the redirect).
  useEffect(() => {
    if (!checkoutStatus) return;
    setParams({}, { replace: true });
    if (checkoutStatus === "canceled") {
      toast.push("Checkout canceled. You were not charged.", "info");
      return;
    }
    if (checkoutStatus !== "success") return;

    let cancelled = false;
    let timer = 0;
    let attempts = 0;
    async function check() {
      attempts += 1;
      try {
        const t = await billingApi.transactions();
        if (cancelled) return;
        setTxns(t);
        const hit = recentTopup(t);
        if (hit) {
          const b = await billingApi.balance();
          if (cancelled) return;
          setBalance(b);
          setThanks({
            amount: hit.amount,
            balance: b.credit_balance,
            reference: hit.id.slice(0, 8),
          });
          return;
        }
      } catch {
        // transient; keep polling
      }
      if (!cancelled && attempts < TOPUP_POLL_MAX) {
        timer = window.setTimeout(check, TOPUP_POLL_MS);
      }
    }
    check();
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [checkoutStatus]);

  async function topup(e: React.FormEvent) {
    e.preventDefault();
    if (!balance) return;
    const val = Number(amount);
    if (!Number.isFinite(val) || val < balance.min_topup) {
      toast.push(
        `Enter an amount of at least ${formatCreditsExact(balance.min_topup)} ${balance.currency}`,
        "err",
      );
      return;
    }
    setBusy(true);
    setStripeDown(false);
    try {
      const { checkout_url } = await billingApi.topup(val);
      window.location.assign(checkout_url);
    } catch (err) {
      if (err instanceof ApiError && err.status === 503) {
        setStripeDown(true);
      } else {
        toast.push(err instanceof Error ? err.message : "Top-up failed", "err");
      }
    } finally {
      setBusy(false);
    }
  }

  if (error) return <Alert kind="error">{error}</Alert>;
  if (!balance || !txns) return <Loading />;

  return (
    <div>
      {thanks && <TopupThanks {...thanks} onClose={() => setThanks(null)} />}
      <div className="page-head">
        <h1>Billing</h1>
      </div>

      <div className="grid-2">
        <div className="card">
          <div className="section-title">Balance</div>
          <div style={{ fontSize: "2.2rem" }} className="grad-text">
            {formatCredits(balance.credit_balance)}
          </div>
          <div className="muted small">credits ({balance.currency})</div>
        </div>

        <div className="card">
          <div className="section-title">Top up</div>
          {stripeDown && (
            <Alert kind="warn">
              Stripe isn't configured in this environment, so top-ups are unavailable. Contact
              {" "}{consultant} to add credits manually.
            </Alert>
          )}
          <form onSubmit={topup}>
            <div className="field">
              <label>Amount ({balance.currency})</label>
              <input
                type="number"
                min={balance.min_topup}
                value={amount}
                onChange={(e) => setAmount(e.target.value)}
              />
              <div className="muted small">
                Minimum {formatCreditsExact(balance.min_topup)} {balance.currency}.
              </div>
            </div>
            <button type="submit" className="btn btn-primary btn-block" disabled={busy}>
              {busy ? <Spinner /> : "Continue to checkout"}
            </button>
          </form>
        </div>
      </div>

      <div className="card mt">
        <div className="section-title">Transactions</div>
        {txns.length === 0 ? (
          <p className="muted small">No transactions yet.</p>
        ) : (
          <div className="table-wrap">
            <table className="data">
              <thead>
                <tr>
                  <th>Date</th>
                  <th>Kind</th>
                  <th>Project</th>
                  <th style={{ textAlign: "right" }}>Amount</th>
                </tr>
              </thead>
              <tbody>
                {pageItems.map((t) => (
                  <tr key={t.id}>
                    <td className="faint">{relTime(t.created_at)}</td>
                    <td>
                      <span className="badge">{t.kind}</span>
                    </td>
                    <td className="mono tiny">
                      {t.project_id ? (
                        <Link to={`/projects/${t.project_id}`} title="Open the project">
                          {t.project_id}
                        </Link>
                      ) : (
                        "-"
                      )}
                    </td>
                    <td
                      style={{
                        textAlign: "right",
                        color: t.amount >= 0 ? "var(--ok)" : "var(--danger)",
                      }}
                    >
                      {t.amount >= 0 ? "+" : ""}
                      {formatCreditsExact(t.amount)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            <Pager page={page} pages={pages} onPage={setPage} />
          </div>
        )}
      </div>
    </div>
  );
}
