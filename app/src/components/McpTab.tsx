import { useEffect, useState } from "react";
import { mcpApi } from "../lib/endpoints";
import { useAuth } from "../lib/auth";
import { useToast } from "../lib/toast";
import { Alert, CopyField, Loading, formatCreditsExact, relTime } from "./ui";
import type { McpToken, McpUsage } from "../types";

// §MCP project tokens: mint a token here, point your terminal agent at the MCP,
// and its queries bill this project. What you ask is NOT stored - only the
// counters below - so there is no transcript to show and none to leak.

export default function McpTab({
  projectId,
  readOnly = false,
}: {
  projectId: string;
  readOnly?: boolean;
}) {
  const toast = useToast();
  const { settings, config } = useAuth();
  const brand = settings?.brand_name ?? "Openvisor";
  const [tokens, setTokens] = useState<McpToken[] | null>(null);
  const [usage, setUsage] = useState<McpUsage | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  // The plaintext exists exactly once, in the create response - keep it on
  // screen until the user dismisses it, because it can never be shown again.
  const [minted, setMinted] = useState<{ name: string; token: string } | null>(null);

  function load() {
    mcpApi
      .list(projectId)
      .then((r) => {
        setTokens(r.tokens);
        setUsage(r.usage);
      })
      .catch((err) => setError(err instanceof Error ? err.message : "Failed to load."));
  }
  useEffect(load, [projectId]);

  async function mint(e: React.FormEvent) {
    e.preventDefault();
    if (!name.trim()) return;
    setBusy(true);
    try {
      const t = await mcpApi.create(projectId, name.trim());
      setMinted({ name: t.name, token: t.token });
      setName("");
      load();
    } catch (err) {
      toast.push(err instanceof Error ? err.message : "Could not create the token", "err");
    } finally {
      setBusy(false);
    }
  }

  async function revoke(t: McpToken) {
    if (!confirm(`Revoke "${t.name}"? Any agent using it stops working immediately.`)) return;
    try {
      await mcpApi.revoke(projectId, t.id);
      toast.push("Token revoked", "ok");
      load();
    } catch (err) {
      toast.push(err instanceof Error ? err.message : "Could not revoke", "err");
    }
  }

  if (error) return <Alert kind="error">{error}</Alert>;
  if (!tokens || !usage) return <Loading />;

  const mcpUrl = `https://mcp.${config?.deploy_domain ?? "openvisor.local"}/mcp`;

  return (
    <div className="stack">
      <div className="card">
        <div className="section-title">Queries</div>
        <div className="build-stats">
          <div className="stat">
            <label>last 30 days</label>
            <span className="mono">{usage.queries_30d.toLocaleString()}</span>
          </div>
          <div className="stat">
            <label>credits (30d)</label>
            <span className="mono grad-text">{formatCreditsExact(usage.credits_30d)}</span>
          </div>
          <div className="stat">
            <label>all time</label>
            <span className="mono">{usage.queries_total.toLocaleString()}</span>
          </div>
          <div className="stat stat-lifetime">
            <label>credits (all time)</label>
            <span className="mono grad-text">{formatCreditsExact(usage.credits_total)}</span>
          </div>
        </div>
        <p className="tiny faint" style={{ margin: "0.6rem 0 0" }}>
          Counted, not recorded: {brand} keeps the number of queries and what they cost,
          never the questions or the answers. Your terminal conversation stays yours.
        </p>
      </div>

      <div className="card">
        <div className="section-title">Connect an agent</div>
        <p className="muted small" style={{ marginTop: 0 }}>
          Point Claude Code, opencode or any MCP client at this endpoint with a token below.
          It sees only this project, and every query is billed to it.
        </p>
        <label className="field-label">MCP endpoint</label>
        <CopyField value={mcpUrl} block />
        <label className="field-label mt">Claude Code</label>
        <CopyField
          block
          value={`claude mcp add ${settings?.brand_slug ?? "openvisor"} ${mcpUrl} --header "Authorization: Bearer <your-token>"`}
        />
      </div>

      {minted && (
        <Alert kind="success">
          <strong>Copy “{minted.name}” now</strong> - it is shown once and stored only as a
          hash.
          <div style={{ marginTop: "0.5rem" }}>
            <CopyField value={minted.token} block />
          </div>
          <button type="button" className="btn btn-sm mt" onClick={() => setMinted(null)}>
            Done
          </button>
        </Alert>
      )}

      <div className="card">
        <div className="between mb">
          <div className="section-title" style={{ margin: 0 }}>
            Tokens
          </div>
        </div>
        {!readOnly && (
          <form className="row gap-sm mb" onSubmit={mint}>
            <input
              className="rq-search"
              style={{ maxWidth: 280 }}
              placeholder="Token name (e.g. laptop, CI)"
              value={name}
              onChange={(e) => setName(e.target.value)}
              maxLength={128}
            />
            <button className="btn btn-primary btn-sm" disabled={busy || !name.trim()}>
              {busy ? "Creating…" : "Create token"}
            </button>
          </form>
        )}
        {tokens.length === 0 ? (
          <p className="muted small" style={{ margin: 0 }}>
            No tokens yet.
          </p>
        ) : (
          <div className="stack-sm">
            {tokens.map((t) => (
              <div key={t.id} className="between token-row">
                <div>
                  <strong>{t.name}</strong>
                  <div className="tiny faint">
                    created {relTime(t.created_at)} ·{" "}
                    {t.last_used_at ? `last used ${relTime(t.last_used_at)}` : "never used"}
                  </div>
                </div>
                {!readOnly && (
                  <button type="button" className="btn btn-sm btn-ghost" onClick={() => revoke(t)}>
                    Revoke
                  </button>
                )}
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
