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
  donsetch:
    "Let the agent research the live web: search, read a page as clean markdown, or crawl a documentation site. Keyless - no account, no API key. Choose below which of the three it may use.",
};

// Keyed web-search providers. One §Tools row each, so the copy is per PROVIDER
// rather than per kind - they share the `websearch` kind and differ only in
// which index they answer from and where you buy the key.
const PROVIDER_COPY: Record<string, { blurb: string; keyUrl: string; placeholder: string }> = {
  serper: {
    blurb:
      "Google results through the Serper API, exposed to the dev agent as a web_search tool. Billed per search on your Serper account.",
    keyUrl: "https://serper.dev",
    placeholder: "your Serper API key",
  },
  staan: {
    blurb:
      "European search index (the Qwant + Ecosia joint venture) - queries stay under EU jurisdiction. Needs the Web-for-AI product on your Staan account.",
    keyUrl: "https://staan.ai",
    placeholder: "your Staan API key",
  },
};

function toolCopy(t: Tool): string {
  if (t.kind === "websearch") {
    return (
      PROVIDER_COPY[t.provider ?? ""]?.blurb ??
      "A web-search provider the development agent can query during builds."
    );
  }
  return KIND_COPY[t.kind] ?? "An MCP service the development agent can act through during builds.";
}

/** A keyed provider can't search without its key, so the switch stays locked
 *  until one is stored - the server refuses the enable either way. */
function needsKey(t: Tool): boolean {
  return t.kind === "websearch" && !t.has_api_key;
}

// Brand tiles in the ProgramIcon duotone family. GitHub sits on the family's
// slate pair; GitLab keeps its tanuki orange - brand recognition beats palette
// purity on a store shelf.
const KIND_GRADIENT: Record<string, [string, string]> = {
  github: ["#64748b", "#334155"],
  gitlab: ["#fc6d26", "#e24329"],
  donsetch: ["#0ea5e9", "#4338ca"],
};

// Per-provider tiles for the websearch rows, which share one kind.
const PROVIDER_GRADIENT: Record<string, [string, string]> = {
  serper: ["#4285f4", "#0f9d58"],
  staan: ["#8b5cf6", "#2563eb"],
};

const GITHUB_GLYPH = {
  box: 16,
  d: "M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27s1.36.09 2 .27c1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.01 8.01 0 0 0 16 8c0-4.42-3.58-8-8-8Z",
};
const GITLAB_GLYPH = {
  box: 24,
  d: "m23.6 9.593-.033-.086L20.3 1.011a.851.851 0 0 0-.336-.405.875.875 0 0 0-1 .054.875.875 0 0 0-.29.44l-2.207 6.748H7.537L5.33 1.1a.858.858 0 0 0-.29-.441.875.875 0 0 0-1-.054.86.86 0 0 0-.336.405L.437 9.502l-.032.086a6.066 6.066 0 0 0 2.012 7.01l.011.009.03.021 4.977 3.727 2.462 1.863 1.5 1.132a1.009 1.009 0 0 0 1.22 0l1.499-1.132 2.462-1.863 5.006-3.75.013-.01a6.068 6.068 0 0 0 2.003-7.002Z",
};

// A globe under a magnifier: the web, searched.
const DONSETCH_GLYPH = {
  box: 24,
  d: "M10.5 2a8.5 8.5 0 1 0 5.262 15.176l4.03 4.031a1 1 0 0 0 1.415-1.414l-4.03-4.031A8.5 8.5 0 0 0 10.5 2Zm0 2c.9 0 1.86 1.01 2.4 2.75H8.1C8.64 5.01 9.6 4 10.5 4ZM7.7 8.25h5.6a11.6 11.6 0 0 1 0 4.5H7.7a11.6 11.6 0 0 1 0-4.5Zm-1.55 4.5H4.29a6.53 6.53 0 0 1 0-4.5h1.86a13.6 13.6 0 0 0 0 4.5Zm.5 2h1.45c.29.96.68 1.79 1.15 2.42a6.53 6.53 0 0 1-2.6-2.42Zm4.35 2.75c-.9 0-1.86-1.01-2.4-2.75h4.8c-.54 1.74-1.5 2.75-2.4 2.75Zm2.9-.33c.47-.63.86-1.46 1.15-2.42h1.45a6.53 6.53 0 0 1-2.6 2.42Zm1.55-4.42a13.6 13.6 0 0 0 0-4.5h1.86a6.53 6.53 0 0 1 0 4.5h-1.86Zm-.4-6.5c-.29-.96-.68-1.79-1.15-2.42a6.53 6.53 0 0 1 2.6 2.42h-1.45Zm-6.85 0H6.75a6.53 6.53 0 0 1 2.6-2.42c-.47.63-.86 1.46-1.15 2.42Z",
};

// A plain magnifier for the keyed search providers.
const SEARCH_GLYPH = {
  box: 24,
  d: "M10 2a8 8 0 1 0 4.9 14.32l4.4 4.39a1 1 0 0 0 1.4-1.42l-4.38-4.39A8 8 0 0 0 10 2Zm0 2a6 6 0 1 1 0 12 6 6 0 0 1 0-12Z",
};

function ToolIcon({ tool, size = 56 }: { tool: Tool; size?: number }) {
  const grad =
    tool.kind === "websearch"
      ? PROVIDER_GRADIENT[tool.provider ?? ""]
      : KIND_GRADIENT[tool.kind];
  const glyph =
    tool.kind === "github" ? GITHUB_GLYPH
    : tool.kind === "gitlab" ? GITLAB_GLYPH
    : tool.kind === "donsetch" ? DONSETCH_GLYPH
    : tool.kind === "websearch" ? SEARCH_GLYPH
    : null;
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
    patch: { url?: string; api_key?: string; enabled?: boolean; capabilities?: string[] },
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
                  disabled={busy === t.id || (needsKey(t) && !t.enabled)}
                  title={
                    needsKey(t) && !t.enabled
                      ? "Set the provider API key first"
                      : t.enabled
                        ? undefined
                        : "Enabling runs the tool-poisoning scan"
                  }
                  onChange={(v) =>
                    save(t, { enabled: v }, `${t.name} ${v ? "enabled" : "disabled"}.`)
                  }
                />
              </div>
            </div>
            <p className="store-desc">{toolCopy(t)}</p>
            <div onClick={(e) => e.stopPropagation()}>
              <McpNameChip server={t.mcp_server} />
            </div>
            {/* Not wrapped in a stopPropagation guard: the chips are read-only,
                so they must not steal the click that opens the card. */}
            {t.all_capabilities && (
              <div className="row gap-sm wrap">
                {t.all_capabilities.map((c) => (
                  <span
                    key={c.slug}
                    className={`chip tiny ${t.capabilities?.includes(c.slug) ? "" : "faint"}`}
                    title={
                      t.capabilities?.includes(c.slug)
                        ? `${c.label} is available to builds`
                        : `${c.label} is turned off - builds never see it`
                    }
                  >
                    {t.capabilities?.includes(c.slug) ? "✓" : "✗"} {c.label}
                  </span>
                ))}
              </div>
            )}
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
                disabled={busy === selected.id || (needsKey(selected) && !selected.enabled)}
                title={
                  needsKey(selected) && !selected.enabled
                    ? "Set the provider API key first"
                    : selected.enabled
                      ? undefined
                      : "Enabling runs the tool-poisoning scan"
                }
                onChange={(v) =>
                  save(selected, { enabled: v }, `${selected.name} ${v ? "enabled" : "disabled"}.`)
                }
              />
            </div>
          </div>
          <p className="muted small" style={{ marginTop: "0.35rem" }}>
            {toolCopy(selected)}
            {selected.kind === "websearch" && PROVIDER_COPY[selected.provider ?? ""] && (
              <>
                {" "}Get a key at{" "}
                <a
                  href={PROVIDER_COPY[selected.provider ?? ""].keyUrl}
                  target="_blank"
                  rel="noreferrer"
                >
                  {PROVIDER_COPY[selected.provider ?? ""].keyUrl.replace("https://", "")}
                </a>
                .
              </>
            )}
          </p>
          {selected.kind === "donsetch" && selected.all_capabilities && (
            <div className="field mt">
              <label>Capabilities</label>
              <p className="tiny faint" style={{ marginTop: 0 }}>
                What a build may call. A capability turned off here is absent from the
                agent's tool list for every run - not merely discouraged.
              </p>
              {selected.all_capabilities.map((c) => {
                const on = selected.capabilities?.includes(c.slug) ?? false;
                const last = on && (selected.capabilities?.length ?? 0) === 1;
                return (
                  <div key={c.slug} className="row gap-sm mt" style={{ alignItems: "center" }}>
                    <Toggle
                      checked={on}
                      disabled={busy === selected.id || (last && selected.enabled)}
                      title={
                        last && selected.enabled
                          ? "The last capability - disable the tool itself instead"
                          : undefined
                      }
                      onChange={(v) => {
                        const cur = selected.capabilities ?? [];
                        const next = v ? [...cur, c.slug] : cur.filter((x) => x !== c.slug);
                        save(
                          selected,
                          { capabilities: next },
                          `${c.label} ${v ? "enabled" : "disabled"}.`,
                        );
                      }}
                    />
                    <span className="small">{c.label}</span>
                  </div>
                );
              })}
              <p className="tiny faint mt">
                Page fetch and site crawl reach arbitrary public pages; private and
                loopback addresses are refused by the engine. Search alone is the
                low-blast-radius option.
              </p>
            </div>
          )}
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
            {selected.kind === "donsetch" && (
              <p className="tiny faint mt">
                The web-research sidecar's base URL. The path a build receives is derived
                from the capabilities above, so it changes when you toggle them.
              </p>
            )}
          </div>
          {selected.kind !== "donsetch" && (
          <div className="field mt">
            <label>
              API key{" "}
              <span
                title={
                  selected.kind === "websearch"
                    ? `The provider API key for ${selected.name}. It is re-probed against the provider every time you enable this row, and rides to the sidecar per request - the platform never stores it anywhere else.`
                    : `Personal access token used as the Bearer credential for the ${selected.name} MCP endpoint. Per-project keys fall back to the project's ${selected.kind === "github" ? "GITHUB_TOKEN" : "GITLAB_TOKEN"} Memory secret before this one.`
                }
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
                placeholder={
                  selected.has_api_key
                    ? "••••••••"
                    : selected.kind === "websearch"
                      ? (PROVIDER_COPY[selected.provider ?? ""]?.placeholder ?? "provider API key")
                      : selected.kind === "github"
                        ? "ghp_… / github_pat_…"
                        : "glpat-…"
                }
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
              {selected.has_api_key ? "✓ Key set - saving replaces it." : "No key yet."}{" "}
              {selected.kind === "websearch"
                ? "Enabling re-verifies it against the provider; clearing it disables the row."
                : "Builds prefer the project's own Memory token when present."}
            </p>
          </div>
          )}
          {selected.kind === "donsetch" && (
            <p className="tiny faint mt">
              No API key: the engine queries public search backends directly and holds no
              account. Nothing to configure beyond the capabilities above.
            </p>
          )}
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
