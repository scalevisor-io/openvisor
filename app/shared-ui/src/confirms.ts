import type { MessageConfirmMeta, SharedMessage } from "./types";

// §12 request-confirmation helpers, shared so the spoke SPA and the hub console
// derive the same semantics from the message stream (both render the same
// immutable messages; "resolved" is a pure function of what follows, exactly
// like questions.ts). The ✓/✗ actions post canned replies - start_request posts
// "Go ahead", cancel_request posts "Not now" - so the panel freezes on the
// matching human message whichever surface performed the action.

export const CONFIRM_APPROVE_LABEL = "Go ahead";
export const CONFIRM_DISMISS_LABEL = "Not now";

// Validate a message's meta into a renderable confirmation, or null. Defensive:
// meta travels through several serializers and older records carry none.
export function messageConfirm(m: SharedMessage): MessageConfirmMeta | null {
  const meta = m.meta as MessageConfirmMeta | null | undefined;
  if (!meta || meta.kind !== "confirm_request") return null;
  if (typeof meta.request_id !== "string" || meta.request_id.length === 0) return null;
  return { kind: "confirm_request", request_id: meta.request_id };
}

export interface ConfirmState {
  // The proposal still awaits a decision: no human message follows the ack.
  active: boolean;
  // How it resolved when the following reply matched a canned label; a typed
  // free-form reply resolves it without a highlighted outcome.
  resolution: "approved" | "dismissed" | null;
}

export function confirmState(messages: SharedMessage[], confirmId: string): ConfirmState {
  const idx = messages.findIndex((m) => m.id === confirmId);
  if (idx < 0) return { active: false, resolution: null };
  const reply = messages
    .slice(idx + 1)
    .find((m) => m.author === "customer" || m.author === "admin");
  if (!reply) return { active: true, resolution: null };
  const body = reply.body.trim();
  if (body === CONFIRM_APPROVE_LABEL) return { active: false, resolution: "approved" };
  if (body === CONFIRM_DISMISS_LABEL) return { active: false, resolution: "dismissed" };
  return { active: false, resolution: null };
}
