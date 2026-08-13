import type { ProjectStatus } from "./types";
import type { SharedProjectKind } from "./now";
import "./StatusTimeline.css";

// The five-stage delivery timeline from the landing's project-dashboard card,
// mapped onto the real §8 status machine. Stages are the customer's mental
// model of progress; the status badge next to it keeps the exact state.
const AI_STAGES = ["Review", "Estimate", "Build", "Demo", "Delivery"] as const;
const DIRECT_STAGES = ["Review", "Quote", "Work", "Delivery"] as const;
const AUTO_STAGES = ["Watching", "Build", "Delivery"] as const;
const CHAT_STAGES = ["Open", "Human review", "Closed"] as const;

interface TimelinePos {
  /** Index of the pulsing "you are here" stage; null = nothing active. */
  active: number | null;
  /** Stages strictly before this index render as done. */
  done: number;
}

function aiPosition(status: ProjectStatus, demoExists: boolean): TimelinePos {
  switch (status) {
    case "draft":
      return { active: null, done: 0 };
    case "awaiting_review":
    case "awaiting_admin":
      return { active: 0, done: 0 };
    case "payment_due":
      return { active: 1, done: 1 };
    case "development":
      return { active: 2, done: 2 };
    case "awaiting_customer":
      // With a live demo the customer is reviewing it; without one the build
      // is parked (failed/timed out) and Build is still the active stage.
      return demoExists ? { active: 3, done: 3 } : { active: 2, done: 2 };
    case "finished":
      return { active: null, done: AI_STAGES.length };
    case "canceled":
      return { active: null, done: 0 };
  }
}

function autoPosition(status: ProjectStatus): TimelinePos {
  // The sentinel has no review/estimate phases: it idles in `development`
  // (watching), a parked or merge-awaiting build sits with the customer/admin,
  // and `finished` closes the engagement.
  switch (status) {
    case "draft":
      return { active: null, done: 0 };
    case "development":
      return { active: 0, done: 0 };
    case "awaiting_review":
    case "awaiting_admin":
    case "awaiting_customer":
    case "payment_due":
      return { active: 1, done: 1 };
    case "finished":
      return { active: null, done: AUTO_STAGES.length };
    case "canceled":
      return { active: null, done: 0 };
  }
}

function chatPosition(status: ProjectStatus): TimelinePos {
  // A chat lives in `development` (open) and closes at `finished`; any
  // awaiting_* state means the human consultant has the thread.
  switch (status) {
    case "draft":
      return { active: null, done: 0 };
    case "development":
      return { active: 0, done: 0 };
    case "awaiting_review":
    case "awaiting_admin":
    case "awaiting_customer":
    case "payment_due":
      return { active: 1, done: 1 };
    case "finished":
      return { active: null, done: CHAT_STAGES.length };
    case "canceled":
      return { active: null, done: 0 };
  }
}

function directPosition(status: ProjectStatus): TimelinePos {
  switch (status) {
    case "draft":
      return { active: null, done: 0 };
    case "awaiting_review":
    case "awaiting_admin":
      return { active: 0, done: 0 };
    case "awaiting_customer":
    case "payment_due":
      return { active: 1, done: 1 };
    case "development":
      return { active: 2, done: 2 };
    case "finished":
      return { active: null, done: DIRECT_STAGES.length };
    case "canceled":
      return { active: null, done: 0 };
  }
}

const CHECK = (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    <path d="m5 13 4 4L19 7" />
  </svg>
);

export function StatusTimeline({
  status,
  kind,
  demoExists = false,
  compact = false,
}: {
  status: ProjectStatus;
  kind: SharedProjectKind | string;
  demoExists?: boolean;
  compact?: boolean;
}) {
  const stages =
    kind === "direct_quote" ? DIRECT_STAGES
      : kind === "auto_dev" ? AUTO_STAGES
        : kind === "chat" ? CHAT_STAGES : AI_STAGES;
  const pos =
    kind === "direct_quote"
      ? directPosition(status)
      : kind === "auto_dev"
        ? autoPosition(status)
        : kind === "chat"
          ? chatPosition(status)
          : aiPosition(status, demoExists);
  const canceled = status === "canceled";

  return (
    <ol
      className={
        "status-timeline" + (compact ? " compact" : "") + (canceled ? " canceled" : "")
      }
      aria-label={`Progress: ${status.replace(/_/g, " ")}`}
    >
      {stages.map((label, i) => {
        const state = i < pos.done ? "t-done" : i === pos.active ? "t-active" : "";
        return (
          <li key={label} className={state}>
            <span className="t-node">{i < pos.done && CHECK}</span>
            <span className="t-label">{label}</span>
          </li>
        );
      })}
    </ol>
  );
}
