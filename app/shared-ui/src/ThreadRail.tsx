import "./ThreadRail.css";
import { REQUEST_TYPE_LABELS, requestStatusKind, type SharedRequest } from "./requests";

// §threads: the orchestrator chat's rail of work threads - one chip per request
// with a status dot (pulsing while that request's build is in flight), deep-
// linking into the request's thread. Pure presentational like every shared-ui
// export: the host passes the requests (already sorted) and navigation
// callbacks; there is no data access here.
export function ThreadRail({
  requests,
  buildingId = null,
  buildingIds = null,
  buildingKind = "building",
  onOpen,
  onAll,
  max = 4,
}: {
  requests: SharedRequest[];
  // id of the request whose PRIMARY build is currently in flight, if any
  // (kept as-is for hosts pinned before §parallel-builds MR4)
  buildingId?: string | null;
  // §parallel-builds MR4 (additive): ids of EVERY request with a build in
  // flight - each of their chips pulses. The primary's chip keeps buildingKind;
  // sibling chips always read "building". Absent -> [buildingId].
  buildingIds?: string[] | null;
  // what the primary in-flight build is doing: agent working ("building") vs
  // waiting on the customer's merge ("merge", rendered in the warn color)
  buildingKind?: "building" | "merge";
  onOpen: (id: string) => void;
  // opens the full request list (e.g. the Requests tab); also shown when
  // more requests exist than fit the rail
  onAll?: () => void;
  max?: number;
}) {
  if (requests.length === 0) return null;
  const active = requests.filter((r) => r.status !== "rejected");
  if (active.length === 0) return null;
  const open = active.filter((r) => r.status !== "done");
  const shown = (open.length > 0 ? open : active).slice(0, max);
  const hidden = active.length - shown.length;
  const building = new Set(buildingIds ?? (buildingId ? [buildingId] : []));
  return (
    <div className="tr-rail" role="navigation" aria-label="Work threads">
      {shown.map((r) => (
        <button
          key={r.id}
          type="button"
          className={`tr-chip${building.has(r.id) ? " tr-building" : ""}`}
          onClick={() => onOpen(r.id)}
          title={`${REQUEST_TYPE_LABELS[r.type] ?? r.type} - ${r.status.replace(/_/g, " ")}`}
        >
          <span className={`tr-dot tr-${requestStatusKind(r.status)}`} />
          <span className="tr-title">{r.title}</span>
          {building.has(r.id) && (
            <span
              className={`tr-live${
                r.id === buildingId && buildingKind === "merge" ? " tr-live-merge" : ""
              }`}
            >
              {r.id === buildingId && buildingKind === "merge" ? "merge" : "building"}
            </span>
          )}
        </button>
      ))}
      {(hidden > 0 || onAll) && (
        <button type="button" className="tr-chip tr-more" onClick={onAll} disabled={!onAll}>
          {hidden > 0 ? `+${hidden} more` : "All requests"} →
        </button>
      )}
    </div>
  );
}
