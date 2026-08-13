import type { Ref, UIEvent } from "react";
import "./BuildFeed.css";

// The live build-activity feed (§14.8), shared between the Openvisor spoke SPA
// and the Scalevisor hub customer console so both render the IDENTICAL component
// - same SVG icons, current-item pulse, "thinking" caret, elapsed offsets. Pure
// presentational: the host owns fetching and passes the event list + a `live`
// flag (true while a run is in flight → the last item gets the pulsing current
// treatment).

export interface BuildFeedEvent {
  ts: number | null; // epoch seconds; used for the elapsed "+m:ss" offset
  kind: string;
  title: string;
  detail?: string | null;
}

function FeedIcon({ kind }: { kind: string }) {
  const p = (d: string) => (
    <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5"
         strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d={d} />
    </svg>
  );
  switch (kind) {
    case "command":
      return p("M2.5 3.5l4 4.5-4 4.5M8.5 12.5h5");
    case "edit":
      return p("M11.5 2.5l2 2L5 13l-2.7.7L3 11z");
    case "read":
      return p("M4 2.5h6l2.5 2.5v8.5H4zM10 2.5V5h2.5");
    case "browse":
      return p("M8 1.5a6.5 6.5 0 100 13 6.5 6.5 0 000-13zM1.5 8h13M8 1.5c-4 3.8-4 9.2 0 13M8 1.5c4 3.8 4 9.2 0 13");
    case "think":
      return p("M8 1.5l1.4 3.9 3.9 1.4-3.9 1.4L8 12.1 6.6 8.2 2.7 6.8l3.9-1.4z");
    case "plan":
      return p("M3 4h2M7 4h6M3 8h2M7 8h6M3 12h2M7 12h6");
    case "git":
      return p("M5 3a1.5 1.5 0 100 3 1.5 1.5 0 000-3zM5 10a1.5 1.5 0 100 3 1.5 1.5 0 000-3zM11 5a1.5 1.5 0 100 3 1.5 1.5 0 000-3zM5 6v4M11 8c0 2-2 3.5-4.5 3.5");
    case "scan":
      return p("M8 1.5l5 2v4c0 3.2-2.1 5.7-5 7-2.9-1.3-5-3.8-5-7v-4zM5.7 8l1.6 1.6L10.5 6");
    case "usage":
      return p("M2.5 13.5v-4M6.2 13.5v-7M9.8 13.5V4M13.5 13.5V7.5");
    case "error":
      return p("M8 1.8l6.5 11.4H1.5zM8 6.5V10M8 11.7v.1");
    case "finish":
      return p("M2.5 8.5L6.5 12.5 13.5 4");
    default:
      return p("M8 3v5l3.5 2M8 14.5a6.5 6.5 0 110-13 6.5 6.5 0 010 13z");
  }
}

function fmtElapsed(ms: number): string {
  const s = Math.max(0, Math.floor(ms / 1000));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  return h > 0
    ? `${h}:${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}`
    : `${m}:${String(sec).padStart(2, "0")}`;
}

function fmtOffset(ts: number | null, t0: number | null): string {
  if (ts == null || t0 == null) return "";
  return `+${fmtElapsed((ts - t0) * 1000)}`;
}

export function BuildFeed({
  events,
  live = false,
  emptyLabel = "Provisioning the sandbox",
  scrollRef,
  onScroll,
}: {
  events: BuildFeedEvent[];
  live?: boolean;
  emptyLabel?: string;
  // The host may keep its own scroll container ref + handler (e.g. the spoke's
  // scroll-pinning); both are forwarded to the .agent-feed element.
  scrollRef?: Ref<HTMLDivElement>;
  onScroll?: (e: UIEvent<HTMLDivElement>) => void;
}) {
  const t0 = events.length > 0 ? events[0].ts : null;
  return (
    <div className="agent-feed" ref={scrollRef} onScroll={onScroll}>
      {events.length === 0 ? (
        <div className="feed-empty">
          {emptyLabel}
          <span className="dots">
            <span>.</span>
            <span>.</span>
            <span>.</span>
          </span>
        </div>
      ) : (
        <ol>
          {events.map((e, i) => (
            <li
              key={i}
              className={`feed-item feed-${e.kind}${
                live && i === events.length - 1 ? " feed-current" : ""
              }`}
            >
              <span className="feed-icon">
                <FeedIcon kind={e.kind} />
              </span>
              <div className="feed-body">
                <div className="feed-title">{e.title}</div>
                {e.detail && <div className="feed-detail">{e.detail}</div>}
              </div>
              <span className="feed-ts">{fmtOffset(e.ts, t0)}</span>
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}
