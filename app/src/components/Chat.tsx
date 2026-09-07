import { useCallback, useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { chatApi, chatDocumentApi, chatImageApi, requestsApi } from "../lib/endpoints";
import { useAuth } from "../lib/auth";
import { relTime, Spinner } from "./ui";
import { ConfirmPrompt, MessageBody, PlanDisclosure, PrChips, QuestionPrompt, confirmState, messageConfirm, messageQuestion, messagePrs, questionState, stripPrUrls, type PrRef } from "@shared-ui";
import type { ChatDocument, ChatImage, ImageSupport, DevRunState, Message, ProjectStatus } from "../types";

// Agent/system messages sometimes deep-link to a request detail page (the §12
// classifier's "On it - follow progress here: <url>" ack). Render that as an
// integrated in-app button to the request instead of a raw external URL. Matches
// the full URL so it can be stripped cleanly; the ids are project/request UUIDs.
const REQUEST_LINK_RE =
  /https?:\/\/\S*?\/projects\/([0-9a-fA-F-]{36})\/requests\/([0-9a-fA-F-]{36})/;

function ChatBody({ text, prs = [] }: { text: string; prs?: PrRef[] }) {
  // §PR chips: a chip replaces its raw URL in the text, like the request
  // deep-link button below replaces its URL.
  const cleaned = prs.length ? stripPrUrls(text, prs) : text;
  const chips = prs.length > 0 && (
    <div style={{ marginTop: "0.45rem" }}>
      <PrChips refs={prs} />
    </div>
  );
  const m = cleaned.match(REQUEST_LINK_RE);
  if (!m)
    return (
      <>
        <MessageBody text={cleaned} />
        {chips}
      </>
    );
  const [full, projectId, requestId] = m;
  const rest = cleaned.replace(full, "").replace(/\s+$/, "");
  return (
    <>
      {rest && <MessageBody text={rest} />}
      <Link
        className="btn btn-sm"
        to={`/projects/${projectId}/requests/${requestId}`}
        style={{ marginTop: "0.5rem", display: "inline-flex" }}
      >
        View request →
      </Link>
      {chips}
    </>
  );
}

// Live message list for a thread. Uses the project WebSocket for push updates
// with a polling fallback; dedupes by message id so both sources are safe.
const MAX_IMAGES = 4;
const MAX_DOCUMENTS = 4;

// §chat documents: what the picker offers. Mirrors services/documents.py -
// the server sniffs the bytes, this only shapes the file dialog.
const DOCUMENT_ACCEPT =
  ".pdf,.docx,.md,.markdown,.html,.htm,.txt,.csv,.json,application/pdf," +
  "application/vnd.openxmlformats-officedocument.wordprocessingml.document," +
  "text/markdown,text/html,text/plain,text/csv,application/json";
const IMAGE_ACCEPT = "image/png,image/jpeg,image/webp,image/gif";

function fmtBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${Math.round(n / 1024)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

// Feather-style file icon, the same visual language as the attach button.
function DocIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor"
         strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
      <path d="M14 2v6h6" />
    </svg>
  );
}

// §chat documents: a chip naming the file - kind, pages, size - that downloads
// the original on click (never an inline render: an uploaded HTML page must not
// run as the app). `truncated` warns that the model read the head only.
function DocChip({ href, doc, onRemove }: { href?: string; doc: ChatDocument; onRemove?: () => void }) {
  const facts = [
    doc.pages ? `${doc.pages} page${doc.pages === 1 ? "" : "s"}` : null,
    fmtBytes(doc.size_bytes),
    doc.truncated ? "text cut at the platform cap" : null,
  ].filter(Boolean).join(" · ");
  const inner = (
    <>
      <DocIcon />
      <span className="chat-doc-name" title={doc.filename}>{doc.filename}</span>
      <span className="chat-doc-facts">{facts}</span>
    </>
  );
  return (
    <span className="chat-attachment chat-doc">
      {href ? (
        <a href={href} className="chat-doc-link" title={`Download ${doc.filename}`}>{inner}</a>
      ) : (
        <span className="chat-doc-link">{inner}</span>
      )}
      {onRemove && (
        <button type="button" className="chat-attachment-x" title="Remove" onClick={onRemove}>
          ×
        </button>
      )}
    </span>
  );
}

// §chat images / §chat documents: what the sender attached, recorded on
// Message.meta so a reader needs no second query. An image opens full-size; a
// document chip downloads the original.
function MessageAttachments({ projectId, message }: { projectId: string; message: Message }) {
  // meta is a platform-authored union; images ride in the generic half.
  const images = ((message.meta as Record<string, unknown> | null)?.images ?? []) as ChatImage[];
  const documents = ((message.meta as Record<string, unknown> | null)?.documents ?? []) as ChatDocument[];
  if (!images.length && !documents.length) return null;
  return (
    <div className="chat-attachments msg-attachments">
      {images.map((img) => (
        <a key={img.id} href={chatImageApi.url(projectId, img.id)} target="_blank"
           rel="noreferrer noopener" className="chat-attachment">
          <img src={chatImageApi.url(projectId, img.id)} alt={img.filename} />
        </a>
      ))}
      {documents.map((doc) => (
        <DocChip key={doc.id} doc={doc} href={chatDocumentApi.url(projectId, doc.id)} />
      ))}
    </div>
  );
}


// Mirrors PLAN_CHAT_CHARS in workers/tasks.py: how much of the plan the
// approval message itself carries. Only the remainder needs revealing.
const PLAN_CHAT_CHARS = 4000;

export default function Chat({
  projectId,
  thread,
  canEmail = false,
  imageSupport,
  emptyHint,
  starters,
  assistant = false,
  readOnly = false,
  projectStatus,
  devPlan,
  onStatus,
  onDev,
}: {
  projectId: string;
  thread: string;
  canEmail?: boolean;
  // §sharing: a read-only share follows the thread but can't post - the
  // composer and every posting affordance disappear.
  readOnly?: boolean;
  // §chat kind: every customer message gets an AI answer, so show a thinking
  // bubble while it generates instead of the offer-a-human bubble; outside
  // `development` (human hand-off) the responder is silent, so the bubble says
  // so instead of the generic dev-workflow copy.
  assistant?: boolean;
  projectStatus?: ProjectStatus;
  // §plan visibility: the project's full implementation plan, offered under the
  // plan-approval question whose message body carries only an excerpt.
  devPlan?: string | null;
  onStatus?: (status: ProjectStatus) => void;
  // Worker pushed a dev_run_state change (a build started/moved/ended) - the
  // page refreshes the project so the Development panel follows without a reload.
  onDev?: (state: DevRunState) => void;
  // Empty-thread on-ramp: a hint line plus tappable starters that prefill the
  // composer - chat drives the request classifier, so the blank state should
  // teach that instead of saying "No messages yet."
  // §chat images: the project's {enabled, reason, model} verdict. Undefined on
  // surfaces that don't pass it - attachments stay off, which is the safe default.
  imageSupport?: ImageSupport | null;
  emptyHint?: string;
  starters?: string[];
}) {
  const { settings } = useAuth();
  const consultant = settings?.consultant_first_name ?? "Consultant";
  // §chat documents switch: advisory - the upload route re-checks it. Absent
  // (an older API) reads as on, like the server's missing-row default.
  const docsEnabled = settings?.chat_documents_enabled !== false;
  const canAttach = docsEnabled || !!imageSupport?.enabled;
  const [messages, setMessages] = useState<Message[]>([]);
  const [loading, setLoading] = useState(true);
  const [body, setBody] = useState("");
  const [alsoEmail, setAlsoEmail] = useState(false);
  const [sending, setSending] = useState(false);
  const [pending, setPending] = useState<ChatImage[]>([]);
  const [pendingDocs, setPendingDocs] = useState<ChatDocument[]>([]);
  const [requestingHuman, setRequestingHuman] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // §12 live feedback: "reading" while the worker classifies the message (pushed
  // over WS), "none" when it finished without any action - so a silent verdict
  // still leaves visible feedback instead of dead air.
  const [agentActivity, setAgentActivity] = useState<"reading" | "none" | null>(null);
  const activityTimeout = useRef<ReturnType<typeof setTimeout> | null>(null);
  const listRef = useRef<HTMLDivElement | null>(null);
  const wsOpen = useRef(false);

  const merge = useCallback(
    (incoming: Message[]) => {
      setMessages((prev) => {
        const byId = new Map(prev.map((m) => [m.id, m]));
        for (const m of incoming) if (m.thread === thread) byId.set(m.id, m);
        return [...byId.values()].sort(
          (a, b) => new Date(a.created_at).getTime() - new Date(b.created_at).getTime(),
        );
      });
    },
    [thread],
  );

  // Initial load + reset when the thread changes.
  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setMessages([]);
    chatApi
      .messages(projectId, thread)
      .then((m) => !cancelled && merge(m))
      .catch((err) => !cancelled && setError(err instanceof Error ? err.message : "Load failed"))
      .finally(() => !cancelled && setLoading(false));
    return () => {
      cancelled = true;
    };
  }, [projectId, thread, merge]);

  // WebSocket push.
  useEffect(() => {
    const proto = window.location.protocol === "https:" ? "wss" : "ws";
    let ws: WebSocket | null = null;
    try {
      ws = new WebSocket(`${proto}://${window.location.host}/ws/projects/${projectId}`);
    } catch {
      ws = null;
    }
    if (ws) {
      ws.onopen = () => {
        wsOpen.current = true;
      };
      ws.onclose = () => {
        wsOpen.current = false;
      };
      ws.onerror = () => {
        wsOpen.current = false;
      };
      ws.onmessage = (ev) => {
        try {
          const data = JSON.parse(ev.data);
          if (data.type === "message" && data.message) merge([data.message as Message]);
          if (data.type === "status" && onStatus) onStatus(data.status as ProjectStatus);
          if (data.type === "dev" && data.dev_run_state && onDev)
            onDev(data.dev_run_state as DevRunState);
          if (data.type === "agent_activity" && thread === "main") {
            if (activityTimeout.current) clearTimeout(activityTimeout.current);
            if (data.state === "reading") {
              setAgentActivity("reading");
              // Safety valve: a dead worker never sends "idle" - don't let the
              // indicator spin forever.
              activityTimeout.current = setTimeout(() => setAgentActivity(null), 120_000);
            } else {
              setAgentActivity(data.intent === "none" ? "none" : null);
            }
          }
        } catch {
          // ignore malformed frames
        }
      };
    }
    return () => {
      wsOpen.current = false;
      ws?.close();
      if (activityTimeout.current) clearTimeout(activityTimeout.current);
    };
  }, [projectId, merge, onStatus, onDev, thread]);

  // Polling fallback - cheap and only meaningfully needed when the socket is down.
  useEffect(() => {
    const t = setInterval(() => {
      if (wsOpen.current) return;
      chatApi
        .messages(projectId, thread)
        .then(merge)
        .catch(() => {});
    }, 7000);
    return () => clearInterval(t);
  }, [projectId, thread, merge]);

  // Autoscroll to newest (messages and the live activity indicator alike).
  useEffect(() => {
    const el = listRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [messages, agentActivity]);

  // §chat images / §chat documents: files live here until the message that
  // claims them is posted. Images need a model that reads them (the server
  // re-checks); anything else goes up as a document and the server decides
  // whether it can read it - the errors it returns are written for the sender.
  async function attach(files: File[]) {
    if (readOnly || !files.length) return;
    const images = files.filter((f) => f.type.startsWith("image/"));
    const docs = files.filter((f) => !f.type.startsWith("image/"));
    setError(null);
    if (images.length && !imageSupport?.enabled) {
      setError(imageSupport?.reason ?? "This project's model can't read images.");
    } else if (images.length) {
      try {
        const up = await chatImageApi.upload(projectId, images.slice(0, MAX_IMAGES - pending.length));
        setPending((p) => [...p, ...up].slice(0, MAX_IMAGES));
      } catch (err) {
        setError(err instanceof Error ? err.message : "Could not attach the image.");
      }
    }
    if (docs.length && !docsEnabled) {
      setError("Document attachments are switched off on this instance.");
    } else if (docs.length) {
      try {
        const up = await chatDocumentApi.upload(
          projectId, docs.slice(0, MAX_DOCUMENTS - pendingDocs.length));
        setPendingDocs((p) => [...p, ...up].slice(0, MAX_DOCUMENTS));
      } catch (err) {
        setError(err instanceof Error ? err.message : "Could not attach the document.");
      }
    }
  }

  async function send(e: React.FormEvent) {
    e.preventDefault();
    if (!body.trim() && !pending.length && !pendingDocs.length) return;
    setSending(true);
    setError(null);
    try {
      const msg = await chatApi.send(projectId, thread, body.trim() || "(attachment)",
                                     canEmail && alsoEmail, pending.map((i) => i.id),
                                     pendingDocs.map((d) => d.id));
      merge([msg]);
      setPending([]);
      setPendingDocs([]);
      setBody("");
      setAlsoEmail(false);
      setAgentActivity(null); // a stale "no action" note doesn't apply to this message
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not send message.");
    } finally {
      setSending(false);
    }
  }

  // One-click answer to a §12 clarifying question: the option label is posted
  // as a plain chat message (the classifier reads it like any typed reply).
  async function answerQuestion(label: string) {
    setSending(true);
    setError(null);
    try {
      const msg = await chatApi.send(projectId, thread, label, false);
      merge([msg]);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not send answer.");
    } finally {
      setSending(false);
    }
  }

  // ✓/✗ on a §12 request-proposal ack: the deterministic start/cancel actions
  // (no classifier round-trip); their canned replies arrive over the WS merge
  // and freeze the panel via confirmState.
  async function decideRequest(requestId: string, approve: boolean) {
    setSending(true);
    setError(null);
    try {
      if (approve) await requestsApi.start(projectId, requestId);
      else await requestsApi.cancel(projectId, requestId);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not update the request.");
    } finally {
      setSending(false);
    }
  }

  async function requestHuman() {
    setRequestingHuman(true);
    setError(null);
    try {
      await chatApi.requestHumanAnswer(projectId, thread);
      // the confirmation agent message arrives over the WS / polling merge
    } catch (err) {
      setError(err instanceof Error ? err.message : `Could not notify ${consultant}.`);
    } finally {
      setRequestingHuman(false);
    }
  }

  // §12: no automatic agent replies - after a customer message, the agent
  // bubble offers to pull the consultant in instead. Hidden for the admin's own view.
  const lastMsg = messages[messages.length - 1];
  const offerHuman =
    !assistant && !canEmail && !readOnly &&
    lastMsg?.author === "customer" && agentActivity !== "reading";
  const assistantActive = assistant && projectStatus === "development";
  const thinking = assistantActive && lastMsg?.author === "customer";
  // chat project parked outside `development`: no answer is coming
  const chatParked =
    assistant && !assistantActive && !canEmail && lastMsg?.author === "customer";

  return (
    <div className="chat">
      <div className="chat-messages" ref={listRef}>
        {loading && messages.length === 0 ? (
          <div className="loading-center">
            <Spinner />
          </div>
        ) : messages.length === 0 ? (
          <div className="chat-empty">
            <p className="muted small center">{emptyHint ?? "No messages yet."}</p>
            {starters && starters.length > 0 && !readOnly && (
              <div className="chat-starters">
                {starters.map((s) => (
                  <button
                    key={s}
                    type="button"
                    className="chat-starter"
                    onClick={() => setBody((b) => (b ? b : s))}
                  >
                    {s}
                  </button>
                ))}
              </div>
            )}
          </div>
        ) : (
          messages.map((m) => (
            <div key={m.id} className={`msg author-${m.author}`}>
              {m.author === "system" ? (
                <div className="msg-body">
                  <ChatBody text={m.body} prs={messagePrs(m)} />
                  {m.emailed && <span className="emailed-icon" title="Emailed"> ✉</span>}
                </div>
              ) : (
                <>
                  <div className="msg-head">
                    <span className="msg-author">{m.author}</span>
                    <span>· {relTime(m.created_at)}</span>
                    {m.emailed && (
                      <span className="emailed-icon" title="Also sent as email">
                        ✉
                      </span>
                    )}
                  </div>
                  <div className="msg-body">
                    <ChatBody text={m.body} prs={messagePrs(m)} />
                    <MessageAttachments projectId={projectId} message={m} />
                    {(() => {
                      const q = m.author === "agent" ? messageQuestion(m) : null;
                      if (!q) return null;
                      const { active, selected } = questionState(messages, m.id);
                      return (
                        <>
                          {/* §plan visibility: the body above is an excerpt; the
                              plan being approved is readable in full here. */}
                          {q.plan && devPlan && (
                            <PlanDisclosure plan={devPlan} excerptChars={PLAN_CHAT_CHARS} />
                          )}
                          <QuestionPrompt
                            question={q}
                            active={active && !readOnly}
                            selected={selected}
                            busy={sending}
                            onSelect={answerQuestion}
                          />
                        </>
                      );
                    })()}
                    {(() => {
                      const c = m.author === "agent" ? messageConfirm(m) : null;
                      if (!c) return null;
                      const { active, resolution } = confirmState(messages, m.id);
                      return (
                        <ConfirmPrompt
                          active={active && !readOnly}
                          resolution={resolution}
                          busy={sending}
                          onApprove={() => decideRequest(c.request_id, true)}
                          onDismiss={() => decideRequest(c.request_id, false)}
                        />
                      );
                    })()}
                  </div>
                </>
              )}
            </div>
          ))
        )}
        {thinking && (
          <div className="msg author-agent">
            <div className="msg-head">
              <span className="msg-author">agent</span>
            </div>
            <div className="msg-body row gap-sm">
              <Spinner />
              <span className="muted small">thinking…</span>
            </div>
          </div>
        )}
        {chatParked && (
          <div className="msg author-agent">
            <div className="msg-head">
              <span className="msg-author">agent</span>
            </div>
            <div className="msg-body">
              <span>I'm not sure how to answer this, but {consultant} may pick this thread up.</span>
              {projectStatus !== "awaiting_admin" && !readOnly && (
                <div className="mt">
                  <button
                    type="button"
                    className="btn btn-sm"
                    onClick={requestHuman}
                    disabled={requestingHuman}
                  >
                    {requestingHuman ? <Spinner /> : "Request human answer"}
                  </button>
                </div>
              )}
            </div>
          </div>
        )}
        {agentActivity === "reading" && (
          <div className="msg author-agent">
            <div className="msg-head">
              <span className="msg-author">agent</span>
            </div>
            <div className="msg-body row gap-sm">
              <Spinner />
              <span className="muted small">reading your message…</span>
            </div>
          </div>
        )}
        {agentActivity === "none" && (
          <p className="muted small center">
            The agent read this - no automated action to take.
          </p>
        )}
        {offerHuman && (
          <div className="msg author-agent">
            <div className="msg-head">
              <span className="msg-author">agent</span>
            </div>
            <div className="msg-body">
              <span>Want a human answer? {consultant} can pick this thread up.</span>
              <div className="mt">
                <button
                  type="button"
                  className="btn btn-sm"
                  onClick={requestHuman}
                  disabled={requestingHuman}
                >
                  {requestingHuman ? <Spinner /> : "Request human answer"}
                </button>
              </div>
            </div>
          </div>
        )}
      </div>

      {readOnly ? (
        <p className="tiny faint center" style={{ margin: "0.5rem 0" }}>
          This project is shared with you read-only - you can follow the conversation but not post.
        </p>
      ) : (
      <form
        className="chat-composer"
        onSubmit={send}
        onDragOver={(e) => canAttach && e.preventDefault()}
        onDrop={(e) => {
          if (!canAttach) return;
          e.preventDefault();
          attach([...e.dataTransfer.files]);
        }}
      >
        {error && <div className="tiny" style={{ color: "var(--danger)" }}>{error}</div>}
        {(pending.length > 0 || pendingDocs.length > 0) && (
          <div className="chat-attachments">
            {pending.map((img) => (
              <span key={img.id} className="chat-attachment">
                <img src={chatImageApi.url(projectId, img.id)} alt={img.filename} />
                <button
                  type="button"
                  className="chat-attachment-x"
                  title="Remove"
                  onClick={() => setPending((p) => p.filter((i) => i.id !== img.id))}
                >
                  ×
                </button>
              </span>
            ))}
            {pendingDocs.map((doc) => (
              <DocChip
                key={doc.id}
                doc={doc}
                onRemove={() => setPendingDocs((p) => p.filter((d) => d.id !== doc.id))}
              />
            ))}
          </div>
        )}
        <textarea
          placeholder={!canAttach ? "Write a message…"
            : imageSupport?.enabled && docsEnabled
              ? "Write a message… (paste or drop a document or an image)"
              : docsEnabled ? "Write a message… (paste or drop a document)"
                            : "Write a message… (paste or drop an image)"}
          value={body}
          onChange={(e) => setBody(e.target.value)}
          style={{ minHeight: 64 }}
          onPaste={(e) => {
            const files = [...e.clipboardData.files];
            if (files.length && canAttach) attach(files);
          }}
          onKeyDown={(e) => {
            if ((e.metaKey || e.ctrlKey) && e.key === "Enter") send(e);
          }}
        />
        <div className="between">
          {canEmail ? (
            <label className="checkbox-row tiny">
              <input
                type="checkbox"
                checked={alsoEmail}
                onChange={(e) => setAlsoEmail(e.target.checked)}
              />
              Also send as email
            </label>
          ) : (
            <span className="tiny faint">⌘/Ctrl + Enter to send</span>
          )}
          <div className="row gap-sm">
            {/* Documents attach wherever the instance switch is on (the model
                reads their text); images only when the model reads pixels. The
                button is always PRESENT - disabled with the reason when neither
                applies - so the capability is discoverable instead of mysterious. */}
            <label
              className={`btn btn-sm btn-ghost${canAttach ? "" : " is-disabled"}`}
              title={!canAttach
                ? "Attachments are off: documents are switched off on this instance, and "
                  + (imageSupport?.reason ?? "images need a model that reads them.")
                : docsEnabled && imageSupport?.enabled
                  ? "Attach a document (PDF, Word, Markdown, HTML, text) or an image - or paste / drop one"
                  : docsEnabled
                    ? "Attach a document (PDF, Word, Markdown, HTML, text) - or paste / drop one. "
                      + (imageSupport?.reason ?? "Images need a model that reads them.")
                    : "Attach an image (or paste / drop one). Documents are switched off on this instance."}
            >
              <input
                type="file"
                accept={[docsEnabled ? DOCUMENT_ACCEPT : "", imageSupport?.enabled ? IMAGE_ACCEPT : ""]
                  .filter(Boolean).join(",")}
                multiple
                hidden
                disabled={!canAttach
                  || ((!docsEnabled || pendingDocs.length >= MAX_DOCUMENTS)
                      && (!imageSupport?.enabled || pending.length >= MAX_IMAGES))}
                onChange={(e) => {
                  attach([...(e.target.files ?? [])]);
                  e.target.value = "";
                }}
              />
              {/* Feather-style 24-viewBox paperclip in currentColor, the same
                  visual language as the sidebar/theme icons. */}
              <svg width="15" height="15" viewBox="0 0 24 24" fill="none"
                   stroke="currentColor" strokeWidth="1.8" strokeLinecap="round"
                   strokeLinejoin="round" aria-hidden="true">
                <path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48" />
              </svg>
              <span className="sr-only">Attach a file</span>
            </label>
            <button type="submit" className="btn btn-primary btn-sm"
                    disabled={sending || (!body.trim() && !pending.length && !pendingDocs.length)}>
              {sending ? <Spinner /> : "Send"}
            </button>
          </div>
        </div>
      </form>
      )}
    </div>
  );
}
