// Render a plain-text message body, linkifying bare URLs. SECURITY INVARIANT
// (do not weaken): the scheme allowlist is http/https ONLY, and this component
// never uses dangerouslySetInnerHTML. Both the spoke SPA and the hub console
// render spoke-attested AND customer-authored text through this same code, so a
// looser scheme (javascript:, data:) would be a stored-XSS vector.
const URL_RE = /(https?:\/\/[^\s<>"')\]]+)/g;

export function MessageBody({ text }: { text: string }) {
  const parts = text.split(URL_RE);
  return (
    <>
      {parts.map((part, i) =>
        i % 2 === 1 ? (
          <a key={i} href={part} target="_blank" rel="noreferrer noopener">
            {part}
          </a>
        ) : (
          <span key={i} style={{ whiteSpace: "pre-wrap" }}>
            {part}
          </span>
        ),
      )}
    </>
  );
}
