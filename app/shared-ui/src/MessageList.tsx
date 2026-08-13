import type { SharedMessage } from "./types";
import { MessageBody } from "./MessageBody";

// A read-only, presentational list of chat messages. Data comes from props; the
// hosting app owns fetching (via ProjectApi) and layout chrome. `relTime`
// formatting is injected so each app keeps its own i18n/format helper.
export function MessageList({
  messages,
  relTime,
  emptyLabel = "No messages yet.",
}: {
  messages: SharedMessage[];
  relTime?: (iso: string) => string;
  emptyLabel?: string;
}) {
  if (messages.length === 0) {
    return <p className="muted">{emptyLabel}</p>;
  }
  return (
    <ul className="shared-message-list" style={{ listStyle: "none", padding: 0, margin: 0 }}>
      {messages.map((m) => (
        <li key={m.id} className={`shared-message shared-message-${m.author}`} style={{ marginBottom: "0.75rem" }}>
          <div>
            <strong>{m.author}</strong>{" "}
            {relTime && <span className="muted small">{relTime(m.created_at)}</span>}
          </div>
          <div className="shared-message-body">
            <MessageBody text={m.body} />
          </div>
        </li>
      ))}
    </ul>
  );
}
