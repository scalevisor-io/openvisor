import { useEffect, useId, useRef, useState, type ReactNode } from "react";

export function Spinner() {
  return <span className="spinner" aria-label="loading" />;
}

// The Openvisor mark: an open ring with one tapered spoke in orbit (same
// geometry as landing/public/brand/openvisor-mark.svg). Gradient stops default
// to the brand colors and can follow /api/settings overrides.
export function BrandMark({
  size = 20,
  primary = "#22d3ee",
  secondary = "#7c3aed",
}: {
  size?: number;
  primary?: string;
  secondary?: string;
}) {
  const id = useId();
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 32 32"
      aria-hidden="true"
      style={{ alignSelf: "center", flexShrink: 0 }}
    >
      <defs>
        <linearGradient id={id} x1="0" y1="0" x2="32" y2="32" gradientUnits="userSpaceOnUse">
          <stop stopColor={secondary} />
          <stop offset="1" stopColor={primary} />
        </linearGradient>
      </defs>
      <path
        d="M 23.6693 15.3566 A 8.6 8.6 0 1 1 14.0031 8.3337"
        fill="none"
        stroke={`url(#${id})`}
        strokeWidth="3.2"
        strokeLinecap="round"
      />
      <path
        d="M 19.7489 4.3521 A 13.3 13.3 0 0 1 27.6979 12.3011 A 1.2 1.2 0 0 1 25.4426 13.122 A 9.2263 9.2263 0 0 0 18.0388 9.0506 A 2.5 2.5 0 0 1 19.7489 4.3521 Z"
        fill={`url(#${id})`}
      />
    </svg>
  );
}

// Renders the brand wordmark: when the name carries a dotted suffix (e.g. acme.ai)
// the suffix keeps the two-tone accent; a plain name (Acme AI) renders as-is.
export function BrandName({ name }: { name: string }) {
  // One span, one flex item: the callers' gapped .wordmark flex rows must
  // space the mark from the name, never the name's own two tones (a bare
  // fragment made "Open" and "visor" separate flex items with a gap between
  // them). The stock brand splits before its "visor" tail, matching the
  // brand-kit lockup; a domain-like white-label brand goes two-tone on its
  // last ".".
  if (name === "Openvisor") {
    return (
      <span>
        Open<span className="dot-ai">visor</span>
      </span>
    );
  }
  const dot = name.lastIndexOf(".");
  if (dot <= 0 || dot >= name.length - 1) return <>{name}</>;
  return (
    <span>
      {name.slice(0, dot)}
      <span className="dot-ai">{name.slice(dot)}</span>
    </span>
  );
}

export function Loading({ label = "Loading…" }: { label?: string }) {
  return (
    <div className="loading-center">
      <Spinner /> {label}
    </div>
  );
}

export function Alert({
  kind = "error",
  children,
}: {
  kind?: "error" | "success" | "info" | "warn";
  children: ReactNode;
}) {
  return <div className={`alert alert-${kind}`}>{children}</div>;
}

export function Badge({ label, kind, live = false }: { label: string; kind: string; live?: boolean }) {
  return (
    <span className={`badge badge-${kind}${live ? " badge-live" : ""}`} title={live ? "The agent is working right now" : undefined}>
      <span className="dot" />
      {label.replace(/_/g, " ")}
    </span>
  );
}

export function Toggle({
  checked,
  onChange,
  disabled,
  title,
}: {
  checked: boolean;
  onChange: (v: boolean) => void;
  disabled?: boolean;
  title?: string;
}) {
  return (
    <label className="switch" title={title}>
      <input
        type="checkbox"
        checked={checked}
        disabled={disabled}
        onChange={(e) => onChange(e.target.checked)}
      />
      <span className="slider" />
    </label>
  );
}

/** Client-side pagination over an already-loaded list; clamps the page when the list shrinks. */
export function usePager<T>(items: T[], pageSize: number) {
  const [pageRaw, setPage] = useState(0);
  const pages = Math.max(1, Math.ceil(items.length / pageSize));
  const page = Math.min(pageRaw, pages - 1);
  const pageItems = items.slice(page * pageSize, (page + 1) * pageSize);
  return { pageItems, page, pages, setPage };
}

export function Pager({
  page,
  pages,
  onPage,
}: {
  page: number;
  pages: number;
  onPage: (p: number) => void;
}) {
  if (pages <= 1) return null;
  return (
    <div className="row gap-sm between mt">
      <button className="btn btn-sm" disabled={page === 0} onClick={() => onPage(page - 1)}>
        ← Prev
      </button>
      <span className="tiny faint">
        {page + 1} / {pages}
      </span>
      <button className="btn btn-sm" disabled={page >= pages - 1} onClick={() => onPage(page + 1)}>
        Next →
      </button>
    </div>
  );
}

export function CopyButton({
  value,
  label = "Copy",
  className = "btn btn-sm btn-ghost",
}: {
  value: string;
  label?: string;
  className?: string;
}) {
  const [copied, setCopied] = useState(false);
  async function copy() {
    try {
      await navigator.clipboard.writeText(value);
    } catch {
      // Fallback for insecure contexts
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
    <button type="button" className={className} onClick={copy}>
      {copied ? "Copied" : label}
    </button>
  );
}

export function CopyField({
  value,
  block = false,
  masked = false,
}: {
  value: string;
  block?: boolean;
  masked?: boolean;
}) {
  const [revealed, setRevealed] = useState(false);
  const shown = masked && !revealed ? "••••••••" : value;
  return (
    <div className={`copy-field${block ? " block" : ""}`}>
      <span className="value">{shown}</span>
      {masked && (
        <button type="button" className="btn btn-sm btn-ghost" onClick={() => setRevealed((r) => !r)}>
          {revealed ? "Hide" : "Show"}
        </button>
      )}
      <CopyButton value={value} />
    </div>
  );
}

export function Modal({
  title,
  onClose,
  children,
  wide,
}: {
  title: string;
  onClose: () => void;
  children: ReactNode;
  wide?: boolean;
}) {
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") onClose();
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <div className="modal-overlay" onMouseDown={onClose}>
      <div
        className="modal"
        style={wide ? { maxWidth: 640 } : undefined}
        onMouseDown={(e) => e.stopPropagation()}
      >
        <div className="modal-head">
          <h3 style={{ margin: 0 }}>{title}</h3>
          <button type="button" className="btn btn-sm btn-ghost" onClick={onClose}>
            ✕
          </button>
        </div>
        {children}
      </div>
    </div>
  );
}

// Small polling hook: runs `fn` immediately, then every `intervalMs` until stopped.
export function usePolling(fn: () => void, intervalMs: number, active = true) {
  const ref = useRef(fn);
  ref.current = fn;
  useEffect(() => {
    if (!active) return;
    ref.current();
    const t = setInterval(() => ref.current(), intervalMs);
    return () => clearInterval(t);
  }, [intervalMs, active]);
}

// Animated numeric display: eases the rendered value toward `target` with a
// short rAF count-up (odometer feel for live token/cost counters). Respects
// prefers-reduced-motion by snapping instantly.
export function useCountUp(target: number, duration = 700): number {
  const [value, setValue] = useState(target);
  const fromRef = useRef(target);
  useEffect(() => {
    const from = fromRef.current;
    if (from === target) return;
    if (window.matchMedia?.("(prefers-reduced-motion: reduce)").matches) {
      fromRef.current = target;
      setValue(target);
      return;
    }
    let raf = 0;
    const t0 = performance.now();
    const tick = (t: number) => {
      const k = Math.min(1, (t - t0) / duration);
      const eased = 1 - Math.pow(1 - k, 3);
      const v = k < 1 ? from + (target - from) * eased : target;
      fromRef.current = v;
      setValue(v);
      if (k < 1) raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [target, duration]);
  return value;
}

// Credit amounts arrive from the API as floats; the UI always shows whole
// credits, rounded down to the inferior unit (never display more than owned).
export function formatCredits(n: number): string {
  return Math.floor(n).toLocaleString();
}

// Exact variant for transaction ledgers: consumption entries are fractions of
// a credit (token costs run ~1e-5 credits), so flooring would show -1 for a
// -0.02 debit. Six fraction digits covers the smallest billable amounts.
export function formatCreditsExact(n: number): string {
  return n.toLocaleString(undefined, { maximumFractionDigits: 6 });
}

export function relTime(iso: string | null | undefined): string {
  if (!iso) return "-";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return "-";
  return d.toLocaleString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

// Collapsible section card: the standard .card with a clickable header row.
// `check` renders a green ✓ before the title (e.g. "connection verified").
// Controlled via collapsed/onToggle when the parent drives it (ReposCard
// auto-collapses on a successful connection test), else self-managed.
// Collapsed-state persistence: a card given a `storageKey` remembers the
// user's explicit toggles in localStorage, so a refresh restores the layout.
// Only clicks are stored - programmatic collapses (a parent driving
// `collapsed`, e.g. ReposCard's collapse-on-verify) never are.
export function readCardCollapsed(key: string): boolean | null {
  try {
    const v = localStorage.getItem(`cardcollapse:${key}`);
    return v === null ? null : v === "1";
  } catch {
    return null;
  }
}

export function writeCardCollapsed(key: string, collapsed: boolean): void {
  try {
    localStorage.setItem(`cardcollapse:${key}`, collapsed ? "1" : "0");
  } catch {
    /* storage unavailable (private mode) - state stays session-only */
  }
}

// Collapsible section card: the standard .card with a clickable header row.
// `check` renders a green ✓ before the title (e.g. "connection verified").
// Controlled via collapsed/onToggle when the parent drives it (ReposCard
// auto-collapses on a successful connection test), else self-managed.
export function CollapsibleCard({
  title,
  subtitle,
  check = false,
  defaultCollapsed = false,
  collapsed,
  onToggle,
  storageKey,
  className,
  children,
}: {
  title: ReactNode;
  subtitle?: string;
  check?: boolean;
  defaultCollapsed?: boolean;
  collapsed?: boolean;
  onToggle?: (collapsed: boolean) => void;
  storageKey?: string;
  className?: string;
  children: ReactNode;
}) {
  const [own, setOwn] = useState(
    () => (storageKey ? readCardCollapsed(storageKey) : null) ?? defaultCollapsed,
  );
  const isCollapsed = collapsed ?? own;
  const toggle = () => {
    const next = !isCollapsed;
    if (storageKey) writeCardCollapsed(storageKey, next);
    if (onToggle) onToggle(next);
    else setOwn(next);
  };
  const cardCls = (className ? `card ${className}` : "card") + (isCollapsed ? " collapsed" : "");
  return (
    <div className={cardCls}>
      <button
        type="button"
        className="between"
        aria-expanded={!isCollapsed}
        onClick={toggle}
        style={{
          background: "none", border: "none", padding: 0, width: "100%",
          cursor: "pointer", textAlign: "left", color: "inherit", font: "inherit",
        }}
      >
        <span className="section-title" style={{ margin: 0 }}>
          {check && (
            <span className="ok-text" title="Connection verified">
              ✓{" "}
            </span>
          )}
          {title}
        </span>
        <span className="tiny faint nowrap">
          {subtitle ? `${subtitle} ` : ""}
          {isCollapsed ? "▸" : "▾"}
        </span>
      </button>
      {!isCollapsed && <div className="mt card-collapse-body">{children}</div>}
    </div>
  );
}

// The name a dev run addresses a knowledge source or tool by (§KB/§Tools): the
// one string worth quoting in project instructions ("use the web_search tool").
// The TOOL name leads when we know it - that is what an instruction names - with
// the server it lives under kept as context.
export function McpNameChip({
  server,
  tools = [],
}: {
  server: string | null;
  tools?: string[];
}) {
  if (!server) {
    return (
      <div className="tiny faint mt-xs">
        Retrieved automatically - no tool name to call it by.
      </div>
    );
  }
  return (
    <div className="row gap-sm mt-xs mcp-name" style={{ alignItems: "center", flexWrap: "wrap" }}>
      <span className="tiny faint nowrap">Call it by</span>
      {tools.length > 0 ? (
        <>
          {tools.map((t) => (
            <code key={t} className="mono tiny">
              {t}
            </code>
          ))}
          <span className="tiny faint nowrap">
            in <code className="mono tiny">{server}</code>
          </span>
        </>
      ) : (
        <>
          <code className="mono tiny">{server}</code>
          <span className="tiny faint nowrap">(its own tool names)</span>
        </>
      )}
      <CopyButton value={tools[0] ?? server} label="Copy" className="btn btn-sm btn-ghost tiny" />
    </div>
  );
}
