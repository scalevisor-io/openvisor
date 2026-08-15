import "./MessageBody.css";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import remarkBreaks from "remark-breaks";

// Render an untrusted chat message body as markdown (GFM + hard line breaks,
// so plain-text messages keep their line structure). SECURITY INVARIANT (do
// not weaken): react-markdown builds a React element tree and never injects
// raw HTML (no rehype-raw, no dangerouslySetInnerHTML anywhere), the URL
// scheme allowlist is http/https ONLY (a link to any other scheme renders as
// its text), and images NEVER render - their alt text does - so a stored
// message can't make a reader's browser fetch an attacker URL. Both the spoke
// SPA and the hub console render spoke-attested AND customer-authored text
// through this same code, so a looser scheme (javascript:, data:) is a
// stored-XSS vector and a rendered <img> is a tracking pixel.
const SAFE_HREF = /^https?:\/\//i;

const components: Components = {
  a: ({ href, children }) =>
    href && SAFE_HREF.test(href) ? (
      <a href={href} target="_blank" rel="noreferrer noopener">
        {children}
      </a>
    ) : (
      <>{children}</>
    ),
  img: ({ alt }) => <>{alt ?? ""}</>,
};

export function MessageBody({ text }: { text: string }) {
  return (
    <div className="shared-md">
      <ReactMarkdown
        remarkPlugins={[remarkGfm, remarkBreaks]}
        urlTransform={(url) => (SAFE_HREF.test(url) ? url : "")}
        components={components}
      >
        {text}
      </ReactMarkdown>
    </div>
  );
}
