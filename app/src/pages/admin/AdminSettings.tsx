import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { adminApi, kbApi, toolsApi, type Tool } from "../../lib/endpoints";
import { useAuth } from "../../lib/auth";
import { useToast } from "../../lib/toast";
import { Alert, Loading, Spinner, Toggle } from "../../components/ui";
import type {
  AdminSettings as AdminSettingsT, ConsultantPhoto, KnowledgeBase,
} from "../../types";

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
            <strong>Disable routines</strong>
            <div className="muted small">
              Hides the Routines tab and refuses every routine write, on every project. Saved
              routines are kept but stop firing, so switching this back on resumes them where they
              were.
            </div>
          </div>
          <Toggle
            checked={settings.routines_disabled}
            disabled={busy !== null}
            onChange={(v) => update({ routines_disabled: v }, "routines")}
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

      <ProjectDefaultsCard settings={settings} busy={busy} update={update} />

      <LegalIdentityCard settings={settings} busy={busy} update={update} />

      <ConsultantPhotoCard
        photo={settings.consultant_photo ?? null}
        busy={busy}
        setBusy={setBusy}
        onChange={(photo) => setSettings({ ...settings, consultant_photo: photo })}
      />

      <FeesCard settings={settings} busy={busy} update={update} />

      <DevHarnessCard settings={settings} busy={busy} update={update} />

      <EgressCard settings={settings} busy={busy} update={update} />
    </div>
  );
}


// §project defaults: what a NEW project of each kind is created with. The two
// gates default opposite ways - a knowledge base reaches a project only if its
// selection names it, a tool reaches every project unless that project says
// otherwise - so this card reads as one question (what does a new project start
// with?) while storing a selection for knowledge bases and an exclusion list for
// tools. Per kind, because what a build may read and what a conversation may read
// are different decisions.
const PROJECT_KINDS = [
  { id: "ai", label: "Curated AI MVP" },
  { id: "auto_dev", label: "Auto-developer" },
  { id: "direct_quote", label: "Direct quote" },
  { id: "chat", label: "Just chat" },
];

function ProjectDefaultsCard({
  settings,
  busy,
  update,
}: {
  settings: AdminSettingsT;
  busy: string | null;
  update: (patch: Partial<AdminSettingsT>, field: string) => Promise<void>;
}) {
  const toast = useToast();
  const [kind, setKind] = useState(PROJECT_KINDS[0].id);
  const [kbs, setKbs] = useState<KnowledgeBase[] | null>(null);
  const [tools, setTools] = useState<Tool[] | null>(null);
  const [kbDraft, setKbDraft] = useState<Record<string, string[]>>(settings.default_kb_ids);
  const [offDraft, setOffDraft] = useState<Record<string, string[]>>(settings.default_tools_off);

  // Re-sync when the server echoes the stored maps back (it sorts them).
  useEffect(() => setKbDraft(settings.default_kb_ids), [settings.default_kb_ids]);
  useEffect(() => setOffDraft(settings.default_tools_off), [settings.default_tools_off]);

  useEffect(() => {
    Promise.all([kbApi.list(), toolsApi.list()])
      .then(([k, t]) => {
        setKbs(k);
        setTools(t);
      })
      .catch((err) => {
        setKbs([]);
        setTools([]);
        toast.push(
          err instanceof Error ? err.message : "Could not load knowledge bases and tools",
          "err",
        );
      });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  function flip(map: Record<string, string[]>, id: string): Record<string, string[]> {
    const next = new Set(map[kind] ?? []);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    return { ...map, [kind]: Array.from(next).sort() };
  }

  const kbSelected = new Set(kbDraft[kind] ?? []);
  const toolsOff = new Set(offDraft[kind] ?? []);
  const dirty =
    JSON.stringify(kbDraft) !== JSON.stringify(settings.default_kb_ids) ||
    JSON.stringify(offDraft) !== JSON.stringify(settings.default_tools_off);
  const kindLabel = PROJECT_KINDS.find((k) => k.id === kind)?.label ?? kind;

  return (
    <div className="card" style={{ maxWidth: 640, marginTop: "1.5rem" }}>
      <h3 style={{ marginTop: 0 }}>What a new project starts with</h3>
      <Alert kind="info">
        Knowledge bases are opt-in per project: one reaches a project only if its selection names
        it, so a kind with nothing checked here creates projects that read no knowledge at all - a
        chat that can only answer "I don't have anything on that". Tools are the opposite: every
        enabled tool reaches every project, so unchecking one switches it off for new projects of
        that kind. Applied once, at creation: changing this never touches projects that already
        exist, and it only ever narrows the global lists (a knowledge base or tool disabled under{" "}
        <Link to="/admin/knowledge-bases">Knowledge bases</Link> or{" "}
        <Link to="/admin/tools">Tools</Link> stays off everywhere).
      </Alert>

      <div className="tabs" style={{ marginTop: "1rem" }}>
        {PROJECT_KINDS.map((k) => (
          <div
            key={k.id}
            className={`tab${kind === k.id ? " active" : ""}`}
            onClick={() => setKind(k.id)}
          >
            {k.label}
          </div>
        ))}
      </div>

      {(kbs === null || tools === null) && <Spinner />}
      {kbs !== null && tools !== null && (
        <>
          <div className="section-title mt">Knowledge bases a new {kindLabel} project reads</div>
          <div className="stack" style={{ gap: "0.35rem" }}>
            {kbs.map((kb) => (
              <label key={kb.id} className="checkbox-row">
                <input
                  type="checkbox"
                  checked={kbSelected.has(kb.id)}
                  onChange={() => setKbDraft(flip(kbDraft, kb.id))}
                />
                {kb.name}
                <span className="tiny faint">
                  {" "}
                  ({kb.kind}
                  {!kb.enabled ? " · disabled globally" : ""})
                </span>
              </label>
            ))}
            {kbs.length === 0 && <p className="tiny faint">No knowledge bases configured.</p>}
          </div>

          <div className="section-title mt">Tools a new {kindLabel} project may call</div>
          <div className="stack" style={{ gap: "0.35rem" }}>
            {tools.map((t) => (
              <label key={t.id} className="checkbox-row">
                <input
                  type="checkbox"
                  checked={!toolsOff.has(t.id)}
                  onChange={() => setOffDraft(flip(offDraft, t.id))}
                />
                {t.name}
                <span className="tiny faint">
                  {" "}
                  ({t.kind}
                  {!t.enabled ? " · disabled globally" : ""})
                </span>
              </label>
            ))}
            {tools.length === 0 && <p className="tiny faint">No tools configured.</p>}
          </div>

          <div className="row gap-sm mt">
            <button
              type="button"
              className="btn btn-primary btn-sm"
              disabled={busy !== null || !dirty}
              onClick={() =>
                update(
                  { default_kb_ids: kbDraft, default_tools_off: offDraft },
                  "project_defaults",
                )
              }
            >
              Save defaults
            </button>
            {dirty && <span className="muted small">Unsaved changes</span>}
          </div>
        </>
      )}
    </div>
  );
}

// §legal identity: who the Privacy policy and Terms of service on the public
// landing name as the operating company. The landing is a static build, so those
// pages ship with the value baked in at build time and read this one at runtime -
// leaving both fields empty keeps the built-in value.
function LegalIdentityCard({
  settings,
  busy,
  update,
}: {
  settings: AdminSettingsT;
  busy: string | null;
  update: (patch: Partial<AdminSettingsT>, field: string) => Promise<void>;
}) {
  const [name, setName] = useState(settings.legal_name ?? "");
  const [address, setAddress] = useState(settings.legal_address ?? "");

  // Re-sync when the server answers with the stored (trimmed) values.
  useEffect(() => {
    setName(settings.legal_name ?? "");
    setAddress(settings.legal_address ?? "");
  }, [settings.legal_name, settings.legal_address]);

  const dirty =
    name.trim() !== (settings.legal_name ?? "") || address.trim() !== (settings.legal_address ?? "");

  return (
    <div className="card" style={{ maxWidth: 640, marginTop: "1.5rem" }}>
      <h3 style={{ marginTop: 0 }}>Business legal identity</h3>
      <Alert kind="info">
        The company that legally operates this instance. It is what the public Privacy policy and
        Terms of service name as the data controller and service operator, and it appears in the
        landing footer. Leave a field empty to keep the value the landing was built with.
      </Alert>

      <div style={{ marginTop: "1rem" }}>
        <label className="field">
          <span>Legal name</span>
          <input
            type="text"
            value={name}
            maxLength={200}
            placeholder="Example Consulting Ltd"
            onChange={(e) => setName(e.target.value)}
          />
        </label>
        <label className="field">
          <span>Registered address</span>
          <textarea
            value={address}
            maxLength={500}
            rows={3}
            placeholder={"12 Example Street\n75001 Paris\nFrance"}
            onChange={(e) => setAddress(e.target.value)}
          />
        </label>
        <div className="row gap-sm">
          <button
            type="button"
            className="btn btn-primary btn-sm"
            disabled={busy !== null || !dirty}
            onClick={() => update({ legal_name: name.trim(), legal_address: address.trim() }, "legal")}
          >
            Save legal identity
          </button>
          {dirty && <span className="muted small">Unsaved changes</span>}
        </div>
      </div>
    </div>
  );
}

// §consultant photo: the portrait the public landing shows next to the
// consultant's name (the hero signature and the Direct quote card). The landing
// is a static build, so it loads the photo from the API at runtime and shows its
// photo slots only when one exists - no upload, no slots. Served as uploaded.
function ConsultantPhotoCard({
  photo,
  busy,
  setBusy,
  onChange,
}: {
  photo: ConsultantPhoto | null;
  busy: string | null;
  setBusy: (field: string | null) => void;
  onChange: (photo: ConsultantPhoto | null) => void;
}) {
  const toast = useToast();
  const [error, setError] = useState<string | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  async function upload(file: File) {
    setBusy("photo");
    setError(null);
    try {
      onChange(await adminApi.uploadConsultantPhoto(file));
      toast.push("Photo saved", "ok");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not upload the photo.");
    } finally {
      setBusy(null);
    }
  }

  async function remove() {
    setBusy("photo");
    setError(null);
    try {
      await adminApi.removeConsultantPhoto();
      onChange(null);
      toast.push("Photo removed", "ok");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not remove the photo.");
    } finally {
      setBusy(null);
    }
  }

  const summary = photo
    ? `${photo.content_type.replace("image/", "").toUpperCase()} · ${Math.max(1, Math.round(photo.size_bytes / 1024))} KB`
    : "No photo yet - the landing keeps its text-only layout.";

  return (
    <div className="card" style={{ maxWidth: 640, marginTop: "1.5rem" }}>
      <h3 style={{ marginTop: 0 }}>Consultant photo</h3>
      <Alert kind="info">
        Shown on the public landing next to your name: under the hero call to action and on the
        Direct quote card. Square, at least 320 px, PNG, JPEG or WebP, 1 MB max - it is served
        exactly as uploaded.
      </Alert>

      <div className="row gap-sm" style={{ marginTop: "1rem", alignItems: "center", gap: "1rem" }}>
        {photo ? (
          <img
            src={adminApi.consultantPhotoUrl(photo.sha256)}
            alt=""
            width={72}
            height={72}
            style={{ borderRadius: "50%", objectFit: "cover", flex: "none" }}
          />
        ) : (
          <div
            aria-hidden
            style={{
              width: 72,
              height: 72,
              borderRadius: "50%",
              background: "var(--surface-2)",
              border: "1px dashed var(--border)",
              flex: "none",
            }}
          />
        )}
        <div className="stack-sm">
          <span className="muted small">{summary}</span>
          <div className="row gap-sm">
            <input
              ref={fileRef}
              type="file"
              accept="image/png,image/jpeg,image/webp"
              hidden
              onChange={(e) => {
                const file = e.target.files?.[0];
                e.target.value = "";
                if (file) void upload(file);
              }}
            />
            <button
              type="button"
              className={`btn btn-sm${photo ? " btn-ghost" : " btn-primary"}`}
              disabled={busy !== null}
              onClick={() => fileRef.current?.click()}
            >
              {photo ? "Replace photo" : "Upload photo"}
            </button>
            {photo && (
              <button
                type="button"
                className="btn btn-ghost btn-sm"
                disabled={busy !== null}
                onClick={() => void remove()}
              >
                Remove
              </button>
            )}
          </div>
          {error && (
            <span className="small" style={{ color: "var(--danger)" }}>
              {error}
            </span>
          )}
        </div>
      </div>
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

// §dev harness: which agent driver a dev build runs on. Off by default, and the
// switch is the whole feature: while it is off every project builds on the
// instance default and any per-project pin is ignored (not just hidden). The
// catalog comes from the server because a harness only exists if the runner image
// ships its driver - the admin narrows that list, never extends it.
function DevHarnessCard({
  settings,
  busy,
  update,
}: {
  settings: AdminSettingsT;
  busy: string | null;
  update: (patch: Partial<AdminSettingsT>, field: string) => Promise<void>;
}) {
  const allowed = settings.dev_harness_allowed;
  const enabled = settings.dev_harness_selection_enabled;

  function toggleAllowed(id: string, on: boolean) {
    const next = on ? [...allowed, id] : allowed.filter((h) => h !== id);
    if (next.length === 0) return; // the backend refuses an empty set too
    const patch: Partial<AdminSettingsT> = { dev_harness_allowed: next };
    // Keep the pair consistent: a default outside the allowed set would silently
    // resolve back to the built-in one.
    if (!next.includes(settings.dev_harness_default)) patch.dev_harness_default = next[0];
    void update(patch, `harness_allowed_${id}`);
  }

  return (
    <div className="card" style={{ maxWidth: 640, marginTop: "1.5rem" }}>
      <h3 style={{ marginTop: 0 }}>Dev harness</h3>
      <div className="between" style={{ alignItems: "flex-start" }}>
        <div style={{ paddingRight: "1rem" }}>
          <strong>Per-project harness selection</strong>
          <div className="muted small">
            Lets a project be pinned to a specific agent driver. While this is off, every project
            builds on the instance default below and a previously pinned project falls back to it.
          </div>
        </div>
        <Toggle
          checked={enabled}
          disabled={busy !== null}
          onChange={(v) => update({ dev_harness_selection_enabled: v }, "harness_enabled")}
        />
      </div>

      <div style={{ marginTop: "1rem", opacity: enabled ? 1 : 0.6 }}>
        <div className="muted small" style={{ marginBottom: "0.5rem" }}>
          Harnesses this instance allows. A driver has to be in the runner image to appear here.
        </div>
        {settings.dev_harnesses.map((h) => (
          // A div, not a label, with the name in an inner <label>: the base label
          // style is uppercase mono, and only `.checkbox-row label` restores the
          // body face a sentence needs.
          <div
            key={h.id}
            className="checkbox-row"
            style={{ alignItems: "flex-start", marginBottom: "0.6rem" }}
          >
            <input
              id={`harness-${h.id}`}
              type="checkbox"
              checked={allowed.includes(h.id)}
              disabled={busy !== null || (allowed.length === 1 && allowed.includes(h.id))}
              onChange={(e) => toggleAllowed(h.id, e.target.checked)}
            />
            <div>
              <label htmlFor={`harness-${h.id}`}>{h.label}</label>
              <div className="muted small">{h.description}</div>
            </div>
          </div>
        ))}
        <label className="field" style={{ marginTop: "1rem" }}>
          Instance default
          <select
            value={settings.dev_harness_default}
            disabled={busy !== null}
            onChange={(e) => update({ dev_harness_default: e.target.value }, "harness_default")}
          >
            {settings.dev_harnesses
              .filter((h) => allowed.includes(h.id))
              .map((h) => (
                <option key={h.id} value={h.id}>
                  {h.label}
                </option>
              ))}
          </select>
        </label>
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
