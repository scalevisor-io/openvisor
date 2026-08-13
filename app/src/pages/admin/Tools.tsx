import { useEffect, useState } from "react";
import { toolsApi, type Tool } from "../../lib/endpoints";
import { useToast } from "../../lib/toast";
import { McpNameChip, Modal, Spinner, Toggle } from "../../components/ui";
import { ProgramIcon } from "../../components/programs";

/** §Tools - MCP servers the dev agent can ACT through (GitHub / GitLab PR,
 * issue and review operations), as opposed to knowledge bases which inform.
 * Presented as a store shelf (Programs parity): one card per tool with a brand
 * tile and a one-line pitch; the configuration (enable, endpoint URL, key,
 * connection test) lives in the store-style detail modal. Global defaults
 * here; per-project overrides live on the project page. */

const KIND_COPY: Record<string, string> = {
  github:
    "Give the agent hands on GitHub: open and update pull requests, read and comment on issues, and review code on connected repositories.",
  gitlab:
    "Give the agent hands on GitLab: merge requests, issues and reviews - on gitlab.com or your own self-hosted instance.",
};

// Brand tiles in the ProgramIcon duotone family. GitHub sits on the family's
// slate pair; GitLab keeps its tanuki orange - brand recognition beats palette
// purity on a store shelf.
const KIND_GRADIENT: Record<string, [string, string]> = {
  github: ["#64748b", "#334155"],
  gitlab: ["#fc6d26", "#e24329"],
};

const GITHUB_GLYPH = {
  box: 16,
  d: "M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27s1.36.09 2 .27c1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.01 8.01 0 0 0 16 8c0-4.42-3.58-8-8-8Z",
};
const GITLAB_GLYPH = {
  box: 24,
  d: "m23.6 9.593-.033-.086L20.3 1.011a.851.851 0 0 0-.336-.405.875.875 0 0 0-1 .054.875.875 0 0 0-.29.44l-2.207 6.748H7.537L5.33 1.1a.858.858 0 0 0-.29-.441.875.875 0 0 0-1-.054.86.86 0 0 0-.336.405L.437 9.502l-.032.086a6.066 6.066 0 0 0 2.012 7.01l.011.009.03.021 4.977 3.727 2.462 1.863 1.5 1.132a1.009 1.009 0 0 0 1.22 0l1.499-1.132 2.462-1.863 5.006-3.75.013-.01a6.068 6.068 0 0 0 2.003-7.002Z",
};

function ToolIcon({ tool, size = 56 }: { tool: Tool; size?: number }) {
  const grad = KIND_GRADIENT[tool.kind];
  const glyph = tool.kind === "github" ? GITHUB_GLYPH : tool.kind === "gitlab" ? GITLAB_GLYPH : null;
  if (!grad || !glyph) return <ProgramIcon title={tool.name} seed={tool.id} size={size} />;
  return (
    <span
      className="store-icon"
      aria-hidden="true"
      style={{ width: size, height: size, background: `linear-gradient(135deg, ${grad[0]}, ${grad[1]})` }}
    >
      <svg
        width={Math.round(size * 0.52)}
        height={Math.round(size * 0.52)}
        viewBox={`0 0 ${glyph.box} ${glyph.box}`}
        fill="#fff"
        aria-hidden="true"
      >
        <path d={glyph.d} />
      </svg>
    </span>
  );
}

export default function Tools() {
  const toast = useToast();
  const [tools, setTools] = useState<Tool[] | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [urls, setUrls] = useState<Record<string, string>>({});
  const [keys, setKeys] = useState<Record<string, string>>({});
  const [verify, setVerify] = useState<Record<string, { ok: boolean; detail: string }>>({});
  const [selectedId, setSelectedId] = useState<string | null>(null);

  useEffect(() => {
    toolsApi
      .list()
      .then((t) => {
        setTools(t);
        setUrls(Object.fromEntries(t.map((x) => [x.id, x.url])));
      })
      .catch((e) => toast.push(e instanceof Error ? e.message : "Load failed", "err"));
  }, [toast]);

  async function save(
    t: Tool,
    patch: { url?: string; api_key?: string; enabled?: boolean },
    doneMessage?: string,
  ) {
    setBusy(t.id);
    try {
      const updated = await toolsApi.patch(t.id, patch);
      setTools((cur) => (cur ? cur.map((x) => (x.id === t.id ? updated : x)) : cur));
      setKeys((k) => ({ ...k, [t.id]: "" }));
      toast.push(doneMessage ?? `${t.name} saved.`, "ok");
    } catch (e) {
      toast.push(e instanceof Error ? e.message : "Save failed", "err");
    } finally {
      setBusy(null);
    }
  }

  async function testTool(t: Tool) {
    setBusy(t.id);
    try {
      const res = await toolsApi.verify(t.id);
      setVerify((v) => ({ ...v, [t.id]: res }));
    } catch (e) {
      setVerify((v) => ({ ...v, [t.id]: { ok: false, detail: e instanceof Error ? e.message : "Check failed" } }));
    } finally {
      setBusy(null);
    }
  }

  if (!tools) return <Spinner />;
  const selected = tools.find((t) => t.id === selectedId) ?? null;

  return (
    <div className="stack">
      <div className="section-title">Tools</div>
      <p className="tiny faint">
        Services the development agent can act through during builds - as opposed to
        knowledge bases, which inform it. An enabled tool applies to every project; each
        project can override the enable flag, the endpoint URL and the key on its page.
        Enabling runs the tool-poisoning scan server-side.
      </p>
      <div className="store-grid">
        {tools.map((t) => (
          <div
            key={t.id}
            className="store-card"
            role="button"
            tabIndex={0}
            onClick={() => setSelectedId(t.id)}
            onKeyDown={(e) => {
              if (e.key === "Enter" || e.key === " ") {
                e.preventDefault();
                setSelectedId(t.id);
              }
            }}
          >
            <div className="store-card-head">
              <ToolIcon tool={t} />
              <div className="store-card-titles">
                <h3>{t.name}</h3>
                <span className="store-eyebrow">{t.enabled ? "Enabled" : "Disabled"}</span>
              </div>
              {/* Same switch as the KB/settings pages; the card click still
                  opens the detail, so the switch stops propagation. */}
              <div className="row gap-sm" onClick={(e) => e.stopPropagation()}>
                {busy === t.id && <Spinner />}
                <Toggle
                  checked={t.enabled}
                  disabled={busy === t.id}
                  title={t.enabled ? undefined : "Enabling runs the tool-poisoning scan"}
                  onChange={(v) =>
                    save(t, { enabled: v }, `${t.name} ${v ? "enabled" : "disabled"}.`)
                  }
                />
              </div>
            </div>
            <p className="store-desc">
              {KIND_COPY[t.kind] ?? "An MCP service the development agent can act through during builds."}
            </p>
            <div onClick={(e) => e.stopPropagation()}>
              <McpNameChip server={t.mcp_server} />
            </div>
          </div>
        ))}
      </div>

      {selected && (
        <Modal title="" onClose={() => setSelectedId(null)} wide>
          <div className="store-detail-head">
            <ToolIcon tool={selected} size={64} />
            <div className="store-card-titles">
              <h3 style={{ margin: 0 }}>{selected.name}</h3>
              <span className="store-eyebrow">
                {selected.enabled ? "Enabled · applies to every project" : "Disabled"}
              </span>
            </div>
            <div className="row gap-sm">
              {busy === selected.id && <Spinner />}
              <Toggle
                checked={selected.enabled}
                disabled={busy === selected.id}
                title={selected.enabled ? undefined : "Enabling runs the tool-poisoning scan"}
                onChange={(v) =>
                  save(selected, { enabled: v }, `${selected.name} ${v ? "enabled" : "disabled"}.`)
                }
              />
            </div>
          </div>
          <p className="muted small" style={{ marginTop: "0.35rem" }}>
            {KIND_COPY[selected.kind] ?? "An MCP service the development agent can act through during builds."}
          </p>
          <div className="field mt">
            <label>
              MCP endpoint URL{" "}
              {selected.kind === "gitlab" && (
                <span
                  title="Self-hosted GitLab: https://<your-instance>/api/v4/mcp"
                  style={{ cursor: "help", opacity: 0.7 }}
                >
                  ⓘ
                </span>
              )}
            </label>
            <div className="row gap-sm">
              <input
                value={urls[selected.id] ?? selected.url}
                onChange={(e) => setUrls((u) => ({ ...u, [selected.id]: e.target.value }))}
                style={{ flex: 1 }}
              />
              <button
                className="btn btn-sm"
                disabled={busy === selected.id || (urls[selected.id] ?? selected.url) === selected.url}
                onClick={() => save(selected, { url: urls[selected.id] })}
              >
                Save URL
              </button>
            </div>
            {selected.kind === "gitlab" && (
              <p className="tiny faint mt">
                For a self-hosted GitLab, point this at{" "}
                <code>https://&lt;your-instance&gt;/api/v4/mcp</code>. Projects can override
                it per customer instance.
              </p>
            )}
          </div>
          <div className="field mt">
            <label>
              API key{" "}
              <span
                title={`Personal access token used as the Bearer credential for the ${selected.name} MCP endpoint. Per-project keys fall back to the project's ${selected.kind === "github" ? "GITHUB_TOKEN" : "GITLAB_TOKEN"} Memory secret before this one.`}
                style={{ cursor: "help", opacity: 0.7 }}
              >
                ⓘ
              </span>
            </label>
            <div className="row gap-sm">
              <input
                type="password"
                value={keys[selected.id] ?? ""}
                onChange={(e) => setKeys((k) => ({ ...k, [selected.id]: e.target.value }))}
                placeholder={selected.has_api_key ? "••••••••" : selected.kind === "github" ? "ghp_… / github_pat_…" : "glpat-…"}
                autoComplete="new-password"
                style={{ flex: 1 }}
              />
              <button
                className="btn btn-sm"
                disabled={busy === selected.id || !(keys[selected.id] ?? "").trim()}
                onClick={() => save(selected, { api_key: keys[selected.id] })}
              >
                Save key
              </button>
            </div>
            <p className="tiny faint mt">
              {selected.has_api_key ? "✓ Key set - saving replaces it." : "No key yet."} Builds
              prefer the project's own Memory token when present.
            </p>
          </div>
          <div className="row gap-sm mt">
            <button className="btn btn-sm" disabled={busy === selected.id} onClick={() => testTool(selected)}>
              {busy === selected.id ? <Spinner /> : "Test connection"}
            </button>
          </div>
          {verify[selected.id] && (
            <p className={`tiny mt ${verify[selected.id].ok ? "ok-text" : "danger"}`}>
              {verify[selected.id].ok ? "✓ " : "✗ "}
              {verify[selected.id].detail}
            </p>
          )}
        </Modal>
      )}
    </div>
  );
}
