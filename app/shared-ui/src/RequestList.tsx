import { useState } from "react";
import "./RequestList.css";
import { PrChips } from "./PrChips";
import { validPrRefs } from "./prs";
import { REQUEST_TYPE_LABELS, requestStatusKind, type SharedRequest } from "./requests";

// The searchable, paginated request-card list, shared between the spoke
// RequestsTab and the hub customer console so both render the IDENTICAL list.
// Pure presentational: the host owns the data + navigation and passes the
// requests + an onSelect callback. Self-contained (own search/pagination state +
// co-located CSS, no app-lib imports).

function fmtTime(iso: string): string {
  const d = new Date(iso);
  if (isNaN(d.getTime())) return "";
  return d.toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export function RequestList({
  requests,
  onSelect,
  pageSize = 8,
  emptyLabel = "No requests yet.",
}: {
  requests: SharedRequest[];
  onSelect: (id: string) => void;
  pageSize?: number;
  emptyLabel?: string;
}) {
  const [query, setQuery] = useState("");
  const [pageRaw, setPage] = useState(0);

  if (requests.length === 0) {
    return <div className="rq-empty">{emptyLabel}</div>;
  }

  const q = query.trim().toLowerCase();
  const filtered = q
    ? requests.filter((r) =>
        [r.title, REQUEST_TYPE_LABELS[r.type] ?? r.type, r.status, r.handling]
          .join(" ")
          .toLowerCase()
          .includes(q),
      )
    : requests;
  const pages = Math.max(1, Math.ceil(filtered.length / pageSize));
  const page = Math.min(pageRaw, pages - 1);
  const items = filtered.slice(page * pageSize, (page + 1) * pageSize);

  return (
    <div className="rq">
      <input
        className="rq-search"
        type="text"
        placeholder="Search requests (title, type, status)…"
        value={query}
        onChange={(e) => {
          setQuery(e.target.value);
          setPage(0);
        }}
      />
      {filtered.length === 0 ? (
        <div className="rq-empty">No requests match your search.</div>
      ) : (
        <div className="rq-list">
          {items.map((r) => (
            <button key={r.id} type="button" className="rq-card" onClick={() => onSelect(r.id)}>
              <div className="rq-card-main">
                <div className="rq-badges">
                  <span className="rq-badge">{REQUEST_TYPE_LABELS[r.type] ?? r.type}</span>
                  <span className="rq-badge">{r.handling === "ai" ? "AI" : "Human expert"}</span>
                  <span className={`rq-badge rq-status-${requestStatusKind(r.status)}`}>
                    {r.status.replace(/_/g, " ")}
                  </span>
                </div>
                <strong className="rq-title" title={r.title}>
                  {r.title}
                </strong>
                <PrChips refs={validPrRefs(r.pr_urls)} stop />
              </div>
              <div className="rq-right">
                {r.price_credits != null && (
                  <span className="rq-price">{Math.floor(r.price_credits)} cr</span>
                )}
                {r.tokens_consumed ? (
                  <span className="rq-tokens">{r.tokens_consumed.toLocaleString()} tokens</span>
                ) : null}
                <span className="rq-time">{fmtTime(r.created_at)}</span>
              </div>
            </button>
          ))}
          {pages > 1 && (
            <div className="rq-pager">
              <button
                type="button"
                className="rq-pbtn"
                disabled={page === 0}
                onClick={() => setPage(page - 1)}
              >
                ← Newer
              </button>
              <span className="rq-page">
                {page + 1} / {pages}
              </span>
              <button
                type="button"
                className="rq-pbtn"
                disabled={page >= pages - 1}
                onClick={() => setPage(page + 1)}
              >
                Older →
              </button>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
