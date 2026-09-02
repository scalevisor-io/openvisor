// Shared project types + the ProjectApi contract (§hub pass-through shared-ui).
// Each app implements ProjectApi against its own transport - the spoke's own
// REST, the hub's /api/customer/me/* proxy - so the shared components never
// import an app-specific lib/.

export type MessageAuthor = "customer" | "admin" | "agent" | "system";

// Platform-authored structured payload on a message. Two kinds today: the §12
// clarifying question the chat classifier asks (options the customer answers
// with one click, free text always allowed), and the request-proposal ack's
// one-click ✓/✗ confirmation (wired to the deterministic start/cancel request
// actions - no classifier round-trip).
export interface MessageQuestionOption {
  label: string;
  description?: string | null;
}

export interface MessageQuestionMeta {
  kind: "question";
  question: string;
  options: MessageQuestionOption[];
  allow_free_text?: boolean;
  // §plan visibility: this question is the plan approval, so the host offers
  // the project's full plan next to it (the body holds only an excerpt).
  plan?: boolean;
}

export interface MessageConfirmMeta {
  kind: "confirm_request";
  request_id: string;
}

export interface SharedMessage {
  id: string;
  thread: string;
  author: MessageAuthor | string;
  body: string;
  meta?: MessageQuestionMeta | MessageConfirmMeta | Record<string, unknown> | null;
  created_at: string;
}

export type ProjectStatus =
  | "draft"
  | "awaiting_review"
  | "payment_due"
  | "development"
  | "awaiting_customer"
  | "awaiting_admin"
  | "finished"
  | "canceled";

// The data surface the shared project components need. An app supplies its own
// implementation; the components stay transport-agnostic.
export interface ProjectApi {
  messages(thread: string): Promise<SharedMessage[]>;
  postMessage(body: string, thread: string): Promise<SharedMessage>;
}
