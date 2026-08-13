import { useEffect, useState } from "react";
import { adminApi, tokensApi } from "../lib/endpoints";
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
import type { ApiToken, NewApiToken, NewHubToken } from "../types";

const PAGE_SIZE = 10;

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
  const [tokens, setTokens] = useState<ApiToken[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [fresh, setFresh] = useState<NewApiToken | null>(null);
  const [freshHub, setFreshHub] = useState<NewHubToken | null>(null);
  const [hubBusy, setHubBusy] = useState(false);
  const { pageItems, page, pages, setPage } = usePager(tokens ?? [], PAGE_SIZE);

  function load() {
    tokensApi
      .list()
      .then(setTokens)
      .catch((err) => setError(err instanceof Error ? err.message : "Failed to load tokens."));
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

  // A freshly created token is inlined so the prompt/command work as-is.
  const token = fresh?.token ?? "<your-token>";
  const claudeCommand = `claude mcp add --transport http ${brandSlug} ${mcpUrl} --header "Authorization: Bearer ${token}"`;
  const setupPrompt = `Set up the ${brandName} MCP server in this coding agent, then verify the connection.

Server details:
- Name: ${brandSlug}
- Transport: streamable HTTP (MCP)
- URL: ${mcpUrl}
- Auth: HTTP header "Authorization: Bearer ${token}"

Steps:
1. Register this server through this agent's own MCP configuration mechanism. Examples:
   - Claude Code: ${claudeCommand}
   - Codex: an [mcp_servers.${brandSlug}] entry (url + Authorization header) in ~/.codex/config.toml
   - OpenCode: a "${brandSlug}" entry of type "remote" under the "mcp" key of opencode.json
   - Hermes, OpenClaw or any other agent: its remote/HTTP MCP server settings
2. If the token above still reads <your-token>, ask me for a real one (created at /settings/tokens on ${appHost}) before configuring anything. Never commit the token to version control.
3. Verify the setup by calling the ${brandSlug} "list_projects" tool and show me the result.

Available tools: list_projects, get_project_status and get_project_info are read-only; search_knowledge queries ${consultantName}'s consulting knowledge base and is metered (each call is billed to my ${brandName} organization's credit wallet), so only call it when I ask.`;

  return (
    <div>
      <div className="page-head">
        <div>
          <h1>API tokens</h1>
          <p className="muted">
            Personal tokens for the MCP server: read-only project status, plus the billable
            knowledge tool.
          </p>
        </div>
      </div>

      <div className="card mb">
        <div className="section-title">Use in Claude Code, Codex &amp; other agents</div>
        <p className="muted small">
          Connect any MCP-capable coding agent (Claude Code, Codex, Hermes, OpenClaw, OpenCode...)
          to {brandName}: read-only project tools, plus the <code>search_knowledge</code> tool to
          query {consultantFirst}'s knowledge base from your editor. Each knowledge call is metered (model
          cost x markup) and billed to your organization's credit wallet.
        </p>
        <p className="muted small">
          Easiest path: copy the setup prompt and paste it into your agent - it registers the MCP
          server in its own configuration and verifies the connection.
        </p>
        <div className="row gap-sm mb">
          <CopyButton value={setupPrompt} label="Copy prompt" className="btn btn-primary" />
        </div>
        <details className="prompt-details">
          <summary className="muted small">Show prompt</summary>
          <pre className="prompt-preview">{setupPrompt}</pre>
        </details>
        <p className="muted small">In Claude Code you can also run the command directly:</p>
        <CopyField value={claudeCommand} block />
      </div>

      {fresh && (
        <div className="card mb">
          <Alert kind="success">
            Token <strong>{fresh.name}</strong> created. Copy it now - it won't be shown again.
          </Alert>
          <CopyField value={fresh.token} block />
          <button className="btn btn-sm mt" onClick={() => setFresh(null)}>
            Done
          </button>
        </div>
      )}

      <form className="card mb" onSubmit={create}>
        <div className="section-title">New token</div>
        <div className="row gap-sm">
          <input
            type="text"
            placeholder="Token name (e.g. laptop-cli)"
            value={name}
            onChange={(e) => setName(e.target.value)}
          />
          <button type="submit" className="btn btn-primary" disabled={busy || !name.trim()}>
            {busy ? <Spinner /> : "Create"}
          </button>
        </div>
      </form>

      {isAdmin && (
        <div className="card mb">
          <div className="section-title">Hub enrollment token</div>
          <p className="muted small">
            A hub-scoped token lets a central Scalevisor Hub orchestrate this spoke over MCP: grant
            credits, run evaluations, and audit the knowledge base. Generate one to enroll this
            spoke into a hub, then paste it as the spoke API key in the hub's enrollment form
            alongside the MCP URL below. Unlike a personal token, a hub token can never query the
            knowledge base or be billed.
          </p>
          <p className="muted small">MCP URL to enroll with:</p>
          <CopyField value={mcpUrl} block />
          {freshHub && (
            <div className="mt">
              <Alert kind="success">
                Hub token <strong>{freshHub.name}</strong> created. Copy it now - it won't be shown
                again. Paste it as the spoke API key when enrolling this spoke into a hub.
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
      )}

      {tokens.length === 0 ? (
        <div className="card center muted">No tokens yet.</div>
      ) : (
        <>
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
                {pageItems.map((t) => (
                  <tr key={t.id}>
                    <td>
                      <span className="row gap-sm">
                        {t.name}
                        {t.scope === "hub" && <span className="badge">hub</span>}
                      </span>
                    </td>
                    <td className="faint">{relTime(t.created_at)}</td>
                    <td className="faint">{t.last_used_at ? relTime(t.last_used_at) : "never"}</td>
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
  );
}
