import { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { adminApi, mcpApi, tokensApi } from "../lib/endpoints";
import { useAuth } from "../lib/auth";
import { useToast } from "../lib/toast";
import {
  Alert,
  CopyButton,
  CopyField,
  Loading,
  Pager,
  Spinner,
  relTime,
  usePager,
} from "../components/ui";
import type {
  ApiToken,
  McpProjectToken,
  NewApiToken,
  NewHubToken,
  NewMcpProject,
} from "../types";

const PAGE_SIZE = 10;

// Two audiences, two tabs. An MCP token belongs to a customer's coding agent; a
// hub token belongs to a Scalevisor hub orchestrating this spoke. They share a
// table and nothing else, and one mixed list made it easy to paste the wrong one.
type Tab = "mcp" | "hub";

// `?tab=` is the single source of truth, so /settings/tokens?tab=hub can be linked,
// bookmarked and sent to someone enrolling a spoke. Anything else - including a
// non-admin asking for the admin-only hub tab - falls back to the MCP tab.
function tabFromParam(value: string | null, isAdmin: boolean): Tab {
  return value === "hub" && isAdmin ? "hub" : "mcp";
}

export default function Tokens() {
  const toast = useToast();
  const { config, settings, isAdmin } = useAuth();
  const brandName = settings?.brand_name ?? "Openvisor";
  const brandSlug = settings?.brand_slug ?? "openvisor";
  const consultantName = settings?.consultant_name ?? "Consultant";
  const consultantFirst = settings?.consultant_first_name ?? "Consultant";
  const appHost = `app.${config?.deploy_domain ?? "openvisor.example.com"}`;
  const mcpUrl = config?.landing_base_url
    ? `${config.landing_base_url.replace("://", "://mcp.")}/mcp`
    : `https://mcp.${config?.deploy_domain ?? "openvisor.example.com"}/mcp`;

  const [params, setParams] = useSearchParams();
  const tab = tabFromParam(params.get("tab"), isAdmin);
  const setTab = (t: Tab) => {
    // Replace rather than push: back should leave the page, not walk the tabs.
    // Other query params survive - this owns `tab` and nothing else.
    const next = new URLSearchParams(params);
    if (t === "mcp") next.delete("tab");
    else next.set("tab", t);
    setParams(next, { replace: true });
  };
  const [tokens, setTokens] = useState<ApiToken[] | null>(null);
  const [projectTokens, setProjectTokens] = useState<McpProjectToken[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [fresh, setFresh] = useState<NewApiToken | null>(null);
  const [freshHub, setFreshHub] = useState<NewHubToken | null>(null);
  const [hubBusy, setHubBusy] = useState(false);

  // One-click project + token.
  const [projTitle, setProjTitle] = useState("");
  const [projDesc, setProjDesc] = useState("");
  const [projBusy, setProjBusy] = useState(false);
  const [created, setCreated] = useState<NewMcpProject | null>(null);

  // Hub tokens live in their own tab, so each list shows only its own kind.
  const accountTokens = (tokens ?? []).filter((t) => t.scope !== "hub");
  const hubTokens = (tokens ?? []).filter((t) => t.scope === "hub");
  const { pageItems, page, pages, setPage } = usePager(accountTokens, PAGE_SIZE);

  function load() {
    tokensApi
      .list()
      .then(setTokens)
      .catch((err) => setError(err instanceof Error ? err.message : "Failed to load tokens."));
    mcpApi
      .listMine()
      .then(setProjectTokens)
      .catch(() => setProjectTokens([]));
  }
  useEffect(load, []);

  async function create(e: React.FormEvent) {
    e.preventDefault();
    if (!name.trim()) return;
    setBusy(true);
    try {
      const t = await tokensApi.create(name.trim());
      setFresh(t);
      setName("");
      load();
    } catch (err) {
      toast.push(err instanceof Error ? err.message : "Failed", "err");
    } finally {
      setBusy(false);
    }
  }

  async function createProject(e: React.FormEvent) {
    e.preventDefault();
    if (!projTitle.trim()) return;
    setProjBusy(true);
    try {
      const r = await mcpApi.createProject(projTitle.trim(), projDesc.trim() || undefined);
      setCreated(r);
      setProjTitle("");
      setProjDesc("");
      load();
    } catch (err) {
      toast.push(err instanceof Error ? err.message : "Could not create the project", "err");
    } finally {
      setProjBusy(false);
    }
  }

  async function remove(id: string) {
    try {
      await tokensApi.remove(id);
      toast.push("Token deleted", "ok");
      load();
    } catch (err) {
      toast.push(err instanceof Error ? err.message : "Failed", "err");
    }
  }

  async function mintHub() {
    setHubBusy(true);
    try {
      const t = await adminApi.mintHubToken();
      setFreshHub(t);
      load();
    } catch (err) {
      toast.push(err instanceof Error ? err.message : "Failed", "err");
    } finally {
      setHubBusy(false);
    }
  }

  if (error) return <Alert kind="error">{error}</Alert>;
  if (!tokens) return <Loading />;

  // A freshly created token is inlined so the command works as-is.
  const token = created?.token ?? fresh?.token ?? "<your-token>";
  const serverName = created ? `${brandSlug}-${created.project.id.slice(0, 8)}` : brandSlug;
  const claudeCommand = `claude mcp add --transport http ${serverName} ${mcpUrl} --header "Authorization: Bearer ${token}"`;
  const setupPrompt = `Set up the ${brandName} MCP server in this coding agent, then verify the connection.

Server details:
- Name: ${serverName}
- Transport: streamable HTTP (MCP)
- URL: ${mcpUrl}
- Auth: HTTP header "Authorization: Bearer ${token}"

Steps:
1. Register this server through this agent's own MCP configuration mechanism. Examples:
   - Claude Code: ${claudeCommand}
   - Codex: an [mcp_servers.${serverName}] entry (url + Authorization header) in ~/.codex/config.toml
   - OpenCode: a "${serverName}" entry of type "remote" under the "mcp" key of opencode.json
   - Hermes, OpenClaw or any other agent: its remote/HTTP MCP server settings
2. If the token above still reads <your-token>, ask me for a real one (created at /settings/tokens on ${appHost}) before configuring anything. Never commit the token to version control.
3. Verify the setup by listing the server's tools and showing me the result.

A PROJECT token works on one project: search_knowledge answers from ${consultantName}'s knowledge base using that project's own model and knowledge-base selection, consult_codebase reads its repositories, and delegate_development hands work to the build pipeline. Each of those is metered and billed to that project, so only call them when I ask.

An ACCOUNT-WIDE token reads across my projects (list_projects, get_project_status, get_project_info) and can create one (create_project). It cannot query the knowledge base - that needs a project token.`;

  // Group project tokens under the project they belong to: the link between a key
  // and its project is the whole point of the project scope.
  const grouped = new Map<string, { name: string; tokens: McpProjectToken[] }>();
  for (const t of projectTokens ?? []) {
    const entry = grouped.get(t.project_id) ?? { name: t.project_name, tokens: [] };
    entry.tokens.push(t);
    grouped.set(t.project_id, entry);
  }

  return (
    <div>
      <div className="page-head">
        <div>
          <h1>Tokens</h1>
          <p className="muted">
            Connect coding agents to {brandName} over MCP, and enroll this spoke into a hub.
          </p>
        </div>
      </div>

      <div className="tabs">
        <div className={`tab${tab === "mcp" ? " active" : ""}`} onClick={() => setTab("mcp")}>
          MCP tokens
        </div>
        {isAdmin && (
          <div className={`tab${tab === "hub" ? " active" : ""}`} onClick={() => setTab("hub")}>
            Hub enrollment
          </div>
        )}
      </div>

      {tab === "mcp" && (
        <div className="stack">
          <div className="card">
            <div className="section-title">Connect an agent</div>
            <p className="muted small">
              Point any MCP-capable coding agent (Claude Code, Codex, Hermes, OpenClaw,
              OpenCode...) at {brandName}. There are two kinds of token, and they do different
              things:
            </p>
            <ul className="muted small">
              <li>
                <strong>A project token</strong> is tied to ONE project. It answers from{" "}
                {consultantFirst}'s knowledge base using that project's own model and
                knowledge-base selection, reads that project's code, and can delegate
                development to it. Every call is metered and billed to that project.
              </li>
              <li>
                <strong>An account-wide token</strong> reads across all your projects and can
                create new ones. It cannot query the knowledge base: an answer needs a project
                to choose the model, narrow the knowledge bases and carry the cost.
              </li>
            </ul>
            <p className="muted small">MCP endpoint:</p>
            <CopyField value={mcpUrl} block />
          </div>

          <div className="card">
            <div className="section-title">New MCP project</div>
            <p className="muted small">
              Creates a project and its first token in one step. The project is what holds the
              model and the knowledge bases the agent will use - open it afterwards to change
              either.
            </p>
            <form className="stack-sm" onSubmit={createProject}>
              <input
                type="text"
                placeholder="Title (e.g. Terminal consulting)"
                value={projTitle}
                maxLength={255}
                onChange={(e) => setProjTitle(e.target.value)}
              />
              <input
                type="text"
                placeholder="What it is for (optional)"
                value={projDesc}
                maxLength={4000}
                onChange={(e) => setProjDesc(e.target.value)}
              />
              <div>
                <button
                  type="submit"
                  className="btn btn-primary"
                  disabled={projBusy || !projTitle.trim()}
                >
                  {projBusy ? <Spinner /> : "Create project + token"}
                </button>
              </div>
            </form>
            {created && (
              <div className="mt">
                <Alert kind="success">
                  Project <strong>{created.project.name}</strong> created with token{" "}
                  <strong>{created.token_name}</strong>. Copy the token now - it won't be shown
                  again. This key belongs to that project: its queries use the project's model
                  and knowledge bases, and bill to it.
                </Alert>
                <CopyField value={created.token} block />
                <p className="muted small mt">Add it to Claude Code:</p>
                <CopyField value={claudeCommand} block />
                <div className="row gap-sm mt">
                  <CopyButton value={setupPrompt} label="Copy setup prompt" className="btn" />
                  <Link className="btn btn-sm" to={`/projects/${created.project.id}`}>
                    Choose its model &amp; knowledge bases
                  </Link>
                  <button className="btn btn-sm" onClick={() => setCreated(null)}>
                    Done
                  </button>
                </div>
              </div>
            )}
          </div>

          <div className="card">
            <div className="section-title">Project tokens</div>
            {grouped.size === 0 ? (
              <p className="muted small" style={{ margin: 0 }}>
                No project tokens yet. Create one above, or mint one from any project's MCP tab.
              </p>
            ) : (
              <div className="stack-sm">
                {[...grouped.entries()].map(([pid, g]) => (
                  <div key={pid}>
                    <div className="between">
                      <Link to={`/projects/${pid}`}>
                        <strong>{g.name}</strong>
                      </Link>
                      <span className="tiny faint">
                        {g.tokens.length} token{g.tokens.length > 1 ? "s" : ""}
                      </span>
                    </div>
                    {g.tokens.map((t) => (
                      <div key={t.id} className="tiny faint" style={{ paddingLeft: "0.75rem" }}>
                        {t.name} · created {relTime(t.created_at)} ·{" "}
                        {t.last_used_at ? `last used ${relTime(t.last_used_at)}` : "never used"}
                      </div>
                    ))}
                  </div>
                ))}
                <p className="tiny faint" style={{ margin: 0 }}>
                  Revoke a project token from its own project's MCP tab.
                </p>
              </div>
            )}
          </div>

          <div className="card">
            <div className="section-title">Account-wide tokens</div>
            <p className="muted small">
              Read-only across your projects, plus <code>create_project</code>. No knowledge
              access - use a project token for that.
            </p>
            {fresh && (
              <>
                <Alert kind="success">
                  Token <strong>{fresh.name}</strong> created. Copy it now - it won't be shown
                  again.
                </Alert>
                <CopyField value={fresh.token} block />
                <button className="btn btn-sm mt mb" onClick={() => setFresh(null)}>
                  Done
                </button>
              </>
            )}
            <form className="row gap-sm" onSubmit={create}>
              <input
                type="text"
                placeholder="Token name (e.g. laptop-cli)"
                value={name}
                onChange={(e) => setName(e.target.value)}
              />
              <button type="submit" className="btn btn-primary" disabled={busy || !name.trim()}>
                {busy ? <Spinner /> : "Create"}
              </button>
            </form>
            {accountTokens.length === 0 ? (
              <p className="muted small" style={{ marginBottom: 0 }}>
                No account-wide tokens yet.
              </p>
            ) : (
              <>
                <div className="table-wrap mt">
                  <table className="data">
                    <thead>
                      <tr>
                        <th>Name</th>
                        <th>Created</th>
                        <th>Last used</th>
                        <th></th>
                      </tr>
                    </thead>
                    <tbody>
                      {pageItems.map((t) => (
                        <tr key={t.id}>
                          <td>{t.name}</td>
                          <td className="faint">{relTime(t.created_at)}</td>
                          <td className="faint">
                            {t.last_used_at ? relTime(t.last_used_at) : "never"}
                          </td>
                          <td>
                            <button className="btn btn-sm btn-danger" onClick={() => remove(t.id)}>
                              Delete
                            </button>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
                <Pager page={page} pages={pages} onPage={setPage} />
              </>
            )}
          </div>
        </div>
      )}

      {tab === "hub" && isAdmin && (
        <div className="stack">
          <div className="card">
            <div className="section-title">Hub enrollment token</div>
            <p className="muted small">
              A hub-scoped token lets a central Scalevisor Hub orchestrate this spoke over MCP:
              grant credits, run evaluations, and audit the knowledge base. Generate one to
              enroll this spoke into a hub, then paste it as the spoke API key in the hub's
              enrollment form alongside the MCP URL below. Unlike an MCP token, a hub token can
              never query the knowledge base or be billed.
            </p>
            <p className="muted small">MCP URL to enroll with:</p>
            <CopyField value={mcpUrl} block />
            {freshHub && (
              <div className="mt">
                <Alert kind="success">
                  Hub token <strong>{freshHub.name}</strong> created. Copy it now - it won't be
                  shown again. Paste it as the spoke API key when enrolling this spoke into a
                  hub.
                </Alert>
                <CopyField value={freshHub.token} block />
                <button className="btn btn-sm mt" onClick={() => setFreshHub(null)}>
                  Done
                </button>
              </div>
            )}
            <div className="row gap-sm mt">
              <button className="btn btn-primary" onClick={mintHub} disabled={hubBusy}>
                {hubBusy ? <Spinner /> : "Generate hub token"}
              </button>
            </div>
          </div>

          <div className="card">
            <div className="section-title">Hub tokens</div>
            {hubTokens.length === 0 ? (
              <p className="muted small" style={{ margin: 0 }}>
                No hub tokens yet.
              </p>
            ) : (
              <div className="table-wrap">
                <table className="data">
                  <thead>
                    <tr>
                      <th>Name</th>
                      <th>Created</th>
                      <th>Last used</th>
                      <th></th>
                    </tr>
                  </thead>
                  <tbody>
                    {hubTokens.map((t) => (
                      <tr key={t.id}>
                        <td>
                          <span className="row gap-sm">
                            {t.name}
                            <span className="badge">hub</span>
                          </span>
                        </td>
                        <td className="faint">{relTime(t.created_at)}</td>
                        <td className="faint">
                          {t.last_used_at ? relTime(t.last_used_at) : "never"}
                        </td>
                        <td>
                          <button className="btn btn-sm btn-danger" onClick={() => remove(t.id)}>
                            Delete
                          </button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
