import { useEffect, useState } from "react";
import { usageApi } from "../lib/endpoints";
import { Alert, Loading, formatCreditsExact } from "./ui";
import type { ProjectUsage } from "../types";

// §usage graph: tokens consumed per day. Hand-drawn SVG on purpose - a chart
// library would be the single biggest dependency in the bundle for one bar
// chart. Bars, because consumption is a per-day quantity, not a continuous
// signal: an area chart would imply values between the days that don't exist.

const RANGES = [7, 30, 90] as const;

function fmtDay(iso: string): string {
  const d = new Date(`${iso}T00:00:00Z`);
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric", timeZone: "UTC" });
}

function fmtTokens(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(n >= 10_000_000 ? 0 : 1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(n >= 10_000 ? 0 : 1)}k`;
  return String(n);
}

export default function UsageTab({ projectId }: { projectId: string }) {
  const [days, setDays] = useState<number>(30);
  const [data, setData] = useState<ProjectUsage | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setData(null);
    usageApi
      .series(projectId, days)
      .then(setData)
      .catch((err) => setError(err instanceof Error ? err.message : "Failed to load usage."));
  }, [projectId, days]);

  if (error) return <Alert kind="error">{error}</Alert>;
  if (!data) return <Loading />;

  const peak = Math.max(1, ...data.series.map((b) => b.tokens));
  const spent = data.series.filter((b) => b.tokens > 0).length;
  const reqPeak = Math.max(0, ...data.series.map((b) => b.requests_done + b.requests_canceled));

  return (
    <div className="stack">
      <div className="card">
        <div className="between mb">
          <div className="section-title" style={{ margin: 0 }}>
            Tokens over time
          </div>
          <div className="row gap-sm">
            {RANGES.map((r) => (
              <button
                key={r}
                type="button"
                className={`btn btn-sm${days === r ? " btn-primary" : " btn-ghost"}`}
                onClick={() => setDays(r)}
              >
                {r}d
              </button>
            ))}
          </div>
        </div>

        {data.totals.tokens === 0 ? (
          <p className="muted small" style={{ margin: 0 }}>
            Nothing consumed in this window. Builds, chat answers and MCP queries all land
            here as they happen.
          </p>
        ) : (
          <>
            <div className="usage-chart" role="img"
                 aria-label={`${fmtTokens(data.totals.tokens)} tokens over ${data.days} days`}>
              {data.series.map((b) => (
                <div key={b.day} className="usage-col" title={
                  `${fmtDay(b.day)} - ${b.tokens.toLocaleString()} tokens`
                  + ` · ${formatCreditsExact(b.credits)} cr`
                  + (b.mcp_tokens ? ` (${b.mcp_tokens.toLocaleString()} via MCP)` : "")
                }>
                  {/* MCP traffic stacks inside the day's bar so its share is
                      visible without a second chart. */}
                  <div className="usage-bar" style={{ height: `${(b.tokens / peak) * 100}%` }}>
                    {b.mcp_tokens > 0 && (
                      <div className="usage-bar-mcp"
                           style={{ height: `${(b.mcp_tokens / b.tokens) * 100}%` }} />
                    )}
                  </div>
                </div>
              ))}
            </div>
            <div className="between tiny faint" style={{ marginTop: "0.4rem" }}>
              <span>{fmtDay(data.series[0].day)}</span>
              <span>peak {fmtTokens(peak)} / day</span>
              <span>{fmtDay(data.series[data.series.length - 1].day)}</span>
            </div>
          </>
        )}
      </div>

      <div className="card">
        <div className="between mb">
          <div className="section-title" style={{ margin: 0 }}>
            Request outcomes
          </div>
          <span className="tiny faint">
            {data.totals.requests_done} done · {data.totals.requests_canceled} canceled in this window
          </span>
        </div>
        {reqPeak === 0 ? (
          <p className="muted small" style={{ margin: 0 }}>
            No requests reached done or canceled in this window.
          </p>
        ) : (
          <>
            {/* Same hand-drawn bar idiom as the tokens chart: a day's bar is the
                requests FILED that day that later closed, canceled stacked in the
                danger tone so the failure share reads at a glance. */}
            <div className="usage-chart" role="img"
                 aria-label={`${data.totals.requests_done} done and ${data.totals.requests_canceled} canceled requests over ${data.days} days`}>
              {data.series.map((b) => {
                const total = b.requests_done + b.requests_canceled;
                return (
                  <div key={b.day} className="usage-col" title={
                    `${fmtDay(b.day)} - ${b.requests_done} done · ${b.requests_canceled} canceled`
                  }>
                    {total > 0 && (
                      <div className="usage-bar usage-bar-requests"
                           style={{ height: `${(total / reqPeak) * 100}%` }}>
                        {b.requests_canceled > 0 && (
                          <div className="usage-bar-canceled"
                               style={{ height: `${(b.requests_canceled / total) * 100}%` }} />
                        )}
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
            <div className="between tiny faint" style={{ marginTop: "0.4rem" }}>
              <span>{fmtDay(data.series[0].day)}</span>
              <span>
                lifetime: {data.totals.lifetime_requests_done} done ·{" "}
                {data.totals.lifetime_requests_canceled} canceled
              </span>
              <span>{fmtDay(data.series[data.series.length - 1].day)}</span>
            </div>
          </>
        )}
      </div>

      <div className="card">
        <div className="section-title">This window</div>
        <div className="build-stats">
          <div className="stat">
            <label>tokens</label>
            <span className="mono">{data.totals.tokens.toLocaleString()}</span>
          </div>
          <div className="stat">
            <label>credits</label>
            <span className="mono grad-text">{formatCreditsExact(data.totals.credits)}</span>
          </div>
          <div className="stat">
            <label>via mcp</label>
            <span className="mono">{data.totals.mcp_tokens.toLocaleString()}</span>
          </div>
          <div className="stat">
            <label>active days</label>
            <span className="mono">{spent} / {data.days}</span>
          </div>
          <div className="stat stat-lifetime">
            <label>lifetime</label>
            <span className="mono">
              {data.totals.lifetime_tokens.toLocaleString()} tok ·{" "}
              <span className="grad-text">
                {formatCreditsExact(data.totals.lifetime_credits)} cr
              </span>
            </span>
          </div>
        </div>
      </div>
    </div>
  );
}
