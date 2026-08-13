// Shared Programs (§28) UI pieces: run-state badge, the accumulating live-log
// panel, the template-driven inputs form, and small formatters.
import { useEffect, useRef, useState } from "react";
import type { InputTemplateField, ProgramRun, ProgramRunState, RunLogChunk } from "../types";
import { Badge, usePolling } from "./ui";

const BADGE_KIND: Record<ProgramRunState, string> = {
  queued: "awaiting_review",
  running: "development",
  succeeded: "finished",
  failed: "canceled",
  timeout: "canceled",
  blocked: "canceled",
};

export function RunStateBadge({ state }: { state: ProgramRunState }) {
  return <Badge label={state} kind={BADGE_KIND[state] ?? "draft"} />;
}

// Duotones for the generated program tiles, familied around the brand speed
// gradient (cyan → violet) so every tile reads as part of the same shelf.
const ICON_GRADIENTS: [string, string][] = [
  ["#22d3ee", "#7c3aed"],
  ["#2563eb", "#22d3ee"],
  ["#7c3aed", "#db2777"],
  ["#0d9488", "#22c55e"],
  ["#f59e0b", "#db2777"],
  ["#4f46e5", "#0ea5e9"],
  ["#64748b", "#334155"],
  ["#059669", "#0891b2"],
];

// Deterministic squircle tile for a program: a duotone picked by hashing the
// program id, with the title's monogram set in the MONO face - programs are
// runnable code, so their icon speaks the platform's data voice.
export function ProgramIcon({
  title,
  seed,
  size = 56,
}: {
  title: string;
  seed: string;
  size?: number;
}) {
  let h = 0;
  for (const ch of seed) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  const [from, to] = ICON_GRADIENTS[h % ICON_GRADIENTS.length];
  const words = title.trim().split(/\s+/);
  const monogram = ((words[0]?.[0] ?? "?") + (words[1]?.[0] ?? "")).toUpperCase();
  return (
    <span
      className="store-icon"
      aria-hidden="true"
      style={{
        width: size,
        height: size,
        fontSize: Math.round(size * 0.34),
        background: `linear-gradient(135deg, ${from}, ${to})`,
      }}
    >
      {monogram}
    </span>
  );
}

export function runActive(run: ProgramRun | null | undefined): boolean {
  return !!run && (run.state === "queued" || run.state === "running");
}

export function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

export function runDuration(run: ProgramRun): string {
  if (!run.started_at) return "-";
  const end = run.finished_at ? new Date(run.finished_at) : new Date();
  const s = Math.max(0, Math.round((end.getTime() - new Date(run.started_at).getTime()) / 1000));
  return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${s % 60}s`;
}

// Live/past run log: polls offset chunks while `active`, accumulating into a
// <pre> that follows the tail. Mount with key={runId} so a run switch resets
// the accumulated buffer.
export function RunLog({
  fetchChunk,
  active,
}: {
  fetchChunk: (offset: number) => Promise<RunLogChunk>;
  active: boolean;
}) {
  const [text, setText] = useState("");
  const [done, setDone] = useState(false);
  const offsetRef = useRef(0);
  const preRef = useRef<HTMLPreElement>(null);

  usePolling(
    () => {
      fetchChunk(offsetRef.current)
        .then((c) => {
          if (c.next_offset === 0) {
            // fallback payload (blocked run summary or DB tail) - replace
            if (c.content) setText(c.content);
          } else {
            if (c.content) setText((t) => t + c.content);
            offsetRef.current = c.next_offset;
          }
          if (c.done) setDone(true);
        })
        .catch(() => {});
    },
    2000,
    !done,
  );

  useEffect(() => {
    const el = preRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [text]);

  return (
    <pre ref={preRef} className="log-pane">
      {text || (active ? "Waiting for output…" : "No log available.")}
    </pre>
  );
}

// Deterministic form generated from input.template.yml (§28 locked decision:
// no LLM mapping). Values travel as strings; the API coerces and returns
// per-field errors that land in `errors`.
export function InputsForm({
  fields,
  values,
  errors,
  onChange,
}: {
  fields: InputTemplateField[];
  values: Record<string, string>;
  errors: Record<string, string>;
  onChange: (name: string, value: string) => void;
}) {
  if (fields.length === 0) {
    return (
      <p className="muted small" style={{ margin: 0 }}>
        This program takes no inputs.
      </p>
    );
  }
  return (
    <div className="stack" style={{ gap: "0.75rem" }}>
      {fields.map((f) => {
        const value = values[f.name] ?? "";
        const error = errors[f.name];
        const placeholder =
          f.placeholder || (f.default !== null && f.default !== undefined ? `default: ${f.default}` : "");
        return (
          <div className="field" key={f.name} style={{ marginBottom: 0 }}>
            <label>
              {f.label}
              {f.required && <span className="danger-text"> *</span>}
              {f.secret && <span className="tiny faint"> (secret)</span>}
            </label>
            {f.type === "multiline" ? (
              <textarea
                value={value}
                placeholder={placeholder}
                style={{ minHeight: 70 }}
                onChange={(e) => onChange(f.name, e.target.value)}
              />
            ) : f.type === "choice" ? (
              <select value={value} onChange={(e) => onChange(f.name, e.target.value)}>
                <option value="">{f.default != null ? `default (${f.default})` : "select…"}</option>
                {(f.options ?? []).map((o) => (
                  <option key={o} value={o}>
                    {o}
                  </option>
                ))}
              </select>
            ) : f.type === "boolean" ? (
              <select value={value} onChange={(e) => onChange(f.name, e.target.value)}>
                <option value="">{f.default != null ? `default (${String(f.default)})` : "select…"}</option>
                <option value="true">true</option>
                <option value="false">false</option>
              </select>
            ) : (
              <input
                type={f.secret ? "password" : f.type === "number" ? "number" : "text"}
                value={value}
                placeholder={placeholder}
                onChange={(e) => onChange(f.name, e.target.value)}
              />
            )}
            {f.description && <div className="tiny muted">{f.description}</div>}
            {error && (
              <div className="tiny danger-text" role="alert">
                {error}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}
