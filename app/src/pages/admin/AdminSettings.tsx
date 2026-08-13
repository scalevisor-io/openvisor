import { useEffect, useState } from "react";
import { adminApi } from "../../lib/endpoints";
import { useAuth } from "../../lib/auth";
import { useToast } from "../../lib/toast";
import { Alert, Loading, Toggle } from "../../components/ui";
import type { AdminSettings as AdminSettingsT } from "../../types";

// Admin settings: runtime switches that don't need a redeploy.
// First control - pause new project deposits, independently per kind. Pausing
// only blocks *creating* new projects; requests/edits on existing projects and
// the admin's own creation are unaffected.
export default function AdminSettings() {
  const { refresh } = useAuth();
  const toast = useToast();
  const [settings, setSettings] = useState<AdminSettingsT | null>(null);
  const [busy, setBusy] = useState<string | null>(null);

  useEffect(() => {
    adminApi
      .settings()
      .then(setSettings)
      .catch((err) =>
        toast.push(err instanceof Error ? err.message : "Could not load settings.", "err"),
      );
  }, []);

  if (!settings) return <Loading />;

  async function update(patch: Partial<AdminSettingsT>, field: string) {
    const previous = settings;
    setBusy(field);
    setSettings({ ...settings!, ...patch }); // optimistic
    try {
      const next = await adminApi.updateSettings(patch);
      setSettings(next);
      await refresh(); // propagate the new pause flags into app-wide config
      toast.push("Setting saved", "ok");
    } catch (err) {
      setSettings(previous); // revert
      toast.push(err instanceof Error ? err.message : "Could not save.", "err");
    } finally {
      setBusy(null);
    }
  }

  return (
    <div>
      <div className="page-head">
        <div>
          <h1>Settings</h1>
          <p className="muted">Platform-wide controls. Changes take effect immediately.</p>
        </div>
      </div>

      <div className="card" style={{ maxWidth: 640 }}>
        <h3 style={{ marginTop: 0 }}>New project deposits</h3>
        <Alert kind="info">
          Pause accepting new projects of either kind. Customers see the matching option grayed out
          with an explanation. This does not affect requests or edits on projects that already
          exist, and you can still create projects yourself.
        </Alert>

        <div className="between" style={{ alignItems: "flex-start", marginTop: "1rem" }}>
          <div style={{ paddingRight: "1rem" }}>
            <strong>Pause AI-curated deposits</strong>
            <div className="muted small">
              Stops new self-serve "Curated AI MVP" projects. Existing AI projects keep building.
            </div>
          </div>
          <Toggle
            checked={settings.pause_ai_deposits}
            disabled={busy !== null}
            onChange={(v) => update({ pause_ai_deposits: v }, "ai")}
          />
        </div>

        <hr style={{ border: 0, borderTop: "1px solid var(--border)", margin: "1rem 0" }} />

        <div className="between" style={{ alignItems: "flex-start" }}>
          <div style={{ paddingRight: "1rem" }}>
            <strong>Pause direct-contact deposits</strong>
            <div className="muted small">
              Stops new "Direct contact quote" requests. Open quotes are unaffected.
            </div>
          </div>
          <Toggle
            checked={settings.pause_direct_deposits}
            disabled={busy !== null}
            onChange={(v) => update({ pause_direct_deposits: v }, "direct")}
          />
        </div>

        <hr style={{ border: 0, borderTop: "1px solid var(--border)", margin: "1rem 0" }} />

        <div className="between" style={{ alignItems: "flex-start" }}>
          <div style={{ paddingRight: "1rem" }}>
            <strong>Pause auto-developer deposits</strong>
            <div className="muted small">
              Stops new "Curated AI auto-developer" projects. Existing sentinels keep watching and
              building.
            </div>
          </div>
          <Toggle
            checked={settings.pause_auto_dev_deposits}
            disabled={busy !== null}
            onChange={(v) => update({ pause_auto_dev_deposits: v }, "auto_dev")}
          />
        </div>

        <hr style={{ border: 0, borderTop: "1px solid var(--border)", margin: "1rem 0" }} />

        <div className="between" style={{ alignItems: "flex-start" }}>
          <div style={{ paddingRight: "1rem" }}>
            <strong>Pause chat deposits</strong>
            <div className="muted small">
              Stops new "Just chat with me" projects. Chat is opt-in and starts paused: enabling it
              exposes the knowledge base conversationally (here and to hub catalogs). Existing chats
              keep answering.
            </div>
          </div>
          <Toggle
            checked={settings.pause_chat_deposits}
            disabled={busy !== null}
            onChange={(v) => update({ pause_chat_deposits: v }, "chat")}
          />
        </div>
      </div>

      <FeesCard settings={settings} busy={busy} update={update} />

      <EgressCard settings={settings} busy={busy} update={update} />
    </div>
  );
}

// §fees: per-track engagement fees. The specialities.json value is the shipped
// default; an override set here is what this instance actually charges (it
// rides the evaluation estimate) and advertises to hubs. Clearing a field
// falls back to the default.
function FeesCard({
  settings,
  busy,
  update,
}: {
  settings: AdminSettingsT;
  busy: string | null;
  update: (patch: Partial<AdminSettingsT>, field: string) => Promise<void>;
}) {
  const rows = settings.speciality_fees ?? [];
  const serverDraft = () =>
    Object.fromEntries(
      rows.map((r) => [r.id, r.override_credits != null ? String(r.override_credits) : ""]),
    );
  const [draft, setDraft] = useState<Record<string, string>>(serverDraft);

  // Re-sync when the server answers with normalized values.
  useEffect(() => {
    setDraft(serverDraft());
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [settings.speciality_fees]);

  if (rows.length === 0) return null;

  const dirty = rows.some(
    (r) => (draft[r.id] ?? "") !== (r.override_credits != null ? String(r.override_credits) : ""),
  );
  const invalid = rows.some((r) => {
    const v = (draft[r.id] ?? "").trim();
    return v !== "" && (!Number.isFinite(Number(v)) || Number(v) < 0);
  });

  function save() {
    const overrides: Record<string, number | null> = {};
    for (const r of rows) {
      const v = (draft[r.id] ?? "").trim();
      overrides[r.id] = v === "" ? null : Math.round(Number(v) * 100) / 100;
    }
    void update({ speciality_fee_overrides: overrides }, "fees");
  }

  return (
    <div className="card" style={{ maxWidth: 640, marginTop: "1.5rem" }}>
      <h3 style={{ marginTop: 0 }}>Engagement fees</h3>
      <Alert kind="info">
        A track's one-time engagement fee in credits, charged with the first funding (it rides the
        evaluation estimate); per-token usage stays metered on top. The shipped default comes from
        specialities.json - a value set here overrides it for this instance and is what hubs see.
        Leave a field empty to use the default. 0 disables the fee.
      </Alert>

      <div style={{ marginTop: "1rem" }}>
        {rows.map((r) => {
          const v = (draft[r.id] ?? "").trim();
          const overridden = v !== "" && Number(v) !== r.default_fee_credits;
          return (
            <div key={r.id} className="between" style={{ alignItems: "center", marginTop: "0.6rem" }}>
              <div style={{ paddingRight: "1rem" }}>
                <strong>{r.label}</strong>
                <div className="muted small">
                  default {r.default_fee_credits} cr
                  {overridden && " · overridden"}
                </div>
              </div>
              <input
                type="number"
                min={0}
                step={0.01}
                value={draft[r.id] ?? ""}
                placeholder={String(r.default_fee_credits)}
                aria-label={`${r.label} engagement fee (credits)`}
                style={{ width: "8rem", textAlign: "right" }}
                onChange={(e) => setDraft((d) => ({ ...d, [r.id]: e.target.value }))}
              />
            </div>
          );
        })}
      </div>
      <div className="row gap-sm" style={{ marginTop: "1rem" }}>
        <button
          type="button"
          className="btn btn-primary btn-sm"
          disabled={busy !== null || !dirty || invalid}
          onClick={save}
        >
          Save fees
        </button>
        {invalid && <span className="muted small">Fees must be non-negative numbers</span>}
        {!invalid && dirty && <span className="muted small">Unsaved changes</span>}
      </div>
    </div>
  );
}

// §egress: the dev-sandbox egress allowlist. Off by default; when on, a dev build
// can only reach the hosts listed here (plus each run's own LLM/git/tool hosts,
// added automatically). Enforced on Kubernetes deployments.
function EgressCard({
  settings,
  busy,
  update,
}: {
  settings: AdminSettingsT;
  busy: string | null;
  update: (patch: Partial<AdminSettingsT>, field: string) => Promise<void>;
}) {
  const [draft, setDraft] = useState(settings.egress_allowlist.join("\n"));

  // Keep the textarea in sync when the server returns a normalized list.
  useEffect(() => {
    setDraft(settings.egress_allowlist.join("\n"));
  }, [settings.egress_allowlist]);

  const parsed = draft
    .split("\n")
    .map((l) => l.trim())
    .filter(Boolean);
  const dirty = parsed.join("\n") !== settings.egress_allowlist.join("\n");

  return (
    <div className="card" style={{ maxWidth: 640, marginTop: "1.5rem" }}>
      <h3 style={{ marginTop: 0 }}>Dev-sandbox egress allowlist</h3>
      <Alert kind="info">
        When enabled, a development build's network is locked down so it can only reach the hosts you
        list below - defense against an untrusted agent exfiltrating your knowledge base or code. Each
        run's own required hosts (the model endpoint, its git remote, the in-cluster tools) are always
        allowed automatically, so turning this on won't break a normal build. Entries can be an FQDN
        (<code>pypi.org</code>), a wildcard (<code>*.githubusercontent.com</code>), an IP, or a CIDR.
        Enforced on Kubernetes deployments.
      </Alert>

      <div className="between" style={{ alignItems: "flex-start", marginTop: "1rem" }}>
        <div style={{ paddingRight: "1rem" }}>
          <strong>Restrict dev-build egress</strong>
          <div className="muted small">
            Off by default (builds reach the whole internet). Turn on to enforce the allowlist below.
          </div>
        </div>
        <Toggle
          checked={settings.egress_lockdown_enabled}
          disabled={busy !== null}
          onChange={(v) => update({ egress_lockdown_enabled: v }, "egress_enabled")}
        />
      </div>

      <div style={{ marginTop: "1rem", opacity: settings.egress_lockdown_enabled ? 1 : 0.6 }}>
        <label className="field">
          <span>Allowed hosts (one per line)</span>
          <textarea
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            rows={10}
            spellCheck={false}
            style={{ fontFamily: "monospace", fontSize: "0.85rem" }}
            placeholder={"pypi.org\n*.githubusercontent.com\n93.184.216.0/24"}
          />
        </label>
        <div className="row gap-sm">
          <button
            type="button"
            className="btn btn-primary btn-sm"
            disabled={busy !== null || !dirty}
            onClick={() => update({ egress_allowlist: parsed }, "egress_list")}
          >
            Save allowlist
          </button>
          {dirty && <span className="muted small">Unsaved changes</span>}
        </div>
      </div>
    </div>
  );
}
