import { useState } from "react";
import { MessageBody } from "./MessageBody";
import "./PlanDisclosure.css";

// §plan visibility: the plan-approval message carries only an excerpt - chat
// messages are immutable, and a full implementation plan runs to thousands of
// words. The plan the customer is actually agreeing to is offered in full right
// beside the question, so "approve" is never a decision taken on a third of the
// text. Shared by the spoke SPA chat and the hub customer console.
export function PlanDisclosure({
  plan,
  excerptChars,
}: {
  plan: string;
  excerptChars: number;
}) {
  const [open, setOpen] = useState(false);
  // Nothing to reveal when the message already showed the whole thing.
  if (!plan || plan.length <= excerptChars) return null;
  return (
    <div className="pd">
      <button
        type="button"
        className="pd-toggle"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
      >
        {open ? "Hide the full plan" : "Read the full plan"}
        <span className="pd-count">{plan.length.toLocaleString()} characters</span>
      </button>
      {open && (
        <div className="pd-body">
          <MessageBody text={plan} />
        </div>
      )}
    </div>
  );
}
