import type { MessageQuestionMeta, MessageQuestionOption, SharedMessage } from "./types";

// §12 clarifying-question helpers, shared so the spoke SPA and the hub console
// derive the exact same semantics from the message stream (both surfaces render
// the same immutable messages; "answered" is a pure function of what follows).

// Validate a message's meta into a renderable question, or null. Defensive:
// meta travels through several serializers (spoke REST, WS, hub MCP proxy) and
// older records/servers may carry none.
export function messageQuestion(m: SharedMessage): MessageQuestionMeta | null {
  const meta = m.meta as MessageQuestionMeta | null | undefined;
  if (!meta || meta.kind !== "question" || typeof meta.question !== "string") return null;
  if (!Array.isArray(meta.options)) return null;
  const options: MessageQuestionOption[] = meta.options.filter(
    (o): o is MessageQuestionOption =>
      !!o && typeof o === "object" && typeof (o as MessageQuestionOption).label === "string" &&
      (o as MessageQuestionOption).label.trim().length > 0,
  );
  if (options.length < 2) return null;
  return { kind: "question", question: meta.question, options, allow_free_text: meta.allow_free_text !== false };
}

export interface QuestionState {
  // The question still awaits an answer: no human message follows it in the thread.
  active: boolean;
  // The option label the answering human message matched, when it did.
  selected: string | null;
}

export function questionState(messages: SharedMessage[], questionId: string): QuestionState {
  const idx = messages.findIndex((m) => m.id === questionId);
  if (idx < 0) return { active: false, selected: null };
  const q = messageQuestion(messages[idx]);
  const reply = messages
    .slice(idx + 1)
    .find((m) => m.author === "customer" || m.author === "admin");
  if (!reply) return { active: true, selected: null };
  const match = q?.options.find((o) => o.label.trim() === reply.body.trim());
  return { active: false, selected: match ? match.label : null };
}
