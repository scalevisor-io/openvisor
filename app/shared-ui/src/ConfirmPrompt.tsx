import "./ConfirmPrompt.css";

// The ✓/✗ one-click decision panel under a §12 request-proposal ack ("I read
// this as a ... request"), shared between the spoke SPA chat and the hub
// customer console. Pure presentational: the host owns the start/cancel API
// calls (deterministic actions, no classifier round-trip) and those actions
// post the canned replies that freeze this panel via confirmState. Once
// resolved it shows the outcome; a free-form typed reply resolves it silently.
export function ConfirmPrompt({
  active,
  resolution,
  busy = false,
  onApprove,
  onDismiss,
}: {
  active: boolean;
  resolution: "approved" | "dismissed" | null;
  busy?: boolean;
  onApprove: () => void;
  onDismiss: () => void;
}) {
  if (!active) {
    if (!resolution) return null;
    return (
      <div className="cp cp-done">
        {resolution === "approved" ? (
          <span className="cp-outcome cp-ok">✓ Going ahead</span>
        ) : (
          <span className="cp-outcome cp-no">✕ Not now</span>
        )}
      </div>
    );
  }
  return (
    <div className="cp" role="group">
      <button
        type="button"
        className="cp-btn cp-approve"
        disabled={busy}
        onClick={onApprove}
        title="Start this request now"
      >
        <span className="cp-ico">✓</span> Go ahead
      </button>
      <button
        type="button"
        className="cp-btn cp-dismiss"
        disabled={busy}
        onClick={onDismiss}
        title="Drop this request - you can ask again anytime"
      >
        <span className="cp-ico">✕</span> Not now
      </button>
    </div>
  );
}
