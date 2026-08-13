import { useEffect, useState } from "react";
import { billingApi, quotesApi } from "../lib/endpoints";
import { useAuth } from "../lib/auth";
import { useToast } from "../lib/toast";
import { Alert, Badge, Loading, Spinner, formatCredits, relTime } from "./ui";
import type { Quote } from "../types";

const STATUS_KIND: Record<string, string> = {
  draft: "draft",
  sent: "payment_due",
  paid: "finished",
  accepted: "finished",
  denied: "canceled",
  canceled: "canceled",
};

function decisionLabels(consultant: string): Record<string, string> {
  return {
    accepted: "Accepted",
    denied: "Denied",
    canceled: `Canceled by ${consultant}`,
  };
}

function formatSize(bytes: number): string {
  if (bytes >= 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  if (bytes >= 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${bytes} B`;
}

export default function QuotesTab({
  projectId,
  isAdmin,
  readOnly = false,
}: {
  projectId: string;
  isAdmin: boolean;
  // §sharing: a read-only share reads quotes but never decides them.
  readOnly?: boolean;
}) {
  const { settings } = useAuth();
  const consultant = settings?.consultant_first_name ?? "Consultant";
  const [quotes, setQuotes] = useState<Quote[] | null>(null);
  const [balance, setBalance] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);

  function load() {
    quotesApi
      .list(projectId)
      .then(setQuotes)
      .catch((err) => setError(err instanceof Error ? err.message : "Failed to load quotes."));
    billingApi
      .balance()
      .then((b) => setBalance(b.credit_balance))
      .catch(() => {});
  }
  useEffect(load, [projectId]);

  if (error) return <Alert kind="error">{error}</Alert>;
  if (!quotes) return <Loading />;

  return (
    <div>
      <div className="between mb">
        <p className="muted small" style={{ margin: 0 }}>
          Quotes proposed by {consultant} for this project. Accepting one is paid from your credit
          balance and commits {consultant} to deliver it to your repository.
        </p>
        {isAdmin && !creating && (
          <button className="btn btn-primary btn-sm" onClick={() => setCreating(true)}>
            + New quote
          </button>
        )}
      </div>

      {creating && (
        <QuoteCreateForm
          projectId={projectId}
          onDone={() => {
            setCreating(false);
            load();
          }}
          onCancel={() => setCreating(false)}
        />
      )}

      {quotes.length === 0 ? (
        <div className="card center muted">No quotes yet.</div>
      ) : (
        <div className="stack">
          {quotes.map((q) => (
            <QuoteCard
              key={q.id}
              projectId={projectId}
              quote={q}
              isAdmin={isAdmin}
              balance={balance}
              readOnly={readOnly}
              onChanged={load}
            />
          ))}
        </div>
      )}
    </div>
  );
}

function QuoteCreateForm({
  projectId,
  onDone,
  onCancel,
}: {
  projectId: string;
  onDone: () => void;
  onCancel: () => void;
}) {
  const toast = useToast();
  const [title, setTitle] = useState("");
  const [details, setDetails] = useState("");
  const [price, setPrice] = useState("");
  const [busy, setBusy] = useState(false);

  async function create(e: React.FormEvent) {
    e.preventDefault();
    const priceCredits = Number(price);
    if (!title.trim() || !details.trim() || !(priceCredits > 0)) return;
    setBusy(true);
    try {
      await quotesApi.create(projectId, {
        title: title.trim(),
        details: details.trim(),
        price_credits: priceCredits,
      });
      toast.push("Quote sent to the customer", "ok");
      onDone();
    } catch (err) {
      toast.push(err instanceof Error ? err.message : "Could not create the quote", "err");
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className="card mb" onSubmit={create}>
      <div className="section-title">New quote</div>
      <div className="field">
        <label>Title</label>
        <input type="text" value={title} onChange={(e) => setTitle(e.target.value)} />
      </div>
      <div className="field">
        <label>Details</label>
        <textarea
          value={details}
          rows={5}
          placeholder="Scope, deliverables, assumptions… editable at any time."
          onChange={(e) => setDetails(e.target.value)}
        />
      </div>
      <div className="field">
        <label>Price (credits)</label>
        <input
          type="number"
          min={0}
          step="any"
          value={price}
          onChange={(e) => setPrice(e.target.value)}
          style={{ maxWidth: 200 }}
        />
      </div>
      <div className="wizard-actions" style={{ marginTop: 0 }}>
        <button type="button" className="btn" onClick={onCancel}>
          Cancel
        </button>
        <button
          type="submit"
          className="btn btn-primary"
          disabled={busy || !title.trim() || !details.trim() || !(Number(price) > 0)}
        >
          {busy ? <Spinner /> : "Send quote"}
        </button>
      </div>
    </form>
  );
}

// Exported for the Requests thread view (§threads: a quote attached to a
// request renders as a card inside that request's thread).
export function QuoteCard({
  projectId,
  quote,
  isAdmin,
  balance,
  readOnly = false,
  onChanged,
}: {
  projectId: string;
  quote: Quote;
  isAdmin: boolean;
  balance: number | null;
  readOnly?: boolean;
  onChanged: () => void;
}) {
  const toast = useToast();
  const { settings } = useAuth();
  const consultant = settings?.consultant_first_name ?? "Consultant";
  const [editing, setEditing] = useState(false);
  const [title, setTitle] = useState(quote.title);
  const [details, setDetails] = useState(quote.details);
  const [price, setPrice] = useState(quote.price_credits?.toString() ?? "");
  const [comment, setComment] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [canceling, setCanceling] = useState(false);
  const [cancelComment, setCancelComment] = useState("");
  const [refundStr, setRefundStr] = useState(quote.price_credits?.toString() ?? "");

  const isCredit = quote.price_credits != null;
  const decided = ["accepted", "denied", "canceled"].includes(quote.status);
  const canDecide = isCredit && quote.status === "sent" && !readOnly;
  // Admin withdraws a quote he won't deliver; a refund applies only once paid.
  const canCancel = isAdmin && isCredit && ["sent", "accepted"].includes(quote.status);
  const wasPaid = quote.status === "accepted";
  const enoughCredits =
    balance != null && quote.price_credits != null && balance >= quote.price_credits;

  async function saveEdit() {
    setBusy("edit");
    try {
      await quotesApi.patch(quote.id, {
        title: title.trim() || undefined,
        details: details.trim() || undefined,
        ...(decided || !isCredit ? {} : { price_credits: Number(price) || undefined }),
      });
      toast.push("Quote updated", "ok");
      setEditing(false);
      onChanged();
    } catch (err) {
      toast.push(err instanceof Error ? err.message : "Failed", "err");
    } finally {
      setBusy(null);
    }
  }

  async function decide(action: "accept" | "deny") {
    if (isAdmin) {
      // The decision belongs to the customer; make an admin think twice.
      const ok = window.confirm(
        `You are the admin: quotes are meant to be ${
          action === "accept" ? "accepted" : "denied"
        } by the customer, not on their behalf.` +
          (action === "accept"
            ? ` Accepting will charge the customer's organization ${formatCredits(quote.price_credits!)} credits immediately.`
            : "") +
          " Proceed anyway?",
      );
      if (!ok) return;
    } else if (
      action === "accept" &&
      !window.confirm(
        `Accept "${quote.title}" for ${formatCredits(quote.price_credits!)} credits? ` +
          "The amount is deducted from your balance immediately.",
      )
    ) {
      return;
    }
    setBusy(action);
    try {
      await (action === "accept"
        ? quotesApi.accept(projectId, quote.id, comment.trim() || undefined)
        : quotesApi.deny(projectId, quote.id, comment.trim() || undefined));
      toast.push(action === "accept" ? `Quote accepted - ${consultant} is on it.` : "Quote denied.", "ok");
      setComment("");
      onChanged();
    } catch (err) {
      toast.push(err instanceof Error ? err.message : "Failed", "err");
    } finally {
      setBusy(null);
    }
  }

  async function cancelQuote() {
    const refund = wasPaid ? Number(refundStr) : 0;
    if (wasPaid && (!Number.isFinite(refund) || refund < 0 || refund > quote.price_credits!)) {
      toast.push(`Refund must be between 0 and ${formatCredits(quote.price_credits!)} credits`, "err");
      return;
    }
    setBusy("cancel");
    try {
      await quotesApi.cancel(quote.id, {
        comment: cancelComment.trim() || undefined,
        ...(wasPaid ? { refund_credits: refund } : {}),
      });
      toast.push(
        wasPaid ? `Quote canceled - ${formatCredits(refund)} credits refunded.` : "Quote canceled.",
        "ok",
      );
      setCanceling(false);
      onChanged();
    } catch (err) {
      toast.push(err instanceof Error ? err.message : "Failed", "err");
    } finally {
      setBusy(null);
    }
  }

  async function upload(files: FileList | null) {
    if (!files || files.length === 0) return;
    setBusy("upload");
    try {
      await quotesApi.uploadAttachments(quote.id, [...files]);
      toast.push("Attachment(s) added", "ok");
      onChanged();
    } catch (err) {
      toast.push(err instanceof Error ? err.message : "Upload failed", "err");
    } finally {
      setBusy(null);
    }
  }

  async function removeAttachment(attId: string) {
    setBusy(`del:${attId}`);
    try {
      await quotesApi.deleteAttachment(quote.id, attId);
      onChanged();
    } catch (err) {
      toast.push(err instanceof Error ? err.message : "Failed", "err");
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="card">
      <div className="between">
        <div className="row gap-sm">
          <strong>{quote.title || "Quote"}</strong>
          <Badge label={quote.status} kind={STATUS_KIND[quote.status] ?? "draft"} />
        </div>
        <div className="nowrap" style={{ textAlign: "right" }}>
          <div className="grad-text" style={{ fontSize: "1.15rem", fontWeight: 600 }}>
            {isCredit
              ? `${formatCredits(quote.price_credits!)} credits`
              : `${quote.amount.toLocaleString()} ${quote.currency}`}
          </div>
          <div className="tiny faint">{relTime(quote.created_at)}</div>
        </div>
      </div>

      {editing ? (
        <div className="mt">
          <div className="field">
            <label>Title</label>
            <input type="text" value={title} onChange={(e) => setTitle(e.target.value)} />
          </div>
          <div className="field">
            <label>Details</label>
            <textarea value={details} rows={5} onChange={(e) => setDetails(e.target.value)} />
          </div>
          {isCredit && (
            <div className="field">
              <label>Price (credits){decided ? " - locked after decision" : ""}</label>
              <input
                type="number"
                min={0}
                step="any"
                value={price}
                disabled={decided}
                onChange={(e) => setPrice(e.target.value)}
                style={{ maxWidth: 200 }}
              />
            </div>
          )}
          <div className="row gap-sm">
            <button className="btn btn-primary btn-sm" onClick={saveEdit} disabled={busy === "edit"}>
              {busy === "edit" ? <Spinner /> : "Save"}
            </button>
            <button className="btn btn-sm" onClick={() => setEditing(false)}>
              Cancel
            </button>
          </div>
        </div>
      ) : (
        quote.details && (
          <p className="small mt" style={{ whiteSpace: "pre-wrap", marginBottom: 0 }}>
            {quote.details}
          </p>
        )
      )}

      {quote.attachments.length > 0 && (
        <div className="mt">
          <label>Attachments</label>
          <div className="stack" style={{ gap: "0.3rem" }}>
            {quote.attachments.map((a) => (
              <div key={a.id} className="row gap-sm tiny">
                <a href={quotesApi.attachmentUrl(projectId, quote.id, a.id)} download={a.filename}>
                  {a.filename}
                </a>
                <span className="faint">{formatSize(a.size_bytes)}</span>
                {isAdmin && (
                  <button
                    className="btn btn-ghost btn-sm"
                    onClick={() => removeAttachment(a.id)}
                    disabled={busy === `del:${a.id}`}
                    title="Remove attachment"
                  >
                    ✕
                  </button>
                )}
              </div>
            ))}
          </div>
        </div>
      )}

      {!isCredit && quote.payment_link && quote.status === "sent" && (
        <p className="small mt">
          <a href={quote.payment_link} target="_blank" rel="noreferrer">
            Pay via Stripe →
          </a>
        </p>
      )}

      {decided && (
        <p className="tiny faint mt">
          {decisionLabels(consultant)[quote.status]} {relTime(quote.decided_at)}
          {quote.refunded_credits != null &&
            ` - ${formatCredits(quote.refunded_credits)} of ${formatCredits(quote.price_credits!)} paid credits refunded`}
          {quote.decision_comment ? ` - "${quote.decision_comment}"` : ""}
        </p>
      )}

      {canDecide && (
        <div className="mt" style={{ borderTop: "1px solid var(--border)", paddingTop: "0.75rem" }}>
          <div className="field">
            <label>Comment (optional, sent with your decision)</label>
            <input
              type="text"
              value={comment}
              maxLength={4000}
              onChange={(e) => setComment(e.target.value)}
            />
          </div>
          <div className="row gap-sm">
            <button
              className="btn btn-primary btn-sm"
              onClick={() => decide("accept")}
              disabled={busy != null || !enoughCredits}
              title={
                enoughCredits
                  ? undefined
                  : "Your credit balance doesn't cover this quote - top up first."
              }
            >
              {busy === "accept" ? <Spinner /> : `Accept for ${formatCredits(quote.price_credits!)} credits`}
            </button>
            <button
              className="btn btn-sm"
              onClick={() => decide("deny")}
              disabled={busy != null}
            >
              {busy === "deny" ? <Spinner /> : "Deny"}
            </button>
          </div>
          {!enoughCredits && balance != null && (
            <p className="tiny faint mt" style={{ marginBottom: 0 }}>
              Your balance is {formatCredits(balance)} credits; this quote costs{" "}
              {formatCredits(quote.price_credits!)}. <a href="/billing">Top up</a> to accept it.
            </p>
          )}
        </div>
      )}

      {isAdmin && !editing && (
        <div className="row gap-sm mt">
          <button className="btn btn-sm" onClick={() => setEditing(true)}>
            Edit
          </button>
          <label className="btn btn-sm" style={{ marginBottom: 0 }}>
            {busy === "upload" ? <Spinner /> : "Add files…"}
            <input
              type="file"
              multiple
              style={{ display: "none" }}
              onChange={(e) => {
                upload(e.target.files);
                e.target.value = "";
              }}
            />
          </label>
          {canCancel && !canceling && (
            <button className="btn btn-danger btn-sm" onClick={() => setCanceling(true)}>
              Cancel quote…
            </button>
          )}
        </div>
      )}

      {canceling && (
        <div className="mt" style={{ borderTop: "1px solid var(--border)", paddingTop: "0.75rem" }}>
          <p className="small" style={{ marginTop: 0 }}>
            Cancel this quote because you won't deliver it.
            {wasPaid
              ? " The customer already paid it - choose how much to refund."
              : " It was not accepted yet, so there is nothing to refund."}
          </p>
          {wasPaid && (
            <div className="field">
              <label>
                Refund (0 to {formatCredits(quote.price_credits!)} credits)
              </label>
              <input
                type="number"
                min={0}
                max={quote.price_credits!}
                step="any"
                value={refundStr}
                onChange={(e) => setRefundStr(e.target.value)}
                style={{ maxWidth: 200 }}
              />
            </div>
          )}
          <div className="field">
            <label>Reason (optional, shared with the customer)</label>
            <input
              type="text"
              value={cancelComment}
              maxLength={4000}
              onChange={(e) => setCancelComment(e.target.value)}
            />
          </div>
          <div className="row gap-sm">
            <button className="btn btn-danger btn-sm" onClick={cancelQuote} disabled={busy === "cancel"}>
              {busy === "cancel" ? <Spinner /> : "Confirm cancellation"}
            </button>
            <button className="btn btn-sm" onClick={() => setCanceling(false)} disabled={busy === "cancel"}>
              Keep the quote
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
