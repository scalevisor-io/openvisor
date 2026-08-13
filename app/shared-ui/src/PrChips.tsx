import "./PrChips.css";
import type { PrRef } from "./prs";

// Git merge glyph (inline SVG, stroke=currentColor - the BuildFeed icon idiom).
function PrIcon() {
  return (
    <svg
      viewBox="0 0 16 16"
      width="12"
      height="12"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.6"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <circle cx="4" cy="3.5" r="1.7" />
      <circle cx="4" cy="12.5" r="1.7" />
      <circle cx="12" cy="12.5" r="1.7" />
      <path d="M4 5.2v5.6" />
      <path d="M8.5 3.5H10a2 2 0 0 1 2 2v5.3" />
    </svg>
  );
}

// Clickable PR/MR chips (§PR chips): one per referenced change, opening the
// provider page in a new tab. `stop` keeps the click from bubbling when the
// chips sit inside a clickable card (the shared RequestList's card button).
export function PrChips({ refs, stop = false }: { refs: PrRef[]; stop?: boolean }) {
  if (refs.length === 0) return null;
  return (
    <span className="pr-chips">
      {refs.map((r) => (
        <a
          key={r.url}
          className="pr-chip"
          href={r.url}
          target="_blank"
          rel="noreferrer noopener"
          title={r.url}
          onClick={stop ? (e) => e.stopPropagation() : undefined}
        >
          <PrIcon />
          {(r.provider === "gitlab" ? "!" : "#") + r.number}
        </a>
      ))}
    </span>
  );
}


// Git branch glyph (same inline-SVG idiom as PrIcon).
function BranchIcon() {
  return (
    <svg
      viewBox="0 0 16 16"
      width="12"
      height="12"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.6"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <circle cx="4" cy="3.5" r="1.7" />
      <circle cx="4" cy="12.5" r="1.7" />
      <circle cx="12" cy="4.5" r="1.7" />
      <path d="M4 5.2v5.6" />
      <path d="M12 6.2c0 2.6-3 3.4-6 3.6" />
    </svg>
  );
}

// The branch a run builds on, as a chip button matching the PR/MR chips: a link
// when the repo page for the branch is known (the serializer pins it to the repo
// the change lives on - §repo binding), a plain chip otherwise.
export function BranchChip({ name, url, stop = false }: {
  name: string;
  url?: string | null;
  stop?: boolean;
}) {
  if (!name) return null;
  const inner = (
    <>
      <BranchIcon />
      <span className="mono">{name}</span>
    </>
  );
  if (!url) {
    return <span className="pr-chip pr-chip-static">{inner}</span>;
  }
  return (
    <a
      className="pr-chip"
      href={url}
      target="_blank"
      rel="noreferrer noopener"
      title={url}
      onClick={stop ? (e) => e.stopPropagation() : undefined}
    >
      {inner}
    </a>
  );
}
