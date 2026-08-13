import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

// Safe markdown rendering (react-markdown never injects raw HTML) - used for
// program README descriptions (§28).
export default function Markdown({ children }: { children: string }) {
  return (
    <div className="markdown">
      <ReactMarkdown remarkPlugins={[remarkGfm]}>{children}</ReactMarkdown>
    </div>
  );
}
