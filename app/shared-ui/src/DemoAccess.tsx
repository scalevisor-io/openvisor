import { useState } from "react";
import "./DemoAccess.css";

// The live-demo access block (URL + state + basic-auth), shared between the
// Openvisor spoke SPA and the Scalevisor hub customer console so both render the
// IDENTICAL card. Pure presentational: the host owns the surrounding card (title,
// start/stop controls, timeout note) and passes the demo fields. Self-contained -
// its own copy-to-clipboard + password reveal, no app-lib imports.

function CopyBtn({ value }: { value: string }) {
  const [copied, setCopied] = useState(false);
  async function copy() {
    try {
      await navigator.clipboard.writeText(value);
    } catch {
      // Fallback for insecure contexts.
      const ta = document.createElement("textarea");
      ta.value = value;
      document.body.appendChild(ta);
      ta.select();
      document.execCommand("copy");
      document.body.removeChild(ta);
    }
    setCopied(true);
    setTimeout(() => setCopied(false), 1400);
  }
  return (
    <button type="button" className="demo-btn" onClick={copy}>
      {copied ? "Copied" : "Copy"}
    </button>
  );
}

export function DemoAccess({
  url,
  state,
  authUser,
  authPass,
}: {
  url: string;
  state?: string | null;
  authUser?: string | null;
  authPass?: string | null;
}) {
  const [revealed, setRevealed] = useState(false);
  return (
    <div className="demo-access">
      <div className="demo-field">
        <span className="demo-label">URL</span>
        <div className="demo-row">
          <a className="demo-url" href={url} target="_blank" rel="noreferrer noopener">
            {url}
          </a>
          <CopyBtn value={url} />
          {state && (
            <span className={`demo-state demo-state-${state}`}>
              <span className="demo-dot" />
              {state}
            </span>
          )}
        </div>
      </div>
      {(authUser != null || authPass != null) && (
        <div className="demo-auth">
          {authUser != null && (
            <div className="demo-field">
              <span className="demo-label">Basic-auth user</span>
              <div className="demo-copy">
                <span className="demo-value">{authUser}</span>
                <CopyBtn value={authUser} />
              </div>
            </div>
          )}
          {authPass != null && (
            <div className="demo-field">
              <span className="demo-label">Basic-auth password</span>
              <div className="demo-copy">
                <span className="demo-value">{revealed ? authPass : "••••••••"}</span>
                <button type="button" className="demo-btn" onClick={() => setRevealed((r) => !r)}>
                  {revealed ? "Hide" : "Show"}
                </button>
                <CopyBtn value={authPass} />
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
