import type { NowActionId, ProjectNow } from "./now";
import "./NowPanel.css";

// Renders a projectNow() result: owner chip, headline, one primary action.
// Pure and transport-agnostic - the host passes handlers for the action ids
// its API supports; actions without a handler simply don't render, so the
// spoke (billing page for funds) and the hub (in-place funding) diverge
// without forking the panel. Server-side guards stay authoritative: `meta`
// lets a host disable an action and explain why (e.g. dev_can_resume).
export interface NowActionMeta {
  disabled?: boolean;
  title?: string;
  label?: string; // host override, e.g. "Fund 96 credits"
  sublabel?: string; // small adjunct, e.g. "· 120 credits"
}

const OWNER_LABEL: Record<ProjectNow["owner"], string> = {
  you: "You",
  consultant: "Consultant",
  agent: "Agent",
  done: "Done",
  none: "",
};

export function NowPanel({
  now,
  actions,
  meta = {},
  busy = null,
  consultant,
}: {
  now: ProjectNow;
  actions: Partial<Record<NowActionId, () => void>>;
  meta?: Partial<Record<NowActionId, NowActionMeta>>;
  /** Action id currently in flight; its button shows a working state. */
  busy?: NowActionId | null;
  /** Consultant display name for the owner chip. */
  consultant?: string;
}) {
  const ownerText =
    now.owner === "consultant" && consultant ? consultant : OWNER_LABEL[now.owner];
  const render = (id: NowActionId, label: string, primary: boolean) => {
    const handler = actions[id];
    if (!handler) return null;
    const m = meta[id] ?? {};
    return (
      <button
        key={id}
        type="button"
        className={`btn${primary ? " btn-primary" : ""} now-btn`}
        disabled={busy != null || m.disabled}
        title={m.title}
        onClick={handler}
      >
        {busy === id ? "…" : (m.label ?? label)}
        {m.sublabel ? <span className="now-sublabel">{m.sublabel}</span> : null}
      </button>
    );
  };

  return (
    <section className={`now-panel now-owner-${now.owner}`} aria-live="polite">
      {now.owner !== "none" && (
        <span className="now-chip" title={`Who has the ball: ${ownerText}`}>
          <i className="now-dot" aria-hidden="true" />
          {ownerText}
        </span>
      )}
      <div className="now-copy">
        <h2 className="now-headline">{now.headline}</h2>
        {now.body && <p className="now-body">{now.body}</p>}
      </div>
      <div className="now-actions">
        {now.primary && render(now.primary.id, now.primary.label, true)}
        {now.secondary.map((a) => render(a.id, a.label, false))}
      </div>
    </section>
  );
}
