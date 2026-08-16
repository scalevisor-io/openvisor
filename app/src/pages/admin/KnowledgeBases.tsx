import { useEffect, useState } from "react";
import { kbApi } from "../../lib/endpoints";
import { useToast } from "../../lib/toast";
import { Alert, Badge, CopyField, Loading, McpNameChip, Modal, Toggle } from "../../components/ui";
import type { KbTiers, KnowledgeBase } from "../../types";

// Admin-only management of the instance's knowledge bases (§KB). Five kinds:
// the built-in local /knowledge KB and the repo's Context7 MCP (seeded, only
// toggleable), seeded web-search providers (key + toggle; enabling verifies the
// key server-side), generic MCP knowledge bases the admin adds by URL + key, and
// git repositories cloned by the worker and indexed alongside /knowledge. All KB
// content is treated as confidential consultant IP. The API never returns an
// api_key/PAT or an SSH private key - only whether one is set - so no secret is
// rendered here (an SSH source's PUBLIC deploy key is shown so it can be installed).

// Provider help shown under a websearch row (kb.uri is the provider slug).
const WEBSEARCH_HINT: Record<string, { blurb: string; keyUrl: string }> = {
  serper: {
    blurb: "Google results through the Serper API, exposed to the dev agent as a web_search tool.",
    keyUrl: "https://serper.dev",
  },
  staan: {
    blurb:
      "European search index (Qwant + Ecosia joint venture) - queries stay under EU jurisdiction. Needs the Web-for-AI product.",
    keyUrl: "https://staan.ai",
  },
};

export default function KnowledgeBases() {
  const toast = useToast();
  const [kbs, setKbs] = useState<KnowledgeBase[] | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [editing, setEditing] = useState<KnowledgeBase | "new" | null>(null);
  const [addingGit, setAddingGit] = useState(false);
  const [gitEditing, setGitEditing] = useState<KnowledgeBase | null>(null);
  const [keyEditing, setKeyEditing] = useState<KnowledgeBase | null>(null);
  const [tiersFor, setTiersFor] = useState<KnowledgeBase | null>(null);

  function load() {
    kbApi
      .list()
      .then(setKbs)
      .catch((err) =>
        toast.push(err instanceof Error ? err.message : "Could not load knowledge bases.", "err"),
      );
  }

  useEffect(load, []);

  async function toggle(kb: KnowledgeBase, enabled: boolean) {
    const previous = kbs;
    setBusyId(kb.id);
    // Optimistic for the built-in/mcp rows; git enabling can be rejected server-side
    // (the connection check re-runs), so reload afterwards to reflect verified state.
    setKbs((cur) => cur?.map((r) => (r.id === kb.id ? { ...r, enabled } : r)) ?? cur);
    try {
      await kbApi.update(kb.id, { enabled });
      toast.push(enabled ? "Knowledge base enabled" : "Knowledge base disabled", "ok");
      if (kb.kind === "git" || kb.kind === "websearch") load();
    } catch (err) {
      setKbs(previous ?? null); // revert
      toast.push(err instanceof Error ? err.message : "Could not save.", "err");
    } finally {
      setBusyId(null);
    }
  }

  async function reverify(kb: KnowledgeBase) {
    setBusyId(kb.id);
    try {
      const res = await kbApi.verify(kb.id);
      toast.push(res.ok ? "Connection OK" : `Connection failed: ${res.detail}`, res.ok ? "ok" : "err");
      load();
    } catch (err) {
      toast.push(err instanceof Error ? err.message : "Could not check the connection.", "err");
    } finally {
      setBusyId(null);
    }
  }

  async function reindex(kb: KnowledgeBase) {
    setBusyId(kb.id);
    try {
      await kbApi.reindex();
      toast.push("Reindex started - it runs in the background.", "ok");
    } catch (err) {
      toast.push(err instanceof Error ? err.message : "Could not start reindex.", "err");
    } finally {
      setBusyId(null);
    }
  }

  async function remove(kb: KnowledgeBase) {
    if (!window.confirm(`Remove the "${kb.name}" knowledge base?`)) return;
    setBusyId(kb.id);
    try {
      await kbApi.remove(kb.id);
      toast.push("Knowledge base removed", "ok");
      load();
    } catch (err) {
      toast.push(err instanceof Error ? err.message : "Could not remove.", "err");
    } finally {
      setBusyId(null);
    }
  }

  if (!kbs) return <Loading />;

  return (
    <div>
      <div className="page-head">
        <div>
          <h1>Knowledge bases</h1>
          <p className="muted">
            Sources the dev agent consults while building. All content is treated as confidential.
          </p>
        </div>
        <div className="row gap-sm">
          <button type="button" className="btn" onClick={() => setAddingGit(true)}>
            Add Git repository
          </button>
          <button type="button" className="btn btn-primary" onClick={() => setEditing("new")}>
            Add MCP knowledge base
          </button>
        </div>
      </div>

      <Alert kind="info">
        The built-in knowledge bases can be turned off but not removed. Disabling the local knowledge
        base stops it from being retrieved during builds (and costs nothing while off). Add MCP
        knowledge bases (like Notion) or connect a Git repository to give the agent more context.
      </Alert>

      <div className="card mt" style={{ maxWidth: 760 }}>
        {kbs.map((kb) => {
          const builtin = kb.kind === "local" || kb.kind === "context7";
          return (
            <div key={kb.id} className="kb-row between" style={{ alignItems: "flex-start", gap: "1rem" }}>
              <div style={{ minWidth: 0 }}>
                <div className="row gap-sm" style={{ alignItems: "center", flexWrap: "wrap" }}>
                  <span
                    className={`badge ${
                      kb.kind === "git"
                        ? "badge-kb-git"
                        : kb.kind === "websearch"
                          ? "badge-kb-websearch"
                          : builtin
                            ? "badge-kb-builtin"
                            : "badge-kb-mcp"
                    }`}
                  >
                    {kb.kind === "git" ? "Git" : kb.kind === "websearch" ? "Web search" : builtin ? "Built-in" : "MCP"}
                  </span>
                  <strong>{kb.name}</strong>
                  {kb.kind === "git" && (
                    <span className={`badge ${kb.verified ? "badge-ok" : "badge-warn"}`}>
                      {kb.verified ? "Verified" : "Unverified"}
                    </span>
                  )}
                </div>

                {builtin && (
                  <div className="muted small mt-xs">
                    {kb.kind === "local"
                      ? "The platform /knowledge base, indexed for retrieval."
                      : "Context7 library documentation, available to the agent."}
                  </div>
                )}
                <McpNameChip server={kb.mcp_server} tools={kb.mcp_tools} />
                {kb.kind === "mcp" && (
                  <div className="muted small mt-xs" style={{ wordBreak: "break-all" }}>
                    {kb.uri}
                    {kb.has_api_key ? " · API key set" : " · no API key"}
                  </div>
                )}
                {kb.kind === "websearch" && (
                  <div className="muted small mt-xs">
                    {WEBSEARCH_HINT[kb.uri ?? ""]?.blurb ?? "Web search for the dev agent."}
                    {kb.has_api_key ? " · API key set" : " · no API key"}
                  </div>
                )}
                {kb.kind === "git" && (
                  <>
                    <div className="muted small mt-xs" style={{ wordBreak: "break-all" }}>
                      {kb.uri} · {kb.auth_kind === "ssh" ? "SSH deploy key" : "HTTPS token"} · branch{" "}
                      {kb.ref || "main"}
                    </div>
                    {kb.last_index_error ? (
                      <div className="small mt-xs" style={{ color: "var(--danger, #c0392b)" }}>
                        Last index error: {kb.last_index_error}
                      </div>
                    ) : (
                      kb.last_indexed_at && (
                        <div className="muted small mt-xs">
                          Last indexed {new Date(kb.last_indexed_at).toLocaleString()}
                        </div>
                      )
                    )}
                  </>
                )}

                <div className="row gap-sm mt-xs">
                  {(kb.kind === "local" || kb.kind === "git") && (
                    <button
                      type="button"
                      className="btn btn-sm"
                      disabled={busyId === kb.id || !kb.enabled || (kb.kind === "git" && !kb.verified)}
                      onClick={() => reindex(kb)}
                      title={
                        !kb.enabled
                          ? "Enable the knowledge base to reindex"
                          : kb.kind === "git" && !kb.verified
                            ? "Verify the connection to include this source in the reindex"
                            : "Re-embed and re-index every knowledge source now"
                      }
                    >
                      Reindex now
                    </button>
                  )}
                  {(kb.kind === "local" || kb.kind === "git") && (
                    <button
                      type="button"
                      className="btn btn-sm"
                      disabled={busyId === kb.id}
                      onClick={() => setTiersFor(kb)}
                      title="Inspect how this source's content was classified (facts / standing rules / procedures) and pin corrections"
                    >
                      Content tiers
                    </button>
                  )}
                  {kb.kind === "git" && (
                    <button
                      type="button"
                      className="btn btn-sm"
                      disabled={busyId === kb.id}
                      onClick={() => reverify(kb)}
                    >
                      Check connection
                    </button>
                  )}
                  {kb.kind === "git" && (
                    <button
                      type="button"
                      className="btn btn-sm"
                      disabled={busyId === kb.id}
                      onClick={() => setGitEditing(kb)}
                      title="Change the URL, branch or credentials of this source"
                    >
                      Edit
                    </button>
                  )}
                  {kb.kind === "mcp" && (
                    <button
                      type="button"
                      className="btn btn-sm"
                      disabled={busyId === kb.id}
                      onClick={() => setEditing(kb)}
                    >
                      Edit
                    </button>
                  )}
                  {kb.kind === "websearch" && (
                    <button
                      type="button"
                      className="btn btn-sm"
                      disabled={busyId === kb.id}
                      onClick={() => setKeyEditing(kb)}
                    >
                      {kb.has_api_key ? "Replace API key" : "Set API key"}
                    </button>
                  )}
                  {kb.is_removable && (
                    <button
                      type="button"
                      className="btn btn-sm btn-danger"
                      disabled={busyId === kb.id}
                      onClick={() => remove(kb)}
                    >
                      Remove
                    </button>
                  )}
                </div>
              </div>
              <Toggle
                checked={kb.enabled}
                disabled={busyId === kb.id || (kb.kind === "websearch" && !kb.enabled && !kb.has_api_key)}
                title={
                  kb.kind === "websearch" && !kb.enabled && !kb.has_api_key
                    ? "Set the provider API key first"
                    : undefined
                }
                onChange={(v) => toggle(kb, v)}
              />
            </div>
          );
        })}
      </div>

      {editing && (
        <McpModal
          kb={editing === "new" ? null : editing}
          onClose={() => setEditing(null)}
          onSaved={() => {
            setEditing(null);
            load();
          }}
        />
      )}
      {addingGit && (
        <GitModal
          onClose={() => setAddingGit(false)}
          onDone={() => {
            setAddingGit(false);
            load();
          }}
        />
      )}
      {gitEditing && (
        <GitEditModal
          kb={gitEditing}
          onClose={() => setGitEditing(null)}
          onSaved={() => {
            setGitEditing(null);
            load();
          }}
        />
      )}
      {keyEditing && (
        <WebsearchKeyModal
          kb={keyEditing}
          onClose={() => setKeyEditing(null)}
          onSaved={() => {
            setKeyEditing(null);
            load();
          }}
        />
      )}
      {tiersFor && <TiersModal kb={tiersFor} onClose={() => setTiersFor(null)} />}
    </div>
  );
}

// §KB tiers: the derived split of one retrieval source - the compiled
// standing-rules digest, per-class counts, and every classified block with an
// override control. Pinning a class (or reverting one) dispatches a forced
// reindex, so the index, the digest and the procedure registry follow.
function TiersModal({ kb, onClose }: { kb: KnowledgeBase; onClose: () => void }) {
  const toast = useToast();
  const [tiers, setTiers] = useState<KbTiers | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busyHash, setBusyHash] = useState<string | null>(null);
  const [filter, setFilter] = useState<"all" | "fact" | "rule" | "procedure">("all");
  const [page, setPage] = useState(1);

  const query = () =>
    kbApi.tiers(kb.id, { content_class: filter === "all" ? undefined : filter, page });

  useEffect(() => {
    kbApi
      .tiers(kb.id, { content_class: filter === "all" ? undefined : filter, page })
      .then(setTiers)
      .catch((err) => setError(err instanceof Error ? err.message : "Could not load tiers."));
  }, [kb.id, filter, page]);

  // A filter switch or a reindex can shrink the listing under the current page
  // (e.g. an override moved the last block of the page): fall back to page 1
  // instead of showing an empty window.
  useEffect(() => {
    if (tiers && tiers.blocks.length === 0 && tiers.page > 1) setPage(1);
  }, [tiers]);

  async function pin(hash: string, cls: "fact" | "rule" | "procedure") {
    setBusyHash(hash);
    try {
      await kbApi.overrideBlock(hash, cls);
      setTiers((cur) =>
        cur
          ? {
              ...cur,
              blocks: cur.blocks.map((b) =>
                b.block_hash === hash ? { ...b, content_class: cls, origin: "override" } : b,
              ),
            }
          : cur,
      );
      toast.push("Class pinned - reindex dispatched", "ok");
    } catch (err) {
      toast.push(err instanceof Error ? err.message : "Could not save.", "err");
    } finally {
      setBusyHash(null);
    }
  }

  async function revert(hash: string) {
    setBusyHash(hash);
    try {
      await kbApi.clearOverride(hash);
      toast.push("Override cleared - the block re-classifies on the dispatched reindex", "ok");
      setTiers(await query());
    } catch (err) {
      toast.push(err instanceof Error ? err.message : "Could not revert.", "err");
    } finally {
      setBusyHash(null);
    }
  }

  return (
    <Modal title={`Content tiers - ${kb.name}`} onClose={onClose} wide>
      <Alert kind="info">
        Ingestion classifies every block of this source: <strong>facts</strong> feed retrieval,{" "}
        <strong>standing rules</strong> are injected into every dev run in scope, and{" "}
        <strong>procedures</strong> load in full when a task matches them. Pin a class below to
        correct the classifier - the pin survives reindexes until the block's text changes.
      </Alert>
      {error && <Alert kind="error">{error}</Alert>}
      {!tiers && !error && <Loading />}
      {tiers && (
        <>
          <div className="row gap-sm mt-sm">
            {(
              [
                ["all", `All (${tiers.counts.fact + tiers.counts.rule + tiers.counts.procedure})`],
                ["fact", `Facts (${tiers.counts.fact})`],
                ["rule", `Standing rules (${tiers.counts.rule})`],
                ["procedure", `Procedures (${tiers.counts.procedure})`],
              ] as const
            ).map(([value, label]) => (
              <button
                key={value}
                type="button"
                className={`btn btn-sm${filter === value ? " btn-primary" : ""}`}
                aria-pressed={filter === value}
                onClick={() => {
                  setFilter(value);
                  setPage(1);
                }}
              >
                {label}
              </button>
            ))}
          </div>
          {tiers.digest && (
            <div className="mt-sm">
              <strong>Standing-rules digest</strong>
              <div className="muted small">
                {tiers.digest.char_count} chars, compiled{" "}
                {new Date(tiers.digest.compiled_at).toLocaleString()} - injected into every dev run
                that selects this source.
              </div>
              <pre className="kb-example" style={{ maxHeight: 220, overflow: "auto" }}>
                {tiers.digest.content}
              </pre>
            </div>
          )}
          <div className="mt-sm">
            <strong>Blocks</strong> <span className="muted small">({tiers.total})</span>
            {tiers.blocks.length === 0 && (
              <div className="muted small">
                {filter === "all"
                  ? "Nothing indexed for this source yet - run a reindex first."
                  : "No blocks of this class in this source."}
              </div>
            )}
            {tiers.blocks.map((b) => (
              <div key={b.block_hash} className="kb-row between" style={{ alignItems: "start" }}>
                <div style={{ minWidth: 0 }}>
                  <div className="small">
                    <strong>{b.rel}</strong>
                    <span className="muted">
                      {" "}
                      · {b.chunks} chunk{b.chunks > 1 ? "s" : ""}
                    </span>
                    {b.origin === "override" && <Badge label="pinned" kind="warn" />}
                    {b.origin === "llm" && <Badge label="model" kind="kb-builtin" />}
                  </div>
                  <div
                    className="muted small"
                    style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}
                  >
                    {b.excerpt}
                    {b.excerpt.length >= 300 ? "…" : ""}
                  </div>
                </div>
                <div className="row gap-sm" style={{ flexShrink: 0 }}>
                  <select
                    value={b.content_class}
                    disabled={busyHash === b.block_hash}
                    onChange={(e) =>
                      pin(b.block_hash, e.target.value as "fact" | "rule" | "procedure")
                    }
                  >
                    <option value="fact">fact</option>
                    <option value="rule">rule</option>
                    <option value="procedure">procedure</option>
                  </select>
                  {b.origin === "override" && (
                    <button
                      type="button"
                      className="btn btn-sm"
                      disabled={busyHash === b.block_hash}
                      onClick={() => revert(b.block_hash)}
                      title="Remove the pin and let the classifier decide again"
                    >
                      Revert
                    </button>
                  )}
                </div>
              </div>
            ))}
            {tiers.total > tiers.per_page && (
              <div
                className="row gap-sm mt-sm"
                style={{ alignItems: "center", justifyContent: "center" }}
              >
                <button
                  type="button"
                  className="btn btn-sm"
                  disabled={tiers.page <= 1}
                  onClick={() => setPage((p) => Math.max(1, p - 1))}
                >
                  ‹ Prev
                </button>
                <span className="muted small">
                  page {tiers.page} of {Math.max(1, Math.ceil(tiers.total / tiers.per_page))}
                </span>
                <button
                  type="button"
                  className="btn btn-sm"
                  disabled={tiers.page >= Math.ceil(tiers.total / tiers.per_page)}
                  onClick={() => setPage((p) => p + 1)}
                >
                  Next ›
                </button>
              </div>
            )}
          </div>
        </>
      )}
    </Modal>
  );
}

// Set/replace a seeded web-search provider's API key. Enabling happens with the
// row toggle afterwards (the API re-verifies the key server-side at that point).
function WebsearchKeyModal({
  kb,
  onClose,
  onSaved,
}: {
  kb: KnowledgeBase;
  onClose: () => void;
  onSaved: () => void;
}) {
  const toast = useToast();
  const [apiKey, setApiKey] = useState("");
  const [saving, setSaving] = useState(false);
  const hint = WEBSEARCH_HINT[kb.uri ?? ""];

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setSaving(true);
    try {
      await kbApi.update(kb.id, { api_key: apiKey.trim() });
      toast.push("API key saved - enable the source to start using it", "ok");
      onSaved();
    } catch (err) {
      toast.push(err instanceof Error ? err.message : "Could not save the key.", "err");
    } finally {
      setSaving(false);
    }
  }

  return (
    <Modal title={`API key - ${kb.name}`} onClose={onClose}>
      {hint && (
        <p className="muted small mb">
          {hint.blurb} Get a key at <a href={hint.keyUrl} target="_blank" rel="noreferrer">{hint.keyUrl.replace("https://", "")}</a>.
        </p>
      )}
      <form onSubmit={submit}>
        <label className="field">
          <span>API key</span>
          <input
            type="password"
            value={apiKey}
            onChange={(e) => setApiKey(e.target.value)}
            placeholder={kb.has_api_key ? "••••••••" : ""}
            autoComplete="new-password"
            required
          />
        </label>
        <div className="row gap-sm mt">
          <button type="submit" className="btn btn-primary" disabled={saving}>
            Save key
          </button>
          <button type="button" className="btn" onClick={onClose} disabled={saving}>
            Cancel
          </button>
        </div>
      </form>
    </Modal>
  );
}

// Two-step onboarding for a git knowledge source. Step 1 collects the URL + auth
// mode (SSH: the platform generates a deploy keypair; HTTP: the admin provides a
// PAT). Step 2 shows the generated SSH public key (for SSH) and runs the live
// connection check, then offers Enable (which the API re-verifies server-side).
function GitModal({ onClose, onDone }: { onClose: () => void; onDone: () => void }) {
  const toast = useToast();
  const [step, setStep] = useState<1 | 2>(1);
  const [name, setName] = useState("");
  const [uri, setUri] = useState("");
  const [authKind, setAuthKind] = useState<"ssh" | "http">("ssh");
  const [ref, setRef] = useState("main");
  const [pat, setPat] = useState("");
  const [httpUser, setHttpUser] = useState("");
  const [saving, setSaving] = useState(false);
  const [created, setCreated] = useState<KnowledgeBase | null>(null);
  const [check, setCheck] = useState<{ ok: boolean; detail: string } | null>(null);

  async function create(e: React.FormEvent) {
    e.preventDefault();
    setSaving(true);
    try {
      const kb = await kbApi.createGit({
        name: name.trim() || undefined,
        uri: uri.trim(),
        auth_kind: authKind,
        ref: ref.trim() || undefined,
        ...(authKind === "http" && pat.trim() ? { api_key: pat.trim() } : {}),
        ...(authKind === "http" && httpUser.trim() ? { http_username: httpUser.trim() } : {}),
      });
      setCreated(kb);
      setCheck(null);
      setStep(2);
    } catch (err) {
      toast.push(err instanceof Error ? err.message : "Could not create the source.", "err");
    } finally {
      setSaving(false);
    }
  }

  async function runCheck() {
    if (!created) return;
    setSaving(true);
    try {
      const res = await kbApi.verify(created.id);
      setCheck(res);
    } catch (err) {
      toast.push(err instanceof Error ? err.message : "Could not check the connection.", "err");
    } finally {
      setSaving(false);
    }
  }

  async function enable() {
    if (!created) return;
    setSaving(true);
    try {
      await kbApi.update(created.id, { enabled: true });
      toast.push("Git knowledge source enabled", "ok");
      onDone();
    } catch (err) {
      toast.push(err instanceof Error ? err.message : "Could not enable the source.", "err");
    } finally {
      setSaving(false);
    }
  }

  return (
    <Modal title="Connect a Git repository" onClose={onClose} wide>
      {step === 1 && (
        <form onSubmit={create}>
          <p className="muted small mb">
            The repository is cloned by the platform and indexed alongside the /knowledge base.
            Connect it read-only.
          </p>
          <label className="field">
            <span>Name <span className="muted small">(optional)</span></span>
            <input value={name} onChange={(e) => setName(e.target.value)} placeholder="Internal handbook" maxLength={255} />
          </label>
          <label className="field">
            <span>Authentication</span>
            <div className="row gap-sm">
              <label className="row gap-sm" style={{ alignItems: "center" }}>
                <input type="radio" checked={authKind === "ssh"} onChange={() => setAuthKind("ssh")} /> SSH deploy key
              </label>
              <label className="row gap-sm" style={{ alignItems: "center" }}>
                <input type="radio" checked={authKind === "http"} onChange={() => setAuthKind("http")} /> HTTPS token
              </label>
            </div>
          </label>
          <label className="field">
            <span>Repository URL</span>
            <input
              value={uri}
              onChange={(e) => setUri(e.target.value)}
              placeholder={authKind === "ssh" ? "git@github.com:acme/handbook.git" : "https://github.com/acme/handbook.git"}
              required
              maxLength={512}
            />
          </label>
          <label className="field">
            <span>Branch</span>
            <input value={ref} onChange={(e) => setRef(e.target.value)} placeholder="main" maxLength={128} />
          </label>
          {authKind === "http" && (
            <>
              <label className="field">
                <span>Access token</span>
                <input
                  type="password"
                  value={pat}
                  onChange={(e) => setPat(e.target.value)}
                  placeholder="ghp_… / glpat-… / gldt-…"
                  autoComplete="new-password"
                  required
                />
              </label>
              <label className="field">
                <span>Username (optional)</span>
                <input
                  value={httpUser}
                  onChange={(e) => setHttpUser(e.target.value)}
                  placeholder="oauth2"
                  maxLength={255}
                  autoComplete="off"
                />
                <span className="muted small">
                  Leave empty for a personal access token. A GitLab deploy token needs its
                  generated username (gitlab+deploy-token-N); a Bitbucket app password needs
                  your account username.
                </span>
              </label>
            </>
          )}
          <div className="row gap-sm mt">
            <button type="submit" className="btn btn-primary" disabled={saving}>
              {authKind === "ssh" ? "Create & generate deploy key" : "Create"}
            </button>
            <button type="button" className="btn" onClick={onClose} disabled={saving}>
              Cancel
            </button>
          </div>
        </form>
      )}

      {step === 2 && created && (
        <div>
          {created.auth_kind === "ssh" && created.ssh_public_key && (
            <div className="mb">
              <p className="small">
                Add this as a <strong>read-only deploy key</strong> on your repository, then run the
                connection check below.
              </p>
              <CopyField value={created.ssh_public_key} block />
            </div>
          )}
          {created.auth_kind === "http" && (
            <p className="small mb">
              Run the connection check to confirm the token can read <strong>{created.uri}</strong>.
            </p>
          )}

          <div className="row gap-sm">
            <button type="button" className="btn" onClick={runCheck} disabled={saving}>
              Check connection
            </button>
          </div>
          {check && (
            <Alert kind={check.ok ? "success" : "error"}>
              {check.ok ? "Connected. " : "Connection failed. "}
              {check.detail}
            </Alert>
          )}

          <div className="row gap-sm mt">
            <button
              type="button"
              className="btn btn-primary"
              onClick={enable}
              disabled={saving || !check?.ok}
              title={check?.ok ? "Enable and index this source" : "Pass the connection check first"}
            >
              Enable source
            </button>
            <button type="button" className="btn" onClick={onDone} disabled={saving}>
              Done (leave disabled)
            </button>
          </div>
        </div>
      )}
    </Modal>
  );
}

// Edit an existing git knowledge source: name, URL, branch, and for an HTTP
// source the token (write-only - leave blank to keep the stored one) and Basic
// username. The API re-arms `verified:false` on any target/credential change,
// so saving immediately re-runs the connection check and shows the result; a
// disabled source can be enabled right here once the check passes. An SSH
// source keeps its generated deploy keypair - the public key is re-shown for
// installing on a moved repository, never regenerated.
function GitEditModal({ kb, onClose, onSaved }: {
  kb: KnowledgeBase;
  onClose: () => void;
  onSaved: () => void;
}) {
  const toast = useToast();
  const [name, setName] = useState(kb.name);
  const [uri, setUri] = useState(kb.uri ?? "");
  const [ref, setRef] = useState(kb.ref ?? "main");
  const [pat, setPat] = useState("");
  const [httpUser, setHttpUser] = useState(kb.http_username ?? "");
  const [saving, setSaving] = useState(false);
  const [savedOnce, setSavedOnce] = useState(false);
  const [check, setCheck] = useState<{ ok: boolean; detail: string } | null>(null);

  // After a save the list is stale (name/uri/verified changed), so every way out
  // of the modal goes through onSaved to reload it.
  const close = savedOnce ? onSaved : onClose;

  async function save(e: React.FormEvent) {
    e.preventDefault();
    setSaving(true);
    try {
      await kbApi.update(kb.id, {
        name: name.trim() || kb.name,
        uri: uri.trim(),
        ref: ref.trim() || "main",
        ...(kb.auth_kind === "http" && pat.trim() ? { api_key: pat.trim() } : {}),
        ...(kb.auth_kind === "http" ? { http_username: httpUser.trim() } : {}),
      });
      setSavedOnce(true);
      const res = await kbApi.verify(kb.id);
      setCheck(res);
      toast.push(res.ok ? "Saved - connection check passed" : "Saved, but the connection check failed", res.ok ? "ok" : "err");
    } catch (err) {
      toast.push(err instanceof Error ? err.message : "Could not save the source.", "err");
    } finally {
      setSaving(false);
    }
  }

  async function enable() {
    setSaving(true);
    try {
      await kbApi.update(kb.id, { enabled: true });
      toast.push("Git knowledge source enabled", "ok");
      onSaved();
    } catch (err) {
      toast.push(err instanceof Error ? err.message : "Could not enable the source.", "err");
    } finally {
      setSaving(false);
    }
  }

  return (
    <Modal title="Edit Git knowledge source" onClose={close} wide>
      <form onSubmit={save}>
        <label className="field">
          <span>Name</span>
          <input value={name} onChange={(e) => setName(e.target.value)} maxLength={255} />
        </label>
        <label className="field">
          <span>Repository URL</span>
          <input
            value={uri}
            onChange={(e) => setUri(e.target.value)}
            placeholder={kb.auth_kind === "ssh" ? "git@github.com:acme/handbook.git" : "https://github.com/acme/handbook.git"}
            required
            maxLength={512}
          />
        </label>
        <label className="field">
          <span>Branch</span>
          <input value={ref} onChange={(e) => setRef(e.target.value)} placeholder="main" maxLength={128} />
        </label>
        {kb.auth_kind === "http" && (
          <>
            <label className="field">
              <span>
                Access token <span className="muted small">(leave blank to keep the current token)</span>
              </span>
              <input
                type="password"
                value={pat}
                onChange={(e) => setPat(e.target.value)}
                placeholder={kb.has_api_key ? "••••••••" : "ghp_… / glpat-… / gldt-…"}
                autoComplete="new-password"
              />
            </label>
            <label className="field">
              <span>Username (optional)</span>
              <input
                value={httpUser}
                onChange={(e) => setHttpUser(e.target.value)}
                placeholder="oauth2"
                maxLength={255}
                autoComplete="off"
              />
              <span className="muted small">
                Leave empty for a personal access token. A GitLab deploy token needs its
                generated username (gitlab+deploy-token-N); a Bitbucket app password needs
                your account username.
              </span>
            </label>
          </>
        )}
        {kb.auth_kind === "ssh" && kb.ssh_public_key && (
          <div className="mb">
            <p className="small">
              This source authenticates with its <strong>read-only deploy key</strong> - install it on the
              repository if the URL changed:
            </p>
            <CopyField value={kb.ssh_public_key} block />
          </div>
        )}
        {check && (
          <Alert kind={check.ok ? "success" : "error"}>
            {check.ok ? "Connected. " : "Connection failed. "}
            {check.detail}
          </Alert>
        )}
        <div className="row gap-sm mt">
          <button type="submit" className="btn btn-primary" disabled={saving}>
            Save &amp; check connection
          </button>
          {check?.ok && !kb.enabled && (
            <button
              type="button"
              className="btn btn-primary"
              onClick={enable}
              disabled={saving}
              title="Enable and index this source"
            >
              Enable source
            </button>
          )}
          <button type="button" className="btn" onClick={close} disabled={saving}>
            Close
          </button>
        </div>
      </form>
    </Modal>
  );
}

// Add or edit a generic MCP knowledge base. On add we show an illustrative
// "Notion" example (guidance only - never prefilled into the live inputs).
function McpModal({
  kb,
  onClose,
  onSaved,
}: {
  kb: KnowledgeBase | null;
  onClose: () => void;
  onSaved: () => void;
}) {
  const toast = useToast();
  const [name, setName] = useState(kb?.name ?? "");
  const [uri, setUri] = useState(kb?.uri ?? "");
  const [apiKey, setApiKey] = useState("");
  const [saving, setSaving] = useState(false);
  const isEdit = kb !== null;

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setSaving(true);
    try {
      const key = apiKey.trim() ? apiKey.trim() : undefined;
      if (isEdit) {
        await kbApi.update(kb!.id, { name: name.trim(), uri: uri.trim(), ...(key ? { api_key: key } : {}) });
      } else {
        await kbApi.create({ name: name.trim(), uri: uri.trim(), ...(key ? { api_key: key } : {}) });
      }
      toast.push(isEdit ? "Knowledge base saved" : "Knowledge base added", "ok");
      onSaved();
    } catch (err) {
      toast.push(err instanceof Error ? err.message : "Could not save.", "err");
    } finally {
      setSaving(false);
    }
  }

  return (
    <Modal title={isEdit ? "Edit MCP knowledge base" : "Add MCP knowledge base"} onClose={onClose} wide>
      {!isEdit && (
        <div className="mb">
          <div className="kb-example">
            <div className="kb-ex-row">
              <span className="k">Name</span>
              <span className="v">Notion</span>
            </div>
            <div className="kb-ex-row">
              <span className="k">URL</span>
              <span className="v">https://mcp.notion.com/mcp</span>
            </div>
            <div className="kb-ex-row">
              <span className="k">API key</span>
              <span className="v">ntn_…</span>
            </div>
          </div>
          <p className="muted small mt-xs">
            Example. Get the URL and API key from the provider's MCP integration settings.
          </p>
        </div>
      )}

      <form onSubmit={submit}>
        <label className="field">
          <span>Name</span>
          <input value={name} onChange={(e) => setName(e.target.value)} placeholder="Notion" required maxLength={255} />
        </label>
        <label className="field">
          <span>MCP URL</span>
          <input
            value={uri}
            onChange={(e) => setUri(e.target.value)}
            placeholder="https://mcp.notion.com/mcp"
            type="url"
            required
            maxLength={512}
          />
        </label>
        <label className="field">
          <span>API key {isEdit && <span className="muted small">(leave blank to keep the current key)</span>}</span>
          <input
            type="password"
            value={apiKey}
            onChange={(e) => setApiKey(e.target.value)}
            placeholder={isEdit && kb?.has_api_key ? "••••••••" : "ntn_…"}
            autoComplete="new-password"
          />
        </label>
        <div className="row gap-sm mt">
          <button type="submit" className="btn btn-primary" disabled={saving}>
            {isEdit ? "Save changes" : "Add knowledge base"}
          </button>
          <button type="button" className="btn" onClick={onClose} disabled={saving}>
            Cancel
          </button>
        </div>
      </form>
    </Modal>
  );
}
